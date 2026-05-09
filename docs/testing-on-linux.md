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

Inside the L1 VM or on a direct Linux/KVM host, install the normal libvirt and
test dependencies:

```bash
sudo apt update
sudo apt install -y \
  docker.io \
  docker-compose-v2 \
  qemu-kvm \
  libvirt-daemon-system \
  libvirt-clients
```

On older distributions, the Compose package may be named
`docker-compose-plugin` or `docker-compose`. The e2e runner accepts either
`docker compose` or `docker-compose`, so verify that one of them is available
before running the suite.

Install `uv` if it is not already present:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

If you want to run Docker without `sudo`, add your user to the Docker group and
start a new login session:

```bash
sudo usermod -aG docker "$USER"
```

## Verify the Host Can Run the Linux E2E Path

From the repository checkout, confirm Docker is available:

```bash
docker version
docker compose version
```

Confirm the host exposes KVM:

```bash
test -r /dev/kvm
```

Confirm a privileged Docker container can see `/dev/kvm`:

```bash
docker run --rm --privileged --device /dev/kvm alpine:3.20 test -r /dev/kvm
```

If that command exits successfully, the adaptive e2e runner will detect KVM
capability. If it fails, the real KVM path is skipped with a clear reason.

## Run the E2E Suite

Install locked development dependencies with `uv`:

```bash
uv sync --locked --extra dev
```

Run the adaptive end-to-end suite:

```bash
uv run --locked --extra dev python -m tests.e2e
```

The runner always executes the Docker Compose orchestration scenario. That path
uses a runner container, a mounted backup volume, and fake libvirt tools to test
install/uninstall behavior, preflight checks, backup orchestration, retention,
structured logging, and failure handling.

On Linux hosts with `/dev/kvm`, the runner also probes whether a privileged
container can access KVM. The current portable suite reports the detected KVM
capability, but the real nested-VM backup/restore scenario is still scaffolded
and not enabled. If KVM capability is missing, that path is skipped with a clear
reason instead of failing the suite.

To run only the KVM capability probe path:

```bash
uv run --locked --extra dev python -m tests.e2e --skip-docker
```

To force the suite to skip the KVM probe:

```bash
uv run --locked --extra dev python -m tests.e2e --skip-kvm
```

## Run the Full Local Gate

The repository pre-push hook is the sole local quality gate (there is no
GitHub Actions CI configured) and runs:

```bash
uv run --locked --extra dev python -m tools.gates
```

That gate checks formatting, linting, strict typing, type completeness, warnings
as errors, unit coverage, the adaptive e2e suite, and the 300-line maximum per
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
