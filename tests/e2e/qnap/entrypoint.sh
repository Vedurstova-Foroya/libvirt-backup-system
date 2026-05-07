#!/bin/sh
set -eu

/usr/sbin/sshd

(
  while true; do
    if [ -f /sshkeys/id_ed25519.pub ]; then
      cp /sshkeys/id_ed25519.pub /home/backup/.ssh/authorized_keys
      chown backup:backup /home/backup/.ssh/authorized_keys
      chmod 600 /home/backup/.ssh/authorized_keys
    fi
    sleep 1
  done
) &

tail -f /dev/null
