#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Install the freshpi server, CLI and built payload on the server host over SSH.
#
#   ./deploy.sh <ssh-host> <authorized_keys>
#
#   ssh-host         e.g. admin@192.0.2.10 (needs sudo there)
#   authorized_keys  public keys for the user created on provisioned Pis
#
# First deploy creates /etc/freshpi/freshpi.conf from the example (defaults work as-is).
set -eu
[ $# -eq 2 ] || { echo "usage: $0 <ssh-host> <authorized_keys>" >&2; exit 2; }
HOST=$1
KEYS=$2
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/build/out/payload/config.txt" ] || { echo "run build/build.sh first" >&2; exit 1; }
[ -s "$KEYS" ] || { echo "$KEYS is empty or missing" >&2; exit 1; }

tar -C "$HERE" -czf - server/freshpi.py server/freshpi.service server/freshpi.conf.example build/out/payload \
    | ssh "$HOST" "rm -rf /tmp/freshpi-deploy && mkdir -p /tmp/freshpi-deploy && tar -C /tmp/freshpi-deploy -xzf -"
scp -q "$KEYS" "$HOST:/tmp/freshpi-deploy/authorized_keys"

ssh "$HOST" 'sudo sh -eu' <<'EOF'
D=/tmp/freshpi-deploy
install -D -m 0755 $D/server/freshpi.py /opt/freshpi/freshpi.py
ln -sf /opt/freshpi/freshpi.py /usr/local/sbin/freshpi
install -D -m 0644 $D/server/freshpi.service /etc/systemd/system/freshpi.service
install -D -m 0644 $D/authorized_keys /etc/freshpi/authorized_keys
[ -f /etc/freshpi/freshpi.conf ] || install -m 0644 $D/server/freshpi.conf.example /etc/freshpi/freshpi.conf
mkdir -p /var/lib/freshpi
rm -rf /var/lib/freshpi/payload.new
cp -r $D/build/out/payload /var/lib/freshpi/payload.new
chown -R root:root /var/lib/freshpi/payload.new
rm -rf /var/lib/freshpi/payload.old
[ -d /var/lib/freshpi/payload ] && mv /var/lib/freshpi/payload /var/lib/freshpi/payload.old
mv /var/lib/freshpi/payload.new /var/lib/freshpi/payload
rm -rf /var/lib/freshpi/payload.old $D
systemctl daemon-reload
systemctl enable --now freshpi.service
systemctl restart freshpi.service
freshpi refresh
EOF
