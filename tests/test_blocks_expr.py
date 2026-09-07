"""Conformance of the Python expression port against the shared golden vectors
(Agal/contracts/programs/test-vectors/expressions.json)."""

from __future__ import annotations

import json
import math
import os

import pytest

from agal_one_agent.blocks import expr as E

HERE = os.path.dirname(__file__)
VECTORS = os.path.normpath(os.path.join(HERE, "..", "..", "..", "contracts", "programs", "test-vectors", "expressions.json"))


def _load():
    if not os.path.exists(VECTORS):
        pytest.skip(f"vectors not found at {VECTORS}")
    with open(VECTORS) as f:
        return json.load(f)


def _cases():
    try:
        v = _load()
    except Exception:  # noqa: BLE001 — skip handled inside the test
        return []
    return [(c["expr"], c) for c in v["cases"]]


@pytest.mark.parametrize("src,case", _cases())
def test_vector(src, case):
    v = _load()
    scope = case.get("scope", v["defaults"]["scope"])
    env = dict(v["defaults"]["env"])
    if "env" in case:
        env.update(case["env"])
    if case.get("parseError"):
        with pytest.raises(E.ExprError):
            E.parse(src)
        return
    ast = E.parse(src)
    _, errors = E.validate(ast, scope)
    if case.get("errors"):
        assert len(errors) >= case["errors"], errors
        return
    assert errors == [], errors
    value = E.evaluate(ast, env)
    expected = case["expected"]
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        assert isinstance(value, (int, float)) and not isinstance(value, bool), (value, expected)
        assert math.isclose(value, expected, rel_tol=0, abs_tol=1e-12), (value, expected)
    else:
        assert value == expected and type(value) is type(expected), (value, expected)


def test_vectors_present():
    v = _load()
    assert len(v["cases"]) >= 50
