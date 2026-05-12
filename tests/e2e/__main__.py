from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "tests" / "e2e" / "docker-compose.yml"


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, cwd=ROOT, text=True, check=check)


def docker_available() -> bool:
    return bool(shutil.which("docker")) and run_cmd(["docker", "version"], check=False).returncode == 0


def compose_args() -> list[str]:
    if run_cmd(["docker", "compose", "version"], check=False).returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise RuntimeError("Docker Compose is not available")


def run_mac_safe() -> int:
    if not docker_available():
        print("Docker is required for the Mac-safe e2e path.", file=sys.stderr)
        return 1
    cmd = [
        *compose_args(),
        "-f",
        str(COMPOSE),
        "up",
        "--build",
        "--abort-on-container-exit",
        "--exit-code-from",
        "runner",
    ]
    try:
        run_cmd(cmd)
        return 0
    finally:
        run_cmd([*compose_args(), "-f", str(COMPOSE), "down", "-v"], check=False)


def kvm_skip_reason() -> str | None:
    if platform.system() != "Linux":
        return "host is not Linux"
    if not Path("/dev/kvm").exists():
        return "/dev/kvm is missing"
    if not docker_available():
        return "Docker is unavailable"
    probe = run_cmd(
        ["docker", "run", "--rm", "--privileged", "--device", "/dev/kvm", "alpine:3.20", "test", "-r", "/dev/kvm"],
        check=False,
    )
    if probe.returncode != 0:
        return "privileged Docker probe cannot access /dev/kvm"
    return None


def run_real_kvm_if_available(*, require: bool) -> int:
    reason = kvm_skip_reason()
    if reason:
        if require:
            print(f"FAIL --require-real-kvm: {reason}", file=sys.stderr)
            return 1
        print(f"SKIP real KVM e2e: {reason}")
        return 0
    # The real-KVM scenario (boot a real domain under qemu:///system, run
    # virtnbdbackup against it, restore-verify the result) is not implemented
    # in this portable suite — it needs a guest image, libvirt config, and
    # nested-KVM infra that varies per host. ``--require-real-kvm`` makes a
    # production CI gate fail loudly here rather than silently passing on a
    # scaffolded code path; without the flag the default is to print a notice
    # and continue so contributors without KVM don't see false failures.
    message = "Real KVM e2e capability detected, but the real-domain scenario is scaffolded only."
    if require:
        print(f"FAIL --require-real-kvm: {message}", file=sys.stderr)
        return 1
    print(message)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run adaptive libvirt-backup-system end-to-end tests.")
    parser.add_argument("--skip-docker", action="store_true", help="Skip the Docker Compose orchestration test.")
    parser.add_argument("--skip-kvm", action="store_true", help="Skip the real KVM capability probe/path.")
    parser.add_argument(
        "--require-real-kvm",
        action="store_true",
        help="Fail (instead of skipping) if the real-KVM scenario cannot run end-to-end.",
    )
    args = parser.parse_args(argv)

    if not args.skip_docker:
        mac_safe = run_mac_safe()
        if mac_safe != 0:
            return mac_safe
    if args.skip_kvm:
        if args.require_real_kvm:
            print("FAIL --require-real-kvm: --skip-kvm was also passed", file=sys.stderr)
            return 1
        print("SKIP real KVM e2e: disabled by --skip-kvm")
        return 0
    return run_real_kvm_if_available(require=args.require_real_kvm)


if __name__ == "__main__":
    raise SystemExit(main())
