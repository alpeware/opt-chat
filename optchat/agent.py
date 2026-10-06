"""The Turn Loop for OptChat (§7).

Each user message starts a fresh model call (no conversation carried over).
Input layout:
  [MASTER + VIEW_DOC + AGENTS.md]
  [block 1: view with cache breakpoints]
  [block 2: new user message(s)]
Tools:
  zoom, date, and vendor/workspace tools
Logging:
  user: user messages
  talk: agent replies
  tool: agent tool calls (name + arguments)
  echo: tool results (capped at CAP)
Thoughts (reasoning) are displayed to user but NEVER logged to storage (§2).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import subprocess
from typing import Any, Callable, Dict, List, Optional

from optchat.compactor import Compactor
from optchat.constants import MASTER_PROMPT, VIEW_DOC_PROMPT
from optchat.providers.base import BaseLLMProvider, LLMResponse, StreamCallback
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.view import LiveView

logger = logging.getLogger("optchat.agent")


class TurnAgent:
    def __init__(
        self,
        storage: Storage,
        view: LiveView,
        compactor: Compactor,
        provider: BaseLLMProvider,
        tool_registry: ToolRegistry,
        agents_md_path: Optional[Path] = None,
        git_auto_commit: bool = True,
        ui_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.storage = storage
        self.view = view
        self.compactor = compactor
        self.provider = provider
        self.tool_registry = tool_registry
        self.agents_md_path = agents_md_path
        self.git_auto_commit = git_auto_commit
        self.ui_callback = ui_callback

        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.midrun_queue: asyncio.Queue[str] = asyncio.Queue()
        self.is_running_turn = False
        self._abort_settle = asyncio.Event()

    def _get_system_prompt(self) -> str:
        """Compose constant system prompt: MASTER + VIEW_DOC + AGENTS.md (§7.2).

        Byte-identical across calls for prompt caching (§8).
        """
        parts = [MASTER_PROMPT.strip(), VIEW_DOC_PROMPT.strip()]
        if self.agents_md_path and self.agents_md_path.is_file():
            try:
                with open(self.agents_md_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    parts.append(content)
            except Exception as e:
                logger.warning("Could not read AGENTS.md from %s: %e", self.agents_md_path, e)
        return "\n\n".join(parts)

    def emit_ui(self, event_type: str, content: str) -> None:
        if self.ui_callback:
            self.ui_callback(event_type, content)

    async def submit_user_message(self, text: str) -> None:
        """User input entry point (§7).

        If a turn is running, queues text for injection between tool calls.
        Otherwise, queues text and runs turn loop.
        """
        if self.is_running_turn:
            await self.midrun_queue.put(text)
        else:
            await self.queue.put(text)
            await self.run_turn_loop()

    async def run_turn_loop(self) -> None:
        if self.is_running_turn:
            return

        self.is_running_turn = True
        try:
            while not self.queue.empty():
                # 1. Wait until every line of view is a summary (§6)
                self._abort_settle.clear()
                self.compactor.pump()
                if not self.view.is_settled():
                    self.emit_ui("settling", "Compacting memory summaries before starting turn...")
                settled = await self.view.settle(abort_event=self._abort_settle)
                if not settled:
                    logger.info("Settle wait aborted or timed out. Turn skipped.")
                    break

                # 2. Collect all queued user messages
                texts: List[str] = []
                while not self.queue.empty():
                    texts.append(self.queue.get_nowait())
                if not texts:
                    break

                # 3. Render view BEFORE logging the new message(s) (§7)
                view_text = self.view.render()
                cache_pieces = self.view.render_with_cache_pieces()

                # 4. Log the new messages
                for t in texts:
                    msg = self.storage.append_message("user", t)
                    self.view.on_new_message(msg.i)
                    self.emit_ui("log_user", t)

                # 5. Start FRESH model call (§7)
                system_prompt = self._get_system_prompt()
                user_combined = "\n\n".join(texts)

                # Build two blocks: [view, new_message]
                initial_user_blocks: List[Dict[str, Any]] = [
                    {"type": "text", "text": view_text},
                    {"type": "text", "text": user_combined},
                ]

                # In-turn conversational steps (fresh session)
                call_messages: List[Dict[str, Any]] = [
                    {"role": "user", "content": initial_user_blocks}
                ]

                await self._execute_turn_call(system_prompt, call_messages)

                # 6. Commit / persist after turn (§10)
                if self.git_auto_commit:
                    self._git_commit()

        finally:
            self.is_running_turn = False
            self.emit_ui("turn_complete", "Turn finished.")

    async def _execute_turn_call(
        self,
        system_prompt: str,
        call_messages: List[Dict[str, Any]],
    ) -> None:
        """Run tool loop within a fresh turn call until agent produces final reply."""

        def stream_listener(event_type: str, content: str) -> None:
            # Broadcast to UI (e.g. streaming thoughts or tokens to terminal)
            self.emit_ui(event_type, content)

        max_steps = 30
        for _ in range(max_steps):
            response: LLMResponse = await self.provider.chat(
                system=system_prompt,
                messages=call_messages,
                tools=self.tool_registry.definitions,
                stream_callback=stream_listener,
            )

            # Note: thoughts (reasoning) are displayed to user above, NEVER logged (§2)

            if response.tool_calls:
                # Agent called tools
                assistant_blocks: List[Dict[str, Any]] = []
                if response.text:
                    assistant_blocks.append({"type": "text", "text": response.text})
                for call in response.tool_calls:
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                call_messages.append({"role": "assistant", "content": assistant_blocks})

                # Execute each tool call and log
                tool_results_blocks: List[Dict[str, Any]] = []
                for call in response.tool_calls:
                    tool_call_str = f"{call.name}: {json.dumps(call.arguments)}"
                    tool_msg = self.storage.append_message("tool", tool_call_str)
                    self.view.on_new_message(tool_msg.i)
                    self.emit_ui("log_tool", tool_call_str)

                    # Execute tool
                    result = await self.tool_registry.execute(
                        call.name, call.arguments, self.storage, self.view
                    )

                    echo_msg = self.storage.append_message("echo", result)
                    self.view.on_new_message(echo_msg.i)
                    self.emit_ui("log_echo", result)

                    tool_results_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": result,
                        }
                    )

                # Feed tool results back into model session
                call_messages.append({"role": "user", "content": tool_results_blocks})

                # Check if user sent a mid-run message between tool calls (§7)
                while not self.midrun_queue.empty():
                    mid_text = self.midrun_queue.get_nowait()
                    mid_msg = self.storage.append_message("user", mid_text)
                    self.view.on_new_message(mid_msg.i)
                    self.emit_ui("log_user_midrun", mid_text)
                    call_messages.append(
                        {
                            "role": "user",
                            "content": f"[User message sent during tool execution]:\n{mid_text}",
                        }
                    )

                # Trigger compactor in background as tool calls produce new log entries
                self.compactor.pump()
            else:
                # Final reply (talk)
                if response.text:
                    talk_msg = self.storage.append_message("talk", response.text)
                    self.view.on_new_message(talk_msg.i)
                    self.emit_ui("log_talk", response.text)
                self.compactor.pump()
                break

    def cancel_turn(self) -> None:
        """Cancel current wait/settle."""
        self._abort_settle.set()

    def _git_commit(self) -> None:
        """Auto-commit after turn (§10)."""
        chat_dir = self.storage.chat_dir
        try:
            subprocess.run(
                ["git", "add", "chat/"],
                cwd=str(chat_dir.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            subprocess.run(
                ["git", "commit", "-m", "OptChat auto-commit after turn"],
                cwd=str(chat_dir.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
