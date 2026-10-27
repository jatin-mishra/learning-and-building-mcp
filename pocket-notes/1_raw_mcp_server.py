"""Pocket Notes: an MCP server in raw Python. No libraries, just the stdlib."""

import json
import os
import sys
from pathlib import Path

# -------------- Exceptions ---------------
class ToolError(Exception):
    """A problem the model can fix"""


# -------------- Tool Definitions  --------------
TOOLS = [
    {
        "name": "add_note",
        "description": "Save a short note to the user's Pocket Notes. Use when the user asks to remember, save or note something.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The note to save"
                }
            },
            "required": [
                "text"
            ]
        }
    },
    {
        "name": "search_notes",
        "description": "Search the user's saved notes by keyword. Pass an empty query to list every note.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Word to look for"
                }
            },
            "required": [
                "query"
            ]
        }
    }
]

# -------------- configurations --------------

MODERN = "2026-07-28"                                 # stateless era
LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26"]   # handshake era

SUPPORTED = [MODERN] + LEGACY

SERVER_INFO = {
    "name": "pocket-notes", 
    "version": "1.0.0"
}

CAPABILITIES = {
    "tools": {}
}

NOTES_FILE = Path(os.environ.get("POCKET_NOTES_FILE", Path.home() / ".pocket_notes.json"))


def log(*parts):
    """Logs go to stderr. stdout is reserved for protocol messages only."""
    print("[pocket-notes]", *parts, file=sys.stderr, flush=True)


def send(message):
    """One JSON message per line on stdout. No pretty-printing, no extra prints."""
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


# -------------- Tool Functions --------------
from datetime import datetime, timezone

def load_notes():
    return json.loads(NOTES_FILE.read_text()) if NOTES_FILE.exists() else []

def add_note(args):
    text = str(args.get("text", "")).strip()
    if not text:
        raise ToolError("Note text is empty. Pass the note in the 'text' argument.")
    notes = load_notes()
    note = {
        "id": len(notes) + 1, 
        "text": text,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }
    notes.append(note)
    NOTES_FILE.write_text(json.dumps(notes, indent=2))
    return f"Saved note #{note['id']}: {text}"


def search_notes(args):
    query = str(args.get("query", "")).lower()
    hits = [n for n in load_notes() if query in n["text"].lower()]
    if not hits:
        return f"No notes match '{query}'."
    return "\n".join(f"#{n['id']} ({n['created'][:10]}): {n['text']}" for n in hits)

TOOL_FUNCTIONS = {"add_note": add_note, "search_notes": search_notes}


# -------------- Protocol Brain --------------

def handle_notifications(method, params):
    if method and params:
        log("received call: " + method + ", with params: " + params)


def  handle(mssg, state):
    """Turn one incoming message into one reply (or None for Notifications)"""

    mssg_id, method = mssg.get("id"), mssg.get("method")
    params = mssg.get("params") or {}

    if mssg_id is None:
        handle_notifications(method, params)
        return None

    def ok(result):
        return {
            "jsonrpc" : "2.0",
            "id" : mssg_id,
            "result" : result
        }

    def error(code, message, data=None):
        err = {
            "code" : code,
            "message" : message
        }
        if data is not None:
            err["data"] = data 
        return {
            "jsonrpc" : "2.0",
            "id" : mssg_id,
            "error" : err 
        }

    def route(method, params, modern=False):

        if method == "server/discover":
            return {
                "supportedVersions" : SUPPORTED,
                "capabilities" : CAPABILITIES,
                "instructions" : "Pocket Notes saves and searches the user's short notes. "
            }

        elif method == "ping" and not modern:
            return {}

        elif method == "tools/list":
            return {
                "tools" : TOOLS
            }

        elif method == "tools/call":
            name = params.get("name")
            
            if name not in TOOL_FUNCTIONS:
                return error(-32602, f"Unkown tool: {name}")

            try:
                text, is_error = TOOL_FUNCTIONS[name](params.get("arguments")) or {}, False
            except ToolError as problem:
                text, is_error = str(problem), True

            return {
                "content" : [
                    {
                        "type" : "text",
                        "text" : text
                    }
                ],
                "isError" : is_error
            }

        return error(-32601, f"method not found: {method}")


    # Era 1 (Legacy): the client opens the handshake

    if method == "initialize":
        asked = params.get("protocolVersion")
        state["legacy"] = asked if asked in LEGACY else LEGACY[0]
        log("legacy client, handshake on", state["legacy"])
        return ok({
            "protocolVersion" : state["legacy"],
            "capabilities": CAPABILITIES,
            "serverInfo" : SERVER_INFO
        })

    # Era 2 (New): No Handshake
    meta = params.get("_meta") or {}
    version = meta.get("io.modelcontextprotocol/protocolVersion")
    modern = False
    
    if version is not None:

        if version != MODERN:
            return error(-32022, "Unsupported protocol version", {"supported" : SUPPORTED, "requested" : version})

        if "io.modelcontextprotocol/clientCapabilities" not in meta:
            return error(-32602, "Missing _meta io.modelcontextprotocol/clientCapabilities")

        modern = True

    elif state.get("legacy"):

        modern = False

    else:
        return error(-32602, "Missing _meta. Send server/discover (2026-07-28) or initialize.")


    result = route(method, params, modern)

    if "error" in result:
        return result

    if modern:
        result["resultType"] = "complete" # input_required
        result["_meta"] = {"io.modelcontextprotocol/serverInfo": SERVER_INFO}
        if method in ("server/discover", "tools/list"):
            result.update(ttlMs=3_600_000, cacheScope="public")

    return ok(result)


def main():
    log("started, notes file: ", NOTES_FILE)
    state = { "legacy" : None }

    for line in sys.stdin:

        if not line.strip():
            continue 
        try:
            mssg = json.loads(line)
        except json.JSONDecodeError:
            send({
                "jsonrpc" : "2.0",
                "id" : None,
                "error" : {
                    "code" : -32700,
                    "message" : "Parse error"
                }
            })
            continue 

        reply = handle(mssg, state)

        if reply is not None:
            send(reply)

    log("stdin closed, bye")


if __name__ == "__main__":
    main()

    
    
            