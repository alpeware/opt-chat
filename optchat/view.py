"""Live view management for OptChat and UniiChat (§3, §4, §8).

Maintains a bounded memory view over the chat [0, T) using Victor Taelin's
sawtooth batch compaction (64KB -> 128KB).
Between batch merges, the view ONLY appends to its end, keeping the prefix
100% stable and prompt-cached across turns.
When passing VIEW_MAX (128KB), a single batch merge collapses the most due
pairs down to VIEW_MIN (64KB).

Also provides a dedicated CompactionView (16KB -> 32KB) so compactions do
not waste tokens carrying full workspace history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, List, Optional, Tuple

from optchat.constants import (
    COMPACT_VIEW_MAX,
    COMPACT_VIEW_MIN,
    MARKS,
    VIEW,
    VIEW_MAX,
    VIEW_MIN,
)
from optchat.storage import Storage
from optchat.tree import Part, flatten_newlines

logger = logging.getLogger("optchat.view")


class LiveView:
    def __init__(
        self,
        storage: Storage,
        budget: int = VIEW_MAX,
        min_budget: Optional[int] = None,
        view_path: Optional[Path] = None,
    ):
        self.storage = storage
        self.budget = budget
        # Default min_budget is 64KB (or half budget if customized)
        self.min_budget = (
            min_budget
            if min_budget is not None
            else min(VIEW_MIN, budget // 2 if budget < VIEW_MAX else VIEW_MIN)
        )
        self.parts: List[Part] = []
        self._settle_event = asyncio.Event()

        self.view_path = Path(view_path or (self.storage.chat_dir / "view.json")).resolve()
        self._compaction_view: Optional[CompactionView] = None

    @property
    def compaction_view(self) -> CompactionView:
        if self._compaction_view is None:
            self._compaction_view = CompactionView(self.storage)
        return self._compaction_view

    def compute_size(self) -> int:
        """Calculate total byte size of text of all parts in the view (§5.2)."""
        return sum(part.get_text_bytes(self.storage) for part in self.parts)

    def on_new_message(self, i: int) -> None:
        """Called when a new message i is logged (UniiChat §3.2).
        
        Between batches, the view only grows at its end.
        Once the view passes 128,000 bytes, one batch merges the most due
        pairs until it is at most 64,000 bytes.
        """
        self.parts.append(Part(0, i))
        if self.compute_size() > self.budget:
            self.fit(target=self.min_budget)
        self.save_view()
        if self._compaction_view is not None:
            self._compaction_view.on_new_message(i)
        self._notify_settle()

    def on_node_built(self) -> None:
        """Called when any tree node is saved (§5.2)."""
        if self.compute_size() > self.budget:
            self.fit(target=self.min_budget)
            self.save_view()
        if self._compaction_view is not None:
            self._compaction_view.on_node_built()
        self._notify_settle()

    def fit(self, target: Optional[int] = None) -> None:
        """Coarsen view until size <= target (or budget), merging the most due pair with a built parent.
        
        Uses Victor's corrected formula (UniiChat §3.2):
          due = (T - last) / 2^l        last: the pair's last message
        """
        limit = target if target is not None else self.budget
        total_messages = len(self.storage.messages)
        size = self.compute_size()

        while size > limit:
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
                        last = b.end  # Pair's last message index
                        # Victor's formula: (T - last) / 2^l
                        due = (total_messages - last) / (1 << a.l)
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

    def save_view(self, path: Optional[Path] = None) -> None:
        """Persist view as [l, i] pairs to view.json (UniiChat §1, §3.2)."""
        target_path = Path(path or self.view_path)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target_path.with_suffix(".tmp")
            data = [[p.l, p.i] for p in self.parts]
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp_path, target_path)
        except Exception as e:
            logger.warning("Could not save view to %s: %s", target_path, e)

    def load_view(self, path: Optional[Path] = None) -> bool:
        """Load view from view.json (UniiChat §3.2). Never rebuild if valid on disk."""
        target_path = Path(path or self.view_path)
        if not target_path.is_file():
            return False
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                return False
            loaded_parts = []
            for item in data:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    loaded_parts.append(Part(int(item[0]), int(item[1])))
                else:
                    return False
            self.parts = loaded_parts
            self._notify_settle()
            return True
        except Exception as e:
            logger.warning("Could not load view from %s: %s", target_path, e)
            return False

    def rebuild(self) -> None:
        """Reconstruct view at startup. Loads view.json if present, catching up to new messages."""
        total = len(self.storage.messages)
        if self.load_view():
            changed = False
            # Check if new messages were appended since view.json was saved
            if self.parts and self.parts[-1].end < total:
                start_i = self.parts[-1].end
                for i in range(start_i, total):
                    self.parts.append(Part(0, i))
                changed = True
            if self.compute_size() > self.budget:
                self.fit(target=self.min_budget)
                changed = True
            if changed:
                self.save_view()
            self._notify_settle()
            return

        # Otherwise build from scratch
        self.parts.clear()
        for i in range(total):
            self.parts.append(Part(0, i))
            if self.compute_size() > self.budget:
                self.fit(target=self.min_budget)
        self.save_view()
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
        """Render context block for compactor (UniiChat §4).
        
        Delegates to the 16KB-32KB CompactionView.
        """
        if not self.compaction_view.parts:
            self.compaction_view.rebuild()
        return self.compaction_view.render_for_node(end_message_idx)

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


class CompactionView(LiveView):
    """Dedicated compaction context view (UniiChat §4).
    
    Merged further to 16-32 KB (24 KB average) with the same sawtooth.
    Merged down to 16 KB once, then new lines are appended.
    Merged again once it passes 32 KB.
    """
    def __init__(
        self,
        storage: Storage,
        budget: int = COMPACT_VIEW_MAX,
        min_budget: int = COMPACT_VIEW_MIN,
        view_path: Optional[Path] = None,
    ):
        vpath = view_path or (storage.chat_dir / "compaction_view.json")
        super().__init__(
            storage=storage,
            budget=budget,
            min_budget=min_budget,
            view_path=vpath,
        )

    def render_for_node(self, end_message_idx: int) -> str:
        """Render context block for a compaction node up to end_message_idx (UniiChat §4).
        
        The view ends at the node: it holds the lines before the message,
        or for a merge the lines up to the merge's last message. Only built lines are in it.
        A compaction's view stops at the first unbuilt line, so no call ever sees a placeholder.
        """
        lines: List[str] = []
        for part in self.parts:
            if part.end > end_message_idx:
                break
            node = self.storage.get_node(part.l, part.i)
            if node is None:
                # Stop at the first unbuilt line
                break
            lines.append(flatten_newlines(node.text))
        body = "\n".join(lines)
        if body:
            return f"<chat>\n{body}\n</chat>"
        return "<chat>\n</chat>"
