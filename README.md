# devcontainer-template

A starter repository for projects driven by **Claude Code** and **Codex** inside
a locked-down dev container. Clone it, rename, and start building — the container,
egress firewall, agent permissions, and automated Codex review are already wired
up.

## Prerequisites

- **A Docker engine that allows added Linux capabilities.** The firewall needs
  `NET_ADMIN` + `NET_RAW` (set in `.devcontainer/devcontainer.json` → `runArgs`),
  so the container won't start without them. [OrbStack](https://orbstack.dev),
  Docker Desktop, and Colima all work; fully rootless Docker generally does not.
- **One way to open the container** — either:
  - **VS Code** with the **Dev Containers** extension (`ms-vscode-remote.remote-containers`), or
  - the **Dev Containers CLI**: `npm install -g @devcontainers/cli` (needs Node ≥ 18).
- **Nothing else on the host.** Claude Code, Codex, `gh`, `uv`, and the firewall
  are all installed *inside* the container by `.devcontainer/post-create.sh`; you
  don't install them on your machine.

## Start a new project from this template

```bash
# On GitHub: "Use this template" → create your new repo, then clone it.
# Or, to start from a local copy:
git clone https://github.com/Yuya-Kobayashi/devcontainer-template my-project
cd my-project
rm -rf .git && git init        # detach from the template's history
gh repo create my-project --private --source=. --remote=origin   # your new repo
```

## Run & operate the container

### Spin it up

**VS Code:** open the folder, then run **Dev Containers: Reopen in Container**
from the Command Palette (`F1`). VS Code builds the image and drops you into a
shell in the container.

**CLI:** from the repo root,

```bash
devcontainer up --workspace-folder .          # build + start (first run: a few minutes)
devcontainer exec --workspace-folder . zsh    # open a shell inside it
```

The **first** create runs `.devcontainer/post-create.sh`, which (in order):
takes ownership of the persisted config volumes, brings up the Squid + iptables
firewall, installs Codex and Claude Code via their native installers, installs
`uv` + `pre-commit`, wires up the git pre-push Codex-review hook, and finally
locks `sudo` down to just the firewall script. Every subsequent **start** re-runs
the firewall init (`postStartCommand`) — that's seconds, not minutes.

### Day-to-day

| Action | VS Code | CLI |
|--------|---------|-----|
| Open another shell | new integrated terminal | `devcontainer exec --workspace-folder . zsh` |
| Rebuild after editing `.devcontainer/*` | **Dev Containers: Rebuild Container** | `devcontainer up --workspace-folder . --remove-existing-container` |
| Stop / restart | reopen folder locally, or close VS Code | `docker stop <name>` / `docker start <name>` |

The container name is the workspace folder's basename (set by `name` in
`devcontainer.json`), so `docker ps` shows it under that name.

### What persists

Claude, Codex, and `gh` auth live in **named volumes** keyed per project (see the
[credentials table](.devcontainer/README.md#credentials)), so they **survive
rebuilds** — you authenticate once per repo, not once per rebuild. The CLI tools
themselves live in the non-persisted `~/.local`, so a rebuild reinstalls the
latest. Removing the *container* keeps the volumes; removing the *volumes*
(`docker volume rm …-${devcontainerId}`) is what clears saved logins.

### Running several at once

Because every config volume is keyed to `${devcontainerId}` (unique per
workspace), you can run **multiple template-derived containers concurrently**
without their Claude/Codex/`gh` state clobbering each other.

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
| `.claude/hooks/bash-guard.py` | PreToolUse guard: blocks `sudo`, out-of-namespace git/gh (owner not in the allowed set), credential reads, main-branch checkouts. |
| `.claude/hooks/codex-review.py` | Codex reviews uncommitted changes on Stop and pushed commits on pre-push. |
| `.githooks/pre-push` | Fires the pushed-commit Codex review (installed by post-create). |
| `AGENTS.md` / `CLAUDE.md` | Agent guide and conventions — trim per project. |

## Customizing per project

Add system packages to `.devcontainer/Dockerfile`, network endpoints to
`.devcontainer/allowed-domains.acl`, and project build/test/run notes to
`AGENTS.md`. See `.devcontainer/README.md` → "Customize per project".
