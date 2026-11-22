"""Touch-ledger identifiability at level birth (measurement). A level empty in bbo(h-1) and at the touch in bbo(h) holds
only orders placed in block h. Survivors-before-fills S ⊆ candidate adds with sum(S) = sz + filled lots and |S| = n + k
(k = orders the fills fully consumed), found by meet in the middle; each S is checked by replaying the fills against
the queue head (grouping.p desc, arrival). Reports solution counts and whether the first maker(s) are pinned down.
usage: python -m analysis.touch_ident <mac3 dir with new34/ and hl_data/>
"""
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from analysis import touch_ledger as tl
from engine_py.types import Side, Place, Modify, Cancel, CancelByCloid

def halves(items):
    out = defaultdict(list)          # (count, sum) -> list of index tuples
    m = len(items)
    for mask in range(1 << m):
        c = s = 0; idx = []
        for j in range(m):
            if mask >> j & 1:
                c += 1; s += items[j][0]; idx.append(j)
        out[(c, s)].append(tuple(idx))
    return out

def solutions(cands, n, sz, cap=5000):
    if len(cands) > 40:
        return None
    k = len(cands) // 2
    A, B = cands[:k], cands[k:]
    hb = halves(B)
    sols = []
    for (ca, sa), las in halves(A).items():
        lbs = hb.get((n - ca, sz - sa))
        if lbs:
            for la in las:
                for lb in lbs:
                    sols.append(la + tuple(k + j for j in lb))
                    if len(sols) > cap:
                        return sols
    return sols

M3 = Path(sys.argv[1]); chunks = sorted((M3 / "new34").iterdir())
stat = Counter(); nsol = Counter(); head_same = Counter()
for c in chunks:
    ch = tl.Chunk(c, M3 / "hl_data")
    H = ch.heights
    for asset in (0, 1):
        for i in range(1, len(H)):
            po, o = ch.obs.get((asset, H[i - 1])), ch.obs.get((asset, H[i]))
            if po is None or o is None:
                continue
            for side in (Side.BUY, Side.SELL):
                lv = o[0] if side is Side.BUY else o[1]
                if lv is None or tl.level_empty(po, side, lv[0]) is not True:
                    continue
                px, sz, n = lv
                sv = tl.Solver(ch, asset, side, px, 0, False)
                acts = tl.exec_order(ch.by_height[H[i]].actions)
                cands = []
                for k, a in enumerate(acts):
                    if getattr(a, "asset", None) != asset:
                        continue
                    p = a.new if isinstance(a, Modify) else a
                    if isinstance(p, Place):
                        s = sv.add_size(p, a, None)
                        if s:
                            cands.append((s, p.user, -(p.priority or 0), k))
                blk = ch.by_height[H[i]]
                hashes = [a.hash for a in acts]
                trs = []
                seen = set()
                for hsh in hashes:
                    if hsh in seen: continue
                    seen.add(hsh)
                    trs += [t for t in ch.trades_by_hash.get(hsh, ()) if sv.relevant(t)]
                trs += [t for t in ch.zero_by_height.get(H[i], ()) if sv.relevant(t)]
                F = sum(t.sz for t in trs)
                stat["births"] += 1
                stat["with fills"] += bool(trs)
                sols = []
                over = False
                for extra in range(0, len(trs) + 1):
                    r = solutions(cands, n + extra, sz + F)
                    if r is None:
                        over = True; break
                    for S in r:
                        q = [list(cands[j]) for j in sorted(S, key=lambda j: (cands[j][2], cands[j][3]))]
                        ok = True; full = 0
                        for t in trs:
                            mk = t.seller if t.taker_side is Side.BUY else t.buyer
                            if not q or q[0][1] != mk or q[0][0] < t.sz:
                                ok = False; break
                            q[0][0] -= t.sz
                            if q[0][0] == 0:
                                q.pop(0); full += 1
                        if ok and full == extra:
                            sols.append(S)
                if over:
                    stat["skipped (>40 candidates)"] += 1
                    continue
                stat["measured"] += 1
                b = len(sols)
                nsol["0" if b == 0 else "1" if b == 1 else "2-10" if b <= 10 else "11-100" if b <= 100 else ">100"] += 1
                if b:
                    heads = set()
                    for S in sols:
                        q = sorted((cands[j] for j in S), key=lambda x: (x[2], x[3]))
                        heads.add(tuple(x[1] for x in q[:3]))
                    head1 = {h[0] for h in heads}
                    head_same["first maker unique" if len(head1) == 1 else "first maker ambiguous"] += 1
                    head_same["first 3 makers unique" if len(heads) == 1 else "first 3 makers ambiguous"] += 1
print(dict(stat)); m = stat["measured"]
print("solutions:", {k: f"{v} ({v/m:.0%})" for k, v in sorted(nsol.items())})
print("queue head:", {k: f"{v} ({v/max(1,m-nsol['0']):.0%})" for k, v in sorted(head_same.items())})
