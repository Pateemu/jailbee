#!/usr/bin/env bash
# Provision the jailbee-display container: weston, a self-signed TLS pair and
# the systemd unit. Idempotent. Expects JAILBEE_UID, JAILBEE_GID, JAILBEE_USER,
# JAILBEE_RDP_USER and JAILBEE_RDP_PASSWORD (the fixed RDP/NLA login).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
: "${JAILBEE_UID:?}" "${JAILBEE_GID:?}" "${JAILBEE_USER:?}"
: "${JAILBEE_RDP_USER:?}" "${JAILBEE_RDP_PASSWORD:?}"

# The container was started moments ago: DHCP and the bridge's dnsmasq may not
# have answered yet. Wait for name resolution instead of failing the first
# apt-get on a boot race.
network_up=""
for _ in $(seq 1 60); do
  if getent hosts archive.ubuntu.com >/dev/null 2>&1; then
    network_up=1
    break
  fi
  sleep 1
done
if [ -z "$network_up" ]; then
  echo "jailbee-display has no working DNS after 60s on the jailbee-loose bridge." >&2
  echo "If DHCP/DNS from the bridge is dropped by a host firewall, run 'jailbee doctor'." >&2
  exit 1
fi

apt-get update -qq
apt-get install -y -qq weston openssl winpr-utils

if ! getent passwd "$JAILBEE_UID" >/dev/null; then
  getent group "$JAILBEE_GID" >/dev/null || groupadd -g "$JAILBEE_GID" "$JAILBEE_USER"
  useradd -m -u "$JAILBEE_UID" -g "$JAILBEE_GID" -s /bin/bash "$JAILBEE_USER"
fi
RUN_USER="$(getent passwd "$JAILBEE_UID" | cut -d: -f1)"

install -d -m 0700 -o "$RUN_USER" /etc/jailbee-display
if [ ! -f /etc/jailbee-display/tls.key ] || [ ! -f /etc/jailbee-display/tls.crt ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj /CN=jailbee-display \
    -keyout /etc/jailbee-display/tls.key -out /etc/jailbee-display/tls.crt 2>/dev/null
  chown "$RUN_USER" /etc/jailbee-display/tls.key /etc/jailbee-display/tls.crt
  chmod 0600 /etc/jailbee-display/tls.key
fi

# weston 14 with FreeRDP 3 does Network Level Authentication and checks the
# login against a WinPR SAM file; with no entry every client is refused with
# "Could not find user in SAM database". Write the one fixed login. The binary
# is winpr-hash or winpr-hash<major> depending on the package.
WINPR_HASH="$(dpkg -L winpr-utils | grep -E '/winpr-hash[0-9]*$' | head -n 1)"
[ -n "$WINPR_HASH" ] || { echo "winpr-hash not found in winpr-utils" >&2; exit 1; }
# winpr-hash prints the bare NT hash in this version, not a SAM line. A SAM line
# is user:domain:LM:NT:::, so build it; keep a ready-made line if some version
# prints one.
NT_HASH="$("$WINPR_HASH" -u "$JAILBEE_RDP_USER" -p "$JAILBEE_RDP_PASSWORD")"
case "$NT_HASH" in
  *:*) SAM_LINE="$NT_HASH" ;;
  *) SAM_LINE="$JAILBEE_RDP_USER:::$NT_HASH:::" ;;
esac
printf '%s\n' "$SAM_LINE" > /etc/jailbee-display/SAM
chown "$RUN_USER" /etc/jailbee-display/SAM
chmod 0600 /etc/jailbee-display/SAM

sed "s/__USER__/$RUN_USER/" /root/jailbee-display.service \
  > /etc/systemd/system/jailbee-display.service
systemctl daemon-reload
systemctl enable --now jailbee-display.service
