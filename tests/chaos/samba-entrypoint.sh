#!/bin/sh
set -eu

useradd --uid 10001 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin seaweed 2>/dev/null || true
mkdir -p /srv/storage /run/samba /var/lib/samba/private
chown -R 10001:10001 /srv/storage
printf '%s\n%s\n' "${SAMBA_PASSWORD:-chaos-password}" "${SAMBA_PASSWORD:-chaos-password}" | smbpasswd -s -a seaweed >/dev/null

cat >/etc/samba/smb.conf <<'CONF'
[global]
   server role = standalone server
   security = user
   map to guest = never
   server min protocol = SMB2
   server max protocol = SMB3_11
   smb ports = 445
   load printers = no
   printing = bsd
   disable spoolss = yes
   log file = /dev/stdout
   max log size = 0

[share]
   path = /srv/storage
   browseable = yes
   read only = no
   guest ok = no
   valid users = seaweed
   force user = seaweed
   force group = seaweed
   create mask = 0660
   directory mask = 0770
CONF

exec smbd --foreground --no-process-group --debug-stdout --configfile=/etc/samba/smb.conf
