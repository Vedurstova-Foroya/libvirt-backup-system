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
# virsh / qemu-nbd / kopia against qemu:///system; StateDirectory= creates the
# state dir so lock.py's run-lock mkdir succeeds on a fresh install.
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

[Install]
WantedBy=timers.target
"""


# Maintenance uses ``kopia-passthrough`` for direct repo maintenance. Verify
# goes through the first-class ``verify`` command so cli.py applies the same
# run-lock safety as operator-triggered verification.
UNIT_KOPIA_SERVICE = """[Unit]
Description={description}
After=network-online.target
{requires_mounts_for}

[Service]
Type=oneshot
TimeoutStartSec=infinity
EnvironmentFile={environment_file}
ExecStart={bin_path} --config {config_arg} {kopia_args}
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


UNIT_INTERVAL_TIMER = """[Unit]
Description={description}

[Timer]
OnBootSec=15min
OnUnitActiveSec={interval}

[Install]
WantedBy=timers.target
"""


# Kept exported for legacy imports; not used by the maintenance/verify pair.
UNIT_GENERIC_SERVICE = """[Unit]
Description={description}
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile={environment_file}
ExecStart={bin_path} --config {config_arg} {subcommand}
StateDirectory=libvirt-backup-system
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
"""
