from __future__ import annotations

import math
import os
import re
import shutil
from pathlib import Path

from .config import CONFIG_KEYS, Config, float_value, int_value, prefixed, split_words
from .logging_json import event
from .nbd_probe import probe_qemu_socket_bind_with_lock
from .paths import backup_root
from .preflight_estimate import df_available_kb as _df_available_kb
from .preflight_estimate import estimate_required_kb as _estimate_required_kb
from .shell import run
from .storage import subpath_is_safe
from .vms import is_safe_vm_name, list_vms

REQUIRED_BINARIES = ["virsh", "virtnbdbackup", "virtnbdrestore", "qemu-img", "df"]
BOOLEAN_KEYS = frozenset(
    ("BACKUP_COMPRESS", "BACKUP_REQUIRE_NFS_MOUNT", "INACTIVE_COPY_EVERY_RUN", "REQUIRE_ROOT", "BACKUP_CLEANUP_ON_RUN"),
)
INTEGER_KEYS = frozenset(("COMMAND_TIMEOUT_SECONDS", "SPACE_MARGIN_PERCENT", "BACKUP_RETENTION_MONTHS"))
FLOAT_KEYS = frozenset(("BACKUP_ESTIMATE_GB_PER_VM", "BACKUP_INCREMENTAL_MULTIPLIER"))
SUPPORTED_VIRTNBDBACKUP_MAJORS = frozenset({1, 2})
# fmt: off
ALLOWED_LIBVIRT_URI_PREFIXES = tuple("qemu:/// qemu+ssh:// qemu+tcp:// qemu+tls:// qemu+unix:// test:// test:///".split())
# fmt: on
WRITE_PROBE_NAME = ".libvirt-backup-system-write-test"
SCRATCH_DIR = Path("/var/tmp")  # noqa: S108 - virtnbdbackup's default scratch dir.
HOST_ID_STATE_FILE = "host-id"


def validate_libvirt_uri(uri: str) -> bool:
    return uri.startswith(ALLOWED_LIBVIRT_URI_PREFIXES)


def _write_probe(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    created = False
    try:
        fd = os.open(path, flags, 0o600)
        created = True
        if os.write(fd, b"ok\n") != 3:
            raise OSError("write probe was incomplete")
    finally:
        if fd is not None:
            os.close(fd)
        if created:
            path.unlink(missing_ok=True)


def _validate_required_present(config: Config) -> list[str]:
    failures = [f"{k} must not be empty" for k in sorted(CONFIG_KEYS - {"VM_BLACKLIST"}) if not config.get(k).strip()]
    host_id = config.get("HOST_ID")
    if host_id.strip():
        if host_id in {".", ".."} or "/" in host_id or "\\" in host_id:
            failures.append("HOST_ID must not contain path separators or be '.'/'..'")
        elif any(ord(c) < 32 or ord(c) == 127 for c in host_id):
            failures.append("HOST_ID must not contain control characters or NUL")
        elif host_id != host_id.strip():
            failures.append("HOST_ID must not have leading or trailing whitespace")
    return failures


def _validate_vm_blacklist(config: Config) -> list[str]:
    bad = [name for name in split_words(config.get("VM_BLACKLIST")) if not is_safe_vm_name(name)]
    return [f"VM_BLACKLIST contains unsafe VM name: {name!r}" for name in bad]


def _validate_booleans(config: Config) -> list[str]:
    valid = {"1", "true", "yes", "on", "0", "false", "no", "off"}
    bad = [key for key in sorted(BOOLEAN_KEYS) if config.get(key).strip().lower() not in valid]
    return [f"{key} must be a boolean value" for key in bad]


def _validate_integers(config: Config) -> list[str]:
    failures: list[str] = []
    for key in sorted(INTEGER_KEYS):
        try:
            value = int_value(config.values, key)
        except ValueError:
            failures.append(f"{key} must be an integer")
            continue
        if key == "COMMAND_TIMEOUT_SECONDS" and value <= 0:
            failures.append("COMMAND_TIMEOUT_SECONDS must be greater than 0")
        elif key != "COMMAND_TIMEOUT_SECONDS" and value < 0:
            failures.append(f"{key} must be greater than or equal to 0")
    return failures


def _validate_floats(config: Config) -> list[str]:
    failures: list[str] = []
    for key in sorted(FLOAT_KEYS):
        try:
            value = float_value(config.values, key)
        except ValueError:
            failures.append(f"{key} must be a number")
            continue
        if not math.isfinite(value):
            failures.append(f"{key} must be a finite number")
            continue
        if key == "BACKUP_INCREMENTAL_MULTIPLIER" and value <= 0:
            failures.append("BACKUP_INCREMENTAL_MULTIPLIER must be greater than 0")  # zero collapses estimate
        elif key != "BACKUP_INCREMENTAL_MULTIPLIER" and value < 0:
            failures.append(f"{key} must be greater than or equal to 0")
    return failures


def _parse_major_version(text: str) -> int | None:
    match = re.search(r"(\d+)(?:\.(\d+))?", text)
    return int(match.group(1)) if match else None


def _virtnbdbackup_version_failures() -> list[str]:
    if not shutil.which("virtnbdbackup"):
        return []  # already reported by the missing-binary check
    try:
        result = run(["virtnbdbackup", "--version"], check=False, timeout=10)
    except OSError as exc:
        return [f"virtnbdbackup version probe failed: {exc}"]
    if result.returncode != 0:
        return [f"virtnbdbackup --version failed: rc={result.returncode}"]
    text = (result.stdout or result.stderr).strip()
    major = _parse_major_version(text)
    if major is None:
        return [f"virtnbdbackup --version unparseable: {text!r}"]
    if major not in SUPPORTED_VIRTNBDBACKUP_MAJORS:
        supported = ", ".join(f"{m}.x" for m in sorted(SUPPORTED_VIRTNBDBACKUP_MAJORS))
        return [f"virtnbdbackup major version {major} is unsupported (need {supported}); reported: {text!r}"]
    return []


def _validate_scratch_dir() -> list[str]:
    try:
        _write_probe(SCRATCH_DIR / WRITE_PROBE_NAME)
    except (FileNotFoundError, NotADirectoryError):
        return [f"{SCRATCH_DIR} must exist as a directory for virtnbdbackup scratch state"]
    except OSError as exc:
        return [f"{SCRATCH_DIR} must be writable for virtnbdbackup scratch state: {exc}"]
    return []


def _backup_path_is_mount(backup_path: Path) -> tuple[bool, str | None]:
    try:
        return backup_path.is_mount(), None
    except OSError as exc:
        return False, str(exc)


def _host_id_state_path(config: Config) -> Path:
    return prefixed("/var/lib/libvirt-backup-system", config.prefix) / HOST_ID_STATE_FILE


def stamp_host_id_on_first_run(config: Config) -> list[str]:
    path = _host_id_state_path(config)
    host_id = config.get("HOST_ID")
    try:
        if path.exists():
            stamped = path.read_text(encoding="utf-8").strip()
            if stamped and stamped != host_id:
                return [f"HOST_ID drift detected: state has {stamped!r}, config has {host_id!r}"]
            if not stamped:
                path.write_text(host_id + "\n", encoding="utf-8")
            return []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(host_id + "\n", encoding="utf-8")
        event("info", "stamped HOST_ID state", path=str(path), host_id=host_id)
    except OSError as exc:
        return [f"HOST_ID state check failed: {exc}"]
    return []


def host_id_drift_failures(config: Config) -> list[str]:
    path = _host_id_state_path(config)
    try:
        if not path.exists():
            return []
        stamped = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return [f"HOST_ID state check failed: {exc}"]
    if stamped and stamped != config.get("HOST_ID"):
        return [f"HOST_ID drift detected: state has {stamped!r}, config has {config.get('HOST_ID')!r}"]
    return []


def _validate_backup_path_readonly(config: Config) -> list[str]:
    if not config.get("BACKUP_PATH").strip():
        return []
    backup_path = config.path_value("BACKUP_PATH")
    if not backup_path.is_absolute():
        return ["BACKUP_PATH must be an absolute path"]
    if not backup_path.exists():
        return ["BACKUP_PATH must exist"]
    if not backup_path.is_dir():
        return ["BACKUP_PATH must be a directory"]
    if config.enabled("BACKUP_REQUIRE_NFS_MOUNT"):
        mounted, error = _backup_path_is_mount(backup_path)
        if error is not None:
            return [f"BACKUP_PATH mount probe failed: {error}"]
        if not mounted:
            return ["BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"]
    if not subpath_is_safe(backup_path, backup_root(config)):
        return ["BACKUP_PATH / HOST_ID must stay within BACKUP_PATH"]
    return []


def _validate_backup_path_writable(config: Config) -> list[str]:
    failures = _validate_backup_path_readonly(config)
    if failures or not config.get("BACKUP_PATH").strip():
        return failures
    backup_path = config.path_value("BACKUP_PATH")
    mount_required = config.enabled("BACKUP_REQUIRE_NFS_MOUNT")
    mount_msg = "BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"
    try:
        host_root = backup_root(config)
        if mount_required:
            mounted, error = _backup_path_is_mount(backup_path)
            if error is not None:
                return [f"BACKUP_PATH mount probe failed: {error}"]
            if not mounted:
                return [mount_msg]
        host_root.mkdir(parents=True, exist_ok=True)
        if not subpath_is_safe(backup_path, host_root):
            return ["BACKUP_PATH / HOST_ID must stay within BACKUP_PATH"]
        if mount_required:
            mounted, error = _backup_path_is_mount(backup_path)
            if error is not None:
                return [f"BACKUP_PATH mount probe failed: {error}"]
            if not mounted:
                return [mount_msg]
        _write_probe(host_root / WRITE_PROBE_NAME)
    except OSError as exc:
        return [f"BACKUP_PATH must be writable: {exc}"]
    return []


def _validate_env_values(config: Config, *, require_writable: bool) -> list[str]:
    failures: list[str] = list(_validate_required_present(config))
    failures.extend(_validate_vm_blacklist(config))
    bool_failures = _validate_booleans(config)
    failures.extend(bool_failures)
    failures.extend(_validate_integers(config))
    failures.extend(_validate_floats(config))
    libvirt_uri = config.get("LIBVIRT_URI").strip()
    if libvirt_uri and not validate_libvirt_uri(libvirt_uri):
        failures.append("LIBVIRT_URI must use one of these schemes: " + ", ".join(ALLOWED_LIBVIRT_URI_PREFIXES))
    check = _validate_backup_path_writable if require_writable and not bool_failures else _validate_backup_path_readonly
    failures.extend(check(config))
    return failures


def validate_config(config: Config) -> int:
    failures = _validate_env_values(config, require_writable=False)
    for failure in failures:
        event("error", "config validation failed", reason=failure)
    return 1 if failures else 0


def collect_check_failures(config: Config, *, lock_held: bool = False) -> tuple[list[str], int, int]:
    failures = _validate_env_values(config, require_writable=True)
    if lock_held and not failures:
        failures.extend(stamp_host_id_on_first_run(config))
    for binary in REQUIRED_BINARIES:
        if not shutil.which(binary):
            failures.append(f"missing binary: {binary}")
    failures.extend(_virtnbdbackup_version_failures())
    failures.extend(_validate_scratch_dir())
    if config.enabled("REQUIRE_ROOT") and hasattr(os, "geteuid") and os.geteuid() != 0:
        failures.append("must run as root")
    try:
        vms = list_vms(config)
        if not vms:
            failures.append("no VMs selected")
    except Exception as exc:
        failures.append(f"libvirt VM discovery failed: {exc}")
        vms = []
    failures.extend(probe_qemu_socket_bind_with_lock(config, vms, lock_held=lock_held))
    required_kb = _estimate_required_kb(config, vms)
    backup_path = config.path_value("BACKUP_PATH")
    if config.get("BACKUP_PATH").strip() and backup_path.exists() and backup_path.is_dir():
        try:
            available = _df_available_kb(backup_path)
            if available < required_kb:
                failures.append(f"insufficient backup space: available_kb={available} required_kb={required_kb}")
        except Exception as exc:
            failures.append(f"backup space check failed: {exc}")
    return failures, len(vms), required_kb


def check(config: Config, *, lock_held: bool = False) -> int:
    failures, vm_count, required_kb = collect_check_failures(config, lock_held=lock_held)
    for failure in failures:
        event("error", "preflight failed", reason=failure)
    if failures:
        return 1
    event("info", "preflight passed", vm_count=vm_count, required_kb=required_kb)
    return 0
