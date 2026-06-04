from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

KOPIA_VERSION = "0.17.0"
KOPIA_URL = (
    f"https://github.com/kopia/kopia/releases/download/v{KOPIA_VERSION}/" f"kopia-{KOPIA_VERSION}-linux-x64.tar.gz"
)
KOPIA_SHA256 = "6851bba9f49c2ca2cabc5bec85a813149a180472d1e338fad42a8285dad047ee"
KOPIA_TAR_ROOT = f"kopia-{KOPIA_VERSION}-linux-x64"
KOPIA_TARBALL_NAME = f"{KOPIA_TAR_ROOT}.tar.gz"
VENDORED_KOPIA_TARBALL = Path(__file__).resolve().parent / "vendor" / "kopia" / KOPIA_TARBALL_NAME


class KopiaVendorError(RuntimeError):
    """Raised when the vendored Kopia artifact is missing, corrupt, or unusable."""


def verify_kopia_sha256(data: bytes, *, source: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != KOPIA_SHA256:
        raise KopiaVendorError(f"sha256 mismatch for {source}: expected {KOPIA_SHA256}, got {actual}")


def vendored_kopia_tarball_bytes() -> bytes | None:
    if not VENDORED_KOPIA_TARBALL.is_file():
        return None
    data = VENDORED_KOPIA_TARBALL.read_bytes()
    verify_kopia_sha256(data, source=str(VENDORED_KOPIA_TARBALL))
    return data


def extract_kopia_binary(tarball_bytes: bytes, dest: Path) -> None:
    member_name = f"{KOPIA_TAR_ROOT}/kopia"
    with tempfile.TemporaryDirectory(prefix="kopia-install-") as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        tarball_path = tmp_dir_path / "kopia.tar.gz"
        tarball_path.write_bytes(tarball_bytes)
        with tarfile.open(tarball_path, "r:gz") as tar:
            try:
                member = tar.getmember(member_name)
            except KeyError as exc:
                raise KopiaVendorError(
                    f"kopia tarball does not contain {member_name}; the upstream layout changed"
                ) from exc
            src = tar.extractfile(member)
            if src is None:  # pragma: no cover - defensive: kopia is a regular file
                raise KopiaVendorError(f"could not read {member_name} from kopia tarball")
            extracted = tmp_dir_path / "kopia"
            try:
                extracted.write_bytes(src.read())
            finally:
                src.close()
        extracted.chmod(0o755)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), str(dest))


def ensure_vendored_kopia_on_path() -> Path | None:
    existing = shutil.which("kopia")
    if existing:
        return Path(existing)
    data = vendored_kopia_tarball_bytes()
    if data is None:
        return None
    install_dir = Path(tempfile.mkdtemp(prefix="libvirt-backup-system-kopia-"))
    kopia_path = install_dir / "kopia"
    extract_kopia_binary(data, kopia_path)
    os.environ["PATH"] = f"{install_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    return kopia_path
