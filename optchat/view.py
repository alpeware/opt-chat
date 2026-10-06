"""Live view management for OptChat (§5, §6, §8).

The view tiles the entire chat [0, T) as a list of parts, kept under VIEW budget.
Incremental folding: merges the most 'due' adjacent pair whose parent is built.
Provides settle wait, rendering, compactor context rendering, and cache marks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple

from optchat.constants import MARKS, VIEW
from optchat.storage import Storage
from optchat.tree import Part, flatten_newlines

logger = logging.getLogger("optchat.view")


class LiveView:
    def __init__(self, storage: Storage, budget: int = VIEW):
        self.storage = storage
        self.budget = budget
        self.parts: List[Part] = []
        self._settle_event = asyncio.Event()

    def rebuild(self) -> None:
        """Reconstruct view at startup by simulating append + fit for all messages (§5.2)."""
        self.parts.clear()
        total = len(self.storage.messages)
        for i in range(total):
            self.parts.append(Part(0, i))
            self.fit()
        self._notify_settle()

    def on_new_message(self, i: int) -> None:
        """Called when a new message i is logged (§5.2)."""
        self.parts.append(Part(0, i))
        self.fit()

    def on_node_built(self) -> None:
        """Called when any tree node is saved (§5.2)."""
        self.fit()

    def compute_size(self) -> int:
        """Calculate total byte size of text of all parts in the view (§5.2)."""
        return sum(part.get_text_bytes(self.storage) for part in self.parts)

    def fit(self) -> None:
        """Coarsen view until size <= VIEW, merging the most due pair with a built parent (§5.2)."""
        total_messages = len(self.storage.messages)
        size = self.compute_size()

        while size > self.budget:
            best_due: float = -1.0
            best_idx: int = -1
            best_parent: Optional[Tuple[int, int]] = None

            # Scan adjacent pairs
            for idx in range(len(self.parts) - 1):
                a = self.parts[idx]
                b = self.parts[idx + 1]

                # Match binary pair rule: same level, a.i even, b.i == a.i + 1
                if a.l == b.l and (a.i % 2 == 0) and (b.i == a.i + 1):
                    parent_l = a.l + 1
                    parent_i = a.i // 2
                    if self.storage.has_node(parent_l, parent_i):
                        start = a.i * (1 << a.l)
                        weight = 1 << (a.l + 2)  # 2^(l+2)
                        due = (total_messages - start) / weight
                        if due > best_due:
                            best_due = due
                            best_idx = idx
                            best_parent = (parent_l, parent_i)

            if best_parent is None or best_idx < 0:
                # No eligible pair has its parent built; wait until one is built
                break

            # Replace the pair with parent
            parent_part = Part(best_parent[0], best_parent[1])
            self.parts[best_idx : best_idx + 2] = [parent_part]
            size = self.compute_size()

        self._notify_settle()

    def _notify_settle(self) -> None:
        if self.is_settled():
            self._settle_event.set()
        else:
            self._settle_event.clear()

    def is_settled(self) -> bool:
        """Check if all parts in the view have their summaries built (§6)."""
        for part in self.parts:
            if not self.storage.has_node(part.l, part.i):
                return False
        return True

    async def settle(self, abort_event: Optional[asyncio.Event] = None, timeout: Optional[float] = None) -> bool:
        """Wait until every line of the view is a summary before starting (§6).

        Returns True if settled, False if aborted or timed out.
        """
        if self.is_settled():
            return True

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout is not None else None

        while not self.is_settled():
            if abort_event is not None and abort_event.is_set():
                return False

            remaining = None
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return self.is_settled()

            try:
                if abort_event is not None:
                    # Wait for either settle or abort
                    wait_tasks = [
                        asyncio.create_task(self._settle_event.wait()),
                        asyncio.create_task(abort_event.wait()),
                    ]
                    done, pending = await asyncio.wait(
                        wait_tasks,
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    if abort_event.is_set():
                        return False
                else:
                    await asyncio.wait_for(self._settle_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return self.is_settled()

        return True

    def first_unbuilt_message(self) -> int:
        """First message whose view line is unbuilt (§4.1)."""
        for part in self.parts:
            if not self.storage.has_node(part.l, part.i):
                return part.id
        return len(self.storage.messages)

    def render(self) -> str:
        """Render view inside <chat> tags, one 'id+n|text' per line (§5.1)."""
        lines = [part.render(self.storage) for part in self.parts]
        body = "\n".join(lines)
        if body:
            return f"<chat>\n{body}\n</chat>"
        return "<chat>\n</chat>"

    def render_compactor_context(self, end_message_idx: int) -> str:
        """Render context block for compactor (§4.2).

        NO IDS: view lines are shown bare (text only, one per line).
        Covers lines up to end_message_idx wrapped in <chat> ... </chat>.
        """
        lines: List[str] = []
        for part in self.parts:
            if part.end <= end_message_idx:
                node = self.storage.get_node(part.l, part.i)
                if node is not None:
                    lines.append(flatten_newlines(node.text))
        body = "\n".join(lines)
        if body:
            return f"<chat>\n{body}\n</chat>"
        return "<chat>\n</chat>"

    def render_with_cache_pieces(self, marks: Tuple[int, ...] = MARKS) -> List[str]:
        """Render view split into pieces at line endings before cache marks (§8).

        Converts the full rendered view into cache-marked pieces for API calls.
        """
        text = self.render()
        pieces: List[str] = []
        curr = 0

        for mark in marks:
            if mark < len(text) and mark > curr:
                idx = text.rfind("\n", curr, mark + 1)
                if idx != -1 and idx >= curr:
                    pieces.append(text[curr : idx + 1])
                    curr = idx + 1

        if curr < len(text):
            pieces.append(text[curr:])
        elif not pieces and text:
            pieces.append(text)

        return pieces
