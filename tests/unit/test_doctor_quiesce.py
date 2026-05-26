"""Tests for ``doctor._check_recent_quiesce_fallbacks``.

The check shells out to ``journalctl -u libvirt-backup-system.service`` and
parses the JSON-lines emitted by ``logging_json.event`` for the
``QGA quiesce failed`` warning. Tests drive monkeypatched journalctl output
to cover: no events, one VM, multiple VMs, malformed lines, journalctl
unavailable, and journalctl returning non-zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import doctor
from libvirt_backup_system.shell import CommandResult


def _quiesce_event(vm: str, ts: str) -> str:
    return json.dumps(
        {
            "ts": ts,
            "level": "warning",
            "message": doctor.QUIESCE_FALLBACK_MESSAGE,
            "vm": vm,
            "stderr": "guest agent unreachable",
        }
    )


def _stub_journalctl(monkeypatch: pytest.MonkeyPatch, *, stdout: str, returncode: int = 0) -> list[list[str]]:
    """Patch doctor.run so the journalctl invocation returns ``stdout``."""
    monkeypatch.setattr(doctor, "systemctl_available", lambda _root: True)
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> CommandResult:
        calls.append(args)
        return CommandResult(args, returncode, stdout, "")

    monkeypatch.setattr(doctor, "run", fake_run)
    return calls


def test_quiesce_check_skipped_when_systemctl_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "systemctl_available", lambda _root: False)
    assert doctor._check_recent_quiesce_fallbacks(tmp_path) == []


def test_quiesce_check_no_events_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_journalctl(monkeypatch, stdout="")
    assert doctor._check_recent_quiesce_fallbacks(Path("/")) == []
    assert calls[0][:2] == ["journalctl", "-u"]
    # Window MUST match the plan's 7-day promise so operators don't miss
    # fallbacks they cared about because doctor only looked at the last 24h.
    assert "--since" in calls[0]
    assert "7 days ago" in calls[0]


def test_quiesce_check_returns_clean_when_journalctl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cold install: journalctl present but no archive yet, returns rc=1. The
    # check MUST stay quiet so doctor still passes on a host that's never
    # run a backup.
    _stub_journalctl(monkeypatch, stdout="", returncode=1)
    assert doctor._check_recent_quiesce_fallbacks(Path("/")) == []


def test_quiesce_check_surfaces_single_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_journalctl(monkeypatch, stdout=_quiesce_event("vm-alpha", "2026-05-22T12:00:00.000000Z"))
    findings = doctor._check_recent_quiesce_fallbacks(Path("/"))
    assert findings == [
        "recent QGA quiesce fallback for vm-alpha (last seen 2026-05-22T12:00:00.000000Z);"
        " install qemu-guest-agent inside the VM"
    ]


def test_quiesce_check_surfaces_multiple_vms_sorted_with_last_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = "\n".join(
        [
            _quiesce_event("vm-beta", "2026-05-20T08:00:00.000000Z"),
            _quiesce_event("vm-alpha", "2026-05-21T08:00:00.000000Z"),
            _quiesce_event("vm-beta", "2026-05-22T08:00:00.000000Z"),
        ]
    )
    _stub_journalctl(monkeypatch, stdout=stdout)
    findings = doctor._check_recent_quiesce_fallbacks(Path("/"))
    # Alphabetical so operators see deterministic output across runs; the
    # vm-beta line MUST carry the most recent timestamp because the helper
    # is last-write-wins.
    assert findings[0].startswith("recent QGA quiesce fallback for vm-alpha")
    assert findings[1].startswith("recent QGA quiesce fallback for vm-beta")
    assert "2026-05-22T08:00:00.000000Z" in findings[1]


def test_quiesce_check_ignores_unrelated_warnings_and_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    # The marker substring inline forces the parser past the cheap reject
    # at the top of the loop so the JSONDecodeError and non-dict branches
    # are actually exercised. Plain "unrelated" lines also stay out of the
    # output.
    marker = doctor.QUIESCE_FALLBACK_MESSAGE
    stdout = "\n".join(
        [
            "not-json-at-all",
            json.dumps({"level": "info", "message": "unrelated"}),
            "[1, 2, 3]",  # JSON list — valid JSON but not an event record
            f"broken-{marker}-line",  # marker substring inline, malformed JSON
            f'[1, 2, "{marker}"]',  # marker as array element, valid JSON, not a dict
            _quiesce_event("vm-only", "2026-05-22T12:00:00.000000Z"),
            "",  # blank line
        ]
    )
    _stub_journalctl(monkeypatch, stdout=stdout)
    findings = doctor._check_recent_quiesce_fallbacks(Path("/"))
    assert len(findings) == 1
    assert "vm-only" in findings[0]


def test_quiesce_check_handles_unknown_vm_field(monkeypatch: pytest.MonkeyPatch) -> None:
    # Defensive: a malformed log entry that hits the QGA marker but lacks
    # the `vm` key should still surface so the operator sees something to
    # investigate, rather than silently dropping the fallback.
    record = json.dumps({"level": "warning", "message": doctor.QUIESCE_FALLBACK_MESSAGE, "ts": ""})
    _stub_journalctl(monkeypatch, stdout=record)
    findings = doctor._check_recent_quiesce_fallbacks(Path("/"))
    assert findings == ["recent QGA quiesce fallback for <unknown-vm>; install qemu-guest-agent inside the VM"]


def test_quiesce_check_skips_substring_match_in_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # The marker substring may appear inside another field (for example a
    # remediation hint quoting the warning). Only treat a record as a real
    # fallback when ``message`` matches exactly.
    record = json.dumps(
        {
            "level": "info",
            "message": "follow-up note",
            "vm": "vm-gamma",
            "context": doctor.QUIESCE_FALLBACK_MESSAGE,
        }
    )
    _stub_journalctl(monkeypatch, stdout=record)
    assert doctor._check_recent_quiesce_fallbacks(Path("/")) == []
