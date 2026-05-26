from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import installer_binaries
from libvirt_backup_system.installer_binaries import BinaryInstallError, install_kopia, install_nbdcopy
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.test_installer_binaries import _FakeResponse, _pin_libnbd_sha256


def test_install_nbdcopy_skips_when_binary_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nbdcopy_path = tmp_path / "usr/bin/nbdcopy"
    nbdcopy_path.parent.mkdir(parents=True)
    nbdcopy_path.write_text("placeholder\n", encoding="utf-8")
    nbdcopy_path.chmod(0o755)

    seen: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        seen.append(args)
        assert args == [str(nbdcopy_path), "--version"]
        return CommandResult(args=args, returncode=0, stdout="nbdcopy 1.18.1\n", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    def boom(_url: str) -> _FakeResponse:
        raise AssertionError("download must not run when nbdcopy is already present")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.urllib.request.urlopen", boom)

    install_nbdcopy(prefix=tmp_path)
    # Only the version probe ran; no dpkg invocations.
    assert seen == [[str(nbdcopy_path), "--version"]]


def test_install_nbdcopy_skip_handles_failed_version_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # File exists but `--version` exits non-zero -> treat as absent and
    # install via the pinned .debs.
    nbdcopy_path = tmp_path / "usr/bin/nbdcopy"
    nbdcopy_path.parent.mkdir(parents=True)
    nbdcopy_path.write_text("garbage\n", encoding="utf-8")
    nbdcopy_path.chmod(0o755)

    libnbd0 = b"libnbd0 deb bytes"
    libnbd_bin = b"libnbd-bin deb bytes"
    _pin_libnbd_sha256(monkeypatch, libnbd0, libnbd_bin)

    fetched: list[str] = []

    def fake_urlopen(url: str) -> _FakeResponse:
        fetched.append(url)
        return _FakeResponse(libnbd0 if "libnbd0" in url else libnbd_bin)

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.urllib.request.urlopen", fake_urlopen)

    seen: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        seen.append(args)
        if args[:2] == [str(nbdcopy_path), "--version"]:
            return CommandResult(args=args, returncode=1, stdout="", stderr="oops\n")
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    install_nbdcopy(prefix=tmp_path)

    # Both pinned URLs were fetched, in the libnbd0->libnbd-bin order.
    assert fetched == [installer_binaries.LIBNBD0_DEB_URL, installer_binaries.LIBNBD_BIN_DEB_URL]
    # dpkg -i call happened with both deb paths.
    dpkg_calls = [args for args in seen if args[:2] == ["dpkg", "-i"]]
    assert len(dpkg_calls) == 1
    assert "libnbd0_" in dpkg_calls[0][2] and "libnbd-bin_" in dpkg_calls[0][3]


def test_install_nbdcopy_oserror_probe_treated_as_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nbdcopy_path = tmp_path / "usr/bin/nbdcopy"
    nbdcopy_path.parent.mkdir(parents=True)
    nbdcopy_path.write_text("garbage\n", encoding="utf-8")
    nbdcopy_path.chmod(0o755)

    libnbd0 = b"libnbd0"
    libnbd_bin = b"libnbd-bin"
    _pin_libnbd_sha256(monkeypatch, libnbd0, libnbd_bin)

    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda url: _FakeResponse(libnbd0 if "libnbd0" in url else libnbd_bin),
    )

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        if args[:2] == [str(nbdcopy_path), "--version"]:
            raise OSError("exec failed")
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    install_nbdcopy(prefix=tmp_path)


def test_install_nbdcopy_commanderror_probe_treated_as_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nbdcopy_path = tmp_path / "usr/bin/nbdcopy"
    nbdcopy_path.parent.mkdir(parents=True)
    nbdcopy_path.write_text("garbage\n", encoding="utf-8")
    nbdcopy_path.chmod(0o755)

    libnbd0 = b"libnbd0"
    libnbd_bin = b"libnbd-bin"
    _pin_libnbd_sha256(monkeypatch, libnbd0, libnbd_bin)

    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda url: _FakeResponse(libnbd0 if "libnbd0" in url else libnbd_bin),
    )

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        if args[:2] == [str(nbdcopy_path), "--version"]:
            raise CommandError(CommandResult(args=args, returncode=1, stdout="", stderr=""))
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    install_nbdcopy(prefix=tmp_path)


def test_install_nbdcopy_downloads_and_installs_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    libnbd0 = b"\x21<libnbd0 deb>\x00"
    libnbd_bin = b"\x21<libnbd-bin deb>\x00"
    _pin_libnbd_sha256(monkeypatch, libnbd0, libnbd_bin)

    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda url: _FakeResponse(libnbd0 if "libnbd0" in url else libnbd_bin),
    )

    captured: list[list[str]] = []
    seen_payloads: dict[str, bytes] = {}

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        captured.append(args)
        if args[:2] == ["dpkg", "-i"]:
            for path in args[2:]:
                seen_payloads[Path(path).name] = Path(path).read_bytes()
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    install_nbdcopy(prefix=tmp_path)

    dpkg_args = next(args for args in captured if args[:2] == ["dpkg", "-i"])
    # Verify the .debs handed to dpkg are the bytes we mocked, in the
    # libnbd0->libnbd-bin order.
    libnbd0_name = f"libnbd0_{installer_binaries.LIBNBD_VERSION}_amd64.deb"
    libnbd_bin_name = f"libnbd-bin_{installer_binaries.LIBNBD_VERSION}_amd64.deb"
    assert Path(dpkg_args[2]).name == libnbd0_name
    assert Path(dpkg_args[3]).name == libnbd_bin_name
    assert seen_payloads[libnbd0_name] == libnbd0
    assert seen_payloads[libnbd_bin_name] == libnbd_bin


def test_install_nbdcopy_apt_get_fallback_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    libnbd0 = b"libnbd0-payload"
    libnbd_bin = b"libnbd-bin-payload"
    _pin_libnbd_sha256(monkeypatch, libnbd0, libnbd_bin)

    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda url: _FakeResponse(libnbd0 if "libnbd0" in url else libnbd_bin),
    )

    captured: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        captured.append(args)
        if args[:2] == ["dpkg", "-i"]:
            raise CommandError(CommandResult(args=args, returncode=1, stdout="", stderr="dependency problems"))
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    install_nbdcopy(prefix=tmp_path)

    # The apt-get install -f -y fallback ran AFTER dpkg failed.
    commands = [args for args in captured if args[:1] in (["dpkg"], ["apt-get"])]
    assert commands[0][:2] == ["dpkg", "-i"]
    assert commands[1] == ["apt-get", "install", "-f", "-y"]


def test_install_nbdcopy_raises_when_apt_get_also_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    libnbd0 = b"libnbd0"
    libnbd_bin = b"libnbd-bin"
    _pin_libnbd_sha256(monkeypatch, libnbd0, libnbd_bin)

    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda url: _FakeResponse(libnbd0 if "libnbd0" in url else libnbd_bin),
    )

    def fake_run(args: list[str], **kwargs: Any) -> CommandResult:
        if args[:2] == ["dpkg", "-i"]:
            raise CommandError(CommandResult(args=args, returncode=1, stdout="", stderr="dpkg failed"))
        if args[:1] == ["apt-get"]:
            raise CommandError(CommandResult(args=args, returncode=100, stdout="", stderr="apt also failed"))
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    with pytest.raises(BinaryInstallError, match="apt-get install -f failed"):
        install_nbdcopy(prefix=tmp_path)


def test_install_nbdcopy_raises_on_download_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_url: str) -> _FakeResponse:
        raise OSError("network down")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.urllib.request.urlopen", boom)

    with pytest.raises(BinaryInstallError, match="failed to download"):
        install_nbdcopy(prefix=tmp_path)


def test_install_nbdcopy_raises_on_sha256_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    libnbd0 = b"libnbd0"
    libnbd_bin = b"libnbd-bin"
    # Pin libnbd0 correctly so the FIRST verify passes, but force a
    # wrong sha256 on libnbd-bin so the SECOND verify fails. Asserts
    # that *each* download is independently verified.
    monkeypatch.setattr(installer_binaries, "LIBNBD0_SHA256", hashlib.sha256(libnbd0).hexdigest())
    monkeypatch.setattr(installer_binaries, "LIBNBD_BIN_SHA256", "b" * 64)

    monkeypatch.setattr(
        "libvirt_backup_system.installer_binaries.urllib.request.urlopen",
        lambda url: _FakeResponse(libnbd0 if "libnbd0" in url else libnbd_bin),
    )

    with pytest.raises(BinaryInstallError, match="sha256 mismatch"):
        install_nbdcopy(prefix=tmp_path)


def test_install_kopia_and_nbdcopy_default_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    # Calling install_kopia() / install_nbdcopy() without a prefix MUST
    # default to / so production installs land in /usr/local/bin / /usr/bin.
    # The probes use Path("/usr/local/bin/kopia") / Path("/usr/bin/nbdcopy")
    # so we monkeypatch _kopia_installed_version + _nbdcopy_present to
    # confirm the path passed in matches the production default.
    seen: dict[str, Path] = {}

    def fake_kopia_probe(path: Path) -> str | None:
        seen["kopia"] = path
        return installer_binaries.KOPIA_VERSION

    def fake_nbdcopy_probe(path: Path) -> bool:
        seen["nbdcopy"] = path
        return True

    monkeypatch.setattr("libvirt_backup_system.installer_binaries._kopia_installed_version", fake_kopia_probe)
    monkeypatch.setattr("libvirt_backup_system.installer_binaries._nbdcopy_present", fake_nbdcopy_probe)

    install_kopia()
    install_nbdcopy()

    assert seen["kopia"] == Path("/usr/local/bin/kopia")
    assert seen["nbdcopy"] == Path("/usr/bin/nbdcopy")
