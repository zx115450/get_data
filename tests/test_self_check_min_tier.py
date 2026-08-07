"""self_check：random 小档压测辅助。"""
from __future__ import annotations

from agent.tools import self_check as sc
from agent.prompts.types import TYPE_STRING


def test_small_n_tier_indices():
    assert sc._small_n_tier_indices(27) == [0, 1, 2]
    assert sc._small_n_tier_indices(1) == [0]
    assert sc._small_n_tier_indices(2) == [0, 1]
    assert sc._small_n_tier_indices(9) == [0, 1, 2]


def test_constraint_lo():
    assert sc._constraint_lo({"n": [6, 1000], "T": [1, 100]}) == 6
    assert sc._constraint_lo({"T": [1, 100], "|S|": [3, 50]}) == 3
    assert sc._constraint_lo({"T": [1, 100], "S": [6, 1000]}) == 6
    assert sc._constraint_lo({}) is None
    assert sc._constraint_lo(None) is None


def test_append_min_tier_stress():
    checks: list = []
    sc._append_min_tier_random_stress(checks, 27, seeds_per_index=2, base_seed=2200)
    # 3 indices × 2 seeds
    assert len(checks) == 6
    assert all(t == "random" and slot is None for t, _, _, slot in checks)
    idxs = {idx for _, _, idx, _ in checks}
    assert idxs == {0, 1, 2}
    seeds = [seed for _, seed, _, _ in checks]
    assert seeds == [2200, 2201, 2210, 2211, 2220, 2221]


def test_rnd_empty_hint():
    assert sc._rnd_empty_hint(["FAIL other"]) == ""
    h = sc._rnd_empty_hint(
        [
            "FAIL type=random seed=11: ERROR gen rc=3: "
            "FAIL random_t::next(long long n): n must be positive"
        ]
    )
    assert "枚举" in h
    assert "pool" in h or "p1" in h


def test_val_readstring_hint():
    assert sc._val_readstring_hint(["FAIL type=x validate: other"]) == ""
    h = sc._val_readstring_hint(
        [
            "FAIL type=edge_S_min seed=1000 validate: ERROR validate rc=3: FAIL |S| out of range",
            "FAIL type=random seed=2000 validate: ERROR validate rc=3: FAIL |S| out of range",
        ]
    )
    assert "readToken" in h
    assert "readString" in h


def test_type_string_has_pos_neg_examples():
    assert "【反例 · 禁止】" in TYPE_STRING
    assert "【正例 · 必须】" in TYPE_STRING
    assert "cand" in TYPE_STRING
    assert "崩溃" in TYPE_STRING
    assert "readToken" in TYPE_STRING
    assert "readString" in TYPE_STRING
