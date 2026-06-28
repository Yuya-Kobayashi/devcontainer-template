FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# The base image ships a Yarn apt source whose GPG key (62D54FD4003F6525) is
# no longer accepted, causing `apt-get update` to fail before features install.
# Drop the source; install Yarn via corepack/npm if a project needs it.
RUN rm -f /etc/apt/sources.list.d/yarn.list

# Bake tooling and firewall deps into the image. Doing this at build time means
# the running container does not need `apt-get` or general-purpose sudo.
# squid is the egress forward proxy (see devc-init-firewall.sh, squid.conf).
#
# This is the project-agnostic baseline. Add project-specific tools (ffmpeg,
# imagemagick, a language toolchain, ...) in the apt list below, and allowlist
# any new network endpoints they need in allowed-domains.acl.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ripgrep \
      fd-find \
      jq \
      curl \
      iptables \
      ipset \
      dnsutils \
      squid \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf "$(command -v fdfind)" /usr/local/bin/fd

# Replace the distro squid config with our CONNECT-only FQDN allowlist.
COPY squid.conf /etc/squid/squid.conf
COPY allowed-domains.acl /etc/squid/allowed-domains.acl
RUN chown root:root /etc/squid/squid.conf /etc/squid/allowed-domains.acl \
 && chmod 0644 /etc/squid/squid.conf /etc/squid/allowed-domains.acl

# Install the egress allowlist script. It is the only command vscode may invoke
# with sudo (see post-create.sh, which writes /etc/sudoers.d/vscode).
#
# Installed under a `devc-` prefix rather than `/usr/local/bin/init-firewall.sh`:
# the `ghcr.io/anthropics/devcontainer-features/claude-code` feature historically
# clobbered that exact path with its own (stale) firewall script. We don't use
# that feature (Claude Code is installed via the native installer in
# post-create.sh), but the prefix keeps the sudoers rule and postStartCommand
# stable should anyone ever add it.
COPY devc-init-firewall.sh /usr/local/bin/devc-init-firewall.sh
RUN chmod 0755 /usr/local/bin/devc-init-firewall.sh \
 && chown root:root /usr/local/bin/devc-init-firewall.sh
