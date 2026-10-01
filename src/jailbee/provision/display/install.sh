#!/usr/bin/env bash
# Provision the jailbee-display container: weston, a self-signed TLS pair and
# the systemd unit. Idempotent. Expects JAILBEE_UID, JAILBEE_GID, JAILBEE_USER.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
: "${JAILBEE_UID:?}" "${JAILBEE_GID:?}" "${JAILBEE_USER:?}"

apt-get update -qq
apt-get install -y -qq weston openssl

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

sed "s/__USER__/$RUN_USER/" /root/jailbee-display.service \
  > /etc/systemd/system/jailbee-display.service
systemctl daemon-reload
systemctl enable --now jailbee-display.service
