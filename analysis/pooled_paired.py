"""Tournament screen (DAYTIME_PROTOCOL "Tournament"): one variant vs its reference, pooled over several dev sets.

Each set contributes its own paired txs (tx set fixed by that set's reference, as analysis/contested_paired.py).
Sets can share chunks (dev_margin60 and dev-fast share 4): a chunk already pooled from an earlier set is skipped in
later sets (counted once; the per-set lines still show each set in full), and the shared chunks are printed. The
pooled chunk-cluster bootstrap resamples the distinct chunks of all sets together.
Prints the pooled maker-strict contested / uncontested diffs with 95% CIs, each set's own point, and the promotion
bar check (pooled contested point >= +1.0 pt, lower >= -1.0 pt, no acting set's point < 0).

usage: python -m analysis.pooled_paired REF_DIR:VAR_DIR:CHUNK_PARENT [...] [--boot 4000] [--inert SET ...]
       (--inert: set names, i.e. chunk-parent folder names, where the switch cannot act; excluded from the
        "no acting set < 0" check only)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from analysis.contested import load
from analysis.contested_paired import paired, per_tx, scope_of

FIELD = "matched_notional_full"          # maker-strict


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("triples", nargs="+")
    ap.add_argument("--boot", type=int, default=4000)
    ap.add_argument("--inert", nargs="*", default=[])
    a = ap.parse_args(argv)
    ref_all, var_all, cont, unc, per_set = {}, {}, [], [], []
    owner: dict[str, str] = {}                          # chunk name -> the set it is pooled from (first one)
    dup: list[str] = []
    for t in a.triples:
        ref_dir, var_dir, chunks = t.split(":")
        name = Path(chunks).name
        rr, vv = load(Path(ref_dir)), load(Path(var_dir))
        scope = scope_of(rr, Path(chunks), False)
        ref, var = per_tx(rr, FIELD), per_tx(vv, FIELD)
        per_set.append((name, paired(ref, var, [k for k, s in scope.items() if s], a.boot)))
        chunks = {c for c, _ in scope}
        for c in sorted(chunks):
            if owner.setdefault(c, name) != name:
                dup.append(f"{c} ({owner[c]} + {name})")
        mine = lambda c: owner[c] == name
        ref_all.update({k: x for k, x in ref.items() if mine(k[0])})
        var_all.update({k: x for k, x in var.items() if mine(k[0])})
        cont += [k for k, s in scope.items() if s and mine(k[0])]
        unc += [k for k, s in scope.items() if not s and mine(k[0])]
    pc = paired(ref_all, var_all, cont, a.boot)
    pu = paired(ref_all, var_all, unc, a.boot)
    pt = lambda r: f"{100 * r['diff']:+.1f} [{100 * r['lo']:+.1f}, {100 * r['hi']:+.1f}] (n {r['n']}, chunks {r['chunks']})"
    print(f"pooled maker-strict contested   {pt(pc)}  ref {pc['ref']:.3f} -> {pc['var']:.3f}")
    print(f"pooled maker-strict uncontested {pt(pu)}  ref {pu['ref']:.3f} -> {pu['var']:.3f}")
    for name, r in per_set:
        print(f"  {name:14} contested {pt(r)}")
    if dup:
        print(f"  shared chunks counted once ({len(dup)}): " + ", ".join(dup))
    acting = [r for n, r in per_set if n not in a.inert]
    ok = 100 * pc["diff"] >= 1.0 and 100 * pc["lo"] >= -1.0 and all(r["diff"] >= 0 for r in acting)
    print(f"promotion bar (point >= +1.0, lower >= -1.0, no acting set < 0): {'PASS' if ok else 'fail'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
