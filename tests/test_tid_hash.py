"""I27: Hyperliquid's trade id is the top 50 bits of SipHash-1-3 (zero keys) over (buyer_oid, seller_oid) LE u64s.
Triples from recorded userFills where both sides were captured (public trade ids / order ids)."""
from data.tid_hash import siphash13, tid


def test_tid_matches_recorded_fills():
    assert tid(609934270842, 609934284737) == 638314451391629
    assert tid(609934270842, 609934312074) == 361346074318540
    assert tid(609934275848, 609934336355) == 84542641196200
    assert tid(609934243376, 609934241551) == 138275746160289
    assert tid(609934284737, 609934270842) != 638314451391629      # order matters: (buyer, seller)


def test_siphash13_reference_vector():
    # Rust std DefaultHasher::new() (SipHash-1-3, keys 0) of the empty input
    assert siphash13(b"") == 0xD1FBA762150C532C
