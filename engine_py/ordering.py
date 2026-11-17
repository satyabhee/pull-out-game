"""In-block ordering policies as swappable functions.

A policy takes a block's order-book actions (data.parse types, each carrying tx_index and
action_index) and returns them in execution order. Policies only reorder; they never drop actions.

- project_priority (specs/01-book.md section 2): ALO submissions, then cancels, then GTC/IOC
  submissions; within each phase by (tx_index, action_index).
- first_come: block listing order, (tx_index, action_index).

A modify (cancel + new order) is ONE action in the cancel phase: the old order is cancelled and the
replacement submitted at that point. Splitting it across phases could place the replacement before
the cancel ran, and would break "an unresolved target places no new order" (01-book, Exchange
identities).
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from data.parse import RawAction, RawCancel, RawModify, RawPlace
from .types import Tif

Policy = Callable[[Sequence[RawAction]], list[RawAction]]


def _pos(a: RawAction) -> tuple[int, int]:
    return a.tx_index, a.action_index


def project_priority(actions: Sequence[RawAction]) -> list[RawAction]:
    def phase(a: RawAction) -> int:
        if isinstance(a, RawPlace):
            return 0 if a.tif == Tif.ALO else 2
        if isinstance(a, (RawCancel, RawModify)):
            return 1
        raise TypeError(a)
    return sorted(actions, key=lambda a: (phase(a), *_pos(a)))


def first_come(actions: Sequence[RawAction]) -> list[RawAction]:
    return sorted(actions, key=_pos)


POLICIES: dict[str, Policy] = {"project": project_priority, "first_come": first_come}
