"""Background compactor worker for OptChat (§4).

Builds binary summary tree nodes using a cheap model:
- Free nodes for short messages and short merges (< 512 bytes)
- Order: messages compressed in order (rule 3: end <= first(mem))
- Conversational retry enforcing 512-byte limit with cut limit marker
- Constant COMPACT prompt with SCALE example
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, List, Optional, Set, Tuple

from optchat.constants import COMPACT_PROMPT, JOBS, NODE, RETRY, SCALE, TRIES
from optchat.providers.base import BaseLLMProvider
from optchat.storage import Storage
from optchat.tree import check_free_level0, check_free_merge, flatten_newlines
from optchat.view import LiveView

logger = logging.getLogger("optchat.compactor")


class Compactor:
    def __init__(
        self,
        storage: Storage,
        view: LiveView,
        provider: BaseLLMProvider,
        concurrency: int = JOBS,
        retry_delay: float = RETRY,
    ):
        self.storage = storage
        self.view = view
        self.provider = provider
        self.concurrency = concurrency
        self.retry_delay = retry_delay

        self.busy: Set[Tuple[int, int]] = set()
        self.failed_reported: Set[Tuple[int, int]] = set()
        self._running_tasks: Set[asyncio.Task[None]] = set()
        self._is_stopped = False

    def start(self) -> None:
        """Start or kick the pump."""
        self._is_stopped = False
        self.pump()

    def stop(self) -> None:
        """Stop running tasks."""
        self._is_stopped = True
        for task in list(self._running_tasks):
            task.cancel()
        self._running_tasks.clear()
        self.busy.clear()

    def pump(self) -> None:
        """Scan levels and start all eligible nodes (§4.1)."""
        if self._is_stopped:
            return

        total_messages = len(self.storage.messages)
        if total_messages == 0:
            return

        first_unbuilt = self.view.first_unbuilt_message()

        l = 0
        while (1 << l) <= total_messages:
            node_span = 1 << l
            i = 0
            while (i + 1) * node_span <= total_messages:
                if len(self.busy) >= self.concurrency:
                    return

                # Rule 3: end = i for level 0, (i+1)*2^l for merge
                end = i if l == 0 else (i + 1) * node_span

                if self.storage.has_node(l, i) or (l, i) in self.busy:
                    i += 1
                    continue

                if not self._is_ready(l, i):
                    i += 1
                    continue

                if end > first_unbuilt:
                    i += 1
                    continue

                # Eligible!
                self.busy.add((l, i))
                task = asyncio.create_task(self._build_node(l, i, end))
                self._running_tasks.add(task)
                task.add_done_callback(self._running_tasks.discard)

                i += 1
            l += 1

    def _is_ready(self, l: int, i: int) -> bool:
        if l == 0:
            return self.storage.get_message(i) is not None
        # Level > 0 requires both children built (§4.1)
        child_a_built = self.storage.has_node(l - 1, 2 * i)
        child_b_built = self.storage.has_node(l - 1, 2 * i + 1)
        return child_a_built and child_b_built

    async def _build_node(self, l: int, i: int, end: int) -> None:
        try:
            # 1. Check free nodes (§3)
            if l == 0:
                msg = self.storage.get_message(i)
                assert msg is not None
                free_text = check_free_level0(msg)
                if free_text is not None:
                    self._save_and_continue(l, i, free_text)
                    return
            else:
                child_a = self.storage.get_node(l - 1, 2 * i)
                child_b = self.storage.get_node(l - 1, 2 * i + 1)
                assert child_a is not None and child_b is not None
                free_text = check_free_merge(child_a.text, child_b.text)
                if free_text is not None:
                    self._save_and_continue(l, i, free_text)
                    return

            # 2. Build model input blocks (§4.2)
            context_block = self.view.render_compactor_context(end)

            if l == 0:
                msg = self.storage.get_message(i)
                assert msg is not None
                ws_tag = f"[{msg.workspace}] " if msg.workspace else ""
                step_block = (
                    f"For scale, this line is exactly 512 bytes:\n{SCALE}\n\n"
                    f"Compress this message into one line, in at most 512 bytes:\n"
                    f"{msg.kind}: {ws_tag}{msg.text}"
                )
            else:
                child_a = self.storage.get_node(l - 1, 2 * i)
                child_b = self.storage.get_node(l - 1, 2 * i + 1)
                assert child_a is not None and child_b is not None
                step_block = (
                    f"For scale, this line is exactly 512 bytes:\n{SCALE}\n\n"
                    f"Merge these two lines into one, in at most 512 bytes:\n"
                    f"{flatten_newlines(child_a.text)}\n"
                    f"{flatten_newlines(child_b.text)}"
                )

            # 3. Conversational retry loop enforcing <= NODE bytes (§4.3)
            conversation: List[Dict[str, Any]] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": context_block},
                        {"type": "text", "text": step_block},
                    ],
                }
            ]

            tries: List[str] = []
            while len(tries) < TRIES:
                reply = await self.provider.compact_step(
                    system=COMPACT_PROMPT,
                    messages=conversation,
                )
                line = reply.strip()
                if not line:
                    raise RuntimeError("Empty response received from compactor model")

                tries.append(line)
                byte_len = len(line.encode("utf-8"))
                if byte_len <= NODE or len(tries) >= TRIES:
                    break

                # Show line cut where limit falls (§4.3)
                cut_line = line.encode("utf-8")[:NODE].decode("utf-8", errors="ignore")
                retry_msg = (
                    f"That line is {byte_len} bytes; the limit is 512. "
                    f"It must end where it is cut here:\n"
                    f"{cut_line}| ← LIMIT"
                )
                conversation.append({"role": "assistant", "content": line})
                conversation.append({"role": "user", "content": retry_msg})

            best_line = min(tries, key=lambda s: len(s.encode("utf-8")))
            self._save_and_continue(l, i, best_line)

        except Exception as err:
            if (l, i) not in self.failed_reported:
                logger.error("Compactor failed for node (%d, %d): %s", l, i, err)
                self.failed_reported.add((l, i))

            # Retry after RETRY delay (§4.1)
            await asyncio.sleep(self.retry_delay)
            self.busy.discard((l, i))
            if not self._is_stopped:
                self.pump()

    def _save_and_continue(self, l: int, i: int, text: str) -> None:
        self.storage.save_node(l, i, text)
        self.view.on_node_built()
        self.busy.discard((l, i))
        self.failed_reported.discard((l, i))
        if not self._is_stopped:
            self.pump()
