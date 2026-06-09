from __future__ import annotations

import math
import os
import secrets
import shutil
from pathlib import Path

from . import disk_compat, kopia_repo, preflight_host_id, preflight_kopia_password_file
from .config import Config, float_value, int_value, split_words
from .config import prefixed as _prefixed
from .config_data import CONFIG_KEYS
from .logging_json import event
from .preflight_backup_path import (
    WRITE_PROBE_NAME,
    validate_backup_path_readonly,
    validate_backup_path_writable,
    write_probe,
)
from .preflight_estimate import df_available_kb as _df_available_kb
from .preflight_estimate import estimate_required_kb as _estimate_required_kb
from .storage import subpath_is_safe
from .vms import is_safe_vm_uuid, list_vms

REQUIRED_BINARIES = ["virsh", "qemu-nbd", "nbdcopy", "qemu-img", "df", "kopia"]
BOOLEAN_KEYS = frozenset(("BACKUP_REQUIRE_NFS_MOUNT", "REQUIRE_ROOT"))
INTEGER_KEYS = frozenset(
    "COMMAND_TIMEOUT_SECONDS KEEP_ANNUAL KEEP_DAILY KEEP_HOURLY KEEP_LATEST KEEP_MONTHLY KEEP_WEEKLY KOPIA_PARALLELISM SPACE_MARGIN_PERCENT".split()  # noqa: E501
)
FLOAT_KEYS = frozenset(("BACKUP_ESTIMATE_GB_PER_VM",))
ALLOWED_LIBVIRT_URI_PREFIXES = tuple("qemu:/// qemu+unix:// test:// test:///".split())
REMOTE_LIBVIRT_URI_PREFIXES = tuple("qemu+ssh:// qemu+tcp:// qemu+tls://".split())
SCRATCH_DIR = Path("/var/tmp")  # noqa: S108 - filesystem scratch dir for write probes.
HOST_ID_STATE_FILE = preflight_host_id.HOST_ID_STATE_FILE
LOCAL_KOPIA_REPO_MISSING_FAILURE = (
    "local kopia repo is not initialized; run start to create/connect BACKUP_PATH/<HOST_ID>/kopia-repo"
)
JOIN_COMMAND_RECOVERY = (
    "on an already joined host run: sudo libvirt-backup-system add-node; "
    "paste the printed install command on this host"
)
LOCAL_KOPIA_REPO_JOIN_FAILURE = "this host is not joined to the existing backup set; " + JOIN_COMMAND_RECOVERY
LOCAL_KOPIA_REPO_CONNECT_FAILURE = "local kopia repo could not be connected with the shared password; " + (
    JOIN_COMMAND_RECOVERY
)
prefixed = _prefixed


def validate_libvirt_uri(uri: str) -> bool:
    return uri.startswith(ALLOWED_LIBVIRT_URI_PREFIXES) and not uri.startswith(REMOTE_LIBVIRT_URI_PREFIXES)


def _validate_required_present(config: Config) -> list[str]:
    optional_keys = {"KOPIA_REPO_PATH", "VM_BLACKLIST"}
    failures = [f"{k} must not be empty" for k in sorted(CONFIG_KEYS - optional_keys) if not config.get(k).strip()]
    host_id = config.get("HOST_ID")
    host_failure = preflight_host_id.validation_failure(host_id, allow_empty=True)
    if host_failure is not None:
        failures.append(host_failure)
    return failures


def _validate_vm_blacklist(config: Config) -> list[str]:
    bad = [entry for entry in split_words(config.get("VM_BLACKLIST")) if not is_safe_vm_uuid(entry)]
    return [f"VM_BLACKLIST contains invalid VM UUID: {entry!r}" for entry in bad]


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
        if value < 0:
            failures.append(f"{key} must be greater than or equal to 0")
    return failures


def _validate_scratch_dir() -> list[str]:
    try:
        write_probe(SCRATCH_DIR / WRITE_PROBE_NAME)
    except (FileNotFoundError, NotADirectoryError):
        return [f"{SCRATCH_DIR} must exist as a directory for write probes"]
    except OSError as exc:
        return [f"{SCRATCH_DIR} must be writable for write probes: {exc}"]
    return []


def _validate_kopia_repo_path(config: Config) -> list[str]:
    """Confirm KOPIA_REPO_PATH stays within BACKUP_PATH when set.

    Empty is fine: kopia_repo.local_repo_path falls back to the convention
    BACKUP_PATH/<HOST_ID>/kopia-repo/. A non-empty override must resolve to
    a subpath of BACKUP_PATH or the per-host scoping breaks down (the repo
    would land outside the tree that peer hosts discover).
    """
    raw = config.get("KOPIA_REPO_PATH").strip()
    if not raw:
        return []
    backup_raw = config.get("BACKUP_PATH").strip()
    if not backup_raw:
        # BACKUP_PATH-empty is already reported by _validate_required_present;
        # avoid a redundant secondary failure rooted at the same cause.
        return []
    backup_path = config.path_value("BACKUP_PATH")
    repo_path = Path(raw)
    if not repo_path.is_absolute():
        return ["KOPIA_REPO_PATH must be an absolute path"]
    if not subpath_is_safe(backup_path, repo_path):
        return [f"KOPIA_REPO_PATH must stay within BACKUP_PATH ({backup_path}): {repo_path}"]
    convention = backup_path / config.get("HOST_ID") / kopia_repo.REPO_DIR_NAME
    if repo_path != convention:
        return [f"KOPIA_REPO_PATH must use BACKUP_PATH/HOST_ID/{kopia_repo.REPO_DIR_NAME}: {convention}"]
    return []


def _validate_kopia_password_file(config: Config) -> list[str]:
    return preflight_kopia_password_file.validate_kopia_password_file(config)


def _existing_peer_repos(config: Config) -> list[kopia_repo.PeerRepo]:
    if not config.get("BACKUP_PATH").strip():
        return []
    try:
        peers = kopia_repo.discover_peer_repos(config)
    except kopia_repo.PeerDiscoveryError:
        return []
    return [peer for peer in peers if peer.host_id != config.get("HOST_ID")]


def _validate_local_kopia_repo(config: Config, *, require_existing: bool = False) -> list[str]:
    if not config.get("BACKUP_PATH").strip():
        return []
    if not kopia_repo.local_repo_exists(config):
        if require_existing:
            if _existing_peer_repos(config):
                return [LOCAL_KOPIA_REPO_JOIN_FAILURE]
            return [LOCAL_KOPIA_REPO_MISSING_FAILURE]
        return []
    if kopia_repo.ensure_local_connected(config) is None:
        return [LOCAL_KOPIA_REPO_CONNECT_FAILURE]
    return _validate_local_kopia_repo_writable(config)


def _validate_peer_kopia_repos(config: Config) -> list[str]:
    if not config.get("BACKUP_PATH").strip():
        return []
    try:
        peers = kopia_repo.discover_peer_repos(config)
    except kopia_repo.PeerDiscoveryError as exc:
        return [str(exc)]
    failures: list[str] = []
    for peer in peers:
        if peer.host_id == config.get("HOST_ID"):
            continue
        if kopia_repo.ensure_peer_connected(config, peer.host_id) is None:
            failures.append(
                f"existing peer kopia repo {peer.host_id} could not be opened with this host's token; "
                f"{JOIN_COMMAND_RECOVERY}"
            )
    return failures


def _validate_local_kopia_repo_writable(config: Config) -> list[str]:
    try:
        repo_path = kopia_repo.local_repo_path(config)
        probe_name = f"{WRITE_PROBE_NAME}.kopia.{secrets.token_hex(8)}"
        write_probe(repo_path / probe_name)
    except ValueError as exc:
        return [f"local kopia repo path rejected: {exc}"]
    except OSError as exc:
        return [f"local kopia repo must be writable: {exc}"]
    return []


def host_id_state_path(config: Config) -> Path:
    return preflight_host_id.host_id_state_path(config)


def stamp_host_id_on_first_run(config: Config) -> list[str]:
    return preflight_host_id.stamp_host_id_on_first_run(config)


def host_id_drift_failures(config: Config) -> list[str]:
    return preflight_host_id.host_id_drift_failures(config)


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
    check = validate_backup_path_writable if require_writable and not bool_failures else validate_backup_path_readonly
    failures.extend(check(config))
    failures.extend(_validate_kopia_repo_path(config))
    return failures


def validate_config(config: Config) -> int:
    failures = _validate_env_values(config, require_writable=False)
    for failure in failures:
        event("error", "config validation failed", reason=failure)
    return 1 if failures else 0


def peer_repo_access_failures(config: Config) -> list[str]:
    return _validate_peer_kopia_repos(config)


def repo_creation_failures(config: Config) -> list[str]:
    failures: list[str] = []
    host_failure = preflight_host_id.validation_failure(config.get("HOST_ID"))
    if host_failure is not None:
        failures.append(host_failure)
    bool_failures = _validate_booleans(config)
    failures.extend(bool_failures)
    failures.extend([] if bool_failures else validate_backup_path_writable(config))
    failures.extend(_validate_kopia_repo_path(config))
    return failures


def collect_check_failures(config: Config, *, lock_held: bool = False) -> tuple[list[str], int, int]:
    failures = _validate_env_values(config, require_writable=True)
    if lock_held and not failures:
        failures.extend(stamp_host_id_on_first_run(config))
    elif not lock_held:
        failures.extend(host_id_drift_failures(config))
    failures.extend(f"missing binary: {binary}" for binary in REQUIRED_BINARIES if not shutil.which(binary))
    failures.extend(_validate_scratch_dir())
    failures.extend(_validate_kopia_password_file(config))
    failures.extend(_validate_local_kopia_repo(config, require_existing=True))
    failures.extend(_validate_peer_kopia_repos(config))
    if config.enabled("REQUIRE_ROOT") and hasattr(os, "geteuid") and os.geteuid() != 0:
        failures.append("must run as root")
    try:
        vms = list_vms(config)
        if not vms:
            failures.append("no VMs selected")
        running_vms = [vm for vm in vms if vm.running]
        failures.extend(disk_compat.selected_vm_disk_compatibility_failures(config, running_vms))
    except Exception as exc:
        failures.append(f"libvirt VM discovery failed: {exc}")
        vms = running_vms = []
    required_kb = _estimate_required_kb(config, running_vms)
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
    event("info", "check passed", vm_count=vm_count, required_kb=required_kb)
    return 0
