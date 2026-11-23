"""M1: generate simulated chunks from seeds (sampler -> chain -> mask + truth).

A chunk is written in the real formats under <root>/windows/, <root>/hl_data/ (one recorder hour per seed, so
chunks never share files) and <root>/truth/; a chunk set is a folder of links (<root>/<set>/). The same seed and
config always write the same bytes.

usage: python -m replay.sim.generate <root> --seeds 0-199 [--set sim200] [--full-visibility]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from replay.sim import mask
from replay.sim.config import SimConfig
from replay.sim.rules_engine import Chain
from replay.sim.sampler import Sampler
from replay.sim.truth import write_truth


def chunk_name(cfg: SimConfig) -> str:
    h0 = cfg.start_height + cfg.seed * cfg.chunk_spacing_heights
    return f"{h0}-{h0 + cfg.n_blocks - 1}"


def generate_chunk(cfg: SimConfig, root: Path) -> dict:
    t_gen = time.perf_counter()
    root = Path(root)
    rng = random.Random(cfg.seed)
    sampler = Sampler(cfg, rng)
    chain = Chain(sampler.accounts, open_order_cap=cfg.open_order_cap, order=cfg.truth_order)
    chain.burn_before_item = sampler.burn
    h0 = cfg.start_height + cfg.seed * cfg.chunk_spacing_heights
    t0 = cfg.start_time_ms + cfg.seed * cfg.chunk_spacing_ms + 120_000
    window = root / "windows" / chunk_name(cfg)
    window.mkdir(parents=True, exist_ok=True)
    rec = mask.Recorder(root, cfg.recv_lag_ms)
    truth_extra: list[dict] = [{"kind": "config", **cfg.as_dict()}]

    sampler.seed_book(chain, t0)
    rec.book(chain, t0 - 1, "full")                              # the snapshot the replay seeds from
    rec.book(chain, t0 - 1, "sf4")
    next_snap, next_fast = t0 - 1 + cfg.snapshot_every_ms, t0 - 1 + cfg.fast_every_ms
    t = t0
    placed_items: list[tuple[int, int, int, int]] = []          # (height, tx index, item, listing position)
    accounts_written = False
    first_t = last_t = None
    n_trades = 0
    for b in range(cfg.n_blocks):
        height = h0 + b
        t = t if b == 0 else t + cfg.block_ms + rng.randint(-cfg.block_jitter_ms, cfg.block_jitter_ms)
        first_t = first_t if first_t is not None else t
        last_t = t
        txs = sampler.block_txs(chain, height, t)
        for tx in txs:
            tx.hash = mask.tx_hash(cfg.seed, height, tx.index)
        batches = sampler.batches(txs)
        truth_extra.append({"kind": "block", "height": height, "time": t,
                            "batches": [[tx.index for tx in batch] for batch in batches]})
        trades = chain.run_block(height, t, batches)
        n_trades += len(trades)
        if cfg.listing == "execution":                          # L1 (Nov 23): the explorer lists execution order
            listing = [tx for batch in batches for tx in chain.exec_order(batch)]
        else:                                                   # proposer order, batches merged
            listing = sorted(txs, key=lambda tx: tx.index)
        mask.write_block(window, height, t, cfg.seed, listing)
        for pos, tx in enumerate(listing):
            if tx.kind == "order":
                placed_items += [(height, tx.index, k, pos) for k in range(len(tx.items))]
        rec.add("blocks", t, {"height": height, "blockTime": t, "hash": mask.block_hash(cfg.seed, height),
                              "proposer": mask.PROPOSER, "numTxs": len(txs)})
        rec.trades(trades, t)
        rec.bbo(chain, t)
        while next_fast <= t:
            rec.book(chain, t, "fast")
            next_fast += cfg.fast_every_ms
        while next_snap <= t:
            rec.book(chain, t, "full")
            rec.book(chain, t, "sf4")
            next_snap += cfg.snapshot_every_ms
        if not accounts_written and t - t0 >= cfg.account_snapshot_after_ms:
            users = sorted(sampler.accounts)[:cfg.account_snapshots]
            mask.write_accounts(window, sampler.accounts, users, chain)
            accounts_written = True

    # anchors: a seeded sample of placements whose oid an orderStatus lookup would return
    oid_of = {(r["height"], r["tx"], r["item"]): r["oid"] for r in chain.truth if r["kind"] == "placed"}
    picks = rng.sample(placed_items, min(cfg.anchors_per_chunk, len(placed_items)))
    anchors = [{"oid": oid_of[p[:3]], "height": p[0], "tx": p[3], "item": p[2]}     # tx = listing position
               for p in sorted(picks, key=lambda p: (p[0], p[3], p[2]))]
    (window / "anchors.json").write_text(json.dumps(anchors, indent=1))
    meta = {"kind": "chunk", "status": "complete", "sim": True, "seed": cfg.seed,
            "start_height": h0, "end_height": h0 + cfg.n_blocks - 1, "lead_in_blocks": cfg.lead_in,
            "warmup_end_height": h0 + cfg.lead_in, "snapshot_time": t0 - 1,
            "first_block_time": first_t, "last_block_time": last_t}
    (window / "window.json").write_text(json.dumps(meta, indent=1))
    rec.write()
    truth_bytes = write_truth(root / "truth" / f"{window.name}.parquet", truth_extra + chain.truth)
    return {"chunk": window.name, "seed": cfg.seed, "blocks": cfg.n_blocks, "trades": n_trades,
            "orders": sum(1 for r in chain.truth if r["kind"] == "placed"), "truth_bytes": truth_bytes,
            "seconds": round(time.perf_counter() - t_gen, 2)}


def link_set(root: Path, name: str, chunks: list[str]) -> Path:
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    for c in chunks:
        link = d / c
        if not link.exists():
            link.symlink_to(Path(root) / "windows" / c)
    return d


def parse_seeds(spec: str) -> list[int]:
    out = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root")
    ap.add_argument("--seeds", default="0-199")
    ap.add_argument("--set", default=None)
    ap.add_argument("--full-visibility", action="store_true")
    ap.add_argument("--truth-order", choices=("class_sort", "proposer"), default=None,
                    help="the chain's in-block order (default: SimConfig's)")
    ap.add_argument("--batch-cut-prob", type=float, default=None, help="probability a block holds a batch cut")
    a = ap.parse_args(argv)
    over = {k: v for k, v in (("truth_order", a.truth_order), ("batch_cut_prob", a.batch_cut_prob)) if v is not None}
    root = Path(a.root).expanduser()
    names = []
    for seed in parse_seeds(a.seeds):
        cfg = SimConfig.full_visibility(seed=seed, **over) if a.full_visibility else SimConfig(seed=seed, **over)
        if (root / "windows" / chunk_name(cfg) / "window.json").exists():
            names.append(chunk_name(cfg))                       # reuse the seed instead of storing it twice
            continue
        info = generate_chunk(cfg, root)
        names.append(info["chunk"])
        print(json.dumps(info), flush=True)
    if a.set:
        link_set(root, a.set, names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
