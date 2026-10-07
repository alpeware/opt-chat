"""Google Antigravity CLI (agy) Provider for OptChat.

Uses the local `agy` binary to execute model turns and compactor steps without
requiring external API keys.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any, Dict, List, Optional

from optchat.providers.base import BaseLLMProvider, LLMResponse, StreamCallback, ToolCall, ToolDefinition

logger = logging.getLogger("optchat.providers.agy")


class AgyProvider(BaseLLMProvider):
    def __init__(
        self,
        model: str = "gemini-3.8-flash-medium",
        compact_model: str = "gemini-3.8-flash-low",
        agy_path: Optional[str] = None,
        timeout: float = 300.0,
        workspace: Optional[str] = None,
        conversation_id: Optional[str] = None,
    ):
        self.model = model
        self.compact_model = compact_model
        self.agy_path = agy_path or shutil.which("agy") or "/home/simonpure/.local/bin/agy"
        self.timeout = timeout
        self.workspace = str(Path(workspace).resolve()) if workspace else os.getcwd()
        self.conversation_id = conversation_id
        self.last_conversation_id: Optional[str] = conversation_id

        if not os.path.isfile(self.agy_path):
            raise FileNotFoundError(f"Antigravity CLI (agy) not found at {self.agy_path}")

    async def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolDefinition]] = None,
        cache_breakpoints: Optional[List[int]] = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> LLMResponse:
        """Execute chat turn using agy --output-format stream-json."""
        parts = [f"=== SYSTEM INSTRUCTIONS ===\n{system}\n"]

        if tools:
            tool_lines = ["=== AVAILABLE TOOLS ==="]
            for t in tools:
                tool_lines.append(f"Tool: {t.name}\nDescription: {t.description}\nParameters: {json.dumps(t.parameters)}\n")
            tool_lines.append(
                "When you need to call a tool, output the invocation cleanly, for example:\n"
                "  tool_name(param1=val1, param2=val2)\n"
                "or:\n"
                "  ```tool_call\n"
                '  {"name": "tool_name", "arguments": {...}}\n'
                "  ```\n"
            )
            parts.insert(1, "\n".join(tool_lines))

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                block_texts = []
                for b in content:
                    if isinstance(b, dict):
                        btype = b.get("type")
                        if btype == "tool_result":
                            block_texts.append(f"[Tool Result]: {b.get('content', '')}")
                        elif btype == "tool_use":
                            block_texts.append(f"[Tool Call]: {b.get('name')}({json.dumps(b.get('input', {}))})")
                        else:
                            block_texts.append(b.get("text", ""))
                    else:
                        block_texts.append(str(b))
                parts.append(f"=== {role.upper()} ===\n" + "\n".join(block_texts))
            else:
                parts.append(f"=== {role.upper()} ===\n{content}")

        full_prompt = "\n\n".join(parts)

        cmd = [
            self.agy_path,
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--model",
            self.model,
            "--output-format",
            "stream-json",
        ]
        if self.conversation_id:
            cmd.extend(["--conversation", self.conversation_id])

        env = {**os.environ, "OPTCHAT_DISABLE_MCP": "1"}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            cwd=self.workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        full_text_chunks: List[str] = []
        final_result_data: Dict[str, Any] = {}
        stderr_chunks: List[str] = []

        async def write_stdin():
            assert proc.stdin is not None
            try:
                proc.stdin.write(full_prompt.encode("utf-8"))
                await proc.stdin.drain()
            finally:
                proc.stdin.close()
                await proc.stdin.wait_closed()

        async def read_stdout():
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    event_data = json.loads(raw)
                    event_type = event_data.get("event")
                    if event_type == "init":
                        cid = event_data.get("conversation_id")
                        if cid:
                            self.last_conversation_id = cid
                            if not self.conversation_id:
                                self.conversation_id = cid
                    elif event_type == "step_update":
                        su = event_data.get("step_update", {})
                        delta = su.get("text_delta")
                        if delta:
                            full_text_chunks.append(delta)
                            if stream_callback:
                                stream_callback("text", delta)
                    elif event_type == "result":
                        res = event_data.get("result", {})
                        final_result_data.update(res)
                        cid = res.get("conversation_id") or event_data.get("conversation_id")
                        if cid:
                            self.last_conversation_id = cid
                            if not self.conversation_id:
                                self.conversation_id = cid
                except Exception:
                    pass

        async def read_stderr():
            assert proc.stderr is not None
            while True:
                err_line = await proc.stderr.readline()
                if not err_line:
                    break
                stderr_chunks.append(err_line.decode("utf-8", errors="replace"))

        try:
            await asyncio.wait_for(
                asyncio.gather(write_stdin(), read_stdout(), read_stderr()),
                timeout=self.timeout,
            )
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(f"agy turn timed out after {self.timeout}s")

        if proc.returncode != 0:
            err_text = "".join(stderr_chunks).strip()
            raise RuntimeError(f"agy process failed (code {proc.returncode}): {err_text}")

        response_text = final_result_data.get("response", "").strip() or "".join(full_text_chunks).strip()
        usage = final_result_data.get("usage", {})

        if stream_callback and not full_text_chunks and response_text:
            stream_callback("text", response_text)

        # Inspect if agy output requested a tool call (spawn, zoom, date, bash, etc.)
        tool_calls: List[ToolCall] = []
        if tools:
            import ast
            import re

            tool_names = {t.name.lower(): t.name for t in tools}

            # 1. Check for JSON block
            json_blocks = re.findall(r"```(?:tool_call|json)?\s*(\{.*?\})\s*```", response_text, re.DOTALL)
            for jb in json_blocks:
                try:
                    jdata = json.loads(jb)
                    jname = jdata.get("name") or jdata.get("tool")
                    jargs = jdata.get("arguments") or jdata.get("parameters") or {}
                    if jname and jname.lower() in tool_names:
                        rname = tool_names[jname.lower()]
                        tool_calls.append(ToolCall(id=f"agy_{rname}_json", name=rname, arguments=jargs))
                except Exception:
                    pass

            # 2. Check for functional syntax if no json blocks matched
            if not tool_calls:
                pattern = r"(\b\w+)\s*\((.*?)\)"
                for match in re.finditer(pattern, response_text, re.DOTALL):
                    fn = match.group(1).lower()
                    if fn in tool_names:
                        rname = tool_names[fn]
                        args_str = match.group(2).strip()
                        args: Dict[str, Any] = {}

                        if rname == "zoom":
                            nums = re.findall(r"\d+", args_str)
                            if len(nums) >= 2:
                                args = {"id": int(nums[0]), "n": int(nums[1])}
                        elif rname == "date":
                            nums = re.findall(r"\d+", args_str)
                            if nums:
                                args = {"id": int(nums[0])}
                        else:
                            try:
                                expr = ast.parse(f"{rname}({args_str})").body[0].value  # type: ignore
                                for kw in expr.keywords:
                                    args[kw.arg] = ast.literal_eval(kw.value)
                                if not expr.keywords and expr.args:
                                    val = ast.literal_eval(expr.args[0])
                                    if rname == "spawn":
                                        args["tasks"] = val if isinstance(val, list) else [str(val)]
                                    elif rname == "bash":
                                        args["command"] = str(val)
                                    elif rname in ("read_file", "write_file", "edit_file"):
                                        args["path"] = str(val)
                            except Exception:
                                if rname == "spawn":
                                    quoted = re.findall(r"['\"](.*?)['\"]", args_str)
                                    if quoted:
                                        args["tasks"] = quoted

                        if args:
                            tool_calls.append(ToolCall(id=f"agy_{rname}_call", name=rname, arguments=args))

        return LLMResponse(
            text=response_text,
            reasoning=None,
            tool_calls=tool_calls,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cache_read_tokens", 0),
        )

    async def compact_step(
        self,
        system: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        """Execute compactor step using agy."""
        parts = [
            "CRITICAL REQUIREMENT: Output ONLY the single summary line directly. "
            "Do NOT call any tools. Do NOT run commands or scripts. "
            "Do NOT read or write files. Do NOT write intro or outro. "
            "Output only plain text.\n",
            f"System:\n{system}\n",
        ]
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                block_texts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content]
                parts.append(f"{role}:\n" + "\n".join(block_texts))
            else:
                parts.append(f"{role}:\n{content}")

        full_prompt = "\n\n".join(parts)

        cmd = [
            self.agy_path,
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--model",
            self.compact_model,
            "--output-format",
            "json",
        ]

        env = {**os.environ, "OPTCHAT_DISABLE_MCP": "1"}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode("utf-8")),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError("agy compactor step timed out")

        out_text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"agy compactor failed (code {proc.returncode}): {err_text}")

        try:
            data = json.loads(out_text)
            return data.get("response", "").strip()
        except Exception:
            import re
            m = re.search(r"\{.*\}", out_text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                    return data.get("response", "").strip()
                except Exception:
                    pass
            return out_text
