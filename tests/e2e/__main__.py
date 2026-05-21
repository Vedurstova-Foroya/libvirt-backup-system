from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tests.e2e.real_kvm_case import real_kvm_skip_reason

ROOT = Path(__file__).resolve().parents[2]


def run_cmd(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, cwd=ROOT, text=True, check=check)


def run_real_kvm_if_available(*, require: bool) -> int:
    reason = real_kvm_skip_reason()
    if reason:
        if require:
            print(f"FAIL --require-real-kvm: {reason}", file=sys.stderr)
            return 1
        print(f"SKIP real KVM e2e: {reason}")
        return 0
    # Capability probe passed: a failure here is a real backup/verify break.
    return run_cmd([sys.executable, "-m", "tests.e2e.real_kvm_case"], check=False).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run libvirt-backup-system end-to-end tests.")
    parser.add_argument("--skip-kvm", action="store_true", help="Skip the real KVM capability probe/path.")
    parser.add_argument(
        "--require-real-kvm",
        action="store_true",
        help="Fail (instead of skipping) if the real-KVM scenario cannot run end-to-end.",
    )
    args = parser.parse_args(argv)

    if args.skip_kvm:
        if args.require_real_kvm:
            print("FAIL --require-real-kvm: --skip-kvm was also passed", file=sys.stderr)
            return 1
        print("SKIP real KVM e2e: disabled by --skip-kvm")
        return 0
    return run_real_kvm_if_available(require=args.require_real_kvm)


if __name__ == "__main__":
    raise SystemExit(main())
