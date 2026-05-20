from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore_define import define_restored_domain
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID


def test_define_restored_domain_preserves_uuid_and_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    xml_path = tmp_path / "restored.xml"
    xml_path.write_text("<domain><name>restore_alpha</name><devices /></domain>\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> CommandResult:
        calls.append(args)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore_define.run", fake_run)

    assert define_restored_domain(backup_config, xml_path, ALPHA_UUID, "alpha")
    root = ET.parse(xml_path).getroot()  # noqa: S314
    assert root.findtext("name") == "alpha"
    assert root.findtext("uuid") == ALPHA_UUID
    assert calls == [["virsh", "-c", backup_config.get("LIBVIRT_URI"), "define", str(xml_path)]]


def test_define_restored_domain_preserves_uuid_without_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    xml_path = tmp_path / "restored.xml"
    xml_path.write_text("<domain><devices /></domain>\n", encoding="utf-8")
    monkeypatch.setattr(
        "libvirt_backup_system.restore_define.run",
        lambda args, **_kwargs: CommandResult(args, 0, "", ""),
    )

    assert define_restored_domain(backup_config, xml_path, ALPHA_UUID, None)
    root = ET.parse(xml_path).getroot()  # noqa: S314
    assert root.find("name") is None
    assert root.findtext("uuid") == ALPHA_UUID


def test_define_restored_domain_reports_bad_xml(
    tmp_path: Path, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = tmp_path / "restored.xml"
    xml_path.write_text("<domain>", encoding="utf-8")

    assert not define_restored_domain(backup_config, xml_path, ALPHA_UUID, "alpha")
    assert "restore adjusted domain XML is unusable" in capsys.readouterr().err


def test_define_restored_domain_reports_wrong_xml_root(
    tmp_path: Path, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = tmp_path / "restored.xml"
    xml_path.write_text("<notdomain />", encoding="utf-8")

    assert not define_restored_domain(backup_config, xml_path, ALPHA_UUID, "alpha")
    assert "not a libvirt domain" in capsys.readouterr().err


def test_define_restored_domain_reports_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = tmp_path / "restored.xml"
    xml_path.write_text("<domain><name>alpha</name></domain>\n", encoding="utf-8")

    def fail_write(self: ET.ElementTree, file_or_filename: object, *args: object, **kwargs: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr(ET.ElementTree, "write", fail_write)

    assert not define_restored_domain(backup_config, xml_path, ALPHA_UUID, "alpha")
    assert "could not update adjusted domain XML" in capsys.readouterr().err


def test_define_restored_domain_reports_define_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = tmp_path / "restored.xml"
    xml_path.write_text("<domain><name>alpha</name></domain>\n", encoding="utf-8")

    def fail(args: list[str], **_kwargs: object) -> CommandResult:
        raise CommandError(CommandResult(args, 1, "", "uuid already in use"))

    monkeypatch.setattr("libvirt_backup_system.restore_define.run", fail)

    assert not define_restored_domain(backup_config, xml_path, ALPHA_UUID, "alpha")
    assert "define restored domain failed" in capsys.readouterr().err


def test_define_restored_domain_reports_virsh_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    xml_path = tmp_path / "restored.xml"
    xml_path.write_text("<domain><name>alpha</name></domain>\n", encoding="utf-8")

    def missing(args: list[str], **_kwargs: object) -> CommandResult:
        raise FileNotFoundError(2, "virsh")

    monkeypatch.setattr("libvirt_backup_system.restore_define.run", missing)

    assert not define_restored_domain(backup_config, xml_path, ALPHA_UUID, "alpha")
    assert "virsh define unavailable" in capsys.readouterr().err
