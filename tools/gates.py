from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_LINES = 300
IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
IGNORED_FILES = {
    "uv.lock",
}


def run(args: list[str], *, env: dict[str, str] | None = None) -> int:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, cwd=ROOT, env=merged, check=False).returncode


def text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.name in IGNORED_FILES:
            continue
        if not path.is_file():
            continue
        try:
            path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files.append(path)
    return sorted(files)


def check_max_loc() -> int:
    failures: list[str] = []
    for path in text_files():
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > MAX_FILE_LINES:
            failures.append(f"{path.relative_to(ROOT)} has {line_count} lines; max is {MAX_FILE_LINES}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"LOC gate passed: all text files are <= {MAX_FILE_LINES} lines")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run repository quality gates.")
    parser.add_argument("--fix", action="store_true", help="Apply formatter/linter fixes before checking.")
    args = parser.parse_args(argv)

    commands: list[list[str]] = []
    if args.fix:
        commands.extend(
            [
                [sys.executable, "-m", "ruff", "format", "."],
                [sys.executable, "-m", "ruff", "check", ".", "--fix"],
            ]
        )
    commands.extend(
        [
            [sys.executable, "-m", "ruff", "format", "--check", "."],
            [sys.executable, "-m", "ruff", "check", "."],
            [sys.executable, "-m", "mypy", "libvirt_backup_system", "tools"],
            [sys.executable, "-m", "pyright", "libvirt_backup_system", "tools"],
            [sys.executable, "-m", "pyright", "--verifytypes", "libvirt_backup_system"],
            [sys.executable, "-m", "coverage", "run", "-m", "pytest"],
            [sys.executable, "-m", "coverage", "report"],
            [sys.executable, "-m", "tests.e2e"],
        ]
    )

    env = {
        "PYRIGHT_PYTHON_FORCE_VERSION": "1.1.390",
        "PYRIGHT_PYTHON_IGNORE_WARNINGS": "1",
        "PYTHONPATH": str(ROOT),
        "PYTHONWARNINGS": "error",
    }
    for command in commands:
        code = run(command, env=env)
        if code != 0:
            return code
    return check_max_loc()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
