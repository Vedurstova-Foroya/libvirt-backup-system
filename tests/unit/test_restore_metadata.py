from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.restore_metadata import read_backup_domain_config


def _write_config(path: Path, body: str) -> None:
    path.write_text(f"<domain>{body}</domain>\n", encoding="utf-8")


def test_read_backup_domain_config_parses_name_and_file_disks(tmp_path: Path) -> None:
    _write_config(
        tmp_path / "vmconfig.virtnbdbackup.0.xml",
        """
        <name>vm-0</name>
        <devices>
          <disk type='file' device='disk'><source file='/var/lib/libvirt/images/vm-0.qcow2'/></disk>
          <disk type='block' device='disk'><source dev='/dev/sdb'/></disk>
          <disk type='file' device='cdrom'><source file='/iso/install.iso'/></disk>
          <disk type='file' device='disk'><source file=''/></disk>
        </devices>
        """,
    )

    parsed = read_backup_domain_config(tmp_path)

    assert parsed.name == "vm-0"
    assert parsed.disk_paths == (Path("/var/lib/libvirt/images/vm-0.qcow2"),)
    assert parsed.disk_output_dir == Path("/var/lib/libvirt/images")


def test_read_backup_domain_config_uses_next_valid_xml(tmp_path: Path) -> None:
    (tmp_path / "vmconfig.virtnbdbackup.9.xml").write_text("<domain>", encoding="utf-8")
    _write_config(tmp_path / "vmconfig.virtnbdbackup.0.xml", "<name>-bad</name>")

    parsed = read_backup_domain_config(tmp_path)

    assert parsed.name is None
    assert parsed.disk_paths == ()


def test_read_backup_domain_config_empty_when_glob_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_glob(self: Path, pattern: str) -> list[Path]:
        raise OSError("denied")

    monkeypatch.setattr(Path, "glob", fail_glob)

    parsed = read_backup_domain_config(tmp_path)

    assert parsed.name is None
    assert parsed.disk_paths == ()


def test_disk_output_dir_requires_one_absolute_parent() -> None:
    chain = Path("/tmp/unused")
    parsed = read_backup_domain_config(chain)
    assert parsed.disk_output_dir is None
