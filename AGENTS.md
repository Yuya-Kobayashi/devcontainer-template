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

## Model split: Fable orchestrates, pinned agents execute

When the main loop runs on a **Fable** (Mythos-class) model, spend it on what
it is uniquely good at — planning, architecture, judgment calls — and
delegate the mechanical work to the pinned agents in `.claude/agents/`:

| Agent | Tier | Delegate to it |
|---|---|---|
| `coder` | full | Writing/editing files + the change's own tests, builds, formatters — anything whose plan may need judgment mid-flight |
| `coder-lite` | budget | Routine, fully-specified, low-risk implementation: spelled-out diffs, renames, boilerplate, doc/config tweaks with clear acceptance checks |
| `worker` | full | Non-editing legwork: bulk reading, codebase exploration/search, verifying a change against acceptance checks, adversarial second opinions, research sweeps — returns findings, never edits |
| `worker-lite` | budget | Mechanical legwork: grep-and-summarize sweeps, checklist verification, inventory-style fact collection |

Hand `coder`/`coder-lite` a concrete plan, the target files or worktree,
and acceptance checks, and review the report and diff when it returns.
Choose the budget tier only when the task cannot need judgment mid-flight:
rework is reviewed and re-briefed by the orchestrator at main-loop rates,
which erases budget-tier savings quickly.

**Agent names are role + tier, never model names.** Each agent pins its
model in its own frontmatter (`model:`), and that line is the only place a
model is named — retargeting a tier to a new model is a one-line
frontmatter edit with no renames anywhere else. Keep model names out of
agent names, docs, and prompts.

**Choose the model by choosing the agent — pins live in frontmatter, never
the call.** The Agent tool's per-invocation `model` override does not
survive an interrupt: resuming a background agent re-resolves the model
from the agent definition, and an overridden run silently continues on the
session model (observed: 29 turns on the pinned model, then the session
model from the resume onward). Use a per-call override only for a short,
foreground, run-to-completion call. Never set `CLAUDE_CODE_SUBAGENT_MODEL`
— it outranks both the frontmatter and the per-call parameter, removing
the choice entirely.

**Don't spawn unpinned agents on Fable.** The built-in agent types
(general-purpose, Explore, Plan, claude, guide agents) carry no frontmatter
pin, so they inherit the session model and bill at main-loop rates. Route
every delegation through the pinned roster above — `worker` covers what
Explore or a read-only general-purpose spawn would do; `coder` covers
implementation. If a task genuinely fits no pinned agent, run it in the
main loop or add a pinned agent for it rather than reaching for a built-in.

**Keep in the main loop:** planning, final review and judgment,
orchestration `Bash` (git/gh/skills), and anything that needs the full
conversation context.

This is a standing instruction — treat it as the user having asked for
subagent use, every session. It is a convention, not a hook: honor it by
default, and edit directly only when a delegation round-trip is clearly
wasteful (e.g. a one-line fix you must verify yourself anyway). On a
non-Fable main model the split does not apply.

## Shared cache vs. corpus

Worktree isolation is the point — but two classes of repo-rooted data want to
escape it in opposite directions. `.claude/skills/_lib/repo_paths.py` resolves
both so a skill works the same whether it runs in the main checkout or in any
worktree:

- **Cache** (`cache_root()`) — large, gitignored, re-derivable data a skill
  *downloads* or *builds* (datasets, model weights, dependency caches, fetched
  results). It always resolves to the **main checkout** (via `git rev-parse
  --git-common-dir`, which every worktree shares), so N agents download once and
  read the same bytes — never once per worktree. By convention shared caches
  live under the gitignored top-level `cache/`.
- **Corpus** (`corpus_root()`) — tracked, committable artifacts a run *produces
  and commits*. It resolves to `$AGENT_CORPUS_ROOT` when an orchestrator sets it
  to the run's worktree (so output is committed in place), and otherwise
  collapses to the main checkout. Unset = identical to a plain run, so the split
  is a no-op until a multi-agent driver opts in.

Use them from a skill like:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "_lib"))
from repo_paths import cache_root, corpus_root

vods = cache_root() / "cache" / "vods"        # shared, download once
report = corpus_root() / "reports" / "out.md"  # committed by this run
```

## Automated review

When a turn ends with uncommitted code changes, the **Codex Stop hook** reviews
them and feeds findings back for you to address (`.claude/hooks/codex-review.py`).
On `git push`, the **pre-push hook** runs an advisory Codex review of the pushed
commits, archived under `.claude/logs/codex-reviews/`. Both no-op silently when
Codex isn't installed/logged in.
