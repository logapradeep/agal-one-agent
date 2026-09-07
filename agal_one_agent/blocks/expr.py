"""Expression language — Python port of Agal/contracts/programs/reference/expr.mjs.

Grammar and semantics: Agal/contracts/programs/README.md §3. Conformance:
tests/test_blocks_expr.py runs the shared golden vectors
(Agal/contracts/programs/test-vectors/expressions.json). Behaviour must match the
JavaScript reference exactly, including null handling and float results.

AST nodes are tuples:
  ("num", v) ("str", v) ("bool", v) ("ref", scope|None, name)
  ("call", fn, [args]) ("un", op, a) ("bin", op, a, b)
"""

from __future__ import annotations

import math
import re
from typing import Any, Optional

KEYWORDS = {"and", "or", "not", "true", "false"}
# arity: -n means variadic with at least n arguments
FUNCTIONS = {
    "baseline": 1, "since": 1, "abs": 1, "clamp": 3, "hour": 0, "minute": 0, "weekday": 0,
    "mean": -2, "min": -2, "max": -2,
}
MAX_LEN = 500
_CMP = ("<", "<=", ">", ">=", "==", "!=")


class ExprError(ValueError):
    def __init__(self, message: str, pos: int = 0):
        super().__init__(message)
        self.pos = pos


# ---------------------------------------------------------------- tokenizer

def tokenize(src: str) -> list[tuple[str, Any, int]]:
    if not isinstance(src, str) or len(src) == 0:
        raise ExprError("empty expression", 0)
    if len(src) > MAX_LEN:
        raise ExprError(f"expression longer than {MAX_LEN} characters", 0)
    toks: list[tuple[str, Any, int]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in " \t\n\r":
            i += 1
            continue
        if c.isdigit():
            j = i
            while j < n and src[j].isdigit():
                j += 1
            if j < n and src[j] == "." and j + 1 < n and src[j + 1].isdigit():
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            toks.append(("num", float(src[i:j]), i))
            i = j
            continue
        if "a" <= c <= "z":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_") and src[j].isascii():
                j += 1
            w = src[i:j]
            toks.append(("kw" if w in KEYWORDS else "id", w, i))
            i = j
            continue
        if ("A" <= c <= "Z") or c == "_":
            raise ExprError(f"identifiers start with a lowercase letter (at {i})", i)
        if c == "'":
            j = src.find("'", i + 1)
            if j < 0:
                raise ExprError("unterminated string", i)
            toks.append(("str", src[i + 1:j], i))
            i = j + 1
            continue
        two = src[i:i + 2]
        if two in ("<=", ">=", "==", "!="):
            toks.append(("op", two, i))
            i += 2
            continue
        if c in "<>+-*/%(),.":
            toks.append(("op", c, i))
            i += 1
            continue
        raise ExprError(f"unexpected character '{c}' at {i}", i)
    toks.append(("eof", "", n))
    return toks


# ------------------------------------------------------------------- parser

def parse(src: str):
    toks = tokenize(src)
    p = 0

    def peek():
        return toks[p]

    def nxt():
        nonlocal p
        t = toks[p]
        p += 1
        return t

    def is_op(v):
        t = peek()
        return t[0] == "op" and t[1] == v

    def is_kw(v):
        t = peek()
        return t[0] == "kw" and t[1] == v

    def expect(v):
        if not is_op(v):
            t = peek()
            raise ExprError(f"expected '{v}' at {t[2]}", t[2])
        nxt()

    def p_or():
        a = p_and()
        while is_kw("or"):
            nxt()
            a = ("bin", "or", a, p_and())
        return a

    def p_and():
        a = p_not()
        while is_kw("and"):
            nxt()
            a = ("bin", "and", a, p_not())
        return a

    def p_not():
        if is_kw("not"):
            nxt()
            return ("un", "not", p_not())
        return p_cmp()

    def p_cmp():
        a = p_add()
        t = peek()
        if t[0] == "op" and t[1] in _CMP:
            op = nxt()[1]
            b = p_add()
            t2 = peek()
            if t2[0] == "op" and t2[1] in _CMP:
                raise ExprError(f"comparisons do not chain (at {t2[2]})", t2[2])
            return ("bin", op, a, b)
        return a

    def p_add():
        a = p_mul()
        while is_op("+") or is_op("-"):
            op = nxt()[1]
            a = ("bin", op, a, p_mul())
        return a

    def p_mul():
        a = p_un()
        while is_op("*") or is_op("/") or is_op("%"):
            op = nxt()[1]
            a = ("bin", op, a, p_un())
        return a

    def p_un():
        if is_op("-"):
            nxt()
            return ("un", "neg", p_un())
        return p_primary()

    def p_primary():
        t = peek()
        if t[0] == "num":
            nxt()
            return ("num", t[1])
        if t[0] == "str":
            nxt()
            return ("str", t[1])
        if t[0] == "kw" and t[1] in ("true", "false"):
            nxt()
            return ("bool", t[1] == "true")
        if t[0] == "id":
            nxt()
            if is_op("("):
                nxt()
                args = []
                if not is_op(")"):
                    args.append(p_or())
                    while is_op(","):
                        nxt()
                        args.append(p_or())
                expect(")")
                return ("call", t[1], args)
            if is_op("."):
                nxt()
                m = nxt()
                if m[0] != "id":
                    raise ExprError(f"expected name after '.' at {m[2]}", m[2])
                return ("ref", t[1], m[1])
            return ("ref", None, t[1])
        if is_op("("):
            nxt()
            e = p_or()
            expect(")")
            return e
        raise ExprError(f"unexpected token '{t[1]}' at {t[2]}", t[2])

    ast = p_or()
    t = peek()
    if t[0] != "eof":
        raise ExprError(f"unexpected token '{t[1]}' at {t[2]}", t[2])
    return ast


# ---------------------------------------------------------------- validator

def validate(ast, scope: Optional[dict] = None) -> tuple[str, list[str]]:
    """Return (type, errors). scope = {"variables": {name: type}, "settings": {name: type}}."""
    errors: list[str] = []
    variables = (scope or {}).get("variables", {}) or {}
    settings = (scope or {}).get("settings", {}) or {}

    def type_of(n) -> str:
        k = n[0]
        if k == "num":
            return "number"
        if k == "str":
            return "string"
        if k == "bool":
            return "bool"
        if k == "ref":
            _, sc, name = n
            if sc is not None:
                if sc != "settings":
                    errors.append(f"unknown scope '{sc}' (only settings.<name>)")
                    return "error"
                if name not in settings:
                    errors.append(f"unknown setting '{name}'")
                    return "error"
                return settings[name]
            if name not in variables:
                errors.append(f"unknown variable '{name}'")
                return "error"
            return "number" if variables[name] == "enum" else variables[name]
        if k == "call":
            _, fn, args = n
            if fn not in FUNCTIONS:
                errors.append(f"unknown function '{fn}'")
                return "error"
            ar = FUNCTIONS[fn]
            if ar >= 0 and len(args) != ar:
                errors.append(f"{fn}() takes {ar} argument{'' if ar == 1 else 's'}")
            if ar < 0 and len(args) < -ar:
                errors.append(f"{fn}() takes at least {-ar} arguments")
            if fn in ("baseline", "since"):
                a = args[0] if args else None
                if a is None or a[0] != "ref" or a[1] is not None:
                    errors.append(f"{fn}() takes a variable name")
                    return "number"
                if a[2] not in variables:
                    errors.append(f"unknown variable '{a[2]}'")
                elif fn == "baseline" and variables[a[2]] != "number":
                    errors.append("baseline() needs a numeric variable")
                return "number"
            arg_types = [type_of(a) for a in args]
            if fn in ("hour", "minute", "weekday"):
                return "number"
            for i, t in enumerate(arg_types):
                if t not in ("number", "error"):
                    errors.append(f"{fn}() argument {i + 1} must be a number")
            return "number"
        if k == "un":
            _, op, a = n
            t = type_of(a)
            if op == "not":
                if t not in ("bool", "error"):
                    errors.append("'not' needs a bool")
                return "bool"
            if t not in ("number", "error"):
                errors.append("unary '-' needs a number")
            return "number"
        if k == "bin":
            _, op, a, b = n
            ta, tb = type_of(a), type_of(b)
            if ta == "error" or tb == "error":
                return "bool" if op in ("and", "or") + _CMP else "number"
            if op in ("and", "or"):
                if ta != "bool" or tb != "bool":
                    errors.append(f"'{op}' needs bools")
                return "bool"
            if op in ("<", "<=", ">", ">="):
                if ta != "number" or tb != "number":
                    errors.append(f"'{op}' needs numbers")
                return "bool"
            if op in ("==", "!="):
                if ta != tb:
                    errors.append(f"'{op}' needs the same type on both sides")
                return "bool"
            if ta != "number" or tb != "number":
                errors.append(f"'{op}' needs numbers")
            return "number"
        errors.append("malformed expression")
        return "error"

    return type_of(ast), errors


# ---------------------------------------------------------------- evaluator

def _is_null(v) -> bool:
    return v is None


def evaluate(ast, env: Optional[dict] = None):
    """env = {vars, settings, baselines, since, clock:{hour,minute,weekday}}. Null is None."""
    env = env or {}
    vars_ = env.get("vars", {}) or {}
    settings = env.get("settings", {}) or {}
    baselines = env.get("baselines", {}) or {}
    since = env.get("since", {}) or {}
    clock = env.get("clock", {}) or {"hour": 0, "minute": 0, "weekday": 1}

    def ev(n):
        k = n[0]
        if k in ("num", "str", "bool"):
            return n[1]
        if k == "ref":
            _, sc, name = n
            return settings.get(name) if sc is not None else vars_.get(name)
        if k == "call":
            _, fn, args = n
            if fn == "baseline":
                return baselines.get(args[0][2])
            if fn == "since":
                return since.get(args[0][2])
            if fn == "hour":
                return clock.get("hour", 0)
            if fn == "minute":
                return clock.get("minute", 0)
            if fn == "weekday":
                return clock.get("weekday", 1)
            a = [ev(x) for x in args]
            if any(_is_null(x) for x in a):
                return None
            if fn == "abs":
                return abs(a[0])
            if fn == "clamp":
                return min(max(a[0], a[1]), a[2])
            if fn == "mean":
                s = 0.0
                for x in a:
                    s = s + x
                return s / len(a)
            if fn == "min":
                return min(a)
            if fn == "max":
                return max(a)
            raise ExprError(f"unknown function {fn}")
        if k == "un":
            _, op, a = n
            v = ev(a)
            if op == "not":
                return False if _is_null(v) else (not v)
            return None if _is_null(v) else -v
        if k == "bin":
            _, op, a, b = n
            if op == "and":
                va = ev(a)
                if _is_null(va) or va is False:
                    return False
                vb = ev(b)
                return False if _is_null(vb) else vb
            if op == "or":
                va = ev(a)
                if va is True:
                    return True
                vb = ev(b)
                return (va is True) if _is_null(vb) else vb
            va, vb = ev(a), ev(b)
            if op in _CMP:
                if _is_null(va) or _is_null(vb):
                    return False
                if op == "<":
                    return va < vb
                if op == "<=":
                    return va <= vb
                if op == ">":
                    return va > vb
                if op == ">=":
                    return va >= vb
                if op == "==":
                    return _strict_eq(va, vb)
                return not _strict_eq(va, vb)
            if _is_null(va) or _is_null(vb):
                return None
            if op == "+":
                return va + vb
            if op == "-":
                return va - vb
            if op == "*":
                return va * vb
            if op == "/":
                return None if vb == 0 else va / vb
            if op == "%":
                return None if vb == 0 else math.fmod(va, vb)
        raise ExprError("malformed expression")

    return ev(ast)


def _strict_eq(a, b) -> bool:
    """JavaScript === for the value kinds we carry (bool, number, string)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return type(a) is type(b) and a == b


def run(src: str, scope: Optional[dict], env: Optional[dict]) -> dict:
    ast = parse(src)
    t, errs = validate(ast, scope)
    if errs:
        return {"errors": errs}
    return {"type": t, "value": evaluate(ast, env)}


_IDENT = re.compile(r"^[a-z][a-zA-Z0-9_]{0,63}$")


def is_identifier(name: str) -> bool:
    return bool(_IDENT.match(name or ""))
