"""Run every check against a live MCP server and collect findings."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .client import PROTOCOL_VERSION, MCPError, RPCError, StdioClient

TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # 64 is the limit several major clients enforce
MAX_TOOLS = 40
SLOW_START = 5.0


@dataclass
class Finding:
    rule: str
    severity: str  # error | warn | info
    where: str
    message: str


@dataclass
class Report:
    server: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    tools: list = field(default_factory=list)
    findings: list = field(default_factory=list)

    def add(self, rule, severity, where, message):
        self.findings.append(Finding(rule, severity, where, message))

    def to_dict(self):
        return {**asdict(self), "findings": [asdict(f) for f in self.findings]}

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)


def run(config: dict, update_snapshot: bool = False) -> Report:
    report = Report()
    start = time.perf_counter()
    try:
        client = StdioClient(config["command"], config.get("env"), config.get("cwd"), config.get("timeout", 30))
    except MCPError as e:
        report.add("MC001", "error", "startup", str(e))
        return report
    try:
        session(client, config, report, update_snapshot, start)
    except MCPError as e:
        report.add("MC001", "error", "protocol", str(e))
    finally:
        client.close()
    if client.stdout_noise:
        report.add("MC002", "error", "stdout", f"{len(client.stdout_noise)} non-JSON line(s) on stdout, "
                   f"e.g. {client.stdout_noise[0]!r}. Real clients break on this: log to stderr instead.")
    return report


def session(client: StdioClient, config: dict, report: Report, update_snapshot: bool, start: float) -> None:
    init, _ = client.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                            "clientInfo": {"name": "mcp-check", "version": "0.1.0"}})
    client.notify("notifications/initialized")
    report.timings["startup_s"] = round(time.perf_counter() - start, 3)
    report.server = {"name": init.get("serverInfo", {}).get("name"), "version": init.get("serverInfo", {}).get("version"),
                     "protocolVersion": init.get("protocolVersion")}
    caps = init.get("capabilities") or {}
    if report.timings["startup_s"] > SLOW_START:
        report.add("MC001", "warn", "startup", f"Took {report.timings['startup_s']:.1f}s to initialize. "
                   "Clients start servers at launch, so users wait for this.")

    try:
        client.request("ping")
    except MCPError as e:
        report.add("MC009", "warn", "ping", f"ping failed ({e}). The spec requires servers to answer ping.")

    tools = []
    if "tools" in caps:
        tools = list_all(client, "tools/list", "tools", report)
        check_tools(tools, report)
    for cap, method, key in (("resources", "resources/list", "resources"), ("prompts", "prompts/list", "prompts")):
        if cap in caps:
            try:
                list_all(client, method, key, report)
            except MCPError as e:
                report.add("MC009", "error", method, f"Advertised '{cap}' but {method} failed: {e}")

    if config.get("snapshot"):
        compare_snapshot(Path(config["snapshot"]), tools, report, update_snapshot)
    if not check_unknown_tool(client, report):
        return
    for i, case in enumerate(config.get("cases", []), 1):
        run_case(client, case, i, report)


def list_all(client: StdioClient, method: str, key: str, report: Report) -> list:
    items, cursor, total = [], None, 0.0
    for _ in range(100):  # pagination guard against a server that loops its cursor
        result, secs = client.request(method, {"cursor": cursor} if cursor else {})
        total += secs
        items += result.get(key) or []
        cursor = result.get("nextCursor")
        if not cursor:
            break
    report.timings[f"{method}_s"] = round(total, 3)
    return items


def check_tools(tools: list, report: Report) -> None:
    seen = set()
    schema_chars = 0
    for tool in tools:
        name = tool.get("name", "")
        where = f"tool {name or '?'}"
        report.tools.append(name)
        schema_chars += len(json.dumps(tool))
        if name in seen:
            report.add("MC004", "error", where, "Duplicate tool name. Clients will call the wrong one.")
        seen.add(name)
        if not TOOL_NAME.fullmatch(name):
            report.add("MC003", "warn", where, "Name should be 1-64 chars of letters, digits, _ . or -. "
                       "Some clients reject other names.")
        desc = (tool.get("description") or "").strip()
        if len(desc) < 20:
            report.add("MC005", "warn", where, "Missing or very short description. The model picks tools by "
                       "description, so it may never use this one.")
        check_schema(tool.get("inputSchema"), where, report)
        if not tool.get("annotations"):
            report.add("MC008", "info", where, "No annotations. Set readOnlyHint / destructiveHint so clients "
                       "know whether to ask before running it.")
    if len(tools) > MAX_TOOLS:
        report.add("MC007", "warn", "tools", f"{len(tools)} tools. Many clients degrade past ~{MAX_TOOLS}; "
                   "consider splitting the server.")
    report.timings["tool_schema_tokens"] = schema_chars // 4


def check_schema(schema, where: str, report: Report) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        report.add("MC006", "error", where, 'inputSchema must be a JSON Schema object with "type": "object".')
        return
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        report.add("MC006", "error", where, "inputSchema.properties must be an object.")
        return
    for req in schema.get("required") or []:
        if req not in props:
            report.add("MC006", "error", where, f"'{req}' is required but not in properties.")
    for pname, pschema in props.items():
        if not isinstance(pschema, dict):
            report.add("MC006", "error", where, f"Parameter '{pname}' schema is not an object.")
            continue
        if not any(k in pschema for k in ("type", "anyOf", "oneOf", "allOf", "enum", "const", "$ref")):
            report.add("MC006", "warn", where, f"Parameter '{pname}' has no type.")
        if not pschema.get("description"):
            report.add("MC006", "info", where, f"Parameter '{pname}' has no description.")


def check_unknown_tool(client: StdioClient, report: Report) -> bool:
    """A good server answers a bad call with an error, not a crash or a hang. Returns False if it died."""
    try:
        result, _ = client.request("tools/call", {"name": "__mcp_check_no_such_tool__", "arguments": {}})
        if not result.get("isError"):
            report.add("MC010", "warn", "robustness", "Calling a tool that doesn't exist did not return an error.")
    except RPCError:
        pass  # a JSON-RPC error reply is the correct answer
    except MCPError as e:
        report.add("MC010", "error", "robustness", f"Calling an unknown tool broke the server: {e}")
        return False
    return True


def result_text(result: dict) -> str:
    parts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
    if result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"]))
    return "\n".join(parts)


def run_case(client: StdioClient, case: dict, i: int, report: Report) -> None:
    tool, expect = case["tool"], case.get("expect", {})
    where = f"case {i} ({tool})"
    started = time.perf_counter()
    try:
        result, secs = client.request("tools/call", {"name": tool, "arguments": case.get("args", {})})
        text, is_error = result_text(result), bool(result.get("isError"))
    except RPCError as e:  # protocol-level error reply counts as an error result
        text, is_error, secs = str(e), True, time.perf_counter() - started
    except MCPError as e:
        report.add("MC011", "error", where, str(e))
        return
    report.timings[f"case_{i}_s"] = round(secs, 3)
    if is_error != bool(expect.get("error", False)):
        report.add("MC011", "error", where, f"Expected error={expect.get('error', False)}, got error={is_error}: {text[:200]!r}")
    if "contains" in expect and expect["contains"] not in text:
        report.add("MC011", "error", where, f"Output does not contain {expect['contains']!r}: {text[:200]!r}")
    if "regex" in expect and not re.search(expect["regex"], text):
        report.add("MC011", "error", where, f"Output does not match /{expect['regex']}/: {text[:200]!r}")
    if "max_seconds" in expect and secs > expect["max_seconds"]:
        report.add("MC011", "error", where, f"Took {secs:.2f}s (limit {expect['max_seconds']}s).")


def shape(tools: list) -> dict:
    return {t["name"]: {"description": t.get("description", ""), "inputSchema": t.get("inputSchema", {})}
            for t in tools if t.get("name")}


def compare_snapshot(path: Path, tools: list, report: Report, update: bool) -> None:
    current = shape(tools)
    existed = path.exists()
    if update or not existed:
        path.write_text(json.dumps({"tools": current}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        report.add("MC012", "info", str(path), f"Snapshot {'updated' if existed else 'written'} "
                   f"with {len(current)} tools. Commit it; later runs fail on breaking changes.")
        return
    old = json.loads(path.read_text(encoding="utf-8")).get("tools", {})
    for name in old.keys() - current.keys():
        report.add("MC012", "error", f"tool {name}", "Removed since the snapshot. Breaking: clients that call it will fail.")
    for name in current.keys() - old.keys():
        report.add("MC012", "info", f"tool {name}", "New since the snapshot.")
    for name in old.keys() & current.keys():
        diff_schema(name, old[name]["inputSchema"], current[name]["inputSchema"], report)


def diff_schema(name: str, old: dict, new: dict, report: Report) -> None:
    where = f"tool {name}"
    old_p, new_p = old.get("properties") or {}, new.get("properties") or {}
    old_r, new_r = set(old.get("required") or []), set(new.get("required") or [])
    for p in old_p.keys() - new_p.keys():
        report.add("MC012", "error", where, f"Parameter '{p}' was removed. Breaking.")
    for p in (new_r - old_r):
        report.add("MC012", "error", where, f"Parameter '{p}' is now required. Breaking: old calls omit it.")
    for p in old_p.keys() & new_p.keys():
        if old_p[p].get("type") != new_p[p].get("type"):
            report.add("MC012", "error", where, f"Parameter '{p}' changed type "
                       f"{old_p[p].get('type')} -> {new_p[p].get('type')}. Breaking.")
    for p in new_p.keys() - old_p.keys() - new_r:
        report.add("MC012", "info", where, f"New optional parameter '{p}'.")
