import json
import sys
from pathlib import Path

from mcpcheck.checks import run
from mcpcheck.cli import main

SERVER = str(Path(__file__).parent / "mock_server.py")


def check(*flags, **config):
    report = run({"command": [sys.executable, SERVER, *flags], "timeout": 10, **config})
    return report, {(f.rule, f.severity) for f in report.findings}


def test_healthy_server_passes_cleanly():
    report, found = check()
    assert report.ok and report.findings == []
    assert report.tools == ["add", "echo"]
    assert report.server["name"] == "mock"


def test_stdout_noise_is_an_error():
    report, found = check("--noisy")
    assert ("MC002", "error") in found and not report.ok


def test_bad_schema_and_name():
    _, found = check("--bad-schema")
    assert {("MC003", "warn"), ("MC005", "warn"), ("MC006", "error"), ("MC006", "warn"), ("MC008", "info")} <= found


def test_crash_on_unknown_tool():
    report, found = check("--crash-on-unknown")
    assert ("MC010", "error") in found


def test_cases_pass_and_fail():
    cases = [
        {"tool": "add", "args": {"a": 2, "b": 3}, "expect": {"contains": "5"}},
        {"tool": "echo", "args": {"text": "hi there"}, "expect": {"regex": "^hi"}},
        {"tool": "nope", "expect": {"error": True}},
        {"tool": "add", "args": {"a": 1, "b": 1}, "expect": {"contains": "3"}},
    ]
    report, _ = check(cases=cases)
    failed = [f.where for f in report.findings if f.rule == "MC011"]
    assert failed == ["case 4 (add)"]


def test_snapshot_detects_breaking_changes(tmp_path):
    snap = tmp_path / "snap.json"
    report, _ = check(snapshot=str(snap))
    assert snap.exists() and report.ok
    report, _ = check("--v2", snapshot=str(snap))
    msgs = [f.message for f in report.findings if f.rule == "MC012" and f.severity == "error"]
    assert any("echo" in f.where for f in report.findings if f.rule == "MC012")
    assert any("'c' is now required" in m for m in msgs)
    assert any("'b' changed type integer -> string" in m for m in msgs)
    assert not report.ok


def test_missing_command():
    report = run({"command": ["definitely-not-a-real-binary-xyz"]})
    assert report.findings[0].rule == "MC001" and not report.ok


def test_cli_config_file_and_exit_codes(tmp_path, capsys):
    cfg = tmp_path / "mcp-check.json"
    cfg.write_text(json.dumps({"command": [sys.executable, SERVER, "--noisy"], "timeout": 10}), encoding="utf-8")
    assert main(["--config", str(cfg)]) == 1
    assert main(["--config", str(cfg), "--fail-on", "never"]) == 0
    capsys.readouterr()
    assert main(["--json", "--", sys.executable, SERVER]) == 0
    assert json.loads(capsys.readouterr().out)["tools"] == ["add", "echo"]
