"""TOWL v3 types (TOWL_SPEC.md §4): one collection type, closed records, and an opaque ``json`` for values the
catalog could not type. There is no null type: absence is a runtime fact (§9.2), never part of a type. A record
may carry an informational ``optional`` set (members the provider may leave out), printed as ``Name?: T``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet


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
JSON = _Scalar("json")
ERROR = _Scalar("?")  # poison type produced by a diagnostic; assignable anywhere, so one error does not cascade


@dataclass(frozen=True)
class TList(Type):
    element: Type

    def __str__(self):
        return f"list[{self.element}]"

    __repr__ = __str__


class TRecord(Type):
    """Closed record. ``fields`` may be computed lazily (recursive provider shapes); the thunk returns either the
    fields or ``(fields, optional)``. ``optional`` never affects typing; it is shown to authors."""

    __slots__ = ("_fields", "_optional", "_thunk", "name")

    def __init__(self, fields=None, name=None, thunk=None, optional=()):
        self._fields = dict(fields) if fields is not None else None
        self._optional = frozenset(optional)
        self._thunk = thunk
        self.name = name

    def _force(self):
        got = self._thunk()
        if isinstance(got, tuple):
            self._fields, self._optional = dict(got[0]), frozenset(got[1])
        else:
            self._fields = dict(got)

    @property
    def fields(self) -> Dict[str, Type]:
        if self._fields is None:
            self._force()
        return self._fields

    @property
    def optional(self) -> FrozenSet[str]:
        if self._fields is None:
            self._force()
        return self._optional

    def __str__(self):
        if self.name:
            return self.name
        return _fields_text(self, lambda v: str(v))

    __repr__ = __str__

    def __eq__(self, other):
        if not isinstance(other, TRecord):
            return False
        if self.name and other.name:
            return self.name == other.name
        return self.fields == other.fields

    def __hash__(self):
        return hash(self.name) if self.name else hash(tuple(sorted(self.fields)))


def _fields_text(rec: TRecord, show) -> str:
    if not rec.fields:
        return "{}"
    opt = rec.optional
    return "{ " + ", ".join(f"{k}{'?' if k in opt else ''}: {show(v)}" for k, v in rec.fields.items()) + " }"


def is_scalar(t: Type) -> bool:
    return t in (STRING, INT, NUMBER, BOOL, TIMESTAMP)


def is_numeric(t: Type) -> bool:
    return t in (INT, NUMBER)


def is_ordered(t: Type) -> bool:
    return t in (INT, NUMBER, STRING, TIMESTAMP)


def is_equatable(t: Type, _seen=None) -> bool:
    if t is JSON:
        return False
    if isinstance(t, TList):
        return is_equatable(t.element, _seen)
    if isinstance(t, TRecord):
        seen = _seen or set()
        if t.name in seen:
            return True
        return all(is_equatable(v, seen | {t.name}) for v in t.fields.values())
    return True


def assignable(src: Type, dst: Type, _depth=0) -> bool:
    """``src`` may be used where ``dst`` is expected. The only coercion is int -> number. A record literal may
    omit members the destination marks optional (or list-typed); it may not add unknown members."""
    if src == dst or src is ERROR or dst is ERROR or dst is JSON:
        return True
    if src is INT and dst is NUMBER:
        return True
    if isinstance(src, TList) and isinstance(dst, TList):
        return assignable(src.element, dst.element, _depth)
    if isinstance(src, TRecord) and isinstance(dst, TRecord):
        if _depth > 6:
            return True
        return all(
            (k in src.fields and assignable(src.fields[k], t, _depth + 1)) or (k not in src.fields and (k in dst.optional or isinstance(t, TList)))
            for k, t in dst.fields.items()
        ) and all(k in dst.fields for k in src.fields)
    return False


def join(a: Type, b: Type):
    """Join for list literals: equal, or int/number, element-wise for lists and records."""
    if a == b:
        return a
    if a is ERROR:
        return b
    if b is ERROR:
        return a
    if is_numeric(a) and is_numeric(b):
        return NUMBER
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
    """Runtime check that a host-supplied input matches its declared type. A ``null`` (or missing) record field is
    an absent field (TOWL §5); a ``null`` anywhere else does not conform."""
    if t in (JSON, ERROR):
        return True
    if value is None:
        return False
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
        return isinstance(value, dict) and all(k in t.fields for k in value) and all(
            value.get(k) is None or conforms(value[k], ft) for k, ft in t.fields.items())
    return False


def normalize(value: Any, t: Type, _depth=0) -> Any:
    """Normalize a decoded provider value against its type: absent list members become ``[]`` (defaulted)."""
    if value is None:
        return [] if isinstance(t, TList) else None
    if isinstance(t, TList) and isinstance(value, list):
        return [normalize(v, t.element, _depth + 1) for v in value if v is not None]
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
    """Type text with records expanded to ``depth`` levels (optional members marked ``?``); deeper named records
    print by name."""
    if isinstance(t, TList):
        return f"list[{describe(t.element, depth)}]"
    if isinstance(t, TRecord):
        if depth <= 0 and t.name:
            return t.name
        return _fields_text(t, lambda v: describe(v, depth - 1))
    return str(t)
