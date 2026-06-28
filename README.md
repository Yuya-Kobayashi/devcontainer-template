# devcontainer-template

A starter repository for projects driven by **Claude Code** and **Codex** inside
a locked-down dev container. Clone it, rename, and start building — the container,
egress firewall, agent permissions, and automated Codex review are already wired
up.

## Start a new project from this template

```bash
# On GitHub: "Use this template" → create your new repo, then clone it.
# Or, to start from a local copy:
git clone https://github.com/Yuya-Kobayashi/devcontainer-template my-project
cd my-project
rm -rf .git && git init        # detach from the template's history
gh repo create my-project --private --source=. --remote=origin   # your new repo
```

Then **Reopen in Container** (VS Code) or `devcontainer up --workspace-folder .`.
First create builds the image, installs Claude Code + Codex, brings up the
firewall, and locks down `sudo` (a few minutes).

## First-run setup inside the container

```bash
claude        # authenticate Claude Code  (shared across all your template projects)
codex login   # authenticate Codex        (shared across all your template projects)
gh auth login # authenticate GitHub       (per project — do this in each repo)
```

See the **[credentials table](.devcontainer/README.md#credentials)** for how the
shared-vs-per-project split works (it's the named-volume naming in
`.devcontainer/devcontainer.json`).

## What's in here

| Path | What it is |
|------|------------|
| `.devcontainer/` | The container, Squid+iptables egress firewall, and lifecycle scripts. See its [README](.devcontainer/README.md). |
| `.claude/settings.json` | Bypass-permissions mode + credential deny-list + the two hooks below. |
| `.claude/hooks/bash-guard.py` | PreToolUse guard: blocks `sudo`, foreign-repo git/gh, credential reads, main-branch checkouts. |
| `.claude/hooks/codex-review.py` | Codex reviews uncommitted changes on Stop and pushed commits on pre-push. |
| `.githooks/pre-push` | Fires the pushed-commit Codex review (installed by post-create). |
| `AGENTS.md` / `CLAUDE.md` | Agent guide and conventions — trim per project. |

## Customizing per project

Add system packages to `.devcontainer/Dockerfile`, network endpoints to
`.devcontainer/allowed-domains.acl`, and project build/test/run notes to
`AGENTS.md`. See `.devcontainer/README.md` → "Customize per project".
