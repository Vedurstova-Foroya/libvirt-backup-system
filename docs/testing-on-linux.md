# Testing on Linux with Nested KVM

This guide explains how to prepare a Linux/KVM/libvirt machine for nested
virtualization and how to run the libvirt-backup-system end-to-end test suite on
Linux.

Nested virtualization is useful when you want a disposable Linux VM to behave
like a small hypervisor lab:

```text
L0: bare-metal Linux host running QEMU/KVM
└── L1: Linux VM with libvirt and QEMU/KVM
    └── L2: nested VM created inside the L1 VM
```

The Linux kernel documentation describes these layers as **L0**, **L1**, and
**L2**: the bare-metal KVM host, the guest hypervisor, and the nested guest.
Libvirt manages the VMs, but QEMU/KVM does the virtualization work. For the L1
guest to run accelerated L2 VMs, it must see CPU virtualization extensions and
have access to `/dev/kvm`.

## When to Use This

Use nested KVM for labs, CI hosts, hypervisor testing, backup-system testing,
OpenStack or Proxmox experiments, and teaching environments.

For production VM hosting, run the real VMs directly on the bare-metal L0 host
unless you have a strong reason to stack hypervisors. Nested virtualization works
well for testing, but it adds overhead around storage, networking, interrupts,
timers, and VM exits.

## Enable Nested KVM on the L0 Host

First check whether nested KVM is already enabled on the bare-metal host.

For Intel CPUs:

```bash
cat /sys/module/kvm_intel/parameters/nested
```

For AMD CPUs:

```bash
cat /sys/module/kvm_amd/parameters/nested
```

The value should be `Y` or `1`.

To enable nested KVM persistently on Intel:

```bash
echo "options kvm_intel nested=1" | sudo tee /etc/modprobe.d/kvm-intel.conf
```

To enable nested KVM persistently on AMD:

```bash
echo "options kvm_amd nested=1" | sudo tee /etc/modprobe.d/kvm-amd.conf
```

Then reboot the host. You can also unload and reload the KVM modules, but only
when no VMs are running.

## Create an L1 VM That Can Run KVM

The L1 VM must receive the host CPU virtualization features. With libvirt, use
host CPU passthrough:

```xml
<cpu mode='host-passthrough'/>
```

With `virt-install`, the important option is `--cpu host-passthrough`:

```bash
virt-install \
  --name nested-host \
  --memory 8192 \
  --vcpus 4 \
  --cpu host-passthrough \
  ...
```

After the L1 VM boots, verify that the virtualization CPU flags and KVM device
are visible inside the guest:

```bash
egrep -wo 'vmx|svm' /proc/cpuinfo | sort -u
ls -l /dev/kvm
```

Intel hosts should show `vmx`; AMD hosts should show `svm`. The `/dev/kvm`
device must exist and be readable by the user or service account that will run
the tests.

## Install Linux Test Dependencies

Inside the L1 VM or on a direct Linux/KVM host, install the libvirt and QEMU
tooling:

```bash
sudo apt update
sudo apt install -y \
  qemu-kvm \
  qemu-utils \
  libvirt-daemon-system \
  libvirt-clients
```

The production installer can fetch pinned `kopia` and `nbdcopy` builds; see
[Bundled binary install](install.md#bundled-binary-install). The real-KVM e2e
case probes before it runs its sandboxed install, so both binaries must already
be runnable on `PATH`. On Debian/Ubuntu, `nbdcopy` is provided by `libnbd-bin`:

```bash
sudo apt install -y libnbd-bin
```

Install `kopia` from the pinned release documented in
[install.md](install.md#bundled-binary-install), or pre-place a compatible
`/usr/local/bin/kopia` as shown in the offline install notes.

Install `uv` if it is not already present:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Verify the Host Can Run the Real-KVM E2E Path

Confirm the host exposes KVM:

```bash
test -r /dev/kvm
```

Confirm libvirt's session URI is reachable for your user (the e2e case uses
`qemu:///session` so it can run without root):

```bash
virsh -c qemu:///session uri
```

Confirm the binaries the engine shells out to are on `PATH`:

```bash
command -v virsh qemu-img qemu-nbd nbdcopy kopia
```

If any of those is missing, the real-KVM case is skipped with a clear reason
instead of failing the suite.

## Run the E2E Suite

Install locked development dependencies with `uv`:

```bash
uv sync --locked --extra dev
```

Run the end-to-end suite:

```bash
uv run --locked --extra dev python -m tests.e2e
```

The runner executes a single scenario: `tests/e2e/real_kvm_case.py`. It probes
the host for `/dev/kvm`, libvirt, `kopia`, and `nbdcopy`; if any probe fails the
case is skipped with a notice. When the probes pass it defines two ephemeral
domains under `qemu:///session` (one running, one shut off) backed by tiny
qcow2 disks under a temporary workdir, installs `libvirt-backup-system` into a
`--prefix` sandbox with a generated kopia password, then drives `check`,
`list-vms --json`, `run`, `verify`, and `restore` against the sandbox. The
scenario reuses the host `nbdcopy` from `PATH` during the sandboxed install, so
the e2e path does not run `dpkg` against the host package database just to
bootstrap the prefixed tree, and asserts that:

- the kopia repo materializes under `BACKUP_PATH/<host-id>/kopia-repo/`,
- the running VM gets new kopia snapshots on every `run` and the offline VM is
  logged as `skipping vm because it is offline` on every run,
- a delete + restore + re-backup cycle adds only a small bounded number of
  bytes to the repo (the post-restore dedup invariant — proves the
  post-restore-bloat problem that the old chain engine had is fixed),
- a `kopia policy set --global --keep-latest=1` followed by `kopia maintenance
  run --full` prunes older snapshots.

Domains and the temp workdir are torn down in a `finally` block.

### Skipping the case

To skip the real-KVM case entirely (for example, on a workstation without
`/dev/kvm` access):

```bash
uv run --locked --extra dev python -m tests.e2e --skip-kvm
```

> **Coverage caveat.** Even when the real-KVM case runs end-to-end, it does
> not exercise everything you depend on in production. The following must be
> validated by hand before any production deploy:
>
> - The systemd unit environment (kernel-tunable/module restrictions,
>   `StateDirectory=`, `RequiresMountsFor=BACKUP_PATH`, scratch dir under
>   `/var/tmp`) — only the scheduled run path exercises these. Manual
>   invocations on the interactive shell can mask a unit that would fail in
>   service mode.
> - Behavior under real production disk layouts (LVM, RBD, iSCSI block-backed
>   disks). The real-KVM case uses tiny qcow2 files.
> - `qemu:///system` against unprivileged backup paths. The real-KVM case uses
>   `qemu:///session` so it can run without root; production runs as root
>   against `qemu:///system` (see [Install](install.md)).

## Production reliance: real-KVM gate

The default e2e is permissive about missing KVM capability — it skips with a
notice when `/dev/kvm`, libvirt, `kopia`, or `nbdcopy` are unavailable.
**Before any production reliance on libvirt-backup-system**, wire up a self-
hosted or nightly CI gate that runs the suite with `--require-real-kvm`:

```bash
uv run --locked --extra dev python -m tests.e2e --require-real-kvm
```

The repository gate has the same hard-fail mode for the real-KVM lane:

```bash
uv run --locked --extra dev python -m tools.gates --require-real-kvm
```

That flag turns SKIP-on-missing-capability into a hard failure, so a CI host
that lost `/dev/kvm` or its libvirt/kopia tooling cannot silently pass a build
that depends on real backup-engine behavior. Once the capability probe succeeds
the real-KVM case runs unconditionally; a backup or verify failure is fatal
whether or not `--require-real-kvm` was passed.

## Run the Full Local Gate

The repository ships with a local pre-push hook that runs `python -m tools.gates`
against the working tree before a push leaves the developer's machine.

Install the pre-push hook into a fresh clone with:

```bash
uv run --locked --extra dev python -m tools.install_hooks
```

The hook simply runs the gate:

```bash
uv run --locked --extra dev python -m tools.gates
```

That gate checks formatting, linting, strict typing, type completeness, warnings
as errors, unit coverage, the e2e suite, and the 300-line maximum per
authored text file.

## Networking Notes

The simplest nested lab network is NAT inside the L1 VM:

```text
L0 physical LAN
└── L1 VM has normal network access
    └── L2 VMs use a NAT network inside L1
```

Bridging L2 VMs all the way onto the physical LAN can require extra host or
switch configuration because the L0 network may see multiple MAC addresses
behind one L1 VM. Depending on the bridge, vSwitch, Wi-Fi driver, port-security
policy, or hypervisor network mode, you may need promiscuous mode, MAC spoofing,
or bridge forwarding enabled.

## Without Nested KVM

QEMU can run inside the L1 guest without `/dev/kvm`, but it falls back to
software emulation through TCG. That is much slower and is not suitable for
realistic VM backup testing.

## References

- [Linux Kernel Documentation: Nested VMX][1]
- [Red Hat Enterprise Linux 7: Nested Virtualization][2]
- [linux-kvm.org: Nested Guests][3]
- [Red Hat Enterprise Linux 10: Creating nested virtual machines][4]

[1]: https://docs.kernel.org/virt/kvm/x86/nested-vmx.html
[2]: https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/7/html/virtualization_deployment_and_administration_guide/nested_virt
[3]: https://www.linux-kvm.org/page/Nested_Guests
[4]: https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/configuring_and_managing_linux_virtual_machines/creating-nested-virtual-machines
