#!/bin/bash
# Provisions the jailbee-litellm container. Idempotent; re-run by
# `jailbee litellm up --reinstall`. Runs with open egress (the NIC ACL is
# attached only after this succeeds).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv ca-certificates
python3 -m venv /opt/litellm
/opt/litellm/bin/pip install --upgrade pip
if [ "${JAILBEE_LITELLM_UNLOCKED_VERSION:-}" != "" ]; then
    /opt/litellm/bin/pip install "litellm[proxy]==${JAILBEE_LITELLM_UNLOCKED_VERSION}"
else
    /opt/litellm/bin/pip install --require-hashes --no-deps -r /root/litellm-requirements.lock
fi
install -m 0644 /root/jailbee-litellm@.service /etc/systemd/system/jailbee-litellm@.service
systemctl daemon-reload
