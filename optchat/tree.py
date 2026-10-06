"""Tree and binary indexing logic for OptChat (§3, §7.1).

Binary summaries:
  node(0, i) = message i, in <= NODE bytes
  node(l, i) = merge(node(l-1, 2i), node(l-1, 2i+1)), in <= NODE bytes
  covers(l, i) = [i * 2^l, (i+1) * 2^l)
  addressing: id+n where id = i * 2^l, n = 2^l
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

from optchat.constants import NODE, PLACEHOLDER
from optchat.storage import Message, Storage, TreeNode


def is_power_of_two(n: int) -> bool:
    return n > 0 and (n & (n - 1)) == 0


def node_coords(id_: int, n: int) -> Tuple[int, int]:
    """Convert (id, n) address to (level, index) coordinates."""
    if not is_power_of_two(n):
        raise ValueError(f"n must be a power of 2, got {n}")
    if id_ % n != 0:
        raise ValueError(f"id must be a multiple of n, got id={id_}, n={n}")
    l = int(math.log2(n))
    i = id_ // n
    return l, i


def node_address(l: int, i: int) -> Tuple[int, int]:
    """Convert (level, index) to (id, n) address."""
    n = 1 << l
    id_ = i * n
    return id_, n


def node_covers(l: int, i: int) -> Tuple[int, int]:
    """Return range of message indices [start, end) covered by node (l, i)."""
    n = 1 << l
    start = i * n
    end = start + n
    return start, end


def flatten_newlines(text: str) -> str:
    """Replace newlines with spaces for view line rendering (§5.1)."""
    return " ".join(text.splitlines())


def check_free_level0(msg: Message) -> Optional[str]:
    """Free node at level 0: verbatim text if <= NODE bytes (§3)."""
    raw = f"{msg.kind}: {msg.text}"
    if len(raw.encode("utf-8")) <= NODE:
        return raw
    return None


def check_free_merge(child_a_text: str, child_b_text: str) -> Optional[str]:
    """Free node at level > 0: childA + '\\n' + childB if <= NODE bytes (§3)."""
    merged = f"{child_a_text}\n{child_b_text}"
    if len(merged.encode("utf-8")) <= NODE:
        return merged
    return None


class Part:
    """A tile in the view covering messages [i * 2^l, (i+1) * 2^l)."""
    __slots__ = ("l", "i")

    def __init__(self, l: int, i: int):
        self.l = l
        self.i = i

    @property
    def id(self) -> int:
        return self.i * (1 << self.l)

    @property
    def n(self) -> int:
        return 1 << self.l

    @property
    def end(self) -> int:
        return (self.i + 1) * (1 << self.l)

    def render(self, storage: Storage) -> str:
        """Render part as 'id+n|text' with newlines replaced by spaces (§5.1)."""
        node = storage.get_node(self.l, self.i)
        if node is not None:
            text = flatten_newlines(node.text)
        else:
            text = PLACEHOLDER
        return f"{self.id}+{self.n}|{text}"

    def get_text_bytes(self, storage: Storage) -> int:
        """Byte length of text for view budget calculation (§5.2)."""
        node = storage.get_node(self.l, self.i)
        if node is not None:
            return node.size
        return len(PLACEHOLDER.encode("utf-8"))

    def __repr__(self) -> str:
        return f"Part(l={self.l}, i={self.i}, id={self.id}, n={self.n})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Part):
            return False
        return self.l == other.l and self.i == other.i

    def __hash__(self) -> int:
        return hash((self.l, self.i))


def execute_zoom(storage: Storage, id_: int, n: int) -> str:
    """Execute zoom(id, n) tool (§7.1).

    If n == 1: returns id+0|kind: message text (whole, newlines kept)
    Else: returns the two children rendered as id+n|text
    """
    total = len(storage.messages)
    if not is_power_of_two(n) or id_ % n != 0 or id_ + n > total:
        return f"No line {id_}+{n}."

    if n == 1:
        msg = storage.get_message(id_)
        if msg is None:
            return f"No line {id_}+{n}."
        return f"{id_}+0|{msg.kind}: {msg.text}"

    l, i = node_coords(id_, n)
    child_l = l - 1
    child_i_a = 2 * i
    child_i_b = 2 * i + 1

    part_a = Part(child_l, child_i_a)
    part_b = Part(child_l, child_i_b)

    line_a = part_a.render(storage)
    line_b = part_b.render(storage)
    return f"{line_a}\n{line_b}"


def execute_date(storage: Storage, id_: int) -> str:
    """Execute date(id) tool (§7.1). Returns local date and time of message id."""
    msg = storage.get_message(id_)
    if msg is None:
        return f"No message {id_}."
    return msg.date
