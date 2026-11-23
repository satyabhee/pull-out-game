"""deep_ledger fast5: forced_subset (orders in every consistent removal set)."""
from engine_py.replay import forced_subset


def test_unique_solution_is_fully_forced():
    assert forced_subset([5, 3, 7], 1, 3) == (1,)
    assert forced_subset([5, 3, 7], 2, 12) == (0, 2)


def test_only_the_intersection_of_several_solutions():
    # k=2, target 8: {0,1} (5+3) and {1,2}? 3+5 -> sizes [5,3,5]: {0,1} and {1,2}; index 1 in both
    assert forced_subset([5, 3, 5], 2, 8) == (1,)
    assert forced_subset([4, 4, 4], 1, 4) == ()          # any one of three: nothing forced


def test_none_and_whole_level():
    assert forced_subset([5, 3], 1, 4) == "none"
    assert forced_subset([5, 3], 2, 8) == (0, 1)        # n = 0 at that price: the whole level goes
    assert forced_subset([5, 3], 3, 8) == "none"


def test_budget():
    assert forced_subset(list(range(1, 40)), 10, 200, budget=50) == "budget"
