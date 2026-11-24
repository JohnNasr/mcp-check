import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .checks import run

COLORS = {"error": "\033[31m", "warn": "\033[33m", "info": "\033[36m"}
RESET, DIM, BOLD, GREEN = "\033[0m", "\033[2m", "\033[1m", "\033[32m"
EPILOG = """examples:
  mcp-check -- npx -y @modelcontextprotocol/server-everything
  mcp-check -- python server.py
  mcp-check --config mcp-check.json --fail-on warn
  mcp-check --config mcp-check.json --update-snapshot
"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = []
    if "--" in argv:
        i = argv.index("--")
        argv, command = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="mcp-check", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
                                 description="Test an MCP server: protocol, tool schemas, sample calls, breaking changes.")
    ap.add_argument("--config", help="JSON file with command, env, cases and snapshot (default: mcp-check.json if present)")
    ap.add_argument("--snapshot", help="snapshot file for breaking-change detection")
    ap.add_argument("--update-snapshot", action="store_true", help="rewrite the snapshot from the current server")
    ap.add_argument("--timeout", type=float, help="seconds to wait for each reply (default 30)")
    ap.add_argument("--fail-on", choices=["error", "warn", "never"], default="error")
    ap.add_argument("--quiet", action="store_true", help="hide info-level findings")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--version", action="version", version=f"mcp-check {__version__}")
    args = ap.parse_args(argv)

    config = {}
    cfg_path = Path(args.config or "mcp-check.json")
    if args.config or (not command and cfg_path.exists()):
        try:
            config = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"mcp-check: cannot read {cfg_path}: {e}", file=sys.stderr)
            return 2
        base = cfg_path.parent  # paths in the config are relative to the config file
        config["cwd"] = str(base / config.get("cwd", "."))
        if config.get("snapshot"):
            config["snapshot"] = str(base / config["snapshot"])
    if command:
        config["command"] = command
    if args.snapshot:
        config["snapshot"] = args.snapshot
    if args.timeout:
        config["timeout"] = args.timeout
    if not config.get("command"):
        ap.print_help()
        return 2

    report = run(config, update_snapshot=args.update_snapshot)
    shown = [f for f in report.findings if not (args.quiet and f.severity == "info")]
    if args.json:
        print(json.dumps({**report.to_dict(), "findings": [f.__dict__ for f in shown]}, indent=2))
    else:
        print_text(report, shown, color=sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    if args.fail_on == "never":
        return 0
    bad = {"error"} if args.fail_on == "error" else {"error", "warn"}
    return 1 if any(f.severity in bad for f in report.findings) else 0


def print_text(report, findings, color: bool) -> None:
    c = (lambda code, s: f"{code}{s}{RESET}") if color else (lambda code, s: s)
    s, t = report.server, report.timings
    if s:
        print(c(BOLD, f"{s.get('name') or 'server'} {s.get('version') or ''}".strip())
              + c(DIM, f"  protocol {s.get('protocolVersion')}, started in {t.get('startup_s', 0):.2f}s, "
                       f"{len(report.tools)} tools (~{t.get('tool_schema_tokens', 0):,} tokens of schema)"))
    for f in findings:
        print(f"{c(COLORS[f.severity], f.severity.upper().ljust(5))} {c(BOLD, f.rule)} {f.where}: {f.message}")
    counts = {sev: sum(f.severity == sev for f in report.findings) for sev in COLORS}
    verdict = c(GREEN, "PASS") if report.ok else c(COLORS["error"], "FAIL")
    print(f"\n{verdict}  {counts['error']} errors, {counts['warn']} warnings, {counts['info']} hints")


if __name__ == "__main__":
    sys.exit(main())
