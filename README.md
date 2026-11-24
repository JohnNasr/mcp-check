# mcp-check

**Automated tests for MCP servers, made for CI.**

The official MCP Inspector is a manual GUI: great for poking at a server, but it can't run on every
push. `mcp-check` starts your server, speaks the protocol to it, checks every tool definition, runs
sample calls you define, and **fails the build when you ship a breaking change** to a tool's schema.

No API key, no dependencies, plain Python. Works with any stdio server in any language (Node, Python, Go...).

```
$ mcp-check -- node server.mjs
my-server 1.2.0  protocol 2025-06-18, started in 0.31s, 7 tools (~1,240 tokens of schema)
ERROR MC002 stdout: 1 non-JSON line(s) on stdout, e.g. 'Server listening...'. Real clients break on this: log to stderr instead.
ERROR MC012 tool search: Parameter 'limit' is now required. Breaking: old calls omit it.
WARN  MC005 tool fetch: Missing or very short description. The model picks tools by description, so it may never use this one.
INFO  MC008 tool delete_item: No annotations. Set readOnlyHint / destructiveHint so clients know whether to ask before running it.

FAIL  2 errors, 1 warnings, 1 hints
```

## Install

```powershell
pip install git+https://github.com/JohnNasr/mcp-check
```

## Quick check

Put your server's start command after `--`:

```powershell
mcp-check -- node server.mjs
mcp-check -- python my_server.py
mcp-check -- npx -y @modelcontextprotocol/server-everything
```

## Full setup: sample calls and breaking-change detection

Create `mcp-check.json` next to your server:

```json
{
  "command": ["node", "server.mjs"],
  "env": { "LOG_LEVEL": "warn" },
  "timeout": 30,
  "snapshot": "mcp-snapshot.json",
  "cases": [
    { "tool": "add", "args": { "a": 2, "b": 3 }, "expect": { "contains": "5" } },
    { "tool": "search", "args": { "query": "mcp" }, "expect": { "regex": "results?", "max_seconds": 2 } },
    { "tool": "add", "args": { "a": "oops" }, "expect": { "error": true } }
  ]
}
```

Then run `mcp-check`. The first run writes `mcp-snapshot.json` (the shape of every tool); commit it.
After that, each run compares the live server to the snapshot and fails on breaking changes. When a change
is intended, accept it with `mcp-check --update-snapshot`.

`expect` supports `contains`, `regex`, `error` (true when the call should fail) and `max_seconds`.
Paths in the config are relative to the config file. Keep secrets out of it: `env` values are added to
your current environment, so set real tokens in CI secrets instead.

## What it checks

| Rule | Level | Check |
|---|---|---|
| MC001 | error/warn | The server starts and completes the `initialize` handshake; slow startup (> 5s) warns. |
| MC002 | error | Anything that isn't JSON-RPC printed on stdout. This silently breaks Claude, Cursor and other clients. |
| MC003 | warn | Tool names outside `[A-Za-z0-9_.-]{1,64}`, which some clients reject. |
| MC004 | error | Duplicate tool names. |
| MC005 | warn | Missing or very short tool descriptions. |
| MC006 | error/warn/info | `inputSchema` problems: not an object, `required` naming a missing property, untyped or undocumented parameters. |
| MC007 | warn | More than 40 tools (clients get worse at choosing). Also reports schema size in tokens. |
| MC008 | info | No `annotations` (`readOnlyHint`, `destructiveHint`...). |
| MC009 | warn/error | `ping` fails, or an advertised `resources`/`prompts` list fails. Pagination is followed. |
| MC010 | error/warn | Calling a tool that doesn't exist crashes or hangs the server, or doesn't return an error. |
| MC011 | error | A sample call in `cases` fails its expectation. |
| MC012 | error/info | Breaking changes against the snapshot: removed tools or parameters, newly required parameters, changed types. |

## In GitHub Actions

```yaml
- uses: actions/setup-python@v5
  with: { python-version: "3.12" }
- run: pip install git+https://github.com/JohnNasr/mcp-check
- run: mcp-check --fail-on warn
```

`--json` prints a machine-readable report, `--quiet` hides hints, `--fail-on error|warn|never` sets the exit code.

## Limits

- stdio servers only for now (that's what most local servers use). Streamable HTTP is not covered yet.
- The snapshot compares top-level parameters. Changes deep inside nested objects aren't flagged.
- It runs the tools you list in `cases` for real. Point it at a test setup, not production data.

## Development

```powershell
pip install -e .[dev]
pytest
```

Tests drive `tests/mock_server.py`, a tiny MCP server with switchable flaws.

MIT licensed.
