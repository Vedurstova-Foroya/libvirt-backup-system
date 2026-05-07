from __future__ import annotations

import stat
from pathlib import Path

from tools import gates, install_hooks


def test_run_merges_environment(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(args: list[str], cwd: Path, env: dict[str, str], check: bool) -> object:
        seen.update({"args": args, "cwd": cwd, "env": env, "check": check})
        return type("Proc", (), {"returncode": 7})()

    monkeypatch.setattr("tools.gates.subprocess.run", fake_run)
    assert gates.run(["cmd"], env={"A": "B"}) == 7
    assert seen["args"] == ["cmd"]
    assert seen["cwd"] == gates.ROOT
    assert seen["check"] is False
    assert isinstance(seen["env"], dict)
    assert seen["env"]["A"] == "B"
    assert gates.run(["cmd"]) == 7


def test_text_files_filters_dirs_files_and_binary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("tools.gates.ROOT", tmp_path)
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("lock\n", encoding="utf-8")
    (tmp_path / "directory").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/ignored").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\xff")
    assert [path.name for path in gates.text_files()] == ["ok.py"]


def test_check_max_loc_pass_and_fail(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("tools.gates.ROOT", tmp_path)
    (tmp_path / "short.py").write_text("x\n", encoding="utf-8")
    assert gates.check_max_loc() == 0
    assert "LOC gate passed" in capsys.readouterr().out

    (tmp_path / "long.py").write_text("x\n" * 301, encoding="utf-8")
    assert gates.check_max_loc() == 1
    assert "max is 300" in capsys.readouterr().err


def test_main_runs_fix_and_check_paths(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("tools.gates.check_max_loc", lambda: 0)

    def fake_run(args: list[str], *, env: dict[str, str] | None = None) -> int:
        assert env
        assert env["PYRIGHT_PYTHON_FORCE_VERSION"] == "1.1.390"
        assert env["PYRIGHT_PYTHON_IGNORE_WARNINGS"] == "1"
        calls.append(args)
        return 0

    monkeypatch.setattr("tools.gates.run", fake_run)
    assert gates.main(["--fix"]) == 0
    assert calls[0][-2:] == ["format", "."]
    assert calls[-1][-2:] == ["-m", "tests.e2e"]


def test_main_stops_on_failed_command(monkeypatch) -> None:
    monkeypatch.setattr("tools.gates.check_max_loc", lambda: (_ for _ in ()).throw(AssertionError("unreached")))
    monkeypatch.setattr("tools.gates.run", lambda args, env=None: 9)
    assert gates.main([]) == 9


def test_main_returns_loc_failure(monkeypatch) -> None:
    monkeypatch.setattr("tools.gates.run", lambda args, env=None: 0)
    monkeypatch.setattr("tools.gates.check_max_loc", lambda: 5)
    assert gates.main([]) == 5


def test_install_hooks(tmp_path: Path, monkeypatch, capsys) -> None:
    source = tmp_path / "source/pre-push"
    source.parent.mkdir()
    source.write_text("#!/bin/sh\n", encoding="utf-8")
    target = tmp_path / ".git/hooks/pre-push"
    monkeypatch.setattr("tools.install_hooks.ROOT", tmp_path)
    monkeypatch.setattr("tools.install_hooks.HOOK_SOURCE", source)
    monkeypatch.setattr("tools.install_hooks.HOOK_TARGET", target)

    assert install_hooks.main() == 0
    assert target.read_text(encoding="utf-8") == "#!/bin/sh\n"
    assert target.stat().st_mode & stat.S_IXUSR
    assert "installed .git/hooks/pre-push" in capsys.readouterr().out
