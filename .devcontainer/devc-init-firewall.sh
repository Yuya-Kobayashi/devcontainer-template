#!/usr/bin/env bash
# Egress allowlist for the devcontainer.
#
# Approach: a local Squid forward proxy on 127.0.0.1:3128 with an FQDN
# allowlist (see /etc/squid/squid.conf and /etc/squid/allowed-domains.acl).
# iptables locks the OUTPUT chain so that the only paths off-box are
# loopback (to Squid) and DNS. Everything else must go through the proxy and
# is filtered by hostname rather than fragile resolved-IP pins — GitHub and
# the other CDN-backed services rotate IPs faster than `getent` can keep up
# with, and GitHub specifically recommends FQDN/SNI filtering.
#
# Runs at container start via `postStartCommand`
# (`sudo /usr/local/bin/devc-init-firewall.sh`) and from post-create.sh during
# first-time setup, before the sudoers lockdown, so the proxy is up before
# pip and other tooling need network egress.
#
# The sudoers entry in /etc/sudoers.d/vscode permits exactly this one command
# without a password; nothing else can be run with sudo.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "devc-init-firewall.sh: must run as root (use sudo)" >&2
  exit 1
fi

SQUID_BIN=/usr/sbin/squid
SQUID_PID=/var/run/squid.pid
PROXY_PORT=3128

# --- Start the Squid forward proxy (idempotent) ---

install -d -o proxy -g proxy /var/log/squid /var/spool/squid /var/run/squid

# This script runs from both postCreateCommand and postStartCommand, so on
# first container creation it fires twice in quick succession. If squid is
# already listening, leave it alone — `squid -k shutdown` honors
# shutdown_lifetime (30s default), which is longer than we can reasonably wait
# here, and there is no config change to pick up between the two calls.
if ss -lnt "sport = :$PROXY_PORT" 2>/dev/null | grep -q LISTEN; then
  echo "devc-init-firewall.sh: squid already listening on 127.0.0.1:$PROXY_PORT, skipping start"
else
  # Clear any stale PID file left behind by a previous container instance.
  rm -f "$SQUID_PID"
  "$SQUID_BIN" -f /etc/squid/squid.conf
fi

# Confirm the proxy is actually listening before we lock iptables down — if
# squid failed to start, locking down would leave the container unreachable
# with no way to recover without a rebuild.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if ss -lnt "sport = :$PROXY_PORT" 2>/dev/null | grep -q LISTEN; then
    break
  fi
  sleep 1
done
if ! ss -lnt "sport = :$PROXY_PORT" 2>/dev/null | grep -q LISTEN; then
  echo "devc-init-firewall.sh: ERROR squid is not listening on 127.0.0.1:$PROXY_PORT" >&2
  echo "                    aborting before iptables lockdown; see /var/log/squid/cache.log" >&2
  exit 1
fi

# --- Lock iptables to loopback + DNS + squid's own egress ---

# Reset to ACCEPT so DNS resolution etc. works while we rebuild the chain.
iptables -P OUTPUT ACCEPT
iptables -F OUTPUT
ip6tables -P OUTPUT ACCEPT 2>/dev/null || true
ip6tables -F OUTPUT 2>/dev/null || true

# Loopback (clients reach Squid here) and return traffic.
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# DNS to whatever resolver(s) Docker injected (often a 0.0.0.0/8 proxy).
while read -r ns; do
  [[ -n "$ns" ]] || continue
  iptables -A OUTPUT -p udp --dport 53 -d "$ns" -j ACCEPT
  iptables -A OUTPUT -p tcp --dport 53 -d "$ns" -j ACCEPT
done < <(awk '/^nameserver/ {print $2}' /etc/resolv.conf)

# Squid itself must reach the world. Match by owning uid so we don't need to
# enumerate destination IPs. Everything else (curl, git, gh, pip, the agent)
# runs as a non-proxy uid and is forced through the proxy on loopback.
proxy_uid=$(id -u proxy)
iptables -A OUTPUT -m owner --uid-owner "$proxy_uid" -p tcp --dport 443 -j ACCEPT
iptables -A OUTPUT -m owner --uid-owner "$proxy_uid" -p tcp --dport 80 -j ACCEPT

# Quiet, rate-limited log line on drop so we can see what got blocked.
iptables -A OUTPUT -m limit --limit 10/min -j LOG --log-prefix "devc-egress-drop: " --log-level 4

iptables -P OUTPUT DROP

# IPv6: no allowlisted v6 endpoints; loopback + established only.
ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
ip6tables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
ip6tables -P OUTPUT DROP 2>/dev/null || true

echo "devc-init-firewall.sh: squid on 127.0.0.1:$PROXY_PORT (FQDN allowlist), iptables egress locked"
