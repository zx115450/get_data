"""pipeline/gen_data：normalize_range_json / validate_range_json。"""
from __future__ import annotations

import pytest

from pipeline.gen_data import (
    DEFAULT_MEMORY_LIMIT_MB,
    DEFAULT_REGULAR_COUNT,
    DEFAULT_TIME_LIMIT_MS,
    MIN_REGULAR_COUNT,
    normalize_range_json,
    parse_constraint_entry,
    validate_range_json,
)


def _valid_base(**overrides):
    data = {
        "count": MIN_REGULAR_COUNT,
        "constraints": {"n": {"type": "int", "min": 1, "max": 100}},
        "edge_cases": ["edge_n_min", "edge_n_max"],
    }
    data.update(overrides)
    return data


class TestValidateRangeJson:
    def test_ok(self):
        assert validate_range_json(_valid_base()) == []

    def test_ok_legacy_list(self):
        assert validate_range_json(_valid_base(constraints={"n": [1, 100]})) == []

    def test_not_dict(self):
        errs = validate_range_json([])  # type: ignore[arg-type]
        assert errs and "顶层" in errs[0]

    def test_count_too_small(self):
        errs = validate_range_json(_valid_base(count=3))
        assert any("count" in e for e in errs)

    def test_constraints_empty(self):
        errs = validate_range_json(_valid_base(constraints={}))
        assert any("constraints" in e for e in errs)

    def test_constraints_bad_range(self):
        errs = validate_range_json(_valid_base(constraints={"n": [10, 1]}))
        assert any("constraints" in e for e in errs)

    def test_double_requires_decimals(self):
        errs = validate_range_json(
            _valid_base(
                constraints={
                    "s": {"type": "double", "min": 0, "max": 100},
                }
            )
        )
        assert any("decimals" in e for e in errs)

    def test_double_ok(self):
        assert (
            validate_range_json(
                _valid_base(
                    constraints={
                        "s": {"type": "double", "min": 0, "max": 100, "decimals": 1},
                    }
                )
            )
            == []
        )

    def test_missing_edge_cases(self):
        d = _valid_base()
        del d["edge_cases"]
        errs = validate_range_json(d)
        assert any("edge_cases" in e for e in errs)

    def test_extreme_without_edge_prefix(self):
        errs = validate_range_json(_valid_base(edge_cases=["n_min"]))
        assert any("edge_" in e for e in errs)

    def test_special_samples_count_requires_room(self):
        errs = validate_range_json(
            _valid_base(
                count=MIN_REGULAR_COUNT,
                special_samples_desc="特殊构造",
                special_samples_count=5,
            )
        )
        assert any("special_samples_count" in e or "count" in e for e in errs)


class TestParseConstraintEntry:
    def test_legacy_list(self):
        spec, err = parse_constraint_entry([1, 10])
        assert err is None
        assert spec == {"type": "int", "min": 1, "max": 10}

    def test_float_alias(self):
        spec, err = parse_constraint_entry(
            {"type": "float", "min": 0, "max": 1, "decimals": 2}
        )
        assert err is None
        assert spec["type"] == "double"
        assert spec["decimals"] == 2


class TestNormalizeRangeJson:
    def test_default_count_and_limits(self):
        out = normalize_range_json({"constraints": {"n": [1, 10]}, "edge_cases": []})
        assert out["count"] == DEFAULT_REGULAR_COUNT
        assert out["time_limit_ms"] == DEFAULT_TIME_LIMIT_MS
        assert out["memory_limit_mb"] == DEFAULT_MEMORY_LIMIT_MB
        assert out["constraints"]["n"] == {"type": "int", "min": 1, "max": 10}

    def test_strips_random_and_dedupes(self):
        out = normalize_range_json(
            _valid_base(edge_cases=["edge_n_min", "random", "edge_n_min", "chain"])
        )
        assert "random" not in out["edge_cases"]
        assert out["edge_cases"].count("edge_n_min") == 1
        assert "chain" in out["edge_cases"]

    def test_drops_multi_t_edges_without_t(self):
        out = normalize_range_json(
            _valid_base(edge_cases=["edge_n_min", "edge_Tmax", "edge_T1"])
        )
        assert "edge_Tmax" not in out["edge_cases"]
        assert "edge_T1" not in out["edge_cases"]

    def test_keeps_multi_t_edges_with_t(self):
        out = normalize_range_json(
            _valid_base(
                constraints={"T": [1, 10], "n": [1, 100]},
                edge_cases=["edge_Tmax", "edge_n_min"],
            )
        )
        assert "edge_Tmax" in out["edge_cases"]
        assert out["constraints"]["T"]["type"] == "int"

    def test_empty_special_desc_removed(self):
        out = normalize_range_json(_valid_base(special_samples_desc=""))
        assert "special_samples_desc" not in out

    def test_raises_count_for_special(self):
        out = normalize_range_json(
            _valid_base(
                count=MIN_REGULAR_COUNT,
                special_samples_desc="构造",
                special_samples_count=3,
            )
        )
        assert out["count"] >= 3 + MIN_REGULAR_COUNT
