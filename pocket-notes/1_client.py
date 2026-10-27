#!/usr/bin/env python3
"""A tiny MCP client in raw Python: start the server, talk JSON, print replies."""

import json
import subprocess
import sys
from pathlib import Path

server = subprocess.Popen([sys.executable, str(Path(__file__).with_name("1_raw_mcp_server.py"))],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "tiny-client", "version": "0.1"}}

def request(req_id, method, params=None):
    params = {**(params or {}), "_meta": META}      # every request carries its context
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": req_id,
                                   "method": method, "params": params}) + "\n")
    server.stdin.flush()
    return json.loads(server.stdout.readline())["result"]

print()
print()
print()
print("Versions:", request(1, "server/discover")["supportedVersions"])
print()
print()
print()
print("Tools:", [tool["name"] for tool in request(2, "tools/list")["tools"]])
print()
print()
print()

print(request(3, "tools/call", {"name": "add_note", "arguments": {"text": "Buy milk"}})["content"][0]["text"])
print()
print()
print()

print(request(4, "tools/call", {"name": "search_notes", "arguments": {"query": "milk"}})["content"][0]["text"])
print()
print()
print()

server.stdin.close()
server.wait()