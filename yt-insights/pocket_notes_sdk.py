"""Pocket Notes again, rebuilt on the official MCP Python SDK."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("pocket-notes", version="2.0.0")
NOTES_FILE = Path(os.environ.get("POCKET_NOTES_FILE", Path.home() / ".pocket_notes.json"))


def load_notes() -> list[dict]:
    return json.loads(NOTES_FILE.read_text()) if NOTES_FILE.exists() else []


@mcp.tool()
def add_note(text: str) -> str:
    """Save a short note to the user's Pocket Notes.
    Use when the user asks to remember, save or note something."""
    if not text.strip():
        raise ToolError("Note text is empty. Pass the note in the 'text' argument.")
    notes = load_notes()
    note = {"id": len(notes) + 1, "text": text.strip(),
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    NOTES_FILE.write_text(json.dumps(notes + [note], indent=2))
    return f"Saved note #{note['id']}: {note['text']}"


@mcp.tool()
def search_notes(query: str) -> str:
    """Search the user's saved notes by keyword. Pass an empty query to list every note."""
    hits = [n for n in load_notes() if query.lower() in n["text"].lower()]
    if not hits:
        return f"No notes match '{query}'."
    return "\n".join(f"#{n['id']} ({n['created'][:10]}): {n['text']}" for n in hits)


if __name__ == "__main__":
    mcp.run()
