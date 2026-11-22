"""M0 · Idle: a thin adapter over the existing Python engine (MAHITO_PLAN.md §M0).

The real engine is window-driven, not snapshot-driven: `engine_py.replay.Replay` seeds the book from the recorder
snapshot nearest the window start, re-seeds at later snapshots (rule 8), gates ALOs on the real touch, resolves
cancels, and applies trade corrections, all inside its per-block loop. So a BookHandle wraps a whole Replay
(engine + per-window state), and `apply_block` advances it by exactly one block via `Replay.step_block`.
Deviations from the spec's signatures are listed in docs/deviations.md.

Construction never duplicates the CLI -> Replay mapping: `replay_args` calls `analysis.validate_replay.run`
itself with `Replay` swapped for a recorder that captures the arguments and stops, so the adapter builds
exactly the Replay the scoring path builds. No scoring code is edited.

Units: prices are integer ticks and sizes integer lots, as everywhere in the engine (no floats).
"""
from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from analysis import validate_replay as vr
from engine_py.replay import Observer, Replay
from engine_py.types import Fill, Side

# Engine options shared by every switch setting (frozen config + docs priority).
ENGINE_OPTIONS = ("--alo-gate touch --reseed --nearest-ratio 3 --keep-known --outside-other-txs --deep-book "
                  "--p-priority docs --evict-arrival --oid-bracket --touch-reconcile --touch-evict ledger "
                  "--ledger-k 99").split()

# Switches (Satya, Nov 22): each maps to an option the Python engine already has.
#   stp:        taker = cancel the taker's remainder (engine default) | maker = cancel the resting order (plan §1)
#   order:      proposer = listing order | class_sort = no-GTC/IOC, then cancels, then GTC/IOC (docs)
#   correction: blockend = trade corrections after all of a block's fills (rule 1) | midblock = after each tx
SWITCHES = {
    "stp": {"taker": ["--stp", "cancel_taker"], "maker": ["--stp", "expire_maker"]},
    "order": {"proposer": ["--policy", "listing"], "class_sort": ["--policy", "class"]},
    "correction": {"blockend": ["--trade-learn-at", "block-end"], "midblock": ["--trade-learn-at", "tx"]},
}
# Official baseline since Nov 22 (docs/deviations.md D4): block-end corrections. The 58.8% final240 number is the
# same engine with midblock corrections ("mid-block correction" baseline).
BASELINE_SWITCHES = {"stp": "taker", "order": "proposer", "correction": "blockend"}


def options_for(stp: str = "taker", order: str = "proposer", correction: str = "blockend") -> list[str]:
    """Replay options for one switch setting (the policy option is split off by split_policy)."""
    chosen = {"stp": stp, "order": order, "correction": correction}
    out = list(ENGINE_OPTIONS)
    for name, value in chosen.items():
        if value not in SWITCHES[name]:
            raise ValueError(f"{name}={value!r}: choose from {sorted(SWITCHES[name])}")
        out += SWITCHES[name][value]
    return out


def split_policy(options) -> tuple[str, list[str]]:
    """(policy name, options without --policy): the policy is a run() argument, not a replay option."""
    opts, policy = list(options), "listing"
    if "--policy" in opts:
        i = opts.index("--policy")
        policy = opts[i + 1]
        del opts[i:i + 2]
    return policy, opts


BASELINE_OPTIONS = options_for(**BASELINE_SWITCHES)

TxHash = str


@dataclass(frozen=True)
class BranchContext:
    """The guesses a particle has made (M2). Empty = run exactly as the engine does today.
    M0 defines the fields only; apply_block refuses a non-empty context until M2 adds the hooks."""
    cancel_targets: tuple = ()          # ((cancel key, chosen engine oid), ...)
    rejected: frozenset = frozenset()   # keys of placements this particle treats as rejected
    batch_cuts: tuple = ()              # (block height, tx_index cut points), ...
    priority_mode: str | None = None    # None = the engine's configured mode

    def is_empty(self) -> bool:
        return not (self.cancel_targets or self.rejected or self.batch_cuts or self.priority_mode is not None)


EMPTY = BranchContext()


@dataclass
class BookHandle:
    replay: Replay
    copies: int = field(default=0)      # how many copies were made from this handle (budget: none without branching)

    @property
    def next_height(self) -> int | None:
        rp = self.replay
        return rp.blocks[rp._next_bi].height if rp._next_bi < len(rp.blocks) else None


class _Captured(Exception):
    def __init__(self, args, kwargs):
        super().__init__("captured")
        self.args_, self.kwargs_ = args, kwargs


def _capture(*args, **kwargs):
    raise _Captured(args, kwargs)


def replay_args(window, hl_data, policy_name: str = "listing", **run_kwargs) -> tuple[tuple, dict]:
    """(args, kwargs) that analysis.validate_replay.run would pass to Replay for these run options. They include a
    NEW Engine instance, so use them for exactly one Replay."""
    saved = vr.Replay
    vr.Replay = _capture
    try:
        vr.run(window, hl_data, policy_name, quiet=True, **run_kwargs)
    except _Captured as c:
        return c.args_, c.kwargs_
    finally:
        vr.Replay = saved
    raise RuntimeError("validate_replay.run did not construct a Replay")


def run_kwargs_from_options(hl_data, options=BASELINE_OPTIONS) -> dict:
    """validate_replay.run keyword arguments for CLI-style replay options."""
    import argparse
    ap = argparse.ArgumentParser()
    vr.add_replay_args(ap)
    return vr.replay_kwargs(ap.parse_args(["--hl-data", str(hl_data), *options]))


class EngineAdapter:
    def __init__(self, backend: str = "py"):
        """backend "py" = the Python engine unchanged. "rust" = the same engine with the parts M5 has ported so far
        swapped for replay_core (today: the touch ledger's subset search). Default stays "py" until Satya signs off."""
        if backend not in ("py", "rust"):
            raise ValueError(f"unknown backend {backend!r}")
        self.rust = None
        if backend == "rust":
            from replay_core import replay_core as rust     # ImportError if the crate is not built
            self.rust = rust
        self.backend = backend

    # -------------------------------------------------------------- books
    def new_book(self, window, hl_data, options=BASELINE_OPTIONS, observer: Observer | None = None) -> BookHandle:
        """A book positioned before the window's first block (seeded from the nearest snapshot, as today).
        options: replay options incl. an optional --policy, e.g. options_for(stp=..., order=..., correction=...)."""
        policy_name, opts = split_policy(options)
        args, kw = replay_args(window, hl_data, policy_name, **run_kwargs_from_options(hl_data, opts))
        return self.book_from_replay(Replay(*args, **kw), observer)

    def book_from_replay(self, rp: Replay, observer: Observer | None = None) -> BookHandle:
        if observer is not None:
            rp.observer = observer
        if self.rust is not None:
            rp.subset_search = self.rust.unique_subset
        rp._begin_blocks()
        return BookHandle(rp)

    def copy_book(self, book: BookHandle) -> BookHandle:
        """Independent copy (for branching). Window inputs (decoded blocks, snapshots) are shared, never copied;
        the copy gets a no-op observer so scoring state is never double-counted."""
        rp = book.replay
        memo = {id(rp.blocks): rp.blocks, id(rp.snapshots): rp.snapshots, id(rp.observer): Observer()}
        book.copies += 1
        return BookHandle(copy.deepcopy(rp, memo))

    # -------------------------------------------------------------- blocks
    def apply_block(self, book: BookHandle, block=None, ctx: BranchContext = EMPTY) -> dict[TxHash, list[Fill]]:
        """Replay the handle's next block. `block` (a parse.Block or a height) must be that next block; None = next.
        Returns fills grouped by taker tx hash, in fill order (outside fills under their own hash)."""
        if not ctx.is_empty():
            raise NotImplementedError("branching contexts arrive in M2; M0 runs only the empty context")
        rp = book.replay
        want = book.next_height
        if want is None:
            raise IndexError("window exhausted")
        got = block if block is None or isinstance(block, int) else block.height
        if got is not None and got != want:
            raise ValueError(f"block {got} is not the handle's next block {want}")
        _, fills, _, _ = rp.step_block()
        out: dict[TxHash, list[Fill]] = OrderedDict()
        for f in fills:
            out.setdefault(f.taker_hash, []).append(f)
        return out

    # -------------------------------------------------------------- views
    def level_view(self, book: BookHandle, asset: int, side: Side, px: int) -> tuple[int, int]:
        """(size in lots, order count) resting at px; (0, 0) when the level is empty."""
        lv = book.replay.engine.books[asset].side(side).get(px)
        return (0, 0) if lv is None else (lv.total, len(lv))

    def top_levels(self, book: BookHandle, asset: int, side: Side, k: int = 5) -> list[tuple[int, int, int]]:
        """Best k levels as (px ticks, size lots, order count), best first."""
        levels = book.replay.engine.books[asset].side(side)
        keys = reversed(levels.keys()) if side is Side.BUY else iter(levels.keys())
        out = []
        for px in keys:
            if len(out) == k:
                break
            lv = levels[px]
            out.append((px, lv.total, len(lv)))
        return out
