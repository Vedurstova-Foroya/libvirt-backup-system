"""Top-level preflight tests: config-shape validators + scratch dir + write probe.

Split across ``test_preflight_*`` files so each one stays under the 300-line
project ceiling without losing the related-tests-live-together property.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from libvirt_backup_system import preflight
from tests.unit._preflight_helpers import make_config


def test_validate_libvirt_uri_accepts_known_schemes() -> None:
    assert preflight.validate_libvirt_uri("qemu:///system")
    assert preflight.validate_libvirt_uri("test://")
    assert not preflight.validate_libvirt_uri("https://nope")


def test_validate_config_zero_for_clean_config(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert preflight.validate_config(cfg) == 0


def test_validate_config_one_with_empty_required(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["HOST_ID"] = ""
    assert preflight.validate_config(cfg) == 1


def test_required_present_rejects_path_separators(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="bad/host")
    failures = preflight._validate_required_present(cfg)
    assert any("path separators" in failure for failure in failures)


def test_required_present_rejects_dot_names(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="..")
    failures = preflight._validate_required_present(cfg)
    assert any("path separators" in failure for failure in failures)


def test_required_present_rejects_control_characters(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="bad\x01name")
    failures = preflight._validate_required_present(cfg)
    assert any("control characters" in failure for failure in failures)


def test_required_present_rejects_leading_or_trailing_whitespace(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["HOST_ID"] = "  host  "
    failures = preflight._validate_required_present(cfg)
    assert any("leading or trailing whitespace" in failure for failure in failures)


def test_required_present_rejects_internal_whitespace(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="bad host")
    failures = preflight._validate_required_present(cfg)
    assert any("must not contain whitespace" in failure for failure in failures)


def test_required_present_flags_empty_required_keys(tmp_path: Path) -> None:
    # Any required key going empty should surface a clean "must not be empty"
    # failure. We use SYSTEMD_ON_CALENDAR as a stand-in for the
    # required-but-not-HOST_ID/BACKUP_PATH bucket (those have their own
    # dedicated checks).
    cfg = make_config(tmp_path)
    cfg.values["SYSTEMD_ON_CALENDAR"] = ""
    failures = preflight._validate_required_present(cfg)
    assert any("SYSTEMD_ON_CALENDAR must not be empty" in failure for failure in failures)


def test_required_present_allows_empty_kopia_repo_path(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = ""
    failures = preflight._validate_required_present(cfg)
    assert "KOPIA_REPO_PATH must not be empty" not in failures


def test_validate_local_kopia_repo_delegates_to_repo_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    called: list[bool] = []

    def fake_ensure(_cfg: object, *, apply_global_policy: bool = True) -> int:
        called.append(apply_global_policy)
        return 0

    monkeypatch.setattr(preflight.kopia_repo, "ensure_local_repo", fake_ensure)
    assert preflight._validate_local_kopia_repo(cfg) == []
    assert called == [False]


def test_validate_local_kopia_repo_surfaces_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(preflight.kopia_repo, "ensure_local_repo", lambda *_a, **_k: 1)
    assert "local kopia repo" in preflight._validate_local_kopia_repo(cfg)[0]


def test_validate_vm_blacklist_flags_invalid_uuids(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["VM_BLACKLIST"] = "not-a-uuid, aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    failures = preflight._validate_vm_blacklist(cfg)
    assert any("not-a-uuid" in failure for failure in failures)
    assert all("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" not in failure for failure in failures)


def test_validate_booleans_flags_garbage(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["REQUIRE_ROOT"] = "maybe"
    failures = preflight._validate_booleans(cfg)
    assert any("REQUIRE_ROOT must be a boolean" in failure for failure in failures)


def test_validate_integers_flags_garbage_value(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["COMMAND_TIMEOUT_SECONDS"] = "abc"
    failures = preflight._validate_integers(cfg)
    assert any("COMMAND_TIMEOUT_SECONDS must be an integer" in failure for failure in failures)


def test_validate_integers_flags_non_positive_timeout(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["COMMAND_TIMEOUT_SECONDS"] = "0"
    failures = preflight._validate_integers(cfg)
    assert any("greater than 0" in failure for failure in failures)


def test_validate_integers_flags_negative_other(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["SPACE_MARGIN_PERCENT"] = "-1"
    failures = preflight._validate_integers(cfg)
    assert any("greater than or equal to 0" in failure for failure in failures)


def test_validate_floats_flags_garbage_value(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "nope"
    failures = preflight._validate_floats(cfg)
    assert any("must be a number" in failure for failure in failures)


def test_validate_floats_flags_non_finite_value(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "inf"
    failures = preflight._validate_floats(cfg)
    assert any("must be a finite number" in failure for failure in failures)


def test_validate_floats_flags_zero_multiplier(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_INCREMENTAL_MULTIPLIER"] = "0"
    failures = preflight._validate_floats(cfg)
    assert any("BACKUP_INCREMENTAL_MULTIPLIER must be greater than 0" in failure for failure in failures)


def test_validate_floats_flags_negative_other(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "-1"
    failures = preflight._validate_floats(cfg)
    assert any("BACKUP_ESTIMATE_GB_PER_VM must be greater than or equal to 0" in failure for failure in failures)


def test_validate_env_values_rejects_unknown_libvirt_uri(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["LIBVIRT_URI"] = "https://nope"
    failures = preflight._validate_env_values(cfg, require_writable=False)
    assert any("LIBVIRT_URI must use one of these schemes" in failure for failure in failures)


def test_write_probe_raises_if_write_truncated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.write`` returning a short count -> _write_probe raises OSError."""
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        real_write(fd, data)
        return 0

    monkeypatch.setattr(preflight.os, "write", short_write)
    with pytest.raises(OSError, match="write probe was incomplete"):
        preflight._write_probe(tmp_path / "probe")


def test_write_probe_cleans_up_when_open_fails(tmp_path: Path) -> None:
    """FileNotFoundError flows through ``finally`` without touching the path."""
    with pytest.raises(FileNotFoundError):
        preflight._write_probe(tmp_path / "no-such-dir" / "probe")


def test_validate_scratch_dir_missing_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "SCRATCH_DIR", Path("/no-such-scratch-please"))
    failures = preflight._validate_scratch_dir()
    assert any("must exist as a directory" in failure for failure in failures)


def test_validate_scratch_dir_not_a_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    # /dev/null is not a directory so os.open raises NotADirectoryError.
    monkeypatch.setattr(preflight, "SCRATCH_DIR", Path("/dev/null"))
    failures = preflight._validate_scratch_dir()
    assert failures and ("must exist" in failures[0] or "must be writable" in failures[0])


def test_validate_scratch_dir_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(preflight, "SCRATCH_DIR", scratch)

    def boom(_path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(preflight, "_write_probe", boom)
    failures = preflight._validate_scratch_dir()
    assert failures and "must be writable for write probes" in failures[0]


def test_backup_path_is_mount_returns_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(_self: Path) -> bool:
        raise OSError("ESTALE")

    monkeypatch.setattr(Path, "is_mount", boom)
    mounted, error = preflight._backup_path_is_mount(tmp_path)
    assert mounted is False
    assert error == "ESTALE"


def test_validate_kopia_repo_path_empty_is_allowed(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = ""
    assert preflight._validate_kopia_repo_path(cfg) == []


def test_validate_kopia_repo_path_accepts_discoverable_convention(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = str(tmp_path / "backups" / cfg.get("HOST_ID") / "kopia-repo")
    assert preflight._validate_kopia_repo_path(cfg) == []


def test_validate_kopia_repo_path_rejects_non_discoverable_subpath(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = str(tmp_path / "backups" / "alt-repo")
    failures = preflight._validate_kopia_repo_path(cfg)
    assert failures and "must use BACKUP_PATH/HOST_ID/kopia-repo" in failures[0]


def test_validate_kopia_repo_path_rejects_path_outside_backup_path(tmp_path: Path) -> None:
    # Headline regression: KOPIA_REPO_PATH=/tmp/foo when BACKUP_PATH lives
    # under tmp_path/backups should produce a clear failure that names both
    # the offending override and the BACKUP_PATH it must stay within.
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = "/srv/backups"
    cfg.values["KOPIA_REPO_PATH"] = "/tmp/foo"
    failures = preflight._validate_kopia_repo_path(cfg)
    assert failures and "must stay within BACKUP_PATH" in failures[0]
    assert "/srv/backups" in failures[0]
    assert "/tmp/foo" in failures[0]


def test_validate_kopia_repo_path_requires_absolute(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = "relative/repo"
    failures = preflight._validate_kopia_repo_path(cfg)
    assert failures == ["KOPIA_REPO_PATH must be an absolute path"]


def test_validate_kopia_repo_path_skips_when_backup_path_empty(tmp_path: Path) -> None:
    # BACKUP_PATH empty is already reported by _validate_required_present;
    # _validate_kopia_repo_path must not stack a second failure on the same
    # root cause.
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""
    cfg.values["KOPIA_REPO_PATH"] = "/tmp/foo"
    assert preflight._validate_kopia_repo_path(cfg) == []
