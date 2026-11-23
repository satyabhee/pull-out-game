"""Tournament screen (DAYTIME_PROTOCOL "Tournament"): one variant vs its reference, pooled over several dev sets.

Each set contributes its own paired txs (tx set fixed by that set's reference, as analysis/contested_paired.py);
chunks are distinct across sets, so the pooled chunk-cluster bootstrap resamples chunks of all sets together.
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
    for t in a.triples:
        ref_dir, var_dir, chunks = t.split(":")
        name = Path(chunks).name
        rr, vv = load(Path(ref_dir)), load(Path(var_dir))
        scope = scope_of(rr, Path(chunks), False)
        ref, var = per_tx(rr, FIELD), per_tx(vv, FIELD)
        # prefix chunk keys with the set so identical chunk names in two sets can never merge
        ref_all.update({(f"{name}/{c}", h): x for (c, h), x in ref.items()})
        var_all.update({(f"{name}/{c}", h): x for (c, h), x in var.items()})
        ck = [(f"{name}/{c}", h) for (c, h), s in scope.items() if s]
        uk = [(f"{name}/{c}", h) for (c, h), s in scope.items() if not s]
        cont += ck
        unc += uk
        per_set.append((name, paired(ref, var, [(k[0].split("/", 1)[1], k[1]) for k in ck], a.boot)))
    pc = paired(ref_all, var_all, cont, a.boot)
    pu = paired(ref_all, var_all, unc, a.boot)
    pt = lambda r: f"{100 * r['diff']:+.1f} [{100 * r['lo']:+.1f}, {100 * r['hi']:+.1f}] (n {r['n']}, chunks {r['chunks']})"
    print(f"pooled maker-strict contested   {pt(pc)}  ref {pc['ref']:.3f} -> {pc['var']:.3f}")
    print(f"pooled maker-strict uncontested {pt(pu)}  ref {pu['ref']:.3f} -> {pu['var']:.3f}")
    for name, r in per_set:
        print(f"  {name:14} contested {pt(r)}")
    acting = [r for n, r in per_set if n not in a.inert]
    ok = 100 * pc["diff"] >= 1.0 and 100 * pc["lo"] >= -1.0 and all(r["diff"] >= 0 for r in acting)
    print(f"promotion bar (point >= +1.0, lower >= -1.0, no acting set < 0): {'PASS' if ok else 'fail'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
