#!/usr/bin/env bash
set -euo pipefail

# Take ownership of persistent config volume mounts. Docker creates named-volume
# mount points owned by root, so without this the unprivileged vscode user can't
# write into them — and sudo is locked down to the firewall script by the end of
# this script, so the chown can only happen here at create time.
#
# Docker also creates each mount's *parent* dir root-owned (e.g. ~/.config, the
# parent of the gh volume). Those parents aren't in the chown -R list below, so
# they stay root-owned and vscode can't create siblings in them — which breaks
# Claude Code, whose installer/updater mkdir ~/.cache/claude (EACCES otherwise).
# Own the parents too (non-recursive).
mkdir -p "$HOME/.claude" "$HOME/.codex" "$HOME/.config/gh" "$HOME/.cache"
sudo chown "$(id -u):$(id -g)" "$HOME/.config" "$HOME/.cache"
sudo chown -R "$(id -u):$(id -g)" \
  "$HOME/.claude" "$HOME/.codex" "$HOME/.config/gh" || true

# Claude Code keeps auth + project state in ~/.claude.json, which it rewrites
# atomically (temp file + rename) — that clobbers any symlink shim into a real
# file in the non-persisted home dir, losing state on rebuild. Instead point
# CLAUDE_CONFIG_DIR (set in devcontainer.json) at the persisted .claude volume
# so Claude writes .claude.json directly onto the volume; nothing to do here.

# Bring the egress proxy + firewall up before anything else needs network.
# HTTP(S)_PROXY is set in devcontainer.json's containerEnv, so the installs
# below rely on squid being live. Doing this here (rather than only at
# postStart) closes the window between container create and the first start.
sudo /usr/local/bin/devc-init-firewall.sh

# Install Codex CLI into the user's persisted CODEX_HOME (~/.codex). Codex powers
# the automated reviewer Stop hook (.claude/hooks/codex-review.py). The standalone
# installer keeps package metadata under CODEX_HOME, so install after the ~/.codex
# named volume is mounted instead of baking it into the image. The installer +
# binary are fetched from chatgpt.com, which the egress allowlist permits.
mkdir -p "$HOME/.local/bin"
if ! command -v codex >/dev/null 2>&1; then
  install_codex() {
    release="$1"
    for attempt in 1 2 3; do
      installer_path="$(mktemp)"
      curl -fsSL https://chatgpt.com/codex/install.sh -o "$installer_path"
      # Debian bookworm's default mawk does not match the installer's {64}
      # interval regex, so make the checksum lookup portable before running it.
      sed -i 's|\$1 ~ /\^\[0-9a-fA-F\]{64}\$/|length(\$1) == 64 \&\& \$1 ~ /^[0-9a-fA-F][0-9a-fA-F]*$/|' "$installer_path"

      if CODEX_NON_INTERACTIVE=1 CODEX_INSTALL_DIR="$HOME/.local/bin" sh "$installer_path" --release "$release"; then
        rm -f "$installer_path"
        return 0
      fi
      rm -f "$installer_path"

      if [ "$attempt" -lt 3 ]; then
        sleep $((attempt * 5))
      fi
    done
    return 1
  }

  codex_release="${CODEX_RELEASE:-latest}"
  if ! install_codex "$codex_release"; then
    fallback_release="0.138.0"
    if [ "$codex_release" = "$fallback_release" ]; then
      exit 1
    fi

    echo "warning: Codex CLI install failed for '$codex_release'; retrying with $fallback_release" >&2
    install_codex "$fallback_release"
  fi
fi
sudo ln -sf "$HOME/.local/bin/codex" /usr/local/bin/codex

# Install Claude Code via the native installer. The npm global method (and the
# claude-code devcontainer feature that used it) is deprecated and froze the CLI
# at image-build time, leaving the container behind the host. The native
# installer drops the binary in ~/.local/bin — first on PATH — and ~/.local is
# not a persisted volume, so reinstall on every create to pull the latest. The
# background auto-updater is left enabled so long-lived containers keep current
# too; both the install script and the updater reach claude.ai, which is in the
# egress allowlist (allowed-domains.acl). No sudo needed — it installs into HOME.
if [ ! -x "$HOME/.local/bin/claude" ]; then
  for attempt in 1 2 3; do
    if curl -fsSL https://claude.ai/install.sh | bash; then
      break
    fi
    if [ "$attempt" -lt 3 ]; then
      sleep $((attempt * 5))
    fi
  done
  # A failed install means *no* claude at all. Fail the create loudly (like the
  # Codex block above) instead of letting the container come up silently missing
  # its primary tool.
  if [ ! -x "$HOME/.local/bin/claude" ]; then
    echo "error: Claude Code native install failed after 3 attempts" \
         "(claude.ai unreachable or missing from the egress allowlist?);" \
         "aborting container create" >&2
    exit 1
  fi
fi

# `uv` gives skills/tooling fast, isolated Python environments; pre-commit runs
# the repo's hooks if a .pre-commit-config.yaml is present. ~/.local is not a
# persisted volume, so this reinstalls on each create — same as claude/codex.
pip install --user --upgrade pip pre-commit uv

if [ -f .pre-commit-config.yaml ]; then
  pre-commit install
fi

# Install the committed git hooks (currently .githooks/pre-push: the Codex review
# that catches subagent-authored PRs before they leave the machine — see
# .claude/hooks/codex-review.py --pre-push). We drop a thin shim into the active
# hooks dir rather than set core.hooksPath=.githooks, so the pre-commit
# framework's own hooks keep working if .pre-commit-config.yaml is ever added
# (a relative hooksPath would shadow them). The shim execs the committed hook, so
# edits to .githooks/ take effect without re-running this script.
#
# Migrate off the former relative-hooksPath mechanism: a stale `core.hooksPath
# = .githooks` would make Git ignore the shim we install below.
if [ "$(git config --get core.hooksPath || true)" = ".githooks" ]; then
  git config --unset core.hooksPath
fi
# Install into the active hooks dir: an explicit absolute core.hooksPath when one
# is set (some setups point it at .git/hooks), else the shared .git/hooks default
# — both are used by every linked worktree, so one install covers them all.
hooks_dir="$(git config --get core.hooksPath || true)"
case "$hooks_dir" in
  /*) : ;;  # absolute hooksPath -> install there
  *)  hooks_dir="$(git rev-parse --git-common-dir)/hooks" ;;  # unset/relative -> default
esac
mkdir -p "$hooks_dir"
for hook in pre-push; do
  [ -f ".githooks/$hook" ] || continue
  dest="$hooks_dir/$hook"
  if [ -e "$dest" ] && ! grep -q 'devc:githooks-shim' "$dest" 2>/dev/null; then
    echo "warning: $dest already exists and isn't our shim; leaving it in place" >&2
    continue
  fi
  cat > "$dest" <<'SHIM'
#!/usr/bin/env bash
# devc:githooks-shim — auto-generated by .devcontainer/post-create.sh.
# Delegates to the committed .githooks/<name> of whichever worktree is pushing,
# so the hook logic always tracks the checkout (and survives .githooks edits).
root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
name="$(basename "$0")"
[ -x "$root/.githooks/$name" ] || exit 0
exec "$root/.githooks/$name" "$@"
SHIM
  chmod +x "$dest"
done

# Lock sudo down to exactly one command: the firewall init script. Until this
# runs, vscode has the base-image-default NOPASSWD:ALL granted by the
# common-utils feature; this is what makes the apt installs (at build time) and
# the chown above possible. After it runs, sudo can only invoke
# /usr/local/bin/devc-init-firewall.sh.
#
# Write and chmod in a single sudo invocation: writing the file replaces the
# NOPASSWD:ALL policy, so any *second* sudo call here would already be locked
# out and prompt for a password vscode does not have.
sudo install -m 0440 -o root -g root /dev/stdin /etc/sudoers.d/vscode <<'EOF'
vscode ALL=(root) NOPASSWD: /usr/local/bin/devc-init-firewall.sh
EOF

# Warn if the gh token is sitting in plaintext on disk. Headless devcontainers
# have no system keyring, so `gh auth login` defaults to plaintext storage in
# ~/.config/gh/hosts.yml — which now persists across rebuilds via the
# gh-config-* named volume. Surface this so the user can opt into GH_TOKEN
# or a credential helper if the tradeoff isn't acceptable.
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  if grep -qE '^[[:space:]]+oauth_token:' "$HOME/.config/gh/hosts.yml" 2>/dev/null; then
    echo "warning: gh auth token stored in plaintext at ~/.config/gh/hosts.yml" >&2
    echo "         (persisted via the gh-config-* volume across container rebuilds)" >&2
  fi
fi
