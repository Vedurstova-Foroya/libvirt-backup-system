from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    ManifestDisk,
    parse_manifest,
    read_manifest,
    snapshot_filename_for_target,
    utc_timestamp,
)


def _make_manifest(**overrides: object) -> Manifest:
    defaults: dict[str, object] = {
        "vm_name": "alpha",
        "vm_uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "vm_state": "running",
        "host_id": "host-a",
        "run_id": "run-1",
        "timestamp": "20260521T120000",
        "libvirt_uri": "qemu:///system",
        "domain_xml": "<domain/>",
        "disks": (
            ManifestDisk(
                target="vda",
                source_path="/var/lib/libvirt/images/alpha.qcow2",
                virtual_size_bytes=10_737_418_240,
                snapshot_filename="vda.raw",
            ),
        ),
    }
    defaults.update(overrides)
    return Manifest(**defaults)  # type: ignore[arg-type]


def test_to_json_round_trip_preserves_fields(backup_config: Config) -> None:
    # Mirrors the on-disk shape the backup engine writes: nested disks come
    # back as ManifestDisk tuples after parse, not raw dicts.
    _ = backup_config  # fixture invoked to honor the conftest pattern
    manifest = _make_manifest()
    parsed = parse_manifest(manifest.to_json())
    assert parsed == manifest
    payload = json.loads(manifest.to_json())
    assert payload["vm_name"] == "alpha"
    assert payload["vm_state"] == "running"
    assert isinstance(payload["disks"], list)
    assert payload["disks"][0]["target"] == "vda"


def test_write_persists_manifest_to_disk(backup_config: Config, tmp_path: Path) -> None:
    _ = backup_config
    manifest = _make_manifest()
    out_dir = tmp_path / "staging" / "meta"
    assert manifest.write(out_dir) is True
    written = out_dir / MANIFEST_FILENAME
    assert written.is_file()
    again = read_manifest(written)
    assert again == manifest


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    # ``write`` is responsible for mkdir-ing the staging tree so backup.py
    # does not have to.
    manifest = _make_manifest()
    nested = tmp_path / "a" / "b" / "c"
    assert manifest.write(nested) is True
    assert (nested / MANIFEST_FILENAME).is_file()


def test_write_uses_supplied_vm_name_for_error_attribution(tmp_path: Path) -> None:
    manifest = _make_manifest(vm_name="ignored")
    target = tmp_path / "out"
    assert manifest.write(target, vm_name="explicit") is True
    payload = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert payload["vm_name"] == "ignored"


def test_parse_manifest_rejects_non_object_top_level() -> None:
    with pytest.raises(ValueError, match="not a JSON object"):
        parse_manifest("[]")


def test_parse_manifest_rejects_non_list_disks() -> None:
    body = json.dumps(
        {
            "vm_name": "a",
            "vm_uuid": "b",
            "host_id": "h",
            "run_id": "r",
            "timestamp": "t",
            "libvirt_uri": "u",
            "domain_xml": "x",
            "disks": {"vda": {}},
        }
    )
    with pytest.raises(ValueError, match="disks field is not a list"):
        parse_manifest(body)


def test_parse_manifest_defaults_missing_vm_state_to_running() -> None:
    payload = json.loads(_make_manifest().to_json())
    del payload["vm_state"]
    assert parse_manifest(json.dumps(payload)).vm_state == "running"


def test_parse_manifest_rejects_non_object_disk_row() -> None:
    body = json.dumps(
        {
            "vm_name": "a",
            "vm_uuid": "b",
            "host_id": "h",
            "run_id": "r",
            "timestamp": "t",
            "libvirt_uri": "u",
            "domain_xml": "x",
            "disks": ["not a dict"],
        }
    )
    with pytest.raises(ValueError, match="disk row is not an object"):
        parse_manifest(body)


def test_parse_manifest_rejects_non_string_disk_field() -> None:
    body = json.dumps(
        {
            "vm_name": "a",
            "vm_uuid": "b",
            "host_id": "h",
            "run_id": "r",
            "timestamp": "t",
            "libvirt_uri": "u",
            "domain_xml": "x",
            "disks": [
                {
                    "target": 7,
                    "source_path": "/a",
                    "virtual_size_bytes": 1,
                    "snapshot_filename": "vda.raw",
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="'target' must be a string"):
        parse_manifest(body)


def test_parse_manifest_rejects_non_int_size() -> None:
    body = json.dumps(
        {
            "vm_name": "a",
            "vm_uuid": "b",
            "host_id": "h",
            "run_id": "r",
            "timestamp": "t",
            "libvirt_uri": "u",
            "domain_xml": "x",
            "disks": [
                {
                    "target": "vda",
                    "source_path": "/a",
                    "virtual_size_bytes": "huge",
                    "snapshot_filename": "vda.raw",
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="'virtual_size_bytes' must be an integer"):
        parse_manifest(body)


def test_parse_manifest_rejects_bool_as_int() -> None:
    # ``bool`` is a subclass of ``int`` in Python; the validator must reject
    # it explicitly so a manifest with ``true`` does not pass as 1 byte.
    body = json.dumps(
        {
            "vm_name": "a",
            "vm_uuid": "b",
            "host_id": "h",
            "run_id": "r",
            "timestamp": "t",
            "libvirt_uri": "u",
            "domain_xml": "x",
            "disks": [
                {
                    "target": "vda",
                    "source_path": "/a",
                    "virtual_size_bytes": True,
                    "snapshot_filename": "vda.raw",
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="'virtual_size_bytes' must be an integer"):
        parse_manifest(body)


def test_parse_manifest_rejects_non_string_top_level_field() -> None:
    body = json.dumps(
        {
            "vm_name": 42,
            "vm_uuid": "b",
            "host_id": "h",
            "run_id": "r",
            "timestamp": "t",
            "libvirt_uri": "u",
            "domain_xml": "x",
            "disks": [],
        }
    )
    with pytest.raises(ValueError, match="'vm_name' must be a string"):
        parse_manifest(body)


def test_parse_manifest_accepts_empty_disks() -> None:
    body = json.dumps(
        {
            "vm_name": "a",
            "vm_uuid": "b",
            "host_id": "h",
            "run_id": "r",
            "timestamp": "t",
            "libvirt_uri": "u",
            "domain_xml": "x",
            "disks": [],
        }
    )
    manifest = parse_manifest(body)
    assert manifest.disks == ()


def test_utc_timestamp_formats_supplied_instant() -> None:
    instant = dt.datetime(2026, 5, 21, 12, 34, 56, tzinfo=dt.timezone.utc)
    assert utc_timestamp(instant) == "20260521T123456"


def test_utc_timestamp_uses_current_utc_when_unset() -> None:
    stamp = utc_timestamp()
    # ``YYYYMMDDTHHMMSS`` is fixed-width and not localized.
    assert len(stamp) == 15
    assert stamp[8] == "T"
    assert stamp[:8].isdigit()
    assert stamp[9:].isdigit()


def test_snapshot_filename_for_target() -> None:
    assert snapshot_filename_for_target("vda") == "vda.raw"
    assert snapshot_filename_for_target("sda1") == "sda1.raw"


def test_read_manifest_reads_from_path(tmp_path: Path) -> None:
    manifest = _make_manifest()
    out = tmp_path / "m.json"
    out.write_text(manifest.to_json(), encoding="utf-8")
    assert read_manifest(out) == manifest
