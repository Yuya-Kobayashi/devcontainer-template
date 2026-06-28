#!/usr/bin/env python3
"""PreToolUse Bash hook: defense-in-depth for bypassPermissions mode.

The egress firewall and locked-down sudoers in `.devcontainer/` are the primary
controls. This hook adds policy-level checks that catch attempts before they
reach the kernel:

  1. `gh` / `git` commands targeting a repo whose *owner* is outside the allowed
     set. Cross-repo work within your own account/orgs is allowed, but a repo
     owned by someone else (an org or collaborator grant the token can reach) is
     blocked, so a broad PAT can't be steered at an unrelated repo. The allowed
     owners are the `origin` remote's owner plus anything in
     $BASH_GUARD_ALLOWED_OWNERS (comma-separated). This is a namespace check, not
     a security boundary — the PAT's scope is the real boundary; this just keeps
     an auto-approved agent inside your own namespace. Also blocks the
     irreversible `gh repo` subcommands (delete/transfer/archive/rename/fork) and
     `gh repo create`, on any repo.
  2. Any `sudo` invocation. sudoers permits exactly one command (the firewall
     init) and only so the container lifecycle can run it; the agent never
     should, so this hook denies sudo outright.
  3. Reads of credential-bearing directories: ~/.claude, ~/.ssh, ~/.aws,
     ~/.config/gh, ~/.netrc.
  4. Branch-switching (`git checkout`/`git switch`) in the main checkout: the
     main working tree must stay on `main`; branch work belongs in a worktree
     under `.worktrees/` (see AGENTS.md -> "When to use a worktree").

Output is the structured PreToolUse "deny" decision; the hook never raises.
"""

import json
import os
import re
import subprocess
import sys

# Root of the main checkout. The hook is launched as
# `python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/bash-guard.py"`, so the env var is
# normally set; fall back to deriving it from this file's location.
PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def _origin_owner():
    """The owner (user/org) of the `origin` remote, lowercased, or None.

    `https://github.com/owner/name` and `git@github.com:owner/name` both yield
    `owner`. None when origin is absent or unparseable, in which case the owner
    set may be empty and the checks below fall open (we can't tell what's
    foreign — the firewall and PAT still apply)."""
    try:
        r = subprocess.run(
            ["git", "-C", PROJECT_DIR, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        url = r.stdout.strip().rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        m = re.search(r"[:/]([^/:]+)/[^/:]+$", url)
        return m.group(1).lower() if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def _allowed_owners():
    """Lowercased owner allowlist: the origin owner plus $BASH_GUARD_ALLOWED_OWNERS
    (comma-separated). Empty => can't determine scope => checks fall open."""
    owners = set()
    origin = _origin_owner()
    if origin:
        owners.add(origin)
    for part in os.environ.get("BASH_GUARD_ALLOWED_OWNERS", "").split(","):
        part = part.strip().lower()
        if part:
            owners.add(part)
    return owners


#: Lazily-computed owner allowlist. `None` means "not resolved yet" (distinct from
#: an empty set, which is a valid "couldn't determine scope" result). Resolved by
#: `allowed_owners()` only when a gh/git command is actually seen, so the local
#: `git remote get-url origin` in `_origin_owner` never runs for the common case
#: (ls, cat, …) — the hook fires on *every* Bash call, but only git work pays it.
_ALLOWED_OWNERS = None


def allowed_owners():
    """The owner allowlist, computed once per hook process and cached."""
    global _ALLOWED_OWNERS
    if _ALLOWED_OWNERS is None:
        _ALLOWED_OWNERS = _allowed_owners()
    return _ALLOWED_OWNERS


HOME = os.path.expanduser("~")
SENSITIVE_PATHS = [
    f"{HOME}/.claude",
    f"{HOME}/.ssh",
    f"{HOME}/.aws",
    f"{HOME}/.config/gh",
    f"{HOME}/.netrc",
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/etc/shadow",
]

READ_COMMANDS = (
    "cat", "less", "more", "head", "tail", "bat", "view",
    "cp", "mv", "rsync", "scp", "tar", "zip",
    "grep", "rg", "ag", "ack",
    "find", "fd", "fdfind", "ls",
    "xxd", "hexdump", "od", "strings",
    "openssl",
    # stream editors / formatters that will just as readily dump a file's bytes
    "sed", "awk", "gawk", "mawk", "nawk",
    "dd", "nl", "tac", "cut", "paste", "tee", "base64", "zcat",
)

# Leading `git` global options that can sit between `git` and a subcommand:
# `-c name=value`, `-C <dir>`, and any `--long[=value]`. Anchoring a matcher on
# the subcommand alone lets `git -c k=v checkout`/`git -c k=v clone <url>` slip
# past, so the subcommand matchers below skip this prefix first.
_GIT_GLOBAL_OPTS = r"(?:(?:-[cC]\s+\S+|--\S+)\s+)*"
# Same, but excluding `-C` so a separate matcher can still *capture* the `-C <dir>`.
_GIT_GLOBAL_OPTS_NO_C = r"(?:(?:-c\s+\S+|--\S+)\s+)*"


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def is_placeholder_repo(repo: str) -> bool:
    """gh expands the `:owner`/`:repo` (and `{owner}`/`{repo}`) placeholders to
    the *current* repository, so `gh api repos/:owner/:repo/...` targets this
    repo, not a foreign one — never block it."""
    return ":" in repo or "{" in repo


def owner_allowed(owner: str | None) -> bool:
    """True when `owner` is in the allowlist. Falls open when the owner can't be
    determined or the allowlist is empty (origin unknown) — the firewall and PAT
    scope are the backstops; this check only narrows the GitHub namespace."""
    owners = allowed_owners()
    if not owners or owner is None:
        return True
    return owner.lower() in owners


def _owner_of_url(url: str) -> str | None:
    """The owner segment of a GitHub URL, or None if it isn't a GitHub URL we can
    parse (non-GitHub hosts are left to the firewall)."""
    url = url.strip("'\"").rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    m = re.search(r"github\.com[:/]([^/:]+)/[^/:\s]+$", url, re.IGNORECASE)
    return m.group(1) if m else None


def _deny_owner(what: str, owner: str) -> None:
    allowed = ", ".join(sorted(allowed_owners())) or "(none detected)"
    deny(f"Blocked {what}: owner {owner!r} is outside the allowed set "
         f"[{allowed}]. Add it via $BASH_GUARD_ALLOWED_OWNERS if it's yours.")


def check_github_scope(cmd: str) -> None:
    """Keep `gh`/`git` inside the allowed owner namespace, and block irreversible
    `gh repo` actions on any repo. See the module docstring (item 1)."""
    if not re.search(r"(?:^|[\s;&|`(])(?:gh|git)\b", cmd):
        return

    # gh --repo / -R owner/name
    for m in re.finditer(r"(?:--repo|-R)(?:\s+|=)['\"]?([^\s'\"]+)", cmd):
        repo = m.group(1)
        if is_placeholder_repo(repo):
            continue
        owner = repo.split("/", 1)[0] if "/" in repo else None
        if owner and not owner_allowed(owner):
            _deny_owner(f"gh --repo {repo}", owner)

    # gh api repos/owner/name
    for m in re.finditer(
        r"\bgh\s+api\s+(?:[^\s]+\s+)*['\"]?/?repos/([^/'\"\s]+/[^/'\"\s]+)",
        cmd,
    ):
        repo = m.group(1)
        if is_placeholder_repo(repo):
            continue
        owner = repo.split("/", 1)[0]
        if not owner_allowed(owner):
            _deny_owner(f"gh api repos/{repo}", owner)

    if re.search(r"\bgh\s+repo\s+(delete|transfer|archive|rename|fork)\b", cmd):
        deny("Blocked destructive gh repo subcommand (delete/transfer/archive/rename/fork).")

    if re.search(r"\bgh\s+repo\s+create\b", cmd):
        deny("Blocked gh repo create: this project should not create new GitHub repos.")

    # git remote add/set-url <name> <url>
    for m in re.finditer(
        r"\bgit\s+(?:-C\s+\S+\s+)?remote\s+(?:add|set-url)\s+(?:--\S+\s+)*\S+\s+(\S+)",
        cmd,
    ):
        owner = _owner_of_url(m.group(1))
        if owner and not owner_allowed(owner):
            _deny_owner(f"git remote add/set-url to {m.group(1)}", owner)

    # push / clone / fetch / pull aimed at a GitHub URL outside the namespace. A
    # remote *name* (`origin`) or local path is fine; only URL-looking tokens are
    # checked. Scan the *whole* git invocation, not just post-subcommand args, so
    # a URL injected via a leading config override is inspected too — whether in
    # the value (`-c remote.origin.url=<url> fetch origin`) or the key
    # (`-c url.<url>.insteadOf=x pull`).
    for m in re.finditer(r"\bgit\b([^|;&\n]*)", cmd):
        body = m.group(1)
        sub = re.search(r"\b(push|clone|fetch|pull)\b", body)
        if not sub:
            continue
        for token in body.split():
            cand = token.strip("'\"")
            looks_like_url = "://" in cand or (cand.count(":") and "@" in cand and "github" in cand.lower())
            if not looks_like_url:
                continue
            owner = _owner_of_url(cand)
            if owner and not owner_allowed(owner):
                _deny_owner(f"git {sub.group(1)} to {cand}", owner)


def check_sudo(cmd: str) -> None:
    # Deny *all* sudo from the agent's Bash tool. sudoers permits exactly one
    # command — /usr/local/bin/devc-init-firewall.sh — and only so the container
    # lifecycle (postStartCommand / post-create.sh, which do not go through this
    # hook) can run it. The agent must not: re-running even the firewall init
    # briefly flushes the iptables OUTPUT chain to ACCEPT (full, unrestricted
    # egress) before re-locking it, and that primitive must not be reachable
    # from an auto-approved tool call. Firewall reinit is a lifecycle / human
    # action (run it from your own shell, or rebuild the container).
    #
    # Match `sudo` as a command (not part of another word like "pseudo"). The
    # optional `(?:\S*/)?` catches a path-qualified invocation (`/usr/bin/sudo`,
    # `./sudo`) — sudoers gates the binary by what it runs regardless of how it's
    # named, so the policy check must too.
    for m in re.finditer(
        r"(?:^|[\s;&|`(])(?:\S*/)?sudo\b((?:\s+-[A-Za-z]+)*)\s+(\S+)", cmd):
        deny(
            f"Blocked sudo {m.group(2)}: sudo is denied from the agent's Bash "
            "tool. The firewall init (the only command sudoers permits) is run "
            "by the container lifecycle or a human, not auto-approved tooling."
        )


def check_sensitive_reads(cmd: str) -> None:
    tokens = re.findall(r"[^\s'\"`;|&()<>]+", cmd)
    if not tokens:
        return

    # Only run the path check if the command line invokes one of the read tools.
    invokes_read = any(
        re.search(rf"(?:^|[\s;&|`(]){re.escape(name)}\b", cmd)
        for name in READ_COMMANDS
    )
    if not invokes_read:
        # A scripting interpreter can read any file — not only via `-c`/`-e`, but
        # also a here-doc, `-` (stdin), or a script path (e.g.
        # `python3 - <<'PY' ... open("~/.ssh/id_rsa") ... PY`). Gating on `-[ce]`
        # let the here-doc/stdin form slip the credential check entirely, so treat
        # *any* interpreter invocation as a read vector. This only widens which
        # commands get scanned; the loop below still denies solely on a
        # sensitive-path token, so plain `python3 foo.py` stays allowed.
        if not re.search(r"\b(?:python3?|perl|ruby|node)\b", cmd):
            return

    for tok in tokens:
        # Strip leading redirections / option-value prefixes like --file=...
        if "=" in tok:
            tok = tok.split("=", 1)[1]
        unquoted = tok.strip("'\"")
        # A `$VAR` / `${VAR}` (e.g. `$HOME/.ssh`, `$CLAUDE_CONFIG_DIR/.claude.json`)
        # the shell would expand to a credential dir must be checked too, not
        # skipped as "not absolute". expandvars runs before expanduser (shell order).
        if not unquoted.startswith(("/", "~", "$")):
            continue
        expanded = os.path.expanduser(os.path.expandvars(unquoted))
        if not expanded.startswith("/"):
            continue  # unresolved $VAR or relative — nothing to compare
        for sp in SENSITIVE_PATHS:
            if expanded == sp or expanded.startswith(sp + "/"):
                deny(f"Blocked access to sensitive path {expanded}.")


# --- Worktree discipline -----------------------------------------------------
#
# The main checkout (PROJECT_DIR) must always stay on `main`. All branch work
# happens in a worktree under `.worktrees/`. This blocks `git checkout`/`git
# switch` that would move the *main* working tree's HEAD onto another branch.
# Allowed in the main checkout: returning to `main`, and path restores
# (`git checkout -- <file>`). Branch ops inside a `.worktrees/` tree are fine.

# A checkout/switch invocation: any leading global options, the subcommand, then
# its args up to the next command separator.
_BRANCHY = re.compile(rf"\bgit\s+{_GIT_GLOBAL_OPTS}(checkout|switch)\b([^&|;\n]*)")

WORKTREE_HINT = (
    "do branch work in a worktree instead:\n"
    "  git worktree add .worktrees/<project>/<slug> -b <project>/<slug> main\n"
    "  cd .worktrees/<project>/<slug>\n"
    "See AGENTS.md -> 'When to use a worktree'. "
    "(`git checkout main` to return the main checkout to main is allowed.)"
)


def _resolve_against(base: str, path: str) -> str:
    path = path.strip("'\"")
    return path if os.path.isabs(path) else os.path.join(base or PROJECT_DIR, path)


def _worktree_root(path: str) -> str | None:
    """The git worktree top-level containing `path`, or None if it can't be
    resolved (path missing, not a repo, git unavailable)."""
    try:
        r = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _is_main_checkout(path: str) -> bool:
    """True when `path` belongs to the main checkout (PROJECT_DIR).

    Compares the containing *worktree root*, not the path itself: a `git
    checkout` run from any subdirectory of the main checkout (e.g. `.claude/`)
    still moves the main worktree's HEAD, so the guard must apply there too.
    Paths inside a `.worktrees/<branch>` tree resolve to that worktree's root
    (≠ PROJECT_DIR) and are correctly left alone. Falls back to an exact path
    match when the root can't be resolved (e.g. a not-yet-created worktree
    dir)."""
    target = _worktree_root(path) or path
    try:
        return os.path.realpath(target) == os.path.realpath(PROJECT_DIR)
    except OSError:
        return False


def _is_existing_branch(tree: str, name: str) -> bool:
    try:
        r = subprocess.run(
            ["git", "-C", tree, "rev-parse", "--verify", "--quiet",
             f"refs/heads/{name}"],
            capture_output=True, timeout=3,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _evaluate(sub: str, rest: str, tree: str) -> None:
    """Deny if this checkout/switch would move the main tree off `main`."""
    args = rest.split()
    # A long option can carry its value attached: `--orphan=foo`, `--create=foo`.
    # Compare on the option *name* (before any `=`) so those forms are caught like
    # their bare equivalents — otherwise `git switch --create=foo` / `--orphan=foo`
    # move the main checkout off `main` undetected.
    long_opts = {a.split("=", 1)[0] for a in args if a.startswith("--")}
    # --orphan starts a brand-new (parentless) branch; --detach / -d moves the
    # tree to a detached HEAD. Both leave `main` without naming an existing
    # branch, so neither is caught by the create / branch-name checks below.
    if "--orphan" in long_opts:
        deny(f"Blocked `git {sub} --orphan` in the main checkout "
             f"({PROJECT_DIR}): {WORKTREE_HINT}")
    if "--detach" in long_opts or (sub == "switch" and "-d" in args):
        deny(f"Blocked `git {sub} --detach` in the main checkout "
             f"({PROJECT_DIR}): {WORKTREE_HINT}")
    if "--" in args:
        return  # `git checkout -- <file>` and friends are path restores
    # -b/-B (checkout & switch) and -c/-C (switch) create a branch; so do switch's
    # long forms --create / --force-create, including their `=value` spelling.
    creates = (any(a in ("-b", "-B", "-c", "-C") for a in args)
               or bool(long_opts & {"--create", "--force-create"}))
    prev_branch = "-" in args  # `git switch -` / `git checkout -`
    positionals = [a for a in args if not a.startswith("-")]
    target = positionals[0] if positionals else None

    if creates:
        deny(f"Blocked `git {sub} -b {target or '<branch>'}` in the main "
             f"checkout ({PROJECT_DIR}): {WORKTREE_HINT}")
    if target in ("main", "master"):
        return
    if sub == "switch":
        # `git switch` only ever changes branches.
        if target or prev_branch:
            deny(f"Blocked `git switch {target or '-'}` in the main checkout "
                 f"({PROJECT_DIR}): {WORKTREE_HINT}")
        return
    # `git checkout` without -b/--: a branch switch only if the token names a
    # branch (otherwise it's a file/path restore, which is fine).
    if prev_branch:
        deny(f"Blocked `git checkout -` in the main checkout "
             f"({PROJECT_DIR}): {WORKTREE_HINT}")
    if target and target != "HEAD" and _is_existing_branch(tree, target):
        deny(f"Blocked `git checkout {target}` in the main checkout "
             f"({PROJECT_DIR}): {WORKTREE_HINT}")


def check_worktree_discipline(cmd: str, cwd: str) -> None:
    if not (re.search(r"\bgit\b", cmd) and re.search(r"\b(?:checkout|switch)\b", cmd)):
        return
    # Walk the command left to right, tracking the directory a `cd` would land in
    # so a `cd .worktrees/... && git checkout -b ...` is recognised as worktree
    # work, while a trailing `... && cd` after the checkout is not.
    current = cwd or PROJECT_DIR
    # Split on newlines too: otherwise a `cd <dir>` on its own line is matched as
    # the whole segment and `continue`d past, skipping a `git checkout` that
    # follows it on the next line.
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        seg = seg.strip()
        cdm = re.match(r"cd\s+(\S+)", seg)
        if cdm:
            current = _resolve_against(current, cdm.group(1))
            continue
        gm = _BRANCHY.search(seg)
        if not gm:
            continue
        # An explicit `-C <dir>` on this git command overrides the cwd (other
        # global options like `-c k=v` may sit on either side of it).
        cm = re.search(
            rf"\bgit\s+{_GIT_GLOBAL_OPTS_NO_C}-C\s+(\S+)\s+"
            rf"{_GIT_GLOBAL_OPTS_NO_C}(?:checkout|switch)\b", seg)
        tree = _resolve_against(current, cm.group(1)) if cm else current
        if _is_main_checkout(tree):
            _evaluate(gm.group(1), gm.group(2), tree)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = (data.get("tool_input") or {}).get("command", "") or ""
    if not cmd:
        sys.exit(0)

    check_sudo(cmd)
    check_github_scope(cmd)
    check_sensitive_reads(cmd)
    check_worktree_discipline(cmd, data.get("cwd") or "")
    sys.exit(0)


if __name__ == "__main__":
    main()
