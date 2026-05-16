from __future__ import annotations

UNIT_SERVICE = """[Unit]
Description={description}
Wants=network-online.target
After=network-online.target libvirtd.service
{requires_mounts_for}
[Service]
Type=oneshot
TimeoutStartSec=infinity
EnvironmentFile={environment_file}
ExecStart={bin_path} --config {config_arg} {subcommand}
# Defense-in-depth hardening. The service runs as root because it shells out to
# virsh/virtnbdbackup against qemu:///system; StateDirectory= creates the state
# dir so lock.py's run-lock mkdir succeeds on a fresh install.
StateDirectory=libvirt-backup-system
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
KillMode=mixed
TimeoutStopSec=30min
"""

UNIT_TIMER = """[Unit]
Description=Run libvirt VM backups on schedule

[Timer]
OnCalendar={calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""
