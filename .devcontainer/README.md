# devcontainer

A locked-down dev container for working with **Claude Code** and **Codex** under
an egress allowlist firewall. This is the project-agnostic baseline; extend it
per project (see "Customize per project").

## What's inside

- **Python 3.12** base (`mcr.microsoft.com/devcontainers/python`).
- **Claude Code** (CLI, via the native installer in `post-create.sh`, so it
  tracks latest instead of freezing at image-build) + **gh**.
- **Codex CLI**, installed into the persisted `CODEX_HOME`; it powers the
  automated reviewer Stop hook (`.claude/hooks/codex-review.py`).
- **ripgrep, fd, jq** and the **Squid + iptables firewall**.

## Security model

All outbound traffic is forced through a local Squid proxy (`127.0.0.1:3128`);
`iptables` drops everything else except loopback and DNS. Squid only allows the
FQDNs in [`allowed-domains.acl`](allowed-domains.acl). `sudo` is locked to
exactly one command — the firewall script — after first boot. Claude runs in
`bypassPermissions` mode (`.claude/settings.json`), but the firewall is the real
containment boundary; `.claude/settings.json` additionally denies reads/writes
to credentials (`~/.ssh`, `~/.aws`, gh config, `/etc`, …) and a `bash-guard.py`
PreToolUse hook blocks `sudo`, foreign-repo git/gh, and main-branch checkouts.

## Credentials

| Tool         | Volume                                        | Scope |
|--------------|-----------------------------------------------|-------|
| Claude Code  | `claude-config-${devcontainerId}`             | **Per project** (unique per repo) |
| Codex        | `codex-config-${devcontainerId}`              | **Per project** (unique per repo) |
| GitHub (`gh`)| `gh-config-${devcontainerId}`                 | **Per project** (unique per repo) |

Every volume is keyed to `${devcontainerId}` (unique per workspace), so each
repo cloned from this template gets its **own** Claude, Codex, and GitHub state.
Authenticate each tool once per repo — `claude login`, `codex login`, and
`gh auth login` — after which it persists. All three volumes survive container
rebuilds of that same project.

> Concurrency: because no config volume is shared, you can run multiple of these
> containers **at the same time** without them clobbering each other. (Claude
> rewrites `~/.claude.json` atomically, so a shared volume would be
> last-write-wins on per-project state — keying per project avoids that.)
>
> Prefer to log in just once and share auth across repos instead? Switch the
> Claude/Codex `source=` lines in `devcontainer.json` back to fixed names
> (`claude-config-shared`, `codex-config-shared`) — but then avoid running two
> containers concurrently.

## Adding an allowed domain

Edit [`allowed-domains.acl`](allowed-domains.acl) (a leading dot = host + all
subdomains), then either rebuild, or hot-reload without a rebuild:

```bash
sudo cp .devcontainer/allowed-domains.acl /etc/squid/allowed-domains.acl
sudo /usr/sbin/squid -k reconfigure
```

If a download fails, check what got blocked:

```bash
sudo tail -f /var/log/squid/access.log   # TCP_DENIED = not in allowlist
```

## Customize per project

- **System packages** → add to the `apt-get install` list in [`Dockerfile`](Dockerfile).
- **Network endpoints** → add FQDNs under the marker in `allowed-domains.acl`.
- **Exposed ports** → uncomment/set `appPort` in `devcontainer.json`.
- **Extra config/cache volumes** (e.g. a model cache) → add a `mounts` entry and
  a matching `chown` line in `post-create.sh`.
