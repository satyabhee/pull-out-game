"""M1 simulator settings: every sampler and mask parameter in one place (MAHITO_PLAN M1: "every mask parameter
lives in a config"). Defaults = calibration round r3 against dev20 (Satya Q8; replay/sim/calibration_rounds.py,
docs/m1_calibration.md). Times in ms, prices in ticks, sizes in lots."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class SimConfig:
    # ---- chunk shape (like a real dev20 chunk: 210 blocks, 150 lead-in, ~75 ms per block)
    seed: int = 0
    n_blocks: int = 210
    lead_in: int = 150
    block_ms: int = 75
    block_jitter_ms: int = 8
    start_time_ms: int = 1_830_297_600_000          # 2028-01-01 UTC: never overlaps recorded data
    start_height: int = 2_000_000_000
    chunk_spacing_ms: int = 3_600_000               # seed k starts k hours later (own recorder hour)
    chunk_spacing_heights: int = 100_000

    # ---- markets: asset -> (mid px ticks, grid step ticks, typical maker size lots)
    markets: dict = field(default_factory=lambda: {0: (635_000, 10, 2_000), 1: (178_600, 10, 2_000)})
    mid_step_prob: float = 0.15                     # per block, the mid moves one grid step
    pre_window_ms: int = 30_000                     # resting orders seeded up to this long before the chunk

    # ---- agents
    n_mm: int = 12
    mm_levels: int = 6                              # levels quoted per side
    mm_sizes: int = 3                               # each MM reuses this many sizes (multiples of maker size)
    mm_requote_prob: float = 0.25                   # per block, a MM cancels and replaces its quotes
    mm_requote_on_move: float = 0.8                 # ... when the mid moved this block
    mm_cloid_share: float = 0.85                    # cancels by cloid (rest by oid)
    mm_p_max: int = 200                             # priority bid range for MM post-only orders
    n_bots: int = 4
    bot_broke: int = 1                              # bots with no free margin: their IOCs are rejected
    bot_ioc_prob: float = 0.04                      # per bot per block
    n_retail: int = 24
    retail_prob: float = 0.02                       # per retail account per block
    retail_tif: dict = field(default_factory=lambda: {"Gtc": 0.3, "Ioc": 0.4, "Alo": 0.3})
    retail_broke: int = 2                           # retail accounts with no margin (their orders are rejected)
    reduce_only_share: float = 0.05                 # of retail orders
    n_broke_quoters: int = 2                        # accounts with no margin that keep sending post-only quotes
    broke_quotes_per_block: float = 1.9             # expected post-only orders per block from them (rejected)
    n_stackers: int = 1                             # accounts that rest many deep orders and hit the open-order cap
    stacker_prob: float = 0.2                       # per stacker per block: one more deep post-only order
    open_order_cap: int = 1000                      # resting orders per user (Hyperliquid's base cap)
    deep_orders_per_level: int = 10                  # static pre-window orders on each of the 20 levels per side
    n_flickerers: int = 3                           # fast quoters: each block cancel all and re-place 0..flick_max
    flick_max: int = 9                              # post-only orders at the touch (bursty joins / leaves)
    flick_size_max: int = 10                         # their sizes: 1..flick_size_max maker sizes
    taker_sweep_max: int = 3                        # IOC sizes reach up to this many maker sizes

    # ---- hidden chain events
    truth_order: str = "class_sort"                 # chain's in-block order: class_sort per batch | proposer
    batch_cut_prob: float = 0.10                    # a block holds two consensus batches
    burn_prob: float = 0.03                         # per placed item, an invisible event burns 1-3 oids

    # ---- mask (what the replay sees)
    snapshot_every_ms: int = 12_400                 # full l2Book (20 levels, with n)
    snapshot_depth: int = 20
    fast_every_ms: int = 540                        # l2Book fast (5 levels)
    fast_depth: int = 5
    anchors_per_chunk: int = 50                     # orderStatus answers (placement -> oid)
    account_snapshots: int = 40                     # clearinghouseState for this many accounts ...
    account_snapshot_after_ms: int = 7_000          # ... taken this long after the chunk starts
    recv_lag_ms: int = 50                           # recorder receive time = event time + lag
    merge_batches: bool = True                      # explorer blocks merge batches (False: full visibility)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def full_visibility(cls, **kw) -> "SimConfig":
        """Nothing hidden that the replay cannot see (the M1 property test): no pre-window orders, no margin,
        reduce-only or open-order-cap rejections, no invisible oids, one batch per block, and an anchor (known oid)
        for every placement, so int-oid cancels are resolvable."""
        base = dict(pre_window_ms=0, bot_broke=0, retail_broke=0, reduce_only_share=0.0, batch_cut_prob=0.0,
                    burn_prob=0.0, anchors_per_chunk=10**9,    # every placement's oid is known
                    n_broke_quoters=0, n_stackers=0)           # their rejections are hidden too
        base.update(kw)
        return cls(**base)
