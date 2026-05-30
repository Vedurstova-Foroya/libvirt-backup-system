"""Pinned-version binary installers for kopia and nbdcopy (libnbd-bin).

The system shells out to ``kopia`` and ``nbdcopy``; previously the operator
had to apt-install both before running ``install``. This module wires both
into the install entry point: download the upstream artifact, verify a
pinned sha256, then drop the binary into place.

Architecture: Linux **amd64 only**. The pinned URLs and digests here all
target ``linux-x64`` / ``_amd64.deb`` artifacts; the project's existing
preflight + apt instructions already assume the same platform.

Network: the install step makes outbound HTTPS / HTTP requests against
``github.com`` and ``deb.debian.org``. A pre-placed binary at the pinned
version path lets an offline host skip both calls (see
``docs/install.md`` for the offline procedure).

Determinism: every download is sha256-verified against a constant pinned
in this module before any extract / dpkg step runs. A mismatch raises
``BinaryInstallError`` so a bad pin or a tampered mirror fails loudly
instead of silently installing the wrong bits.
"""

from __future__ import annotations

import hashlib
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .config import prefixed
from .logging_json import event
from .shell import CommandError, run

# --- Pinned versions ---------------------------------------------------------
#
# Bumping any of these constants is a deliberate operator action — the
# matching sha256 MUST be refreshed in the same commit. The doc comment on
# each block names the upstream source so the next bump knows where to look.

# Kopia tarball, published as a GitHub release asset.
# Source: https://github.com/kopia/kopia/releases/tag/v0.17.0
KOPIA_VERSION = "0.17.0"
KOPIA_URL = (
    f"https://github.com/kopia/kopia/releases/download/v{KOPIA_VERSION}/" f"kopia-{KOPIA_VERSION}-linux-x64.tar.gz"
)
# sha256 pulled from the upstream checksums.txt for v0.17.0
# (https://github.com/kopia/kopia/releases/download/v0.17.0/checksums.txt).
# Refresh in the same commit as any KOPIA_VERSION bump.
KOPIA_SHA256 = "6851bba9f49c2ca2cabc5bec85a813149a180472d1e338fad42a8285dad047ee"
# Top-level directory inside the tarball.
KOPIA_TAR_ROOT = f"kopia-{KOPIA_VERSION}-linux-x64"

# libnbd-bin .deb from the Debian "bookworm" archive (the OS the system
# targets per docs/install.md). Version 1.14.2-1 is the bookworm pin
# (https://packages.debian.org/bookworm/libnbd-bin); refresh the version
# AND both digests below in the same commit when bumping.
LIBNBD_VERSION = "1.14.2-1"
LIBNBD_BIN_DEB_URL = f"http://deb.debian.org/debian/pool/main/libn/libnbd/" f"libnbd-bin_{LIBNBD_VERSION}_amd64.deb"
LIBNBD_BIN_SHA256 = "5ff45a2dd463cab00ac91c7e2747e2373ce8031f90d6f50e0e95edae470453dc"

# libnbd0 is libnbd-bin's required transitive dep; pinning it here lets the
# installer install both .debs in one dpkg pass and avoid an apt-get
# round-trip on hosts that do not already have libnbd0.
LIBNBD0_DEB_URL = f"http://deb.debian.org/debian/pool/main/libn/libnbd/" f"libnbd0_{LIBNBD_VERSION}_amd64.deb"
LIBNBD0_SHA256 = "775d8a88ac1d3daf9cea723fb0faca07e6167a4bcd606af37e73eb8ec5eba009"


class BinaryInstallError(RuntimeError):
    """Raised when downloading, verifying, or installing a pinned binary fails."""


@dataclass(frozen=True)
class _BinaryPin:
    name: str
    url: str
    sha256: str


def _download(url: str) -> bytes:
    """Fetch ``url`` and return the raw bytes; raise BinaryInstallError on failure."""
    try:
        # ``urllib.request.urlopen`` follows redirects (kopia GitHub releases
        # 302 from github.com to objects.githubusercontent.com) and returns
        # the asset bytes; we keep the whole response in memory because the
        # pinned artifacts are ~10-20 MB each.
        with urllib.request.urlopen(url) as response:  # noqa: S310
            data = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise BinaryInstallError(
            f"failed to download {url}: {exc}; see docs/install.md for the manual install procedure"
        ) from exc
    if not isinstance(data, bytes):  # pragma: no cover - defensive
        raise BinaryInstallError(f"download of {url} returned non-bytes payload")
    return data


def _verify_sha256(data: bytes, expected: str, *, source: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise BinaryInstallError(
            f"sha256 mismatch for {source}: expected {expected}, got {actual}; " "refusing to install untrusted bytes"
        )


def _fetch_pinned(pin: _BinaryPin) -> bytes:
    event("info", "downloading pinned binary", name=pin.name, url=pin.url)
    data = _download(pin.url)
    _verify_sha256(data, pin.sha256, source=pin.url)
    event("info", "verified pinned binary", name=pin.name, sha256=pin.sha256)
    return data


def _kopia_installed_version(kopia_path: Path) -> str | None:
    """Return the version reported by ``kopia --version`` or None if unusable."""
    if not kopia_path.exists():
        return None
    try:
        result = run([str(kopia_path), "--version"], check=False, timeout=10)
    except (OSError, CommandError):
        return None
    if result.returncode != 0:
        return None
    # ``kopia --version`` prints e.g. "0.17.0 build: abcd from: ...".
    parts = result.stdout.strip().split()
    return parts[0] if parts else None


def _extract_kopia_binary(tarball_bytes: bytes, dest: Path) -> None:
    """Extract the ``kopia`` binary from the in-memory tarball into ``dest``.

    Writes through a tempfile + atomic rename so a crash mid-extract cannot
    leave a half-written binary in place.
    """
    member_name = f"{KOPIA_TAR_ROOT}/kopia"
    with tempfile.TemporaryDirectory(prefix="kopia-install-") as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        tarball_path = tmp_dir_path / "kopia.tar.gz"
        tarball_path.write_bytes(tarball_bytes)
        with tarfile.open(tarball_path, "r:gz") as tar:
            try:
                member = tar.getmember(member_name)
            except KeyError as exc:
                raise BinaryInstallError(
                    f"kopia tarball does not contain {member_name}; the upstream layout changed"
                ) from exc
            extracted = tmp_dir_path / "kopia"
            src = tar.extractfile(member)
            if src is None:  # pragma: no cover - defensive: kopia is a regular file
                raise BinaryInstallError(f"could not read {member_name} from kopia tarball")
            try:
                extracted.write_bytes(src.read())
            finally:
                src.close()
        extracted.chmod(0o755)
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Move into place atomically. shutil.move handles the cross-fs case
        # by falling back to copy+unlink.
        shutil.move(str(extracted), str(dest))


def install_kopia(prefix: Path | None = None) -> None:
    """Install kopia at the pinned version into ``/usr/local/bin/kopia``.

    Idempotent: if the binary is already on disk and reports the pinned
    version, the network round-trip is skipped entirely. Otherwise the
    pinned tarball is fetched, sha256-verified, extracted, and atomically
    moved into place.

    Raises ``BinaryInstallError`` on download / verify / extract failure
    so the installer can hard-fail the whole install.
    """
    root = prefix if prefix is not None else Path("/")
    kopia_path = prefixed("/usr/local/bin/kopia", root)
    installed = _kopia_installed_version(kopia_path)
    if installed == KOPIA_VERSION:
        event("info", "kopia already installed at pinned version", path=str(kopia_path), version=installed)
        return
    pin = _BinaryPin(name="kopia", url=KOPIA_URL, sha256=KOPIA_SHA256)
    tarball_bytes = _fetch_pinned(pin)
    try:
        _extract_kopia_binary(tarball_bytes, kopia_path)
    except (OSError, tarfile.TarError) as exc:
        raise BinaryInstallError(f"failed to extract kopia tarball: {exc}") from exc
    event("info", "installed kopia", path=str(kopia_path), version=KOPIA_VERSION)


def _nbdcopy_present(nbdcopy_path: Path) -> bool:
    """Return True if ``nbdcopy --version`` runs successfully.

    EXACT version match is intentionally NOT required: nbdcopy is a stable
    libnbd-bin entry point and the system only cares that the binary is
    callable. A host that already apt-installed libnbd-bin (any version)
    should not trigger a second-source install.
    """
    if not nbdcopy_path.exists():
        return False
    try:
        result = run([str(nbdcopy_path), "--version"], check=False, timeout=10)
    except (OSError, CommandError):
        return False
    return result.returncode == 0


def _install_debs(deb_paths: list[Path]) -> None:
    """``dpkg -i`` the given .debs, falling back to ``apt-get install -f`` on missing deps.

    ``dpkg -i`` cannot resolve transitive dependencies; if it fails because
    libnbd-bin needs e.g. a libc version that is already present but
    flagged as broken, ``apt-get install -f -y`` will reach into the apt
    cache and finish the install. The second call is a no-op when dpkg
    succeeded on its own.
    """
    args = ["dpkg", "-i", *[str(path) for path in deb_paths]]
    try:
        run(args, check=True, timeout=300)
    except CommandError as dpkg_exc:
        event(
            "warning",
            "dpkg -i reported broken dependencies; attempting apt-get install -f",
            stderr=dpkg_exc.result.stderr.strip(),
        )
        try:
            run(["apt-get", "install", "-f", "-y"], check=True, timeout=600)
        except CommandError as apt_exc:
            raise BinaryInstallError(
                "apt-get install -f failed to repair libnbd-bin dependencies: " f"{apt_exc.result.stderr.strip()}"
            ) from apt_exc


def install_nbdcopy(prefix: Path | None = None) -> None:
    """Install nbdcopy (libnbd-bin + libnbd0) at the pinned version.

    Idempotent: if ``nbdcopy --version`` runs successfully the install is
    skipped. Otherwise the pinned .debs are fetched, sha256-verified, and
    installed via ``dpkg -i`` with an ``apt-get install -f`` fallback for
    transitive deps.

    The ``prefix`` argument is honored for the idempotency probe path. For
    non-rooted installs, a runnable ``nbdcopy`` on PATH also satisfies the
    bootstrap so sandboxed e2e installs do not mutate the host dpkg database.
    """
    root = prefix if prefix is not None else Path("/")
    nbdcopy_path = prefixed("/usr/bin/nbdcopy", root)
    if _nbdcopy_present(nbdcopy_path):
        event("info", "nbdcopy already installed; skipping pinned-deb install", path=str(nbdcopy_path))
        return
    path_nbdcopy = shutil.which("nbdcopy") if root != Path("/") else None
    if path_nbdcopy and _nbdcopy_present(Path(path_nbdcopy)):
        event("info", "nbdcopy available on PATH; skipping pinned-deb install", path=path_nbdcopy)
        return
    libnbd0_pin = _BinaryPin(name="libnbd0", url=LIBNBD0_DEB_URL, sha256=LIBNBD0_SHA256)
    libnbd_bin_pin = _BinaryPin(name="libnbd-bin", url=LIBNBD_BIN_DEB_URL, sha256=LIBNBD_BIN_SHA256)
    libnbd0_bytes = _fetch_pinned(libnbd0_pin)
    libnbd_bin_bytes = _fetch_pinned(libnbd_bin_pin)
    with tempfile.TemporaryDirectory(prefix="libnbd-install-") as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        libnbd0_path = tmp_dir_path / f"libnbd0_{LIBNBD_VERSION}_amd64.deb"
        libnbd_bin_path = tmp_dir_path / f"libnbd-bin_{LIBNBD_VERSION}_amd64.deb"
        libnbd0_path.write_bytes(libnbd0_bytes)
        libnbd_bin_path.write_bytes(libnbd_bin_bytes)
        try:
            _install_debs([libnbd0_path, libnbd_bin_path])
        finally:
            # Tempfile cleanup is implicit, but be loud about scrubbing the
            # downloaded .debs so they cannot linger if the operator
            # interrupts the install partway through dpkg.
            for path in (libnbd0_path, libnbd_bin_path):
                with suppress(FileNotFoundError):
                    path.unlink()
    event("info", "installed nbdcopy", version=LIBNBD_VERSION)
