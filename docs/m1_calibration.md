# M1 calibration: simulated vs real (Satya Q8)

Targets: each statistic within +-25% of dev20, at most 3 tuning rounds. Each round = 30 simulated chunks (seeds
0-29) generated with that round's settings; statistics computed by `replay/sim/calibrate.py` with the same code
on real and simulated files. Rounds and their exact settings: `replay/sim/calibration_rounds.py`
(`python -m replay.sim.calibration_rounds ~/hl-data/sim/cal --round r3`). r3 is now the SimConfig default.

| statistic | real (dev20) | r0 (before) | r1 | r2 | **r3 (after)** | r3 / real | within +-25% |
|---|---|---|---|---|---|---|---|
| trades per block (mean of chunks) | 0.557 | 2.88 | 0.573 | 0.575 | **0.568** | 1.02 | yes |
| orders at the touch (median n) | 13 | 5 | 11 | 12 | **12** | 0.92 | yes |
| orders per level, 20-level book (median of means) | 11.9 | 5.3 | 12.2 | 12.2 | **12.2** | 1.02 | yes |
| touch |dn| median | 2 | 1 | 0 | 2 | **2** | 1.00 | yes |
| touch |dn| p90 | 10 | 2 | 3 | 6 | **7** | 0.70 | **no** |
| touch |dsz|/sz median | 0.144 | 0.2 | 0.0909 | 0.182 | **0.25** | 1.74 | **no** |
| touch |dsz|/sz p90 | 1.06 | 0.714 | 0.667 | 0.737 | **0.86** | 0.81 | yes |
| post-only margin rejection rate | 0.0167 | 0.00166 | 0.0155 | 0.0137 | **0.017** | 1.02 | yes |
| tooManyOpenOrders rate | 0.00143 | 0 | 0.0011 | 0.000963 | **0.00152** | 1.06 | yes |

Targets within +-25%: r0 0/9, r1 5/9, r2 5/9, **r3 7/9**.

**Not met after the 3 allowed rounds:** the 90th percentile of the touch order-count change (7 vs 10) and the
median relative touch size change (0.25 vs 0.14). They pull against each other in this agent model: bigger
bursts raise the p90 count change but also the typical relative size change (r2 had 0.18, closer, with a p90 of
6). Fixing both likely needs a different mechanism (e.g. many small cancels and joins spread over time rather
than whole bursts), not more tuning of these knobs.

What each round changed:
- r1: takers x0.2 (bot IOC 0.20 -> 0.04, retail 0.10 -> 0.02), 12 market makers quoting 6 levels, a static deep
  book of 10 orders on each of 20 levels, 2 broke quoters (1.5 post-only orders per block, all margin-rejected),
  1 stacker near the 1,000 open-order cap (one more deep order per block with p 0.13).
- r2: + 4 flickering quoters (each block cancel all, re-place 0..6 post-only orders at the touch).
- r3: 3 flickerers, bursts 0..9 orders of 1..10 maker sizes; stacker p 0.2; broke quotes 1.9 per block.

Generation time with r3: 3-5 s per chunk alone (up to 11 s while other jobs ran); budget <= 17.7 s.
