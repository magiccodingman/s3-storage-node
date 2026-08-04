#!/bin/sh
set -eu

mkdir -p /run/sshd /root/.ssh /srv/storage
chmod 0700 /root/.ssh
chown -R 10001:10001 /srv/storage 2>/dev/null || true

cat >/etc/ssh/sshd_config <<'CONF'
Port 22
ListenAddress 0.0.0.0
HostKey /run/chaos/ssh_host_ed25519_key
PidFile /run/sshd.pid
AuthorizedKeysFile /run/chaos/authorized_keys
PermitRootLogin yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
StrictModes no
UsePAM no
PermitTunnel no
AllowTcpForwarding no
X11Forwarding no
Subsystem sftp internal-sftp
LogLevel VERBOSE
CONF

exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
