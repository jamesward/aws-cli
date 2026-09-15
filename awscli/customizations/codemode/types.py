"""TOWL v3 types (TOWL_SPEC.md §4): one collection type, closed records, the single union ``T | Null``,
and an opaque ``json`` for values the catalog could not type."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class Type:
    __slots__ = ()


class _Scalar(Type):
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name

    __str__ = __repr__


STRING = _Scalar("string")
INT = _Scalar("int")
NUMBER = _Scalar("number")
BOOL = _Scalar("bool")
TIMESTAMP = _Scalar("timestamp")
NULL = _Scalar("Null")
JSON = _Scalar("json")
ERROR = _Scalar("?")  # poison type produced by a diagnostic; assignable anywhere, so one error does not cascade


@dataclass(frozen=True)
class TList(Type):
    element: Type

    def __str__(self):
        return f"list[{self.element}]"

    __repr__ = __str__


class TRecord(Type):
    """Closed record. ``fields`` may be computed lazily (recursive provider shapes)."""

    __slots__ = ("_fields", "_thunk", "name")

    def __init__(self, fields=None, name=None, thunk=None):
        self._fields = dict(fields) if fields is not None else None
        self._thunk = thunk
        self.name = name

    @property
    def fields(self) -> Dict[str, Type]:
        if self._fields is None:
            self._fields = dict(self._thunk())
        return self._fields

    def __str__(self):
        if self.name:
            return self.name
        if not self.fields:
            return "{}"
        return "{ " + ", ".join(f"{k}: {v}" for k, v in self.fields.items()) + " }"

    __repr__ = __str__

    def __eq__(self, other):
        if not isinstance(other, TRecord):
            return False
        if self.name and other.name:
            return self.name == other.name
        return self.fields == other.fields

    def __hash__(self):
        return hash(self.name) if self.name else hash(tuple(sorted(self.fields)))


@dataclass(frozen=True)
class TNullable(Type):
    inner: Type

    def __str__(self):
        return f"{self.inner} | Null"

    __repr__ = __str__


def nullable(t: Type) -> Type:
    return t if isinstance(t, TNullable) or t is NULL else TNullable(t)


def strip_null(t: Type) -> Type:
    return t.inner if isinstance(t, TNullable) else t


def is_nullable(t: Type) -> bool:
    return isinstance(t, TNullable) or t is NULL


def is_scalar(t: Type) -> bool:
    return t in (STRING, INT, NUMBER, BOOL, TIMESTAMP)


def is_numeric(t: Type) -> bool:
    return t in (INT, NUMBER)


def is_ordered(t: Type) -> bool:
    return t in (INT, NUMBER, STRING, TIMESTAMP)


def is_equatable(t: Type, _seen=None) -> bool:
    if t is JSON:
        return False
    if isinstance(t, TNullable):
        return is_equatable(t.inner, _seen)
    if isinstance(t, TList):
        return is_equatable(t.element, _seen)
    if isinstance(t, TRecord):
        seen = _seen or set()
        if t.name in seen:
            return True
        return all(is_equatable(v, seen | {t.name}) for v in t.fields.values())
    return True


def assignable(src: Type, dst: Type, _depth=0, relaxed=False) -> bool:
    """``src`` may be used where ``dst`` is expected. The only coercion is int -> number.

    ``relaxed`` is TOWL §6.1's parameter rule: a ``T | Null`` value may be supplied where ``T`` is required,
    at any depth, with a runtime ``data`` check (see ``null_at_required``)."""
    if src == dst or src is ERROR or dst is ERROR or dst is JSON:
        return True
    if src is INT and dst is NUMBER:
        return True
    if isinstance(dst, TNullable):
        return src is NULL or assignable(strip_null(src), dst.inner, _depth, relaxed)
    if isinstance(src, TNullable):
        return relaxed and assignable(src.inner, dst, _depth, relaxed)
    if isinstance(src, TList) and isinstance(dst, TList):
        return assignable(src.element, dst.element, _depth, relaxed)
    if isinstance(src, TRecord) and isinstance(dst, TRecord):
        if _depth > 6:
            return True
        return all(
            (k in src.fields and assignable(src.fields[k], t, _depth + 1, relaxed)) or (k not in src.fields and (is_nullable(t) or isinstance(t, TList)))
            for k, t in dst.fields.items()
        ) and all(k in dst.fields for k in src.fields)
    return False


def null_at_required(value: Any, t: Type, path: str = "") -> Optional[str]:
    """First path where a Null sits in a non-nullable position of ``t`` (the runtime side of the relaxation)."""
    if value is None:
        return None if is_nullable(t) or t in (JSON, ERROR) or isinstance(t, TList) else (path or "<value>")
    if isinstance(t, TNullable):
        return null_at_required(value, t.inner, path)
    if isinstance(t, TList) and isinstance(value, list):
        for i, v in enumerate(value):
            p = null_at_required(v, t.element, f"{path}[{i}]")
            if p:
                return p
    if isinstance(t, TRecord) and isinstance(value, dict):
        for k, v in value.items():
            ft = t.fields.get(k)
            if ft is None:
                continue
            p = null_at_required(v, ft, f"{path}.{k}" if path else k)
            if p:
                return p
    return None


def join(a: Type, b: Type) -> Optional[Type]:
    """Join for list literals and branches: equal, or int/number, or Null-lifting."""
    if a == b:
        return a
    if a is ERROR:
        return b
    if b is ERROR:
        return a
    if a is NULL:
        return nullable(b)
    if b is NULL:
        return nullable(a)
    if is_numeric(a) and is_numeric(b):
        return NUMBER
    if isinstance(a, TNullable) or isinstance(b, TNullable):
        j = join(strip_null(a), strip_null(b))
        return nullable(j) if j is not None else None
    if isinstance(a, TList) and isinstance(b, TList):
        j = join(a.element, b.element)
        return TList(j) if j is not None else None
    if isinstance(a, TRecord) and isinstance(b, TRecord) and set(a.fields) == set(b.fields):
        out = {}
        for k, t in a.fields.items():
            j = join(t, b.fields[k])
            if j is None:
                return None
            out[k] = j
        return TRecord(out)
    return None


def conforms(value: Any, t: Type) -> bool:
    """Best-effort runtime check that a host-supplied input matches its declared type."""
    if t in (JSON, ERROR):
        return True
    if t is NULL:
        return value is None
    if isinstance(t, TNullable):
        return value is None or conforms(value, t.inner)
    if t in (STRING, TIMESTAMP):
        return isinstance(value, str)
    if t is INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if t is NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t is BOOL:
        return isinstance(value, bool)
    if isinstance(t, TList):
        return isinstance(value, list) and all(conforms(v, t.element) for v in value)
    if isinstance(t, TRecord):
        return isinstance(value, dict) and all(conforms(value.get(k), ft) for k, ft in t.fields.items()) and all(k in t.fields for k in value)
    return False


def normalize(value: Any, t: Type, _depth=0) -> Any:
    """Normalize a decoded provider value against its type: absent list members become ``[]``."""
    if value is None:
        return [] if isinstance(t, TList) else None
    if isinstance(t, TNullable):
        return normalize(value, t.inner, _depth)
    if isinstance(t, TList) and isinstance(value, list):
        return [normalize(v, t.element, _depth + 1) for v in value]
    if isinstance(t, TRecord) and isinstance(value, dict) and _depth < 32:
        out = {}
        for k, v in value.items():
            ft = t.fields.get(k)
            out[k] = normalize(v, ft, _depth + 1) if ft is not None else v
        for k, ft in t.fields.items():
            if k not in out and isinstance(ft, TList):
                out[k] = []
        return out
    return value


def parse_type(text: str, shape_lookup=None) -> Type:
    """Parse TOWL type text (inputs)."""
    from .syntax import Parser  # local import to avoid a cycle

    return Parser(f"towl 3 input _: {text}\n1", set(), shape_lookup=shape_lookup).program().inputs[0].type


def describe(t: Type, depth: int = 1) -> str:
    """Type text with records expanded to ``depth`` levels; deeper named records print by name."""
    if isinstance(t, TNullable):
        return f"{describe(t.inner, depth)} | Null"
    if isinstance(t, TList):
        return f"list[{describe(t.element, depth)}]"
    if isinstance(t, TRecord):
        if depth <= 0 and t.name:
            return t.name
        if not t.fields:
            return "{}"
        return "{ " + ", ".join(f"{k}: {describe(v, depth - 1)}" for k, v in t.fields.items()) + " }"
    return str(t)
