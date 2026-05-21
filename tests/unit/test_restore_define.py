from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore_define import (
    RESTORED_CONFIG_FILE,
    define_restored_domain,
)
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_xml(tmp_path: Path, body: str, name: str = RESTORED_CONFIG_FILE) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _basic_domain(name_value: str = "old-name", with_uuid: bool = True) -> str:
    uuid_tag = "  <uuid>old-uuid</uuid>\n" if with_uuid else ""
    return (
        "<domain type='kvm'>\n"
        f"  <name>{name_value}</name>\n"
        f"{uuid_tag}"
        "  <memory unit='KiB'>1048576</memory>\n"
        "</domain>\n"
    )


def _config(monkeypatch: pytest.MonkeyPatch, uri: str = "qemu:///system") -> Config:
    # Avoid picking up developer env overrides for LIBVIRT_URI so the value we
    # set below is what reaches the run() stub.
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")
    cfg.values["LIBVIRT_URI"] = uri
    return cfg


def _record_run(monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        result = CommandResult(args=args, returncode=returncode, stdout="", stderr="")
        if check and returncode != 0:
            raise CommandError(result)
        return result

    monkeypatch.setattr("libvirt_backup_system.restore_define.run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# Successful define
# ---------------------------------------------------------------------------


def test_define_restored_domain_success_with_uuid_and_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xml_path = _write_xml(tmp_path, _basic_domain())
    cfg = _config(monkeypatch, uri="qemu+ssh://host/system")
    calls = _record_run(monkeypatch)

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, "alpha-restored") is True

    # virsh define was invoked with the configured URI and the XML path.
    assert calls == [["virsh", "-c", "qemu+ssh://host/system", "define", str(xml_path)]]

    # The XML on disk has the desired identity.
    written = ET.parse(xml_path).getroot()
    assert written.findtext("name") == "alpha-restored"
    assert written.findtext("uuid") == ALPHA_UUID


def test_define_restored_domain_sets_uuid_only_when_name_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xml_path = _write_xml(tmp_path, _basic_domain(name_value="keep-me"))
    cfg = _config(monkeypatch)
    _record_run(monkeypatch)

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, None) is True

    written = ET.parse(xml_path).getroot()
    # Name preserved untouched, uuid replaced.
    assert written.findtext("name") == "keep-me"
    assert written.findtext("uuid") == ALPHA_UUID


def test_define_restored_domain_inserts_uuid_after_name_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No uuid element on disk — the inserter must create one and place it
    # right after ``<name>`` so the libvirt schema validator stays happy.
    xml_path = _write_xml(tmp_path, _basic_domain(with_uuid=False))
    cfg = _config(monkeypatch)
    _record_run(monkeypatch)

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, None) is True

    root = ET.parse(xml_path).getroot()
    children = list(root)
    name_idx = next(i for i, child in enumerate(children) if child.tag == "name")
    uuid_idx = next(i for i, child in enumerate(children) if child.tag == "uuid")
    assert uuid_idx == name_idx + 1
    assert root.findtext("uuid") == ALPHA_UUID


def test_define_restored_domain_inserts_uuid_at_index_zero_when_no_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ``<name>`` element means the ``name is not None`` branch in
    # ``_set_child_text`` is skipped and the uuid lands at insert_at=0.
    xml_path = _write_xml(
        tmp_path,
        "<domain type='kvm'>\n  <memory unit='KiB'>1048576</memory>\n</domain>\n",
    )
    cfg = _config(monkeypatch)
    _record_run(monkeypatch)

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, None) is True

    root = ET.parse(xml_path).getroot()
    assert list(root)[0].tag == "uuid"
    assert root.findtext("uuid") == ALPHA_UUID


def test_define_restored_domain_inserts_name_at_index_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No ``<name>`` to start with: the name-insertion branch builds the
    # element at index 0, then the uuid branch (also missing here) follows.
    xml_path = _write_xml(
        tmp_path,
        "<domain type='kvm'>\n  <memory unit='KiB'>1048576</memory>\n</domain>\n",
    )
    cfg = _config(monkeypatch)
    _record_run(monkeypatch)

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, "fresh-name") is True

    root = ET.parse(xml_path).getroot()
    assert root.findtext("name") == "fresh-name"
    assert root.findtext("uuid") == ALPHA_UUID


# ---------------------------------------------------------------------------
# XML parse failures and structural rejection
# ---------------------------------------------------------------------------


def test_define_restored_domain_returns_false_on_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = _write_xml(tmp_path, "not <xml at all")
    cfg = _config(monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.restore_define.run",
        lambda *a, **kw: pytest.fail("virsh define must not run on parse failure"),
    )

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, "alpha") is False
    assert "adjusted domain XML is unusable" in capsys.readouterr().err


def test_define_restored_domain_returns_false_when_root_is_not_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = _write_xml(tmp_path, "<network><name>oops</name></network>\n")
    cfg = _config(monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.restore_define.run",
        lambda *a, **kw: pytest.fail("virsh define must not run for non-domain XML"),
    )

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, "alpha") is False
    assert "not a libvirt domain" in capsys.readouterr().err


def test_define_restored_domain_returns_false_on_xml_read_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ET.parse raises OSError when the path does not exist; the same except
    # branch handles ParseError, so this covers the ``OSError`` arm.
    missing = tmp_path / "missing.xml"
    cfg = _config(monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.restore_define.run",
        lambda *a, **kw: pytest.fail("virsh define must not run when XML missing"),
    )

    assert define_restored_domain(cfg, missing, ALPHA_UUID, "alpha") is False
    assert "adjusted domain XML is unusable" in capsys.readouterr().err


def test_define_restored_domain_returns_false_on_write_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = _write_xml(tmp_path, _basic_domain())
    cfg = _config(monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.restore_define.run",
        lambda *a, **kw: pytest.fail("virsh define must not run when XML write fails"),
    )

    real_parse = ET.parse

    class _FailingTree:
        def __init__(self, tree: ET.ElementTree) -> None:
            self._tree = tree

        def getroot(self) -> ET.Element:
            return self._tree.getroot()

        def write(self, *args: object, **kwargs: object) -> None:
            raise OSError("disk full")

    def fake_parse(source: object) -> _FailingTree:
        return _FailingTree(real_parse(source))

    monkeypatch.setattr("libvirt_backup_system.restore_define.ET.parse", fake_parse)

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, "alpha") is False
    assert "could not update adjusted domain XML" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# virsh define failures
# ---------------------------------------------------------------------------


def test_define_restored_domain_returns_false_on_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = _write_xml(tmp_path, _basic_domain())
    cfg = _config(monkeypatch)

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise CommandError(CommandResult(args=args, returncode=1, stdout="", stderr="  define exploded  \n"))

    monkeypatch.setattr("libvirt_backup_system.restore_define.run", fake_run)

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, "alpha") is False
    captured = capsys.readouterr().err
    assert "define restored domain failed" in captured
    # The stripped stderr is what the JSON event captures.
    assert "define exploded" in captured


def test_define_restored_domain_returns_false_on_oserror_from_virsh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = _write_xml(tmp_path, _basic_domain())
    cfg = _config(monkeypatch)

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise OSError("virsh not installed")

    monkeypatch.setattr("libvirt_backup_system.restore_define.run", fake_run)

    assert define_restored_domain(cfg, xml_path, ALPHA_UUID, "alpha") is False
    assert "virsh define unavailable" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Constant
# ---------------------------------------------------------------------------


def test_restored_config_file_constant_is_stable() -> None:
    # The restore manifest writer pins this filename — guard against silent
    # rename.
    assert RESTORED_CONFIG_FILE == "libvirt-backup-system-restored.xml"
