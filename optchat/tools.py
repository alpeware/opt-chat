"""Agent tools for OptChat (§7.1, §9).

Core memory tools:
  - zoom(id, n): open line id+n into children or verbatim message
  - date(id): date and time of message id
Standard operational tools:
  - bash(command): run shell command
  - read_file(path, offset, limit): read file text
  - write_file(path, content): create or overwrite file
  - edit_file(path, target, replacement): search and replace in file
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

from optchat.constants import CAP
from optchat.providers.base import ToolDefinition
from optchat.storage import Storage, cap_tool_output
from optchat.tree import execute_date, execute_zoom
from optchat.view import LiveView

ToolHandler = Callable[[Dict[str, Any], Storage, LiveView], Coroutine[Any, Any, str]]


ZOOM_TOOL = ToolDefinition(
    name="zoom",
    description="Open the line id+n of the view into the two lines of n/2 under it; n = 1 gives the message whole.",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "Message id at the start of the line."},
            "n": {"type": "integer", "description": "Number of messages covered by the line (power of 2)."},
        },
        "required": ["id", "n"],
    },
)

DATE_TOOL = ToolDefinition(
    name="date",
    description="The date and time of message id.",
    parameters={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "description": "Message id to query."},
        },
        "required": ["id"],
    },
)

BASH_TOOL = ToolDefinition(
    name="bash",
    description="Execute a bash shell command in the workspace and return output.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command line string to run."},
        },
        "required": ["command"],
    },
)

READ_FILE_TOOL = ToolDefinition(
    name="read_file",
    description="Read file contents from local filesystem.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read."},
            "offset": {"type": "integer", "description": "Starting line number (1-indexed). Optional.", "default": 1},
            "limit": {"type": "integer", "description": "Maximum lines to read. Optional.", "default": 500},
        },
        "required": ["path"],
    },
)

WRITE_FILE_TOOL = ToolDefinition(
    name="write_file",
    description="Write content to a file, creating parent directories if needed.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Target file path."},
            "content": {"type": "string", "description": "Content to write."},
        },
        "required": ["path", "content"],
    },
)

EDIT_FILE_TOOL = ToolDefinition(
    name="edit_file",
    description="Replace target string in a file with replacement string.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to file to edit."},
            "target": {"type": "string", "description": "Exact text to replace."},
            "replacement": {"type": "string", "description": "Replacement text."},
        },
        "required": ["path", "target", "replacement"],
    },
)


async def handle_zoom(args: Dict[str, Any], storage: Storage, view: LiveView) -> str:
    id_ = int(args.get("id", 0))
    n = int(args.get("n", 1))
    return execute_zoom(storage, id_, n)


async def handle_date(args: Dict[str, Any], storage: Storage, view: LiveView) -> str:
    id_ = int(args.get("id", 0))
    return execute_date(storage, id_)


async def handle_bash(args: Dict[str, Any], storage: Storage, view: LiveView) -> str:
    cmd = args.get("command", "")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out_text = stdout.decode("utf-8", errors="replace")
        err_text = stderr.decode("utf-8", errors="replace")
        ret_code = proc.returncode

        res = f"Exit code: {ret_code}\n"
        if out_text:
            res += f"Stdout:\n{out_text}\n"
        if err_text:
            res += f"Stderr:\n{err_text}\n"
        return cap_tool_output(res.strip(), CAP)
    except Exception as e:
        return f"Execution error: {e}"


async def handle_read_file(args: Dict[str, Any], storage: Storage, view: LiveView) -> str:
    path = Path(args.get("path", ""))
    offset = int(args.get("offset", 1))
    limit = int(args.get("limit", 500))

    if not path.is_file():
        return f"File not found: {path}"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        total_lines = len(lines)
        start_idx = max(offset - 1, 0)
        end_idx = min(start_idx + limit, total_lines)
        slice_lines = lines[start_idx:end_idx]
        formatted = "".join(f"{start_idx + idx + 1}: {line}" for idx, line in enumerate(slice_lines))
        return cap_tool_output(formatted, CAP)
    except Exception as e:
        return f"Error reading file {path}: {e}"


async def handle_write_file(args: Dict[str, Any], storage: Storage, view: LiveView) -> str:
    path = Path(args.get("path", ""))
    content = args.get("content", "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content.encode('utf-8'))} bytes to {path}."
    except Exception as e:
        return f"Error writing file {path}: {e}"


async def handle_edit_file(args: Dict[str, Any], storage: Storage, view: LiveView) -> str:
    path = Path(args.get("path", ""))
    target = args.get("target", "")
    replacement = args.get("replacement", "")

    if not path.is_file():
        return f"File not found: {path}"

    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if target not in text:
            return f"Target string not found in {path}."
        new_text = text.replace(target, replacement, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
        return f"Successfully edited {path}."
    except Exception as e:
        return f"Error editing {path}: {e}"


class ToolRegistry:
    def __init__(self) -> None:
        self.definitions: List[ToolDefinition] = []
        self.handlers: Dict[str, ToolHandler] = {}
        self.register(ZOOM_TOOL, handle_zoom)
        self.register(DATE_TOOL, handle_date)
        self.register(BASH_TOOL, handle_bash)
        self.register(READ_FILE_TOOL, handle_read_file)
        self.register(WRITE_FILE_TOOL, handle_write_file)
        self.register(EDIT_FILE_TOOL, handle_edit_file)

    def register(self, defn: ToolDefinition, handler: ToolHandler) -> None:
        self.definitions.append(defn)
        self.handlers[defn.name] = handler

    async def execute(self, name: str, args: Dict[str, Any], storage: Storage, view: LiveView) -> str:
        handler = self.handlers.get(name)
        if not handler:
            return f"Unknown tool: {name}"
        try:
            return await handler(args, storage, view)
        except Exception as e:
            return f"Tool {name} error: {e}"
