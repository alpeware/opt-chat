"""Subagent and background task management for OptChat (§9).

Provides:
  - spawn(tasks): parallel execution of subagents with isolated private tool loops
  - tell(id, message): sending mid-turn guidance to active subagents
  - Delivery of completed reports to the master turn loop formatted as '[id] report'
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional

from optchat.constants import SUBAGENT_PROMPT, VIEW_DOC_PROMPT
from optchat.providers.base import BaseLLMProvider, LLMResponse, ToolCall
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.view import LiveView

logger = logging.getLogger("optchat.subagent")


@dataclass
class SubagentTask:
    id: str
    task: str
    status: str = "running"  # "running", "completed", "failed"
    report: Optional[str] = None
    input_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    created_at: float = field(default_factory=time.time)


@dataclass
class SubagentGroup:
    group_id: str
    tasks: List[SubagentTask]


class SubagentManager:
    """Manages background subagents according to OptChat technical specification (§9)."""

    def __init__(
        self,
        storage: Storage,
        view: LiveView,
        provider: BaseLLMProvider,
        base_tools: ToolRegistry,
        agents_md_path: Optional[Path] = None,
        on_report: Optional[Callable[[str], None]] = None,
        ui_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.storage = storage
        self.view = view
        self.provider = provider
        self.base_tools = base_tools
        self.agents_md_path = agents_md_path
        self.on_report = on_report
        self.ui_callback = ui_callback

        self._counter = 0
        self.active_tasks: Dict[str, SubagentTask] = {}
        self._background_jobs: set[asyncio.Task[Any]] = set()

    def emit_ui(self, event_type: str, content: str) -> None:
        if self.ui_callback:
            self.ui_callback(event_type, content)

    def _get_subagent_system_prompt(self) -> str:
        parts = [SUBAGENT_PROMPT.strip(), VIEW_DOC_PROMPT.strip()]
        if self.agents_md_path and self.agents_md_path.is_file():
            try:
                with open(self.agents_md_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    parts.append(content)
            except Exception as e:
                logger.warning("Could not read AGENTS.md: %s", e)
        return "\n\n".join(parts)

    def spawn(self, tasks: List[str]) -> str:
        """Spawn subagents for the given tasks (§9).

        Returns confirmation string immediately with subagent IDs.
        """
        if not tasks:
            return "No tasks specified for spawn."

        spawned_tasks: List[SubagentTask] = []
        for t_desc in tasks:
            self._counter += 1
            sub_id = f"sub-{self._counter}"
            task_obj = SubagentTask(id=sub_id, task=str(t_desc).strip())
            self.active_tasks[sub_id] = task_obj
            spawned_tasks.append(task_obj)

        group = SubagentGroup(group_id=f"group-{self._counter}", tasks=spawned_tasks)
        view_snapshot = self.view.render()

        # Launch background execution
        job = asyncio.create_task(self._run_group(group, view_snapshot))
        self._background_jobs.add(job)
        job.add_done_callback(self._background_jobs.discard)

        ids_str = ", ".join(t.id for t in spawned_tasks)
        self.emit_ui("subagent_spawn", f"Spawned background subagent(s): {ids_str}")
        return f"Spawned {len(spawned_tasks)} subagent(s): {ids_str}. They are running in the background."

    def tell(self, sub_id: str, message: str) -> str:
        """Deliver a message to a running subagent between tool calls (§9)."""
        sub_task = self.active_tasks.get(sub_id)
        if not sub_task or sub_task.status != "running":
            return f"Subagent {sub_id} is not currently running."

        sub_task.input_queue.put_nowait(message)
        return f"Message delivered to subagent {sub_id}."

    async def _run_subagent(self, task: SubagentTask, view_snapshot: str) -> str:
        """Execute a single subagent's private tool loop (§9).

        Tool calls run in its own private session and are NOT logged to main memory.
        """
        system_prompt = self._get_subagent_system_prompt()
        call_messages: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"=== CURRENT VIEW (CONTEXT ONLY) ===\n{view_snapshot}\n\n"
                    f"=== YOUR TASK ===\n{task.task}"
                ),
            }
        ]

        # Subagents get operational tools (zoom, date, bash, read_file, write_file, edit_file)
        # but NOT spawn or tell
        from optchat.tools import create_subagent_registry

        subagent_tools = create_subagent_registry()

        max_steps = 15
        for _ in range(max_steps):
            try:
                response: LLMResponse = await self.provider.chat(
                    system=system_prompt,
                    messages=call_messages,
                    tools=subagent_tools.definitions,
                )
            except Exception as err:
                logger.error("Subagent %s failed model call: %s", task.id, err)
                task.status = "failed"
                task.report = f"Subagent error during model turn: {err}"
                return task.report

            if response.tool_calls:
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

                # Execute tool calls privately (NOT saved to main storage)
                tool_results_blocks: List[Dict[str, Any]] = []
                for call in response.tool_calls:
                    result = await subagent_tools.execute(
                        call.name, call.arguments, self.storage, self.view
                    )
                    tool_results_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": result,
                        }
                    )

                call_messages.append({"role": "user", "content": tool_results_blocks})

                # Check if master agent sent mid-run tell messages
                while not task.input_queue.empty():
                    tell_msg = task.input_queue.get_nowait()
                    call_messages.append(
                        {
                            "role": "user",
                            "content": f"[Message from OptChat]:\n{tell_msg}",
                        }
                    )
            else:
                # Final response is the report
                report_text = response.text.strip() if response.text else "Completed task with no final message."
                task.report = report_text
                task.status = "completed"
                return report_text

        task.report = "Subagent reached maximum step limit."
        task.status = "completed"
        return task.report

    async def _run_group(self, group: SubagentGroup, view_snapshot: str) -> None:
        """Run all subagents in the spawn group and deliver combined report (§9)."""
        await asyncio.gather(
            *[self._run_subagent(t, view_snapshot) for t in group.tasks],
            return_exceptions=True,
        )

        # When all finish, combine into one message formatted as '[id] report' each (§9)
        reports = []
        for t in group.tasks:
            rep = t.report or "Task ended without report."
            reports.append(f"[{t.id}] {rep}")

        combined_report = "\n\n".join(reports)
        self.emit_ui("subagent_complete", f"Subagent(s) {', '.join(t.id for t in group.tasks)} completed.")

        if self.on_report:
            self.on_report(combined_report)
