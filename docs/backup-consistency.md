# Backup consistency

Each backup run records the consistency level it achieved for that VM. The
level is per restore point, not a global host setting, because QEMU guest agent
health can differ from VM to VM and from run to run.

## Levels

- `crash`: guest filesystem quiesce was unavailable or failed, so the backup
  retried without `--quiesce`. This is equivalent to taking a disk snapshot
  after a power loss: filesystems should replay their journals, but open
  application state may need its own recovery.
- `filesystem`: libvirt snapshot creation with `--quiesce` succeeded through
  QEMU guest agent. The guest filesystems were frozen for the snapshot, so this
  is stronger than crash consistency.
- `unknown`: legacy restore points whose metadata predates the consistency
  field.

The backup command always attempts filesystem quiesce first. If QEMU guest
agent is missing, stopped, missing from the VM XML, or unable to freeze the
guest filesystems, the run falls back to `crash` and the backup still
continues.

Application or database hooks can improve the real state captured by a
filesystem-consistent backup, but this system does not auto-label backups as
application-consistent. The host only records whether QEMU guest agent quiesce
succeeded.

## Where It Appears

`list-restore-points` shows the recorded value in the `consistency` column and
`list-restore-points --json` includes the same value per row. VM-level `du`
output reports the latest restore point as `latest-consistency` in the table
and `latest_consistency` in JSON.

Kopia metadata also carries the value on the meta snapshot and in
`manifest.json`, so manual inspection can confirm what the wrapper displays.

## Enable Filesystem Consistency

Run these commands inside each Debian or Ubuntu guest:

```sh
sudo apt update
sudo apt install -y qemu-guest-agent
sudo systemctl enable --now qemu-guest-agent
sudo systemctl status qemu-guest-agent --no-pager
```

On the libvirt host, confirm the VM has a QEMU guest-agent channel:

```sh
sudo virsh dumpxml "$VM" | grep -A5 org.qemu.guest_agent.0
```

If it is absent, add a virtio channel to the domain XML:

```xml
<channel type='unix'>
  <target type='virtio' name='org.qemu.guest_agent.0'/>
</channel>
```

Edit persistent XML with `sudo virsh edit "$VM"`, then fully stop and start
the VM so QEMU creates the device. After the guest is running again, verify
the host can talk to the agent:

```sh
sudo virsh qemu-agent-command "$VM" '{"execute":"guest-ping"}'
```

The next backup run should record `filesystem` for that VM if the agent can
freeze all guest filesystems. If it still records `crash`, check the backup
log and the guest journal for QEMU guest agent or filesystem freeze errors.

## Application Freeze Hooks

QEMU guest agent can run a guest-side fsfreeze hook before filesystems freeze
and after they thaw. On Debian and Ubuntu the default hook path is:

```text
/etc/qemu/fsfreeze-hook
```

That hook runs executable scripts from:

```text
/etc/qemu/fsfreeze-hook.d/
```

Create that directory if the package did not:

```sh
sudo mkdir -p /etc/qemu/fsfreeze-hook.d
```

Add one executable script per application. Each script receives `freeze`
before the filesystem freeze and `thaw` after the filesystem thaws. For
example, a PostgreSQL script can request a fast checkpoint before freeze:

```sh
sudo tee /etc/qemu/fsfreeze-hook.d/20-postgresql >/dev/null <<'EOF'
#!/bin/sh
set -eu

case "$1" in
  freeze)
    runuser -u postgres -- psql -qAt -c "CHECKPOINT;"
    ;;
  thaw)
    :
    ;;
esac
EOF
sudo chmod 0755 /etc/qemu/fsfreeze-hook.d/20-postgresql
```

Keep hooks short and deterministic. A slow or failing hook can make QEMU guest
agent fsfreeze fail, which causes the backup to fall back to `crash` for that
run. Test hooks in a maintenance window before relying on them for production
data services.
