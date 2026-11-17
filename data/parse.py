"""Parse Hyperliquid explorer blocks into typed order-book actions (facts: report/G0_REPORT.md).

Input: one block as returned by POST rpc.hyperliquid.xyz/explorer {"type":"blockDetails"}
(optionally wrapped in {"blockDetails": ...}), e.g. a captured windows/<range>/<height>.json.gz.

Output: RawPlace / RawCancel / RawModify for the requested assets, still in decimal strings and
exchange ids, plus counts of everything skipped. Unit conversion happens in the replay, against
each market's config, so a bad price is a counted rejection, never a crash.

Decoding rules:
  order        each item -> RawPlace. tif Alo->ALO, Gtc->GTC, Ioc->IOC, FrontendMarket->IOC.
               Trigger orders (t.trigger) are skipped: they rest off-book until the oracle fires.
  cancel       {a, o}        -> RawCancel by exchange oid
  cancelByCloid {asset, cloid} -> RawCancel by cloid
  modify       {oid, order}  -> RawModify; oid may be an int or a cloid hex string
  batchModify  each item     -> RawModify
  twapOrder, scheduleCancel  skipped and counted (unreproducible, see G0 report)
  any tx with error != null  skipped whole (the action had no effect on the chain)
reduce_only is carried but not enforced: the engine has no positions (01-book section 1).
"""
from __future__ import annotations

import gzip
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from engine_py.types import Side, Tif

TIF = {"Alo": Tif.ALO, "Gtc": Tif.GTC, "Ioc": Tif.IOC, "FrontendMarket": Tif.IOC}
HL_ASSETS = {0: "BTC", 1: "ETH"}           # index in info {"type":"meta"} universe


@dataclass(frozen=True, slots=True)
class RawPlace:
    tx_index: int
    action_index: int
    owner: str
    asset: int
    side: Side
    px: str
    sz: str
    tif: Tif
    reduce_only: bool
    cloid: str | None
    tx_hash: str


@dataclass(frozen=True, slots=True)
class RawCancel:
    tx_index: int
    action_index: int
    owner: str
    asset: int
    oid: int | None
    cloid: str | None


@dataclass(frozen=True, slots=True)
class RawModify:
    tx_index: int
    action_index: int
    owner: str
    asset: int
    target_oid: int | None
    target_cloid: str | None
    new: RawPlace


RawAction = RawPlace | RawCancel | RawModify


@dataclass
class ParsedBlock:
    height: int
    block_time: int
    num_txs: int
    actions: list[RawAction] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


def load_block(path: str | Path) -> dict:
    with gzip.open(path, "rt") if str(path).endswith(".gz") else open(path) as f:
        b = json.load(f)
    return b.get("blockDetails", b)


def _place(o: dict, owner: str, ti: int, ai: int, tx_hash: str, skipped: Counter) -> RawPlace | None:
    t = o.get("t", {})
    if "trigger" in t:
        skipped["trigger_order"] += 1
        return None
    tif = TIF.get(t.get("limit", {}).get("tif"))
    if tif is None:
        skipped[f"unknown_tif:{t}"] += 1
        return None
    return RawPlace(ti, ai, owner, o["a"], Side.BUY if o["b"] else Side.SELL, o["p"], o["s"], tif,
                    bool(o.get("r")), o.get("c"), tx_hash)


def _target(t) -> tuple[int | None, str | None]:
    return (t, None) if isinstance(t, int) else (None, t)


def parse_block(block: dict, assets=HL_ASSETS) -> ParsedBlock:
    b = block.get("blockDetails", block)
    pb = ParsedBlock(b["height"], b["blockTime"], b.get("numTxs", len(b["txs"])))
    sk = pb.skipped
    for ti, tx in enumerate(b["txs"]):
        a, owner, ty = tx["action"], tx["user"], tx["action"].get("type")
        if ty == "order":
            items = [o for o in a.get("orders", []) if o.get("a") in assets]
            if items and tx.get("error"):
                sk["errored_tx"] += 1
                continue
            for ai, o in enumerate(a.get("orders", [])):
                if o.get("a") in assets:
                    p = _place(o, owner, ti, ai, tx["hash"], sk)
                    if p:
                        pb.actions.append(p)
        elif ty in ("cancel", "cancelByCloid"):
            items = [(ai, c) for ai, c in enumerate(a.get("cancels", [])) if c.get("a", c.get("asset")) in assets]
            if items and tx.get("error"):
                sk["errored_tx"] += 1
                continue
            for ai, c in items:
                if ty == "cancel":
                    pb.actions.append(RawCancel(ti, ai, owner, c["a"], c["o"], None))
                else:
                    pb.actions.append(RawCancel(ti, ai, owner, c["asset"], None, c["cloid"]))
        elif ty in ("modify", "batchModify"):
            mods = [{"oid": a.get("oid"), "order": a.get("order", {})}] if ty == "modify" else a.get("modifies", [])
            items = [(ai, m) for ai, m in enumerate(mods) if m.get("order", {}).get("a") in assets]
            if items and tx.get("error"):
                sk["errored_tx"] += 1
                continue
            for ai, m in items:
                new = _place(m["order"], owner, ti, ai, tx["hash"], sk)
                if new:
                    oid, cloid = _target(m.get("oid"))
                    pb.actions.append(RawModify(ti, ai, owner, new.asset, oid, cloid, new))
        elif ty == "twapOrder" and a.get("twap", {}).get("a") in assets:
            sk["twap_order"] += 1
        elif ty == "scheduleCancel":
            sk["schedule_cancel"] += 1
    return pb
