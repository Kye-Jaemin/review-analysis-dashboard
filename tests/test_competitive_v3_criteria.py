"""Unit tests for the competitive-criteria grouping normalizer.

_normalize_criteria is the pure (no-LLM) core that turns a raw Claude
grouping response into the canonical groups shape, so we can assert its
guarantees without any API calls.
"""
from app.services.competitive_v3 import _UNASSIGNED_NAME, _normalize_criteria

CATS = ["동기부여·습관 형성", "개인화 코칭·인사이트", "게임화·보상", "음식 DB·검색"]


def _flatten(result):
    return [c for g in result["groups"] for c in g["categories"]]


def test_every_category_assigned_exactly_once():
    parsed = {
        "criteria": ["코칭/Action", "콘텐츠"],
        "assignments": [
            {"category": "동기부여·습관 형성", "criterion": "코칭/Action"},
            {"category": "개인화 코칭·인사이트", "criterion": "코칭/Action"},
            {"category": "게임화·보상", "criterion": "코칭/Action"},
            {"category": "음식 DB·검색", "criterion": "콘텐츠"},
        ],
    }
    result = _normalize_criteria(parsed, CATS)
    flat = _flatten(result)
    assert sorted(flat) == sorted(CATS)
    assert len(flat) == len(set(flat))  # no dupes
    # Declared order preserved.
    assert result["groups"][0]["name"] == "코칭/Action"


def test_missing_assignment_falls_into_unassigned():
    parsed = {
        "criteria": ["코칭/Action"],
        "assignments": [
            {"category": "동기부여·습관 형성", "criterion": "코칭/Action"},
        ],
    }
    result = _normalize_criteria(parsed, CATS)
    assert sorted(_flatten(result)) == sorted(CATS)
    last = result["groups"][-1]
    assert last["name"] == _UNASSIGNED_NAME
    assert "음식 DB·검색" in last["categories"]


def test_unknown_category_in_assignment_ignored():
    parsed = {
        "criteria": ["A"],
        "assignments": [
            {"category": "동기부여·습관 형성", "criterion": "A"},
            {"category": "존재하지 않는 카테고리", "criterion": "A"},
        ],
    }
    result = _normalize_criteria(parsed, CATS)
    flat = _flatten(result)
    assert "존재하지 않는 카테고리" not in flat
    assert sorted(flat) == sorted(CATS)


def test_duplicate_assignment_first_wins():
    parsed = {
        "criteria": ["A", "B"],
        "assignments": [
            {"category": "게임화·보상", "criterion": "A"},
            {"category": "게임화·보상", "criterion": "B"},  # ignored
        ],
    }
    result = _normalize_criteria(parsed, CATS)
    a = next(g for g in result["groups"] if g["name"] == "A")
    assert "게임화·보상" in a["categories"]
    # appears only once overall
    assert _flatten(result).count("게임화·보상") == 1


def test_criterion_only_in_assignments_is_kept():
    parsed = {
        "criteria": [],  # LLM forgot to declare
        "assignments": [
            {"category": "게임화·보상", "criterion": "보상 시스템"},
        ],
    }
    result = _normalize_criteria(parsed, ["게임화·보상"])
    assert any(g["name"] == "보상 시스템" for g in result["groups"])


def test_empty_input():
    result = _normalize_criteria({"criteria": [], "assignments": []}, [])
    assert result == {"groups": []}
