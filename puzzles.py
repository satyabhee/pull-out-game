"""Solutions to the six hidden-information puzzles in "Replay the Order Book".

Each function keeps the stub's signature (P6 adds a confidence tag to each result)
and is run on the worked example at the bottom of the file.
"""
from itertools import combinations


# ---------------------------------------------------------------------------
# P1. Name the anonymous orders
# ---------------------------------------------------------------------------
def _compositions(total, parts):
    """Every ordered way to split `total` (int units) into `parts` positive ints."""
    if parts == 1:
        if total >= 1:
            yield (total,)
        return
    for first in range(1, total - parts + 2):
        for rest in _compositions(total - first, parts - 1):
            yield (first,) + rest


def name_placeholders(level, updates, named, trades, lot=0.5):
    """level:   (px, sz, n) in the starting snapshot
    updates: [(t, sz, n)] feed updates at this price
    named:   [(t, user, kind, sz)] placements / cancels we can decode here
    trades:  [(t, maker, sz)] public fills at this price
    Return every queue [(owner or None, sz), ...] of the n snapshot orders
    that fits every observation."""
    px, sz, n = level
    u = lambda x: round(x / lot)                  # work in integer lots, no float drift
    times = sorted({e[0] for e in updates} | {e[0] for e in named} | {e[0] for e in trades})

    results, seen = [], set()
    for comp in _compositions(u(sz), n):
        # state = (queue, snapshot owners). queue entries: (oid, owner, units), front first.
        # snapshot orders have int oids 0..n-1; everything else gets a string oid.
        states = {(tuple((i, None, c) for i, c in enumerate(comp)), (None,) * n)}

        for t in times:
            # 1) decoded placements and cancels
            for (tt, user, kind, s) in named:
                if tt != t:
                    continue
                nxt = set()
                for q, own in states:
                    if kind == "place":
                        nxt.add((q + ((f"{user}@{t}", user, u(s)),), own))
                    else:  # cancel: the user's own order, or an unnamed snapshot order of that size
                        for k, (oid, o, c) in enumerate(q):
                            if c == u(s) and o in (user, None):
                                own2 = own
                                if isinstance(oid, int):
                                    own2 = own[:oid] + (user,) + own[oid + 1:]
                                nxt.add((q[:k] + q[k + 1:], own2))
                states = nxt

            # 2) public trades: always hit the front order(s), and name them
            for (tt, maker, s) in trades:
                if tt != t:
                    continue
                nxt = set()
                for q, own in states:
                    q, own, rem, ok = list(q), list(own), u(s), True
                    while rem > 0:
                        if not q or q[0][1] not in (None, maker):
                            ok = False
                            break
                        oid, o, c = q[0]
                        if isinstance(oid, int):
                            own[oid] = maker
                        x = min(c, rem)
                        rem -= x
                        q[0] = (oid, maker, c - x)
                        if c - x == 0:
                            q.pop(0)
                    if ok:
                        nxt.add((tuple(q), tuple(own)))
                states = nxt

            # 3) feed update: whatever is left unexplained must be hidden cancels / adds
            for (tt, fsz, fn) in updates:
                if tt != t:
                    continue
                nxt = set()
                for q, own in states:
                    dn = fn - len(q)
                    dsz = u(fsz) - sum(c for _, _, c in q)
                    if dn == 0 and dsz == 0:
                        nxt.add((q, own))
                    elif dn < 0 and dsz < 0:      # hidden cancels of -dn whole orders
                        for gone in combinations(range(len(q)), -dn):
                            if sum(q[k][2] for k in gone) == -dsz:
                                nxt.add((tuple(e for k, e in enumerate(q) if k not in gone), own))
                    elif dn > 0 and dsz > 0:      # undecoded placements join the back
                        for parts in _compositions(dsz, dn):
                            nxt.add((q + tuple((f"?@{t}#{j}", None, c) for j, c in enumerate(parts)), own))
                    # any other gap (e.g. size moved but count didn't) can't be explained
                states = nxt

        for q, own in states:
            key = tuple(zip(own, (c * lot for c in comp)))
            if key not in seen:
                seen.add(key)
                results.append(list(key))
    return results


# ---------------------------------------------------------------------------
# P2. Will the chain accept this IOC?
# ---------------------------------------------------------------------------
MAX_LEV = {"BTC": 40, "ETH": 25}
MIN_NOTIONAL = 10.0


def will_accept(order, snapshot, history):
    """order:    (user, coin, side, sz, px, reduce_only)
    snapshot: {user: (account_value, margin_used, {coin: signed_size})}, may lack the user
    history:  [(user, engine_filled, chain_printed)] earlier IOCs
    Return True if the chain accepts the order."""
    user, coin, side, sz, px, reduce_only = order
    if reduce_only:
        return True                                 # reduce-only skips the margin check

    if user in snapshot:
        value, used, positions = snapshot[user]
        pos = positions.get(coin, 0.0)
        delta = sz if side == "B" else -sz
        # Only the part that opens or grows exposure needs margin.
        if pos == 0 or (pos > 0) == (delta > 0):
            opening = abs(delta)                    # same direction: all of it grows the position
        else:
            opening = max(0.0, abs(delta) - abs(pos))  # opposite: only a flip past zero opens
        if opening == 0:
            return True
        if opening * px < MIN_NOTIONAL:
            return False
        need = opening * px / MAX_LEV[coin]         # best case: max leverage (real setting is hidden)
        return value - used >= need

    # No snapshot: use the learn rule. Look at the user's IOCs that OUR engine could fill
    # (so "nothing to fill" is ruled out). If the latest of those printed nothing, the
    # chain most likely rejected it for margin, and it probably still will.
    fillable = [printed for (u_, filled, printed) in history if u_ == user and filled]
    if fillable and not fillable[-1]:
        return False
    return True


# ---------------------------------------------------------------------------
# P3. Which order does this cancel mean?
# ---------------------------------------------------------------------------
def _x(pos):
    block, tx = pos
    return block + tx / 10_000                      # tx index orders within a block


def _estimate_oid(pos, anchors):
    """Piecewise-linear oid estimate at `pos` from sorted anchors (extrapolates at the ends)."""
    pts = [(_x(p), o) for p, o in anchors]
    x = _x(pos)
    if len(pts) == 1:
        return pts[0][1]
    for (x0, o0), (x1, o1) in zip(pts, pts[1:]):
        if x <= x1 or (x1, o1) == pts[-1]:
            if x < x0 and (x0, o0) != pts[0]:
                continue
            return o0 + (o1 - o0) * (x - x0) / (x1 - x0)
    return pts[-1][1]


def resolve_cancel(oid, orders, anchors):
    """oid:     the oid the cancel names
    orders:  {name: (block, tx_index)} the user's resting orders, oid unknown
    anchors: [((block, tx_index), oid)] placements whose oid we know
    Return the name of the targeted order, or None."""
    anchors = sorted(anchors, key=lambda a: _x(a[0]))
    # Oids are global and monotone in execution order, so the target must be placed
    # after every anchor with a smaller oid and before every anchor with a larger one.
    lo = max((_x(p) for p, o in anchors if o < oid), default=float("-inf"))
    hi = min((_x(p) for p, o in anchors if o > oid), default=float("inf"))
    exact = [p for p, o in anchors if o == oid]
    cands = {}
    for name, pos in orders.items():
        if exact:
            if pos == exact[0]:
                return name
            continue
        if lo < _x(pos) < hi:
            cands[name] = abs(_estimate_oid(pos, anchors) - oid)
    if not cands:
        return None
    return min(cands, key=cands.get)


# ---------------------------------------------------------------------------
# P4. Which post-only orders never rested?
# ---------------------------------------------------------------------------
def which_rejected(before, placements, after, tol=1e-9):
    """before, after: (sz, n) at one price on consecutive feed updates
    placements:    [(user, sz)] post-only orders our engine rested in between
    Return every set of users whose rejection explains `after`."""
    (sz0, n0), (sz1, n1) = before, after
    missing_sz = sz0 + sum(s for _, s in placements) - sz1
    missing_n = n0 + len(placements) - n1
    if missing_n < 0 or missing_sz < -tol:
        return []                                   # level grew more than placements allow
    fits = []
    for combo in combinations(placements, missing_n):
        if abs(sum(s for _, s in combo) - missing_sz) <= tol:
            fits.append({u_ for u_, _ in combo})
    return fits


# ---------------------------------------------------------------------------
# P5. Where are the batch boundaries?
# ---------------------------------------------------------------------------
def tx_class(tx):
    kind, tif = tx[2], tx[3]
    if kind == "order" and tif != "Alo":
        return 2
    return 1 if kind == "cancel" else 0


def _simulate(txs, book, cuts):
    asks = {px: [[u_, s] for u_, s in q] for px, q in book.items()}
    printed = []
    bounds = [0] + sorted(cuts) + [len(txs)]
    for a, b in zip(bounds, bounds[1:]):
        for idx, user, kind, tif, side, px, sz in sorted(txs[a:b], key=lambda t: (tx_class(t), t[0])):
            if kind == "cancel":
                q = asks.get(px, [])
                for o in q:
                    if o[0] == user:
                        q.remove(o)
                        break
            elif kind == "order" and side == "A":
                asks.setdefault(px, []).append([user, sz])   # ALO ask rests (no bids to cross here)
            elif kind == "order" and side == "B":
                rem = sz
                for p in sorted(asks):
                    if p > px or rem <= 0:
                        break
                    q = asks[p]
                    while q and rem > 0:
                        m = q[0]
                        x = min(m[1], rem)
                        printed.append((idx, m[0]))
                        m[1] -= x
                        rem -= x
                        if m[1] <= 0:
                            q.pop(0)
    return sorted(printed)


def batch_boundaries(txs, book, printed):
    """txs:     [(idx, user, kind, tif, side, px, sz)] in explorer order
    book:    {px: [(user, sz)]} asks before the block, front first
    printed: [(taker_idx, maker_user)]
    Return the fewest cut positions (a cut at k splits before txs[k])."""
    target = sorted(printed)
    for k in range(len(txs)):
        for cuts in combinations(range(1, len(txs)), k):
            if _simulate(txs, book, cuts) == target:
                return list(cuts)
    return None                                     # no batching reproduces the trades


# ---------------------------------------------------------------------------
# P6. When do reduce-only orders vanish?
# ---------------------------------------------------------------------------
def silent_cancels(orders, fills, position=None):
    """orders:   {name: (side, sz)} resting reduce-only orders ("B" bid / "A" ask)
    fills:    [(t, side, sz)] the user's fills in time order
    position: signed size from a snapshot, or None if unknown
    Return [(t, name, sure)] for orders the chain cancels on its own.
    sure=True when the position is known; False when it is only possible."""
    INF = float("inf")
    if position is not None:
        lo = hi = position
    else:
        # Reduce-only bids mean the user is short by at least the largest bid (the
        # puzzle's assumption); reduce-only asks mean long by at least the largest ask.
        bids = [s for side, s in orders.values() if side == "B"]
        asks = [s for side, s in orders.values() if side == "A"]
        lo, hi = -INF, INF
        if bids:
            hi = -max(bids)
        if asks:
            lo = max(asks)

    def can_reduce(side, p):                         # a bid reduces a short, an ask a long
        return p < 0 if side == "B" else p > 0

    out, live = [], dict(orders)
    for t, side, sz in fills:
        d = sz if side == "B" else -sz
        lo, hi = lo + d, hi + d
        for name, (oside, osz) in list(live.items()):
            # cancel for sure if NO position in [lo, hi] lets it reduce; maybe if SOME doesn't
            if oside == "B":
                sure, maybe = lo >= 0, hi >= 0
            else:
                sure, maybe = hi <= 0, lo <= 0
            if sure or maybe:
                out.append((t, name, sure))
                del live[name]
        # Open question (puzzle follow-up): a position that shrinks below an order's size
        # but stays nonzero. We leave those orders alone until the data says cancel vs shrink.
    return out


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("P1")
    qs = name_placeholders(
        (100.0, 7.0, 3),
        [(1, 9.0, 4), (2, 6.0, 3), (3, 4.5, 3)],
        [(1, "U", "place", 2.0)],
        [(3, "0xM", 1.5)],
    )
    print(f"  {len(qs)} queues fit")
    for q in qs:
        print("  ", q)

    print("P2")
    snap = {"A": (25, 32, {"BTC": 0.05}), "B": (10_000, 2_000, {})}
    hist = [("C", True, False)] * 3
    for o in [("A", "BTC", "B", 0.01, 60_000, False), ("A", "BTC", "A", 0.02, 60_000, False),
              ("B", "BTC", "B", 1.0, 60_000, False), ("C", "BTC", "B", 0.001, 60_000, False)]:
        print("  ", o[0], o[2], o[3], will_accept(o, snap, hist))

    print("P3")
    print("  ", resolve_cancel(
        5_350,
        {"a": (90, 0), "b": (105, 0), "c": (118, 0)},
        [((100, 3), 5_000), ((120, 1), 5_400)],
    ))

    print("P4")
    print("   case 1:", which_rejected((5.0, 2), [("A", 1.0), ("B", 2.0), ("C", 0.5)], (6.5, 4)))
    print("   case 2 as printed:", which_rejected((5.0, 2), [("A", 1.0), ("B", 1.0), ("C", 0.5)], (7.5, 4)))
    print("   case 2 with after=6.5:", which_rejected((5.0, 2), [("A", 1.0), ("B", 1.0), ("C", 0.5)], (6.5, 4)))

    print("P5")
    txs = [(0, "T", "order", "Ioc", "B", 100, 1.0),
           (1, "A", "cancel", None, "A", 100, 1.0),
           (2, "B", "order", "Alo", "A", 100, 1.0)]
    print("  ", batch_boundaries(txs, {100: [("A", 1.0)]}, [(0, "A")]))

    print("P6")
    print("   known:  ", silent_cancels({"r1": ("B", 0.6), "r2": ("B", 0.6)}, [(1, "B", 1.0)], position=-1.0))
    print("   unknown:", silent_cancels({"r1": ("B", 0.6), "r2": ("B", 0.6)}, [(1, "B", 1.0)]))
