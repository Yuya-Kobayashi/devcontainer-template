# AGENTS.md

## Project

_Describe what this repo is: its goals, data sources, layout, and the
build/test/run/tooling conventions an agent needs. This placeholder ships with
the devcontainer template — replace it with the real thing and trim anything
below that doesn't apply._

`AGENTS.md` is the shared source of truth for Claude Code, Codex, and any other
coding agent working in this repo. `CLAUDE.md` is a short note plus an
`@AGENTS.md` import so Claude Code loads this file every session — keep the
content here, not duplicated there.

## Agent environment notes

Work happens inside the devcontainer (`.devcontainer/`): Python 3.12, an egress
allowlist firewall (local Squid proxy + iptables lockdown), and `sudo` locked to
the firewall script. Do not bypass or broaden the firewall without explicit
approval. If a needed endpoint is blocked, add the narrowest domain entry to
`.devcontainer/allowed-domains.acl` and document why (see `.devcontainer/README.md`).

Keep credentials out of the repository. Claude state belongs in
`/home/vscode/.claude`, Codex state/auth in `$CODEX_HOME` (`/home/vscode/.codex`);
both are persisted named volumes. The `bash-guard.py` PreToolUse hook blocks
reads of credential dirs (`~/.ssh`, `~/.aws`, gh config, …) and denies `sudo`.

`gh`/`git` may target any repo whose **owner** is in the allowed set — the
`origin` owner plus a comma-separated `$BASH_GUARD_ALLOWED_OWNERS` (set it to add
the orgs you own). Repos owned by anyone else are blocked, so a broad auth token
can't be aimed at an unrelated repo; the token's own scope is still the real
boundary. The destructive `gh repo` subcommands (delete/transfer/archive/rename/
fork) and `gh repo create` stay blocked regardless of owner.

## When to use a worktree

The **main checkout stays on `main`** — several sessions may run against it at
once, so it must stay a clean `main` working tree. **Before you `Write`/`Edit`
any file**, create an isolated worktree under `.worktrees/` and `cd` into it:

```sh
# from the repo root, for a task with slug "my-change"
git worktree add .worktrees/<project>/my-change -b <project>/my-change main
cd .worktrees/<project>/my-change
```

`git worktree add … -b <branch> main` creates the branch *and* its directory in
one step — no separate `git checkout -b`. `.worktrees/` is gitignored.

This is enforced: the Bash guard (`.claude/hooks/bash-guard.py`) **denies** `git
checkout`/`git switch` that would move the main checkout off `main`. Returning it
to `main` (`git checkout main`), path restores (`git checkout -- <file>`), and any
branch op inside a `.worktrees/` tree are still allowed. Trivial *reads* in the
main checkout (`git status`, `ls`, reading a file) are fine.

### Branch naming & merging back

Branches use `<project>/<slug>` (kebab-case project + short description); the
worktree directory mirrors the branch name. Non-trivial work merges into `main`
via a Pull Request — never push directly to `main`:

1. Commit on the feature branch, then `git push -u origin <project>/<slug>`.
2. `gh pr create --base main --title "..." --body "..."` (link the issue with `Closes #N`).
3. After merge, `git worktree remove .worktrees/<project>/<slug>` from the main checkout.

## Automated review

When a turn ends with uncommitted code changes, the **Codex Stop hook** reviews
them and feeds findings back for you to address (`.claude/hooks/codex-review.py`).
On `git push`, the **pre-push hook** runs an advisory Codex review of the pushed
commits, archived under `.claude/logs/codex-reviews/`. Both no-op silently when
Codex isn't installed/logged in.
