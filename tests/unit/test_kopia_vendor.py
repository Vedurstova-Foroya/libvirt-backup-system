from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest

from libvirt_backup_system import kopia_vendor
from libvirt_backup_system.kopia_vendor import KopiaVendorError


def _tarball(member_name: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = b"#!/bin/sh\necho 0.17.0\n"
        info = tarfile.TarInfo(member_name or f"{kopia_vendor.KOPIA_TAR_ROOT}/kopia")
        info.size = len(body)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def test_vendored_kopia_tarball_bytes_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kopia_vendor, "VENDORED_KOPIA_TARBALL", tmp_path / "missing.tar.gz")

    assert kopia_vendor.vendored_kopia_tarball_bytes() is None


def test_vendored_kopia_tarball_bytes_verifies_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "kopia.tar.gz"
    payload = _tarball()
    path.write_bytes(payload)
    monkeypatch.setattr(kopia_vendor, "VENDORED_KOPIA_TARBALL", path)
    monkeypatch.setattr(kopia_vendor, "KOPIA_SHA256", hashlib.sha256(payload).hexdigest())

    assert kopia_vendor.vendored_kopia_tarball_bytes() == payload


def test_vendored_kopia_tarball_bytes_rejects_bad_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "kopia.tar.gz"
    path.write_bytes(_tarball())
    monkeypatch.setattr(kopia_vendor, "VENDORED_KOPIA_TARBALL", path)
    monkeypatch.setattr(kopia_vendor, "KOPIA_SHA256", "a" * 64)

    with pytest.raises(KopiaVendorError, match="sha256 mismatch"):
        kopia_vendor.vendored_kopia_tarball_bytes()


def test_extract_kopia_binary_writes_executable(tmp_path: Path) -> None:
    dest = tmp_path / "bin" / "kopia"

    kopia_vendor.extract_kopia_binary(_tarball(), dest)

    assert dest.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert dest.stat().st_mode & 0o777 == 0o755


def test_extract_kopia_binary_rejects_unknown_layout(tmp_path: Path) -> None:
    with pytest.raises(KopiaVendorError, match="upstream layout changed"):
        kopia_vendor.extract_kopia_binary(_tarball("future/kopia"), tmp_path / "kopia")


def test_ensure_vendored_kopia_on_path_keeps_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kopia_vendor.shutil, "which", lambda _name: "/usr/local/bin/kopia")

    assert kopia_vendor.ensure_vendored_kopia_on_path() == Path("/usr/local/bin/kopia")


def test_ensure_vendored_kopia_on_path_returns_none_without_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kopia_vendor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(kopia_vendor, "VENDORED_KOPIA_TARBALL", tmp_path / "missing.tar.gz")

    assert kopia_vendor.ensure_vendored_kopia_on_path() is None


def test_ensure_vendored_kopia_on_path_extracts_and_prepends_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kopia.tar.gz"
    payload = _tarball()
    path.write_bytes(payload)
    monkeypatch.setattr(kopia_vendor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(kopia_vendor, "VENDORED_KOPIA_TARBALL", path)
    monkeypatch.setattr(kopia_vendor, "KOPIA_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setenv("PATH", "/usr/bin")

    kopia_path = kopia_vendor.ensure_vendored_kopia_on_path()

    assert kopia_path is not None
    assert kopia_path.is_file()
    assert os.environ["PATH"].startswith(f"{kopia_path.parent}{os.pathsep}")
