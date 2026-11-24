"""A tiny stdio MCP server for tests. Flags switch on specific flaws:

  --noisy             print a log line to stdout (breaks real clients)
  --bad-schema        a tool whose inputSchema is broken
  --crash-on-unknown  exit when asked for a tool that doesn't exist
  --v2                a breaking change: `add` makes `c` required and `b` a string; `echo` removed
"""
import json
import sys

FLAGS = set(sys.argv[1:])

TOOLS = [
    {"name": "add", "description": "Add two integers and return the sum.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "integer", "description": "first"},
                                                      "b": {"type": "integer", "description": "second"}},
                     "required": ["a", "b"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "echo", "description": "Echo the given text back unchanged.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "text"}},
                     "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
]
if "--v2" in FLAGS:
    TOOLS = [TOOLS[0]]
    TOOLS[0]["inputSchema"]["properties"]["b"]["type"] = "string"
    TOOLS[0]["inputSchema"]["properties"]["c"] = {"type": "integer", "description": "third"}
    TOOLS[0]["inputSchema"]["required"] = ["a", "b", "c"]
if "--bad-schema" in FLAGS:
    TOOLS.append({"name": "bad tool!", "description": "x", "inputSchema": {"type": "object", "properties": {"q": {}},
                                                                            "required": ["missing"]}})


def reply(rid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": rid}
    msg.update({"error": error} if error else {"result": result})
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


if "--noisy" in FLAGS:
    print("Server starting up...", flush=True)

for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if rid is None:
        continue  # notification
    if method == "initialize":
        reply(rid, {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mock", "version": "1.0"}})
    elif method == "ping":
        reply(rid, {})
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments") or {}
        if name == "add":
            reply(rid, {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]})
        elif name == "echo":
            reply(rid, {"content": [{"type": "text", "text": args.get("text", "")}]})
        elif "--crash-on-unknown" in FLAGS:
            sys.exit(3)
        else:
            reply(rid, error={"code": -32602, "message": f"Unknown tool: {name}"})
    else:
        reply(rid, error={"code": -32601, "message": "Method not found"})
