from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from tests.e2e.real_kvm_case import real_kvm_skip_reason

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


def run_real_kvm_if_available(*, require: bool) -> int:
    reason = real_kvm_skip_reason()
    if reason:
        if require:
            print(f"FAIL --require-real-kvm: {reason}", file=sys.stderr)
            return 1
        print(f"SKIP real KVM e2e: {reason}")
        return 0
    # The capability probe passed, so a failure below is a real failure: the
    # host has KVM + libvirt + virtnbdbackup but the backup/verify path is
    # broken. ``--require-real-kvm`` only changes the SKIP-on-missing-capability
    # path; once the probe succeeds the result is binding either way.
    return run_cmd([sys.executable, "-m", "tests.e2e.real_kvm_case"], check=False).returncode


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
