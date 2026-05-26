from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import installer_binaries
from libvirt_backup_system.installer_binaries import (
    KOPIA_TAR_ROOT,
    BinaryInstallError,
    install_kopia,
)
from libvirt_backup_system.shell import CommandError, CommandResult


class _FakeResponse:
    """Stand-in for the object urllib.request.urlopen yields as a context manager.

    The real urllib response is a context manager whose ``.read()`` returns
    bytes; the helper only needs that surface.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:  # noqa: PYI034
        # PYI034 wants ``Self`` here but ``typing.Self`` is 3.11+ and the
        # project targets 3.10. The class is test-only; a forward reference
        # is fine.
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


def _build_kopia_tarball(member_name: str = f"{KOPIA_TAR_ROOT}/kopia") -> bytes:
    """Return a minimal gzipped tarball that contains ``member_name``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = b"#!/bin/sh\necho kopia\n"
        info = tarfile.TarInfo(member_name)
        info.size = len(body)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _pin_kopia_sha256(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    monkeypatch.setattr(installer_binaries, "KOPIA_SHA256", hashlib.sha256(payload).hexdigest())


def _pin_libnbd_sha256(monkeypatch: pytest.MonkeyPatch, libnbd0: bytes, libnbd_bin: bytes) -> None:
    monkeypatch.setattr(installer_binaries, "LIBNBD0_SHA256", hashlib.sha256(libnbd0).hexdigest())
    monkeypatch.setattr(installer_binaries, "LIBNBD_BIN_SHA256", hashlib.sha256(libnbd_bin).hexdigest())


# --- install_kopia ----------------------------------------------------------


def test_install_kopia_downloads_verifies_and_extracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tarball = _build_kopia_tarball()
    _pin_kopia_sha256(monkeypatch, tarball)

    fetched: list[str] = []

    def fake_urlopen(url: str) -> _FakeResponse:
        fetched.append(url)
        return _FakeResponse(tarball)

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.urllib.request.urlopen", fake_urlopen)

    install_kopia(prefix=tmp_path)

    kopia_path = tmp_path / "usr/local/bin/kopia"
    assert kopia_path.is_file()
    # Mode 0o755 + executable bit survive the atomic move into place.
    assert kopia_path.stat().st_mode & 0o777 == 0o755
    assert fetched == [installer_binaries.KOPIA_URL]


def test_install_kopia_skips_when_version_matches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kopia_path = tmp_path / "usr/local/bin/kopia"
    kopia_path.parent.mkdir(parents=True)
    kopia_path.write_text("placeholder\n", encoding="utf-8")
    kopia_path.chmod(0o755)

    version_line = f"{installer_binaries.KOPIA_VERSION} build: x\n"

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        assert args == [str(kopia_path), "--version"]
        return CommandResult(args=args, returncode=0, stdout=version_line, stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    def boom(_url: str) -> _FakeResponse:
        raise AssertionError("download must not run when binary already at pinned version")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.urllib.request.urlopen", boom)

    install_kopia(prefix=tmp_path)

    assert kopia_path.read_text(encoding="utf-8") == "placeholder\n"


def test_install_kopia_reinstalls_when_version_mismatches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Existing binary on disk reports a different version (or fails); the
    # installer must re-download and overwrite rather than silently keep
    # the wrong version.
    kopia_path = tmp_path / "usr/local/bin/kopia"
    kopia_path.parent.mkdir(parents=True)
    kopia_path.write_text("old\n", encoding="utf-8")
    kopia_path.chmod(0o755)

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        return CommandResult(args=args, returncode=0, stdout="0.0.1 build: y\n", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)
    tarball = _build_kopia_tarball()
    _pin_kopia_sha256(monkeypatch, tarball)
    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda _url: _FakeResponse(tarball),
    )

    install_kopia(prefix=tmp_path)

    assert kopia_path.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_install_kopia_treats_failed_version_probe_as_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kopia_path = tmp_path / "usr/local/bin/kopia"
    kopia_path.parent.mkdir(parents=True)
    kopia_path.write_text("garbage\n", encoding="utf-8")
    kopia_path.chmod(0o755)

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        # Non-zero return code -> probe inconclusive; treat as version mismatch.
        return CommandResult(args=args, returncode=2, stdout="", stderr="oops\n")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)
    tarball = _build_kopia_tarball()
    _pin_kopia_sha256(monkeypatch, tarball)
    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda _url: _FakeResponse(tarball),
    )

    install_kopia(prefix=tmp_path)


def test_install_kopia_treats_empty_version_probe_output_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty stdout from `kopia --version` (a corrupted local copy) MUST
    # fall through to a re-install rather than being treated as the
    # pinned version.
    kopia_path = tmp_path / "usr/local/bin/kopia"
    kopia_path.parent.mkdir(parents=True)
    kopia_path.write_text("garbage\n", encoding="utf-8")
    kopia_path.chmod(0o755)

    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.run",
        lambda args, **kwargs: CommandResult(args=args, returncode=0, stdout="\n", stderr=""),
    )
    tarball = _build_kopia_tarball()
    _pin_kopia_sha256(monkeypatch, tarball)
    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda _url: _FakeResponse(tarball),
    )

    install_kopia(prefix=tmp_path)


def test_install_kopia_treats_oserror_probe_as_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A binary that exists on disk but cannot be invoked at all (no exec
    # permission) raises OSError out of shell.run; treat the probe as
    # inconclusive and reinstall.
    kopia_path = tmp_path / "usr/local/bin/kopia"
    kopia_path.parent.mkdir(parents=True)
    kopia_path.write_text("garbage\n", encoding="utf-8")
    kopia_path.chmod(0o755)

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        raise OSError("permission denied")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)
    tarball = _build_kopia_tarball()
    _pin_kopia_sha256(monkeypatch, tarball)
    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda _url: _FakeResponse(tarball),
    )

    install_kopia(prefix=tmp_path)


def test_install_kopia_treats_commanderror_probe_as_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kopia_path = tmp_path / "usr/local/bin/kopia"
    kopia_path.parent.mkdir(parents=True)
    kopia_path.write_text("garbage\n", encoding="utf-8")
    kopia_path.chmod(0o755)

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        raise CommandError(CommandResult(args=args, returncode=1, stdout="", stderr="cmd err"))

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)
    tarball = _build_kopia_tarball()
    _pin_kopia_sha256(monkeypatch, tarball)
    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda _url: _FakeResponse(tarball),
    )

    install_kopia(prefix=tmp_path)


def test_install_kopia_raises_on_download_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_url: str) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.urllib.request.urlopen", boom)
    with pytest.raises(BinaryInstallError, match="failed to download"):
        install_kopia(prefix=tmp_path)


def test_install_kopia_raises_on_sha256_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tarball = _build_kopia_tarball()
    # Force a sha256 the synthetic tarball cannot match so the verify
    # step rejects it; proves a tampered mirror / wrong pin fails loudly.
    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda _url: _FakeResponse(tarball),
    )
    monkeypatch.setattr(installer_binaries, "KOPIA_SHA256", "a" * 64)

    with pytest.raises(BinaryInstallError, match="sha256 mismatch"):
        install_kopia(prefix=tmp_path)


def test_install_kopia_raises_when_tarball_layout_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Future kopia releases that rename the top-level dir must fail loudly
    # so a silent "installed nothing" install cannot happen.
    payload = _build_kopia_tarball(member_name="kopia-future/kopia")
    _pin_kopia_sha256(monkeypatch, payload)
    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda _url: _FakeResponse(payload),
    )

    with pytest.raises(BinaryInstallError, match="upstream layout changed"):
        install_kopia(prefix=tmp_path)


def test_install_kopia_wraps_extract_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tarball = _build_kopia_tarball()
    _pin_kopia_sha256(monkeypatch, tarball)
    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda _url: _FakeResponse(tarball),
    )

    def boom_move(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.shutil.move", boom_move)
    with pytest.raises(BinaryInstallError, match="failed to extract"):
        install_kopia(prefix=tmp_path)
