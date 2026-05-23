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
    install_nbdcopy,
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


def test_install_kopia_treats_commanderror_probe_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_install_kopia_raises_when_tarball_layout_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


# --- install_nbdcopy --------------------------------------------------------


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


def test_install_nbdcopy_skip_handles_failed_version_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_install_nbdcopy_oserror_probe_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_install_nbdcopy_commanderror_probe_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_install_nbdcopy_downloads_and_installs_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_install_nbdcopy_apt_get_fallback_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            raise CommandError(
                CommandResult(args=args, returncode=1, stdout="", stderr="dependency problems")
            )
        return CommandResult(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.run", fake_run)

    install_nbdcopy(prefix=tmp_path)

    # The apt-get install -f -y fallback ran AFTER dpkg failed.
    commands = [args for args in captured if args[:1] in (["dpkg"], ["apt-get"])]
    assert commands[0][:2] == ["dpkg", "-i"]
    assert commands[1] == ["apt-get", "install", "-f", "-y"]


def test_install_nbdcopy_raises_when_apt_get_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_install_nbdcopy_raises_on_download_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_url: str) -> _FakeResponse:
        raise OSError("network down")

    monkeypatch.setattr("libvirt_backup_system.installer_binaries.urllib.request.urlopen", boom)

    with pytest.raises(BinaryInstallError, match="failed to download"):
        install_nbdcopy(prefix=tmp_path)


def test_install_nbdcopy_raises_on_sha256_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
