//! Touch-ledger subset search (port of `engine_py/replay.py::unique_subset`).
//!
//! Chain rule it serves: the recorded bbo gives the touch level's exact size and order count `(sz, n)`. When our
//! level holds `k` more orders than the chain's, the ledger removes them only if exactly ONE set of `k` orders
//! sums to the size excess. This module answers "which set, if unique".
//!
//! Exact DP over states `(count, sum)` with the number of ways capped at 2 (we only need to tell 0 / 1 / many),
//! sums above the target pruned. If `m - k < k` it searches the complement instead (fewer picks). The answer and
//! the `Budget` cut-off must match Python exactly, so the state count is computed the same way: after each item,
//! add the size of the new layer (= previous layer's keys plus the new ones).

use std::collections::HashMap;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Subset {
    /// The unique set of indices, sorted ascending.
    Unique(Vec<usize>),
    None,
    Ambiguous,
    Budget,
}

type Layer = HashMap<(u32, i64), u8>;

pub fn unique_subset(sizes: &[i64], k: i64, target: i64, budget: u64) -> Subset {
    let m = sizes.len() as i64;
    if k < 0 || k > m {
        return Subset::None;
    }
    let flip = m - k < k;
    let total: i64 = sizes.iter().sum();
    let (kk, tt) = if flip { ((m - k) as u32, total - target) } else { (k as u32, target) };

    let mut layers: Vec<Layer> = Vec::with_capacity(sizes.len() + 1);
    let mut first = Layer::new();
    first.insert((0, 0), 1);
    layers.push(first);
    let mut states: u64 = 1;
    for &x in sizes {
        let prev = layers.last().expect("at least one layer");
        let mut cur = prev.clone();
        for (&(c, sm), &w) in prev.iter() {
            if c < kk && sm + x <= tt {
                let e = cur.entry((c + 1, sm + x)).or_insert(0);
                *e = (*e + w).min(2);
            }
        }
        states += cur.len() as u64;
        if states > budget {
            return Subset::Budget;
        }
        layers.push(cur);
    }
    let w = layers.last().and_then(|l| l.get(&(kk, tt))).copied().unwrap_or(0);
    if w == 0 {
        return Subset::None;
    }
    if w > 1 {
        return Subset::Ambiguous;
    }
    // Walk back: item i is in the set iff the state is unreachable from the first i items alone.
    let (mut c, mut sm) = (kk, tt);
    let mut chosen = vec![false; sizes.len()];
    for i in (0..sizes.len()).rev() {
        if layers[i].get(&(c, sm)).copied().unwrap_or(0) != 0 {
            continue;
        }
        chosen[i] = true;
        c -= 1;
        sm -= sizes[i];
    }
    let out = (0..sizes.len()).filter(|&i| chosen[i] != flip).collect();
    Subset::Unique(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    /// Brute force over all k-subsets (small inputs only).
    fn brute(sizes: &[i64], k: usize, target: i64) -> Subset {
        let m = sizes.len();
        let mut sols = vec![];
        for mask in 0u32..(1 << m) {
            if mask.count_ones() as usize == k {
                let s: i64 = (0..m).filter(|&i| mask >> i & 1 == 1).map(|i| sizes[i]).sum();
                if s == target {
                    sols.push((0..m).filter(|&i| mask >> i & 1 == 1).collect::<Vec<_>>());
                }
            }
        }
        match sols.len() {
            0 => Subset::None,
            1 => Subset::Unique(sols.pop().unwrap()),
            _ => Subset::Ambiguous,
        }
    }

    #[test]
    fn examples() {
        assert_eq!(unique_subset(&[5, 3, 2], 1, 3, 300_000), Subset::Unique(vec![1]));
        assert_eq!(unique_subset(&[5, 3, 2], 2, 5, 300_000), Subset::Unique(vec![1, 2]));
        assert_eq!(unique_subset(&[2, 2], 1, 2, 300_000), Subset::Ambiguous);
        assert_eq!(unique_subset(&[4, 4], 1, 3, 300_000), Subset::None);
        assert_eq!(unique_subset(&[1, 2], 3, 3, 300_000), Subset::None);
        assert_eq!(unique_subset(&[1, 2, 3, 4], 3, 9, 300_000), Subset::Unique(vec![1, 2, 3]));   // flipped search
        assert_eq!(unique_subset(&(1..=40).collect::<Vec<_>>(), 10, 200, 50), Subset::Budget);
    }

    proptest! {
        #[test]
        fn matches_brute_force(sizes in proptest::collection::vec(1i64..12, 0..11), k in 0usize..11, t in 0i64..60) {
            prop_assume!(k <= sizes.len());
            prop_assert_eq!(unique_subset(&sizes, k as i64, t, 300_000), brute(&sizes, k, t));
        }
    }
}
