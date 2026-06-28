#!/usr/bin/env python3
"""Best-effort GitHub commit-status posting for the codex review gate. Imported by
the pre-push hook (automatic). Never raises: failing to post a status must never
break a hook, a push, or the gate. Posting needs a token with "Commit statuses:
write"; without it the POST 403s and this quietly returns False (the required
check then just stays unmet, which is the safe direction)."""
import os
import re
import subprocess

CONTEXT = "codex-review"
_GH_RE = re.compile(r"github\.com[:/]+([^/\s]+/[^/\s]+?)(?:\.git)?/?$")
_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}$")

# A finding at one of these priorities is merge-blocking: the `codex-review` status
# is posted `failure` (not `success`) so a required check holds the PR until the
# finding is fixed and the branch re-pushed (which re-runs the review). Lower
# priorities (P2/P3) stay advisory — posted `success`.
BLOCKING_PRIORITIES = ("P0", "P1")


def has_blocking(priorities):
    """True when a {priority: count} tally (e.g. {'P1': 2, 'P3': 1}) contains a
    merge-blocking finding (any P0/P1)."""
    return any((priorities or {}).get(p) for p in BLOCKING_PRIORITIES)


def repo_slug(cwd=None):
    """`owner/repo` parsed from origin's URL (https or ssh), or None."""
    try:
        r = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd,
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    m = _GH_RE.search(r.stdout.strip())
    return m.group(1) if m else None


def post_status(sha, state, description, cwd=None, target_url=None, context=CONTEXT,
                slug=None):
    """POST a `context` commit status for `sha`. `state` is one of
    pending|success|failure|error. Returns True iff the POST succeeded; any
    failure (no gh, no perms, no origin, malformed sha) returns False, never
    raises.

    Pass `slug` ("owner/repo") to skip resolving it from `cwd`'s origin. The
    pre-push reviewer runs detached with `cwd` pinned to the pushing worktree,
    which can be renamed or removed during the minutes-long review; a pre-resolved
    slug lets the terminal status still post after the worktree is gone. gh needs
    only its global auth — not a repo cwd — to POST, so a `cwd` that no longer
    exists is dropped rather than left to make the spawn fail."""
    if not sha or not _SHA_RE.match(sha):
        return False
    slug = slug or repo_slug(cwd)
    if not slug:
        return False
    run_cwd = cwd if (cwd and os.path.isdir(cwd)) else None
    args = ["gh", "api", "-X", "POST", f"repos/{slug}/statuses/{sha}",
            "-f", f"state={state}",
            "-f", f"context={context}",
            "-f", f"description={(description or '')[:140]}"]
    if target_url:
        args += ["-f", f"target_url={target_url}"]
    try:
        r = subprocess.run(args, cwd=run_cwd, capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False
