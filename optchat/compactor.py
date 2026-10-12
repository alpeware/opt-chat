"""Background compactor worker for OptChat and UniiChat (§4).

Builds binary summary tree nodes using a cheap model:
- Free nodes for short messages and short merges (< 512 bytes)
- Order: messages compressed in order (rule 3: end <= first(mem))
- 512-dash ruler for scale without sample-token contamination
- Conversational retry enforcing 512-byte limit with cut limit marker
- Uses dedicated 16KB-32KB CompactionView to minimize token usage
- Ready-queue based node scheduling to avoid O(N²) tree scans
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
from typing import Deque, Dict, List, Optional, Set, Tuple

from optchat.constants import (
    COMPACT_PROMPT,
    JOBS,
    NODE,
    RETRY,
    RULER,
    TRIES,
    UNIFIED_PROMPT,
)
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

        # Ready queue of (level, index, end)
        self.ready_queue: Deque[Tuple[int, int, int]] = deque()
        self._queued: Set[Tuple[int, int]] = set()

    def start(self) -> None:
        """Start or kick the pump."""
        self._is_stopped = False
        self._seed_ready_queue()
        self.pump()

    def stop(self) -> None:
        """Stop running tasks."""
        self._is_stopped = True
        for task in list(self._running_tasks):
            task.cancel()
        self._running_tasks.clear()
        self.busy.clear()
        self.ready_queue.clear()
        self._queued.clear()

    def _seed_ready_queue(self) -> None:
        """Queue all currently unbuilt nodes whose predecessors are ready."""
        total_messages = len(self.storage.messages)
        if total_messages == 0:
            return

        first_unbuilt = self.view.first_unbuilt_message()

        l = 0
        while (1 << l) <= total_messages:
            node_span = 1 << l
            i = 0
            while (i + 1) * node_span <= total_messages:
                node_key = (l, i)
                if not self.storage.has_node(l, i) and node_key not in self.busy and node_key not in self._queued:
                    if self._is_ready(l, i):
                        end = i if l == 0 else (i + 1) * node_span
                        if end <= first_unbuilt:
                            self.ready_queue.append((l, i, end))
                            self._queued.add(node_key)
                i += 1
            l += 1

    def _check_and_enqueue_dependents(self, l: int, i: int) -> None:
        """Enqueue parent merge node if both sibling children are now built."""
        parent_l = l + 1
        parent_i = i // 2
        parent_key = (parent_l, parent_i)

        if not self.storage.has_node(parent_l, parent_i) and parent_key not in self.busy and parent_key not in self._queued:
            if self._is_ready(parent_l, parent_i):
                node_span = 1 << parent_l
                end = (parent_i + 1) * node_span
                first_unbuilt = self.view.first_unbuilt_message()
                if end <= first_unbuilt:
                    self.ready_queue.append((parent_l, parent_i, end))
                    self._queued.add(parent_key)

    def pump(self) -> None:
        """Start ready nodes from the queue up to concurrency limit (UniiChat §4)."""
        if self._is_stopped:
            return

        if not self.ready_queue:
            self._seed_ready_queue()

        while len(self.busy) < self.concurrency and self.ready_queue:
            l, i, end = self.ready_queue.popleft()
            node_key = (l, i)
            self._queued.discard(node_key)

            if self.storage.has_node(l, i) or node_key in self.busy:
                continue

            if not self._is_ready(l, i):
                continue

            self.busy.add(node_key)
            task = asyncio.create_task(self._build_node(l, i, end))
            self._running_tasks.add(task)
            task.add_done_callback(self._running_tasks.discard)

    def _is_ready(self, l: int, i: int) -> bool:
        if l == 0:
            return self.storage.get_message(i) is not None
        # Level > 0 requires both children built (§4.1)
        child_a_built = self.storage.has_node(l - 1, 2 * i)
        child_b_built = self.storage.has_node(l - 1, 2 * i + 1)
        return child_a_built and child_b_built

    async def _build_node(self, l: int, i: int, end: int) -> None:
        try:
            # 1. Check free nodes (§3, UniiChat §2)
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

            # 2. Build model input blocks (UniiChat §4)
            # Uses dedicated 16KB-32KB CompactionView
            context_block = self.view.render_compactor_context(end)

            if l == 0:
                msg = self.storage.get_message(i)
                assert msg is not None
                ws_tag = f"[{msg.workspace}] " if msg.workspace else ""
                step_block = (
                    f"Compaction: compress message {i} into one line of at most 512 bytes "
                    f"(about 70 words), the length of this ruler:\n"
                    f"{RULER}\n"
                    f"<input>\n"
                    f"{msg.kind}: {ws_tag}{msg.text}\n"
                    f"</input>"
                )
            else:
                child_a = self.storage.get_node(l - 1, 2 * i)
                child_b = self.storage.get_node(l - 1, 2 * i + 1)
                assert child_a is not None and child_b is not None
                n = 1 << l
                start_id = i * n
                end_id = start_id + n
                step_block = (
                    f"Compaction: merge lines {2 * i} and {2 * i + 1}, adjacent, into one line of at most "
                    f"512 bytes (about 70 words), the length of this ruler:\n"
                    f"{RULER}\n"
                    f"<chat> may hold their messages, {start_id} to {end_id}, in more detail: "
                    f"take details of them from there too.\n"
                    f"<input>\n"
                    f"{flatten_newlines(child_a.text)}\n"
                    f"{flatten_newlines(child_b.text)}\n"
                    f"</input>"
                )

            # 3. Conversational retry loop enforcing <= NODE bytes (UniiChat §4)
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
                    system=UNIFIED_PROMPT,
                    messages=conversation,
                )
                line = reply.strip()
                if not line:
                    raise RuntimeError("Empty response received from compactor model")

                tries.append(line)
                byte_len = len(line.encode("utf-8"))
                if byte_len <= NODE or len(tries) >= TRIES:
                    break

                # Show line cut where limit falls (UniiChat §4)
                cut_line = line.encode("utf-8")[:NODE].decode("utf-8", errors="ignore")
                retry_msg = (
                    f"Too long: your line is {byte_len} bytes, over the 512-byte limit. Write "
                    f"the whole line again for the same <input>, cutting just enough of the "
                    f"least valuable items to fit before this cut:\n"
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
                self.ready_queue.append((l, i, end))
                self._queued.add((l, i))
                self.pump()

    def _save_and_continue(self, l: int, i: int, text: str) -> None:
        self.storage.save_node(l, i, text)
        self.view.on_node_built()
        self.busy.discard((l, i))
        self.failed_reported.discard((l, i))
        self._check_and_enqueue_dependents(l, i)
        if not self._is_stopped:
            self.pump()
