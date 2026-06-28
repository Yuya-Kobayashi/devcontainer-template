#!/usr/bin/env python3
"""Codex code review, split across two non-overlapping hooks:

  * Stop hook (this module's default entry point): when Claude finishes a turn,
    review the session's *uncommitted* changes — work in progress at Stop — and
    feed findings back so Claude addresses them (auto-fix). Committed work is NOT
    reviewed here; that's the pre-push hook's job.
  * `--pre-push` mode (invoked by `.githooks/pre-push`): when a branch is pushed,
    review the commits being pushed against the trunk and persist the result. A
    per-root state map (`.claude/logs/codex-review-state.json`) records the last
    SHA reviewed so each commit is reviewed once, not on every push. See
    run_pre_push.

Why the split: the push that opens a PR happens *during* a turn, before this Stop
hook fires. If the Stop hook also reviewed committed work, a commit+push+stop in
one turn would review the same delta twice — the pre-push review runs detached and
wouldn't have recorded the commit by the time Stop fires seconds later. So the
Stop hook owns uncommitted work (auto-fix, blocking) and the pre-push hook owns
committed/pushed deltas (advisory, and the only path that catches subagent-
authored PRs the Stop hook structurally can't see). The state map is the pre-push
hook's alone; the Stop hook neither reads nor writes it.

Wired from .claude/settings.json as a `Stop` hook. It self-skips silently when:

  - Codex isn't installed or isn't logged in — keeps this shared, committed hook
    safe for any agent/checkout that doesn't have Codex set up;
  - this is a re-entrant stop (`stop_hook_active`) — so the review runs at most
    once per stop-chain: Claude fixes, stops again, and we don't re-review (no
    loop, no extra spend);
  - there are no uncommitted code/design changes (routine data/analysis/wiki-sync
    turns — and turns that committed their work, now the pre-push hook's job —
    don't burn a Stop review).

Worktree scoping (the reason this isn't a one-liner): the convention in AGENTS.md
is to isolate non-trivial work in a git worktree under `.worktrees/` and
`cd` into it — but a `cd` inside a Bash tool call does not persist, so the
session's working directory (and thus this hook's `cwd`) stays at the *main*
checkout even while the real edits land inside `.worktrees/<branch>/`. Reviewing
`cwd` would then review the wrong tree (the main checkout, where `.worktrees/` is
gitignored and the actual changes are invisible).

So instead of trusting `cwd`, we derive the set of worktrees this session
*actually edited* from the transcript's Write/Edit tool calls, map each edited
file to its containing git worktree, and review each worktree that still has
uncommitted changes — scoped to itself. This reviews the worktree where the work
happened, and deliberately does NOT touch sibling worktrees that other concurrent
sessions are working in (they won't appear in this session's transcript). If the
transcript yields nothing (e.g. edits made via raw Bash), we fall back to the
worktree containing `cwd`.

The devcontainer is already externally sandboxed (Squid + iptables), so Codex is
run with `--dangerously-bypass-approvals-and-sandbox` to skip its own bubblewrap
sandbox, which the OrbStack kernel forbids (no unprivileged user namespaces).

Every run (either hook) appends a small JSON record (outcome + finding counts +
review mode + the artifact path) to `.claude/logs/codex-review.jsonl`, and writes
the full review body to `.claude/logs/codex-reviews/<branch>__<head>.md` — so
"what did Codex say about this change" is answerable after the fact, not just
"how many issues".
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Best-effort GitHub commit-status posting (issue #388 phase 2). Optional: if the
# helper is absent on some checkout, status posting degrades to a silent no-op and
# the review still runs.
try:
    from _codex_gh_status import (post_status as _post_status,
                                  repo_slug as _repo_slug,
                                  has_blocking as _has_blocking)
except Exception:  # pragma: no cover - defensive
    def _post_status(*_a, **_k):
        return False

    def _repo_slug(*_a, **_k):
        return None

    def _has_blocking(priorities):
        # Mirror _codex_gh_status.has_blocking so the merge-block decision (and the
        # dedup-state skip that lets a fixed branch be re-reviewed) still works on a
        # checkout where the helper module is absent.
        return any((priorities or {}).get(p) for p in ("P0", "P1"))

# Only these extensions count as "Claude finished coding". Edit to taste.
CODE_EXTS = (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".go", ".rs",
    ".rb", ".java", ".c", ".h", ".cpp", ".hpp",
)
REVIEW_TIMEOUT_SEC = 240
# Codex tags every finding with a priority like [P0]..[P3]; their presence is a
# reliable "has findings" signal in the final review message.
FINDING_RE = re.compile(r"\[P[0-9]\]")

# Root-level design/governance docs worth reviewing alongside code.
DESIGN_ROOT_DOCS = ("AGENTS.md", "CLAUDE.md")

# Transcript tool_use blocks that mean "Claude edited a file".
EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_TOOL_HINTS = tuple(f'"{t}"' for t in EDIT_TOOLS)

# Append-only JSONL record of every review the hook actually runs (plus the
# "nothing to review" skips), so "how many reviews ran / how many found issues"
# is answerable after the fact. The full review body is also persisted (see
# REVIEWS_DIR); the JSONL is the index over it.
#
# Everything lands under the *main* worktree's .claude/logs so the archive and
# the dedup state are single and shared. The Stop hook is invoked via
# $CLAUDE_PROJECT_DIR from the main checkout (so `__file__` is already the main
# copy), but the pre-push companion runs each worktree's own co-located copy of
# this script — `--git-common-dir` resolves both to the main checkout's .git, so
# a review run from a linked worktree still writes to the one shared place.
# CODEX_REVIEW_LOG overrides the path (tests). Best-effort: never breaks the hook.
def _shared_log_dir():
    """The main worktree's `.claude/logs`, resolved from the shared git dir so a
    run from any linked worktree writes to the same archive. Falls back to this
    script's own `.claude/logs` if git can't answer."""
    here_claude = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=here_claude, capture_output=True, text=True, timeout=10)
        common = out.stdout.strip() if out.returncode == 0 else ""
        if common:  # e.g. /repo/.git  ->  main worktree /repo
            return os.path.join(os.path.dirname(common), ".claude", "logs")
    except Exception:
        pass
    return os.path.join(here_claude, "logs")


LOG_DIR = _shared_log_dir()
LOG_PATH = os.environ.get("CODEX_REVIEW_LOG") or os.path.join(LOG_DIR, "codex-review.jsonl")
# The full review bodies the JSONL only summarizes — one markdown file per review,
# so "what did Codex actually say about this PR" survives the ephemeral run.
# CODEX_REVIEW_DIR overrides for tests.
REVIEWS_DIR = os.environ.get("CODEX_REVIEW_DIR") or os.path.join(LOG_DIR, "codex-reviews")

# Per-worktree-root "last commit reviewed in --base mode" map, so a branch's
# un-merged commits are reviewed once each (the next --base run starts from the
# recorded SHA) instead of re-reviewing the whole branch on every Stop.
STATE_PATH = os.environ.get("CODEX_REVIEW_STATE") or os.path.join(LOG_DIR, "codex-review-state.json")

# Trunk refs a feature branch may fork from (AGENTS.md). Committed work is
# reviewed against the *newest* of these that HEAD descends from — so a stale
# local `main` (behind a freshly-merged `origin/main`) doesn't make the review
# span commits already merged upstream. No fetch: uses whatever refs are present.
BASE_BRANCHES = ("main", "origin/main")


def is_reviewable(path: str) -> bool:
    """True when a changed path (relative to its worktree) is code or a
    design/spec doc. Other markdown (data/content under project dirs) is excluded
    so routine content turns don't trigger a review. Tune CODE_EXTS /
    DESIGN_ROOT_DOCS and this predicate to your project's layout."""
    if path.endswith(CODE_EXTS):
        return True
    if path.endswith(".md"):
        # Design/spec docs live under .claude/ (skill design*.md, SKILL.md,
        # agent definitions) plus the root governance docs.
        return path.startswith(".claude/") or path in DESIGN_ROOT_DOCS
    return False


def emit(obj):
    print(json.dumps(obj))


def log_event(record):
    """Append one timestamped JSON record to LOG_PATH. Best-effort: a logging
    failure must never break the review. Records are kept small (no full review
    text) so a single-line append stays atomic under the repo's concurrent
    sessions (writes below PIPE_BUF don't interleave on POSIX)."""
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        **record,
    }
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def load_state():
    """The {root: last-reviewed-SHA} map, or {} when absent/corrupt."""
    try:
        with open(STATE_PATH) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    """Persist the {root: SHA} map atomically. Best-effort; a lost update under
    concurrent sessions only risks re-reviewing a delta, never a crash."""
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass


def count_priorities(review_text):
    """Tally of priority tags in a review body, e.g. {'P1': 2, 'P3': 1}."""
    counts = {}
    for m in FINDING_RE.finditer(review_text):
        tag = m.group(0)[1:-1]  # "[P1]" -> "P1"
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def git(args, cwd, timeout=20):
    """Run a git command; return CompletedProcess, or None on any failure."""
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None


def rev(root, ref):
    """The commit SHA `ref` resolves to in `root`, or None."""
    r = git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=root)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip() or None


def merge_base(root, a, b):
    r = git(["merge-base", a, b], cwd=root)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip() or None


def is_ancestor(root, a, b):
    r = git(["merge-base", "--is-ancestor", a, b], cwd=root)
    return r is not None and r.returncode == 0


def trunk_merge_base(root, head):
    """The newest merge-base of `head` across BASE_BRANCHES, or None.

    For each present trunk ref, take merge-base(head, ref); keep the
    descendant-most. So an out-of-date local `main` (older merge-base) yields to
    a fresher `origin/main` and the review covers only the branch's own commits —
    not ones already merged upstream. Falls back gracefully when a ref is absent
    (no remote) or when the two have diverged (keeps the first)."""
    best = None
    for ref in BASE_BRANCHES:
        sha = rev(root, ref)
        if not sha:
            continue
        mb = merge_base(root, head, sha)
        if mb and (best is None or is_ancestor(root, best, mb)):
            best = mb       # mb is at or after `best` → newer trunk point
    return best


def delta_for(root, head, last_reviewed):
    """The range to review for commit `head`, or (None, None) when there's nothing
    un-reviewed beyond the trunk. `base` is `last_reviewed` when it's still a valid
    ancestor on this line (so each commit is reviewed once), else the newest trunk
    merge-base (local or remote `main`)."""
    if not head:
        return (None, None)
    mb = trunk_merge_base(root, head)
    if not mb or mb == head:
        return (None, None)  # no commits beyond the trunk
    if last_reviewed == head:
        return (None, None)  # already reviewed up to head
    base = mb
    if (last_reviewed
            and is_ancestor(root, last_reviewed, head)
            and is_ancestor(root, mb, last_reviewed)):
        base = last_reviewed
    if base == head:
        return (None, None)
    return (base, head)


def diff_has_reviewable(root, base, head):
    """True when the `base..head` diff touches a code/design file."""
    r = git(["diff", "--name-only", base, head], cwd=root)
    if r is None or r.returncode != 0:
        return False
    return any(is_reviewable(p.strip().strip('"'))
               for p in r.stdout.splitlines() if p.strip())


def edited_paths_from_transcript(transcript_path):
    """Absolute-ish file paths Claude edited this session, oldest-first, from the
    Write/Edit/MultiEdit/NotebookEdit tool_use blocks in the JSONL transcript."""
    paths = []
    try:
        with open(transcript_path) as fh:
            for line in fh:
                # Fast pre-filter: skip the vast majority of lines (user/assistant
                # text, other tools) before paying for a JSON parse.
                if '"tool_use"' not in line or not any(h in line for h in _TOOL_HINTS):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msg = obj.get("message")
                content = msg.get("content") if isinstance(msg, dict) else obj.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") not in EDIT_TOOLS:
                        continue
                    inp = block.get("input") or {}
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if fp:
                        paths.append(fp)
    except OSError:
        return []
    return paths


def worktree_root(path, fallback_cwd):
    """The git worktree top-level containing `path` (a file or dir). None when the
    path isn't inside a git repository."""
    start = path if os.path.isdir(path) else (os.path.dirname(path) or fallback_cwd)
    if not os.path.isdir(start):
        start = fallback_cwd
    r = git(["rev-parse", "--show-toplevel"], cwd=start)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip() or None


def review_roots(payload, cwd):
    """Distinct git-worktree roots this session edited (order-preserving). Falls
    back to the worktree containing `cwd` when the transcript yields nothing."""
    roots = []
    seen = set()
    transcript = payload.get("transcript_path")
    if transcript and os.path.exists(transcript):
        for p in edited_paths_from_transcript(transcript):
            ap = p if os.path.isabs(p) else os.path.normpath(os.path.join(cwd, p))
            root = worktree_root(ap, cwd)
            if root and root not in seen:
                seen.add(root)
                roots.append(root)
    if not roots:
        root = worktree_root(cwd, cwd)
        if root:
            roots.append(root)
    return roots


def has_reviewable_changes(root):
    """True when `root` has uncommitted changes to a code/design file. Any path
    under a nested `.worktrees/` is ignored so reviewing a parent checkout never
    pulls in another worktree's work (gitignore already hides these; this is
    belt-and-suspenders)."""
    r = git(["status", "--porcelain", "--untracked-files=all"], cwd=root)
    if r is None or r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:  # rename: "old -> new"
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if not path or path.startswith(".worktrees/"):
            continue
        if is_reviewable(path):
            return True
    return False


def _capture_bytes(args, cwd, timeout=90, input_bytes=None):
    """Run a git command in binary mode (no text decoding) so binary patches and
    odd-encoding diffs round-trip intact; CompletedProcess or None on failure."""
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              timeout=timeout, input=input_bytes)
    except Exception:
        return None


def _replicate_worktree_state(root, clone):
    """Copy root's *uncommitted* state into `clone` so an --uncommitted review sees
    the same diff: the tracked working-tree delta vs HEAD (staged + unstaged,
    binary-safe) plus untracked, non-ignored files. Best-effort — a failure
    degrades the review's fidelity, never the shared repo's safety."""
    diff = _capture_bytes(["diff", "--binary", "HEAD"], root)
    if diff is not None and diff.returncode == 0 and diff.stdout:
        ap = _capture_bytes(["apply", "--binary", "--whitespace=nowarn"],
                            clone, input_bytes=diff.stdout)
        if ap is None or ap.returncode != 0:
            log_event({"event": "review_isolation", "root": root_label(root),
                       "warn": "tracked_patch_apply_failed"})
    r = git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=root, timeout=90)
    if r is not None and r.returncode == 0 and r.stdout:
        for rel in r.stdout.split("\0"):
            if not rel:
                continue
            src, dst = os.path.join(root, rel), os.path.join(clone, rel)
            try:
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                shutil.copy2(src, dst)
            except OSError:
                pass


def _git_common_dir(root):
    """Absolute path to root's shared git dir (e.g. /repo/.git), or None."""
    r = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=root)
    if r is None or r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _same_device(a, b):
    """True when paths `a` and `b` live on the same filesystem (so hardlinks
    between them are possible). False on any stat failure."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _prepare_review_clone(root, clone, scope, common, head):
    """Materialize a disposable clone of `root` that mirrors what `scope` reviews.
    Returns True on success.

    The clone has its OWN objects, refs, config and worktree metadata, so anything
    codex does to "its" repo — flipping core.bare, adding worktrees, moving
    HEADs/refs — lands in this throwaway and is deleted with it, never touching the
    shared repository other sessions share (issue #388). A sibling worktree would
    NOT isolate this: worktrees share the common .git (refs + core config), which
    is exactly what gets corrupted.

    `head` is the exact SHA to review, pinned by the caller — NOT re-derived from
    root's HEAD here. That distinction matters: a concurrent session still on the
    old hook can move root's HEAD onto a foreign commit between the caller pinning
    the delta and this clone being checked out. Re-reading HEAD would then review
    the wrong tree (e.g. base..202e924 → a bogus "everything was deleted" P0 — the
    #388 false positive). Pinning makes the review immune to that race.

    Objects are hardlinked (instant, no copy) when the clone is on the same
    filesystem as the source store, else copied — bind-mounted workspaces and the
    container's /tmp are usually different devices, where hardlinks fail (EXDEV)."""
    if not head:
        return False
    hardlinkable = bool(common) and _same_device(
        os.path.dirname(clone), os.path.join(common, "objects"))
    link = "--local" if hardlinkable else "--no-hardlinks"
    r = git(["clone", link, "--no-checkout", "--quiet", root, clone],
            cwd=root, timeout=300)
    if r is None or r.returncode != 0:
        return False
    # Detach at the pinned SHA (the clone holds every object via --local, so this
    # succeeds even if root's branch ref has since moved off it).
    r = git(["checkout", "--quiet", "--detach", head], cwd=clone, timeout=120)
    if r is None or r.returncode != 0:
        return False
    # Sever the clone's link to the source so codex can't reach the shared repo
    # through `origin = <root>` (issue #388 defense-in-depth). The clone is
    # self-contained (objects hardlinked/copied), so review needs no remote.
    git(["remote", "remove", "origin"], cwd=clone)
    if "--uncommitted" in scope:
        _replicate_worktree_state(root, clone)
    return True


# Git plumbing env vars that bind git to a specific repo/worktree. The pre-push
# hook is invoked *by git*, which exports GIT_DIR/GIT_WORK_TREE (etc.) into the
# hook's environment; a review must run free of them so its own clone/checkout —
# and codex — never bind to the real worktree being pushed (issue #388,
# "Failure B": a linked worktree left detached on 202e924).
_GIT_ENV_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")


def _is_inside(path, parent):
    """True when `path` is `parent` or lives under it (realpath-normalized, so a
    symlink into the repo is still caught; lexical for the non-existent tail)."""
    try:
        p, d = os.path.realpath(path), os.path.realpath(parent)
    except OSError:
        return False
    return p == d or p.startswith(d + os.sep)


def _review_base_dir(root, common):
    """Directory to hold the disposable review clone — chosen to sit OUTSIDE the
    reviewed worktree and its shared `.git` (issue #388). `codex exec review`
    resolves its repo by walking *up* from the temp worktrees it spawns; if the
    clone (and the TMPDIR pointed at it) live inside the shared `.git` — or
    anywhere under the worktree — that walk-up re-enters the *parent* shared repo
    and codex's setup mutations (core.bare=true, refs/codex/*, HEAD resets) land
    there, corrupting every worktree on the common `.git`.

    Candidates, in order: `CODEX_REVIEW_TMP` (override), then
    `$XDG_CACHE_HOME`/`~/.cache` + `codex-review`. A candidate is REJECTED when
    it resolves inside the reviewed worktree `root`, the main worktree, or the
    common git dir — so even a project-local cache (`XDG_CACHE_HOME=$repo/.cache`)
    or an override pointed into the repo can't reintroduce the walk-up. On
    rejection or mkdir failure we fall through, ultimately to None = the system
    temp dir (mkdtemp's default), outside the repo."""
    unsafe = [p for p in (root, os.path.dirname(common) if common else None,
                          common) if p]
    candidates = []
    env = os.environ.get("CODEX_REVIEW_TMP")
    if env:
        candidates.append(env)
    cache = (os.environ.get("XDG_CACHE_HOME")
             or os.path.join(os.path.expanduser("~"), ".cache"))
    candidates.append(os.path.join(cache, "codex-review"))
    for base in candidates:
        if any(_is_inside(base, u) for u in unsafe):
            continue
        try:
            os.makedirs(base, exist_ok=True)
            return base
        except OSError:
            continue
    return None  # system temp dir (mkdtemp default) — outside the repo


def run_codex_review(root, scope, head=None):
    """Run Codex's reviewer for `scope` (['--uncommitted'] or ['--base', SHA])
    against a DISPOSABLE CLONE of `root`; return the final review text ('' when
    Codex produced none, or the clone couldn't be prepared). `head` is the exact
    SHA to check out and review; defaults to root's current HEAD (the right choice
    for --uncommitted, where the working tree IS the current HEAD). Raises
    subprocess.TimeoutExpired.

    Isolation (issue #388): `codex exec review` mutates the git dir it runs in
    (creating worktrees, and intermittently leaving core.bare=true / moving
    HEADs). Because every linked worktree shares the common .git, reviewing one
    in place corrupts every concurrent session's repo. Running against a throwaway
    clone — not another worktree — gives codex its own refs/config/worktrees to
    scribble on, all discarded when the temp dir is removed. TMPDIR is pointed
    inside that temp dir too, so codex's own /tmp/codex-review-* worktrees land there
    and are cleaned up with everything else rather than leaking under /tmp.

    The temp dir is created OUTSIDE any git worktree (`_review_base_dir()`: under
    `~/.cache`, or the system temp dir), NEVER inside the shared `.git`. That
    placement is the actual #388 fix: codex resolves its repo by walking *up* from
    the temp worktrees it spawns, so a clone nested in `the shared `.git`` let
    that walk-up reach the parent shared repo (where codex flipped core.bare, wrote
    refs/codex/*, and reset HEAD). Out here the shared repo is unreachable. Objects
    still hardlink when the temp base shares a filesystem with the object store,
    else copy (~49M) — `_prepare_review_clone` decides via `_same_device`."""
    common = _git_common_dir(root)
    review_dir = tempfile.mkdtemp(prefix="codex-review-iso-",
                                  dir=_review_base_dir(root, common))
    # Hard belt: never run where codex's walk-up could re-enter the shared repo.
    if _is_inside(review_dir, root) or (
            common and _is_inside(review_dir, os.path.dirname(common))):
        shutil.rmtree(review_dir, ignore_errors=True)
        return ""
    clone = os.path.join(review_dir, "wt")
    codex_tmp = os.path.join(review_dir, "tmp")
    out_path = os.path.join(review_dir, "review.md")
    # Decouple every git op below — OURS (clone/checkout) and codex's plugin sync —
    # from any GIT_* env the pre-push hook inherited from git. Otherwise GIT_DIR/
    # GIT_WORK_TREE point at the *real* worktree being pushed, and `git checkout
    # --detach` (here) plus codex's reset mutate that live worktree instead of the
    # clone — issue #388 "Failure B".
    saved_git_env = {k: os.environ[k] for k in _GIT_ENV_VARS if k in os.environ}
    try:
        for k in _GIT_ENV_VARS:
            os.environ.pop(k, None)
        os.makedirs(codex_tmp, exist_ok=True)
        if not _prepare_review_clone(root, clone, scope, common,
                                     head or rev(root, "HEAD")):
            return ""
        env = {**os.environ, "TMPDIR": codex_tmp,
               "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0", "GIT_TERMINAL_PROMPT": "0"}
        if common:  # belt: don't let a stray op ascend into the shared repo tree
            env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(common) or common
        subprocess.run(
            # `--disable plugins/apps`: the review needs neither, and codex's
            # curated-plugin startup sync is the writer that drops core.bare /
            # refs/codex/curated-sync → 202e924 (openai/plugins) into whatever repo
            # it resolves. Disabling it removes that writer entirely (issue #388).
            ["codex", "exec", "review", *scope,
             "--disable", "plugins", "--disable", "apps",
             "--dangerously-bypass-approvals-and-sandbox",
             "--ephemeral", "-o", out_path],
            cwd=clone, capture_output=True, text=True, timeout=REVIEW_TIMEOUT_SEC,
            env=env,
        )
        try:
            with open(out_path) as fh:
                return fh.read().strip()
        except OSError:
            return ""
    finally:
        for k, v in saved_git_env.items():
            os.environ[k] = v
        shutil.rmtree(review_dir, ignore_errors=True)


def root_label(root):
    """Short, human-readable label for a worktree root in a multi-root report."""
    marker = os.sep + ".worktrees" + os.sep
    i = root.find(marker)
    if i != -1:
        return root[i + len(marker):]  # e.g. "highlights-v3/author-hooks"
    return "main checkout"


def codex_available():
    """True when Codex is installed and logged in. Keeps the shared, committed
    hook a graceful no-op on any checkout that doesn't have Codex set up."""
    if not shutil.which("codex"):
        return False
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.exists(os.path.join(codex_home, "auth.json"))


def current_branch(root):
    """The branch name checked out in `root`, or 'HEAD' when detached/unknown."""
    r = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    name = r.stdout.strip() if (r and r.returncode == 0) else ""
    return name or "HEAD"


def _artifact_relpath(path):
    """Artifact path for the JSONL record: relative to the repo root when we can
    (e.g. '.claude/logs/codex-reviews/foo.md'), else just the filename."""
    repo_root = os.path.dirname(os.path.dirname(LOG_DIR))  # .../.claude/logs -> repo
    try:
        return os.path.relpath(path, start=repo_root)
    except ValueError:
        return os.path.basename(path)


def write_artifact(root, plan, review_text, prios):
    """Persist the full Codex review under REVIEWS_DIR — one markdown file per
    review (`<branch>__<head>.md`), since the `--ephemeral -o` file is deleted as
    soon as the run returns. Best-effort: returns the path, or None on any
    failure; a write error must never break the review."""
    branch = current_branch(root)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "HEAD"
    when = datetime.datetime.now(datetime.timezone.utc)
    if plan["mode"] == "base":
        tag = plan["head"][:12]
        rng = f"{plan['base'][:12]}..{plan['head'][:12]} (base..head)"
    else:
        tag = "uncommitted-" + when.strftime("%Y%m%dT%H%M%SZ")
        rng = "uncommitted working tree"
    result = "no issues" if not prios else ", ".join(
        f"{k}:{prios[k]}" for k in sorted(prios))
    header = (
        f"# Codex review — {branch}\n\n"
        f"- root: {root_label(root)}\n"
        f"- range: {rng}\n"
        f"- when: {when.isoformat(timespec='seconds')}\n"
        f"- result: {result}\n"
    )
    try:
        os.makedirs(REVIEWS_DIR, exist_ok=True)
        path = os.path.join(REVIEWS_DIR, f"{slug}__{tag}.md")
        with open(path, "w") as fh:
            fh.write(header + "\n" + review_text.rstrip() + "\n")
        return path
    except OSError:
        return None


def execute_plan(plan, state, session_id):
    """Run Codex for one review plan, persist the full review body, append the
    JSONL summary, and — for a committed-delta review that produced output —
    advance the dedup `state` so the same commits aren't reviewed again. Returns
    {"status", "review", "root", "artifact"}; status is one of
    findings | clean | no_output | timeout. `state` is mutated in place; the
    caller owns save_state()."""
    root, mode = plan["root"], plan["mode"]
    scope = ["--uncommitted"] if mode == "uncommitted" else ["--base", plan["base"]]
    rec = {"event": "review", "root": root_label(root), "mode": mode,
           "session": session_id}
    if mode == "base":
        rec["base"], rec["head"] = plan["base"][:12], plan["head"][:12]
    # Pin the reviewed SHA for committed deltas so a concurrent HEAD move can't
    # swap the clone onto a foreign tree (the #388 false-positive race).
    pinned_head = plan["head"] if mode == "base" else None
    try:
        review = run_codex_review(root, scope, pinned_head)
    except subprocess.TimeoutExpired:
        log_event({**rec, "status": "timeout"})
        return {"status": "timeout", "review": "", "root": root, "artifact": None}
    if not review:
        log_event({**rec, "status": "no_output"})
        return {"status": "no_output", "review": "", "root": root, "artifact": None}
    prios = count_priorities(review)
    if mode == "base" and not _has_blocking(prios):
        # Reviewed up to head with no merge-blocking (P0/P1) finding: record it so
        # these commits aren't re-reviewed on the next push. A blocking review
        # deliberately does NOT advance the dedup state — so re-pushing the same
        # head re-runs the review (re-run codex), and a follow-up fix commit is
        # re-reviewed from the trunk merge-base, instead of the failing head being
        # treated as already-reviewed and flipped green by run_pre_push's "no new
        # commits to review" success path.
        state[os.path.realpath(root)] = plan["head"]
    artifact = write_artifact(root, plan, review, prios)
    if artifact:
        rec["artifact"] = _artifact_relpath(artifact)
    if prios:
        log_event({**rec, "status": "findings",
                   "findings": sum(prios.values()), "priorities": prios})
        return {"status": "findings", "review": review, "root": root, "artifact": artifact}
    log_event({**rec, "status": "clean"})
    return {"status": "clean", "review": review, "root": root, "artifact": artifact}


def run_pre_push(pushed_shas):
    """`--pre-push` entrypoint, launched detached by `.githooks/pre-push`.

    Reviews the commits being pushed from the current worktree (our cwd) against
    the trunk merge-base, and writes the result to the shared archive. Reviews
    committed work only (uncommitted edits aren't pushed; the Stop hook covers
    those), dedups against the Stop hook's state map, and is advisory — the
    wrapper never blocks the push on our outcome.

    The wrapper passes the local SHAs git is actually pushing (from its pre-push
    stdin). `codex exec review --base X` diffs X against the *current* worktree
    tree, so it can only faithfully review the checked-out HEAD — we can't review
    a non-checked-out commit without disturbing a worktree other sessions may be
    using. So: review HEAD when it's among the pushed refs (the normal `git push`
    / `git push origin <current-branch>` case), and *visibly skip* any pushed ref
    that isn't HEAD (e.g. `git push origin some-other-branch`) rather than
    silently reviewing the wrong tree. With no SHAs (manual run / empty stdin) we
    review HEAD."""
    if not codex_available():
        return
    root = worktree_root(os.getcwd(), os.getcwd())
    if not root:
        return
    head = rev(root, "HEAD")
    if not head:
        return
    if pushed_shas:
        review_head = False
        for sha in pushed_shas:
            if rev(root, sha) == head:
                review_head = True
            else:  # a ref we can't review from this checkout — surface it
                log_event({"event": "skip", "reason": "pushed_ref_not_head",
                           "root": root_label(root),
                           "pushed": sha[:12], "head": head[:12]})
    else:
        review_head = True  # manual invocation / empty stdin -> review HEAD
    if not review_head:
        return

    # Resolve the repo slug up front, while the pushing worktree is guaranteed to
    # exist. The review runs detached for minutes and the worktree may be renamed
    # or removed before we post the terminal status (issue #438) — carrying the
    # slug lets that POST still land instead of stranding the check on "pending".
    slug = _repo_slug(root)

    state = load_state()
    key = os.path.realpath(root)
    base, h = delta_for(root, head, state.get(key))
    if base is None:
        # Nothing beyond main, or already reviewed: the head is mergeable as far as
        # review goes, so mark the gate status green (a required check would
        # otherwise block a no-op / already-reviewed PR).
        _post_status(head, "success", "codex review: no new commits to review",
                     cwd=root, slug=slug)
        return
    if not diff_has_reviewable(root, base, h):
        state[key] = h  # delta is data-only; mark it reviewed
        save_state(state)
        _post_status(h, "success", "codex review: no reviewable code in delta",
                     cwd=root, slug=slug)
        return
    _post_status(h, "pending", "codex review running", cwd=root, slug=slug)
    result = execute_plan({"mode": "base", "root": root, "base": base, "head": h},
                          state, session_id=None)
    save_state(state)
    _post_review_status(root, h, result, slug=slug)


def _post_review_status(root, sha, result, slug=None):
    """Translate a committed-delta review result into a `codex-review` commit
    status. A review with a merge-blocking finding (P0/P1) posts `failure` so a
    required check holds the PR until it's fixed; lower-priority findings (P2/P3)
    post `success` and are advisory. A clean review posts `success`; a review that
    produced nothing usable posts `failure` so the head isn't treated as reviewed.

    The dedup state is deliberately NOT advanced for a blocking review (see
    execute_plan), so once the finding is fixed and the branch re-pushed the review
    runs again and the head can turn green.

    `slug` is the pre-resolved repo, carried in because this runs after the
    minutes-long review when `root`'s worktree may already be gone (issue #438)."""
    st = result.get("status")
    if st == "clean":
        _post_status(sha, "success", "codex review: no findings", cwd=root, slug=slug)
    elif st == "findings":
        prios = count_priorities(result.get("review", ""))
        desc = "codex review: " + ", ".join(f"{k}:{prios[k]}" for k in sorted(prios))
        if _has_blocking(prios):
            _post_status(sha, "failure", desc + " — fix P0/P1 then re-push",
                         cwd=root, slug=slug)
        else:
            _post_status(sha, "success", desc, cwd=root, slug=slug)
    elif st in ("no_output", "timeout"):
        _post_status(sha, "failure", f"codex review {st} — re-run before merge",
                     cwd=root, slug=slug)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    # Already continuing because of a prior review → don't run again.
    if payload.get("stop_hook_active"):
        return

    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    # Graceful no-op when Codex is unavailable (safe for the shared config).
    if not codex_available():
        return

    session_id = payload.get("session_id")

    # The Stop hook reviews only the session's *uncommitted* work (work in progress
    # at turn end), scoped to the worktree(s) this session edited. Committed/pushed
    # deltas are the pre-push hook's job — keeping them out of here is what stops a
    # commit+push+stop within one turn from being reviewed twice (see module docs).
    # `execute_plan` only touches the state map for `base` mode, so uncommitted
    # reviews need no state at all (state belongs to the pre-push hook).
    plans = [{"mode": "uncommitted", "root": root}
             for root in review_roots(payload, cwd)
             if has_reviewable_changes(root)]
    if not plans:
        return

    findings = []       # (root, review_text) for roots Codex flagged
    produced_output = False
    timed_out = False
    for plan in plans:
        result = execute_plan(plan, {}, session_id)
        status = result["status"]
        if status == "timeout":
            timed_out = True
        elif status != "no_output":  # findings | clean -> Codex produced output
            produced_output = True
            if status == "findings":
                findings.append((result["root"], result["review"]))

    if findings:
        multi = len(plans) > 1
        sections = []
        for root, review in findings:
            sections.append(f"### Review of `{root_label(root)}`\n\n{review}" if multi else review)
        reason = (
            "Codex ran an automated code review on this session's changes and "
            "found issues. Address each finding below, or briefly explain why it "
            "is acceptable, then finish.\n\n" + "\n\n".join(sections)
        )
        emit({"decision": "block", "reason": reason})
        return

    if timed_out:
        emit({"systemMessage": f"Codex review skipped: timed out after {REVIEW_TIMEOUT_SEC}s."})
        return
    if not produced_output:
        emit({"systemMessage": "Codex review skipped: no output (check `codex` auth/logs)."})
        return

    emit({"systemMessage": "✓ Codex review: no issues in this session's changes."})


if __name__ == "__main__":
    # `--pre-push` is the git-hook companion (committed-delta review of the commits
    # being pushed); the default reads a Stop-hook payload on stdin. The wrapper
    # passes the pushed local SHAs as trailing args (parsed from git's pre-push
    # stdin), so we don't parse git's ref lines as JSON here.
    # Drop any GIT_* env git exported into this hook (esp. the pre-push hook, where
    # GIT_DIR/GIT_WORK_TREE point at the worktree being pushed). Every git op here
    # resolves its repo from an explicit cwd/clone; inherited GIT_* would bind them
    # to the real worktree instead (issue #388 "Failure B").
    for _k in _GIT_ENV_VARS:
        os.environ.pop(_k, None)
    args = sys.argv[1:]
    if "--pre-push" in args:
        run_pre_push([a for a in args if a != "--pre-push"])
    else:
        main()
