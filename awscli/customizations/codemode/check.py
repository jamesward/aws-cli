"""TOWL v3 static semantics (TOWL_SPEC.md §11): names, catalog, types, effects. Every expression gets
exactly one type or a diagnostic; all diagnostics are collected in one pass. Nothing here invokes an
operation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import types as T
from .aws_catalog import ABSENCE, AUTHORIZATION, AVAILABILITY, STATE, TRANSIENT, VALIDATION
from .syntax import (
    KEYWORDS, STR_FNS, TEST_FNS, TIME_FNS, AndP, Binding, BlockE, Cmp, Diagnostic,
    ExprArg, Implicit, InP, LambdaArg, ListE, Lit, Member, MethodCall, NotP, OpCall, OrP, Pred, PredArg, Program,
    QuantP, RecordE, Ref, TestP, TowlError,
)


@dataclass
class EffectSite:
    op: object
    call: OpCall
    tolerate: List[str]
    static_width: Optional[int]  # None when dynamic
    depth: int

    def to_dict(self):
        return {
            "line": self.call.pos.line, "operation": self.op.id, "effect": self.op.effect,
            "tolerate": list(self.tolerate),
            "multiplicity": self.static_width if self.static_width is not None else "dynamic", "depth": self.depth,
        }


@dataclass
class Checked:
    program: Program
    source: str
    result_type: T.Type
    binding_types: Dict[str, T.Type]
    types: Dict[int, T.Type]  # id(expr) -> type
    effects: List[EffectSite]
    ops: Dict[int, object]  # id(OpCall) -> OperationSpec
    waves: Dict[int, Optional[int]]  # id(MethodCall for) -> static width
    warnings: List[Diagnostic]
    implicit_inputs: List[str] = field(default_factory=list)  # predefined names the program uses without declaring (now, today)

    @property
    def mutations(self):
        return sum(1 for e in self.effects if e.op.effect != "read")

    def type_of(self, expr):
        return self.types.get(id(expr), T.JSON)


class _Scope:
    __slots__ = ("bindings", "implicit", "pure", "depth")

    def __init__(self, bindings, implicit, pure, depth):
        self.bindings, self.implicit, self.pure, self.depth = bindings, implicit, pure, depth

    def bind(self, name, t):
        return _Scope({**self.bindings, name: t}, self.implicit, self.pure, self.depth)

    def with_implicit(self, t):
        return _Scope(self.bindings, t, True, self.depth)

    def as_pure(self):
        return _Scope(self.bindings, self.implicit, True, self.depth)

    def deeper(self):
        return _Scope(self.bindings, self.implicit, self.pure, self.depth + 1)


class Checker:
    def __init__(self, catalog, max_width=200, max_depth=2):
        self.catalog = catalog
        self.max_width = max_width
        self.max_depth = max_depth
        self.diags: List[Diagnostic] = []
        self.types: Dict[int, T.Type] = {}
        self.ops = {}
        self.effects: List[EffectSite] = []
        self.waves = {}
        self.referenced = set()
        self.lengths: Dict[str, int] = {}
        self.declared: set = set()
        self.implicit_used: set = set()

    # ── entry ────────────────────────────────────────────────────────────────

    def check(self, program: Program, source: str) -> Checked:
        binding_types = {}
        scope = _Scope({}, None, False, 0)
        # predefined inputs (TOWL §5): usable without a declaration; a declaration or binding of the same name wins
        self.declared = {i.name for i in program.inputs} | {b.name for b in program.bindings}
        for name, t in _PREDEFINED_INPUTS.items():
            if name not in self.declared:
                scope = scope.bind(name, t)
        seen = set()
        for inp in program.inputs:
            if inp.name in seen:
                self.error("names", "names.duplicate", inp.pos, f"input '{inp.name}' is declared twice")
            seen.add(inp.name)
            binding_types[inp.name] = inp.type
            scope = scope.bind(inp.name, inp.type)
        for b in program.bindings:
            t = self._binding(b, seen, scope)
            binding_types[b.name] = t
            n = self._static_length(b.expr)
            if n is not None:
                self.lengths[b.name] = n
            scope = scope.bind(b.name, t)
        result_type = self.type_of(program.result, scope)
        for b in program.bindings:
            if b.name not in self.referenced:
                self.error("names", "names.unreferenced", b.pos, f"binding '{b.name}' is never used; every value must flow into the result",
                           "reference it from the result (e.g. include it in the result record) or remove it")
        for inp in program.inputs:
            if inp.name not in self.referenced:
                self.warn("names", "names.unusedInput", inp.pos, f"input '{inp.name}' is never used")
        errors = [d for d in self.diags if d.severity == "error"]
        if errors:
            raise TowlError(errors + [d for d in self.diags if d.severity == "warning"])
        return Checked(program, source, result_type, binding_types, self.types, self.effects, self.ops, self.waves, list(self.diags),
                       sorted(self.implicit_used))

    def _binding(self, b: Binding, seen, scope):
        if b.name in seen:
            self.error("names", "names.duplicate", b.pos, f"'{b.name}' is bound twice; names are bound once (no shadowing)")
        seen.add(b.name)
        if b.name in KEYWORDS:
            self.error("names", "names.keyword", b.pos, f"'{b.name}' is a keyword")
        return self.type_of(b.expr, scope)

    # ── diagnostics ──────────────────────────────────────────────────────────

    def error(self, phase, code, pos, msg, fix=None, t=None):
        self.diags.append(Diagnostic("error", phase, code, pos, msg, fix, str(t) if t is not None else None))
        return T.ERROR

    def warn(self, phase, code, pos, msg, fix=None):
        self.diags.append(Diagnostic("warning", phase, code, pos, msg, fix))

    # ── expressions ──────────────────────────────────────────────────────────

    def type_of(self, e, s: _Scope, expected: Optional[T.Type] = None) -> T.Type:
        t = self._compute(e, s, expected)
        self.types[id(e)] = t
        return t

    def _compute(self, e, s, expected):
        if isinstance(e, Lit):
            v = e.value
            if v is None:
                return T.NULL
            if isinstance(v, bool):
                return T.BOOL
            if isinstance(v, int):
                return T.INT
            if isinstance(v, float):
                return T.NUMBER
            if isinstance(v, str):
                return T.TIMESTAMP if expected is not None and T.strip_null(expected) is T.TIMESTAMP else T.STRING
            return T.JSON
        if isinstance(e, Ref):
            self.referenced.add(e.name)
            if e.name in _PREDEFINED_INPUTS and e.name not in self.declared:
                self.implicit_used.add(e.name)
            if e.name in s.bindings:
                return s.bindings[e.name]
            if e.name in _WELL_KNOWN_INPUTS:
                return self.error("names", "names.undefined", e.pos, f"'{e.name}' is not declared",
                                  f"'{e.name}' is a well-known input the host binds when declared: add 'input {e.name}: {_WELL_KNOWN_INPUTS[e.name]}' after the header")
            return self.error("names", "names.undefined", e.pos, f"'{e.name}' is not defined",
                              f"define it with '{e.name} = ...' before use, or check the spelling; known names: {', '.join(s.bindings) or '(none)'}")
        if isinstance(e, Implicit):
            if s.implicit is not None:
                return s.implicit
            return self.error("syntax", "syntax.pathOutsideElement", e.pos,
                              "a path starting with '.' refers to the element of a list function (project, where, flat, ...) and is not valid here",
                              "name the value instead, e.g. x.Field, or move this into a list function")
        if isinstance(e, RecordE):
            exp = T.strip_null(expected) if expected is not None else None
            exp = exp if isinstance(exp, T.TRecord) else None
            if not e.fields and exp is None and expected is not T.JSON:
                self.error("types", "type.emptyLiteral", e.pos, "'{}' has no type here; only call parameters may be an empty record")
            fields = {}
            for k, v in e.fields:
                if k in fields:
                    self.error("syntax", "syntax.duplicateField", v.pos, f"field '{k}' appears twice")
                fields[k] = self.type_of(v, s, exp.fields.get(k) if exp else None)
            return T.TRecord(fields)
        if isinstance(e, ListE):
            exp_elem = T.strip_null(expected) if expected is not None else None
            exp_elem = exp_elem.element if isinstance(exp_elem, T.TList) else None
            if not e.items:
                if exp_elem is not None:
                    return T.TList(exp_elem)
                return self.error("types", "type.emptyLiteral", e.pos, "'[]' has no element type here",
                                  "an empty list is only valid where a list type is expected (a parameter, concat, in)")
            t = self.type_of(e.items[0], s, exp_elem)
            for it in e.items[1:]:
                u = self.type_of(it, s, exp_elem)
                j = T.join(t, u)
                if j is None:
                    return self.error("types", "type.listElements", it.pos, f"list elements have different types: {t} and {u}")
                t = j
            return T.TList(t)
        if isinstance(e, BlockE):
            if s.pure:
                self.error("syntax", "syntax.pureLayer", e.pos, "a block is not allowed inside a path, shape, predicate, or call parameters")
            inner = s
            seen = set(s.bindings)
            for b in e.bindings:
                if b.name in seen:
                    self.error("names", "names.duplicate", b.pos, f"'{b.name}' shadows or repeats a name in scope")
                seen.add(b.name)
                n = self._static_length(b.expr)
                if n is not None:
                    self.lengths[b.name] = n
                inner = inner.bind(b.name, self.type_of(b.expr, inner))
            t = self.type_of(e.result, inner)
            for b in e.bindings:
                if b.name not in self.referenced:
                    self.error("names", "names.unreferenced", b.pos, f"binding '{b.name}' is never used")
            return t
        if isinstance(e, Member):
            return self._member(e, s)
        if isinstance(e, OpCall):
            return self._op_call(e, s)
        if isinstance(e, MethodCall):
            return self._method(e, s)
        return self.error("syntax", "syntax.unexpected", e.pos, "unexpected expression")

    def _member(self, e: Member, s):
        target = self.type_of(e.target, s)
        if target is T.ERROR:
            return T.ERROR
        if T.strip_null(target) is T.JSON:
            return self.error("types", "type.opaque", e.pos, f"'.{e.name}' on an opaque json value (its operation declares no schema for it)",
                              "use the value whole, or pass it to an operation that accepts json")
        nullable = T.is_nullable(target)
        rec = T.strip_null(target)
        if not isinstance(rec, T.TRecord):
            src = e.target
            if isinstance(src, OpCall):
                fix = f"call(\"{src.namespace}\", \"{src.operation}\") returns the {target} itself, not a record; use the call's value directly and drop '.{e.name}'"
            elif isinstance(src, Ref):
                fix = f"'{src.name}' is already a {target}; use it directly and drop '.{e.name}'"
            else:
                fix = f"the value is already a {target}; use it directly and drop '.{e.name}'"
            return self.error("types", "type.notRecord", e.pos, f"'.{e.name}' needs a record but the value is {target}", fix, target)
        ft = rec.fields.get(e.name)
        if ft is None:
            return self.error("catalog", "catalog.unknownMember", e.pos, f"'{e.name}' is not a member of {rec}",
                              "members: " + ", ".join(sorted(rec.fields)))
        if nullable and not e.null_safe:
            return self.error("types", "type.nullableAccess", e.pos, f"'.{e.name}' on a nullable value ({target})",
                              f"use '?.{e.name}' to propagate Null; the result stays nullable", target)
        if not nullable and e.null_safe:
            self.warn("types", "type.needlessNullSafe", e.pos, f"'?.{e.name}' on a non-nullable value; '.{e.name}' is canonical")
        return T.nullable(ft) if nullable else ft

    # ── calls ────────────────────────────────────────────────────────────────

    def _op_call(self, e: OpCall, s):
        if s.pure:
            self.error("syntax", "syntax.pureLayer", e.pos, "an operation call is not allowed inside a path, shape, predicate, or another call's parameters",
                       f'bind it first: name = call("{e.namespace}", "{e.operation}", ...), then reference the name')
        if e.namespace not in self.catalog.namespaces:
            near = _nearest(e.namespace, self.catalog.namespaces)
            hint = "the first argument of call is an AWS service name such as ec2, s3, iam; find operations with `aws codemode operation search`"
            return self.error("catalog", "catalog.unknownNamespace", e.pos, f"'{e.namespace}' is not a service in this catalog",
                              ("did you mean: " + ", ".join(near) + "; " + hint) if near else hint)
        op = self.catalog.operation(e.namespace, e.operation)
        if op is None:
            near = getattr(self.catalog.resolve(e.namespace, e.operation), "suggestions", ())
            return self.error("catalog", "catalog.unknownOperation", e.pos, f"unknown operation {e.namespace}.{e.operation}",
                              ("did you mean: " + ", ".join(f'call("{e.namespace}", "{n}", ...)' for n in near)) if near else "use `aws codemode operation search` to find the exact API name")
        self.ops[id(e)] = op
        pt = self.type_of(e.params, s.as_pure(), op.input if op.input is not None else T.JSON)
        self._check_params(pt, op, e)
        if isinstance(e.params, RecordE):
            for k, v in e.params.fields:
                if isinstance(v, Lit) and isinstance(v.value, str) and v.value in s.bindings:
                    self.warn("catalog", "catalog.literalLooksLikeName", v.pos, f'parameter \'{k}\' is the literal string "{v.value}", which is also a name in scope',
                              f'if you meant the value of {v.value}, write {v.value} (in the structured form: {{"$": "{v.value}"}})')
        tolerate = []
        if e.options is not None:
            if not isinstance(e.options, RecordE):
                self.error("syntax", "syntax.options", e.options.pos, "call options must be a record literal { tolerate: [...], region: \"...\" }")
            else:
                for k, v in e.options.fields:
                    if k == "tolerate":
                        codes = [it.value if isinstance(it, Lit) and isinstance(it.value, str) else None for it in v.items] if isinstance(v, ListE) else None
                        if codes is None or any(c is None for c in codes):
                            self.error("syntax", "syntax.options", v.pos, "tolerate takes a list of error-code strings")
                            continue
                        tolerate = codes
                        for c in codes:
                            cls = self.catalog.error_class(op, c)
                            if cls not in (ABSENCE, AUTHORIZATION, AVAILABILITY, STATE):
                                why = " (the runtime retries transient errors)" if cls == TRANSIENT else " (a validation error is a program bug)" if cls == VALIDATION else ""
                                self.error("catalog", "catalog.tolerateClass", v.pos, f"'{c}' is classified {cls} and cannot be tolerated; only absence, authorization, availability, and state codes can{why}")
                            elif c not in op.error_codes and not op.open_error_codes:
                                self.error("catalog", "catalog.unknownErrorCode", v.pos, f"'{c}' is not an error code of {op.id}")
                            elif c not in op.error_codes:
                                self.warn("catalog", "catalog.unmodeledErrorCode", v.pos, f"'{c}' is not a modeled error code of {op.id}; accepted as class {cls}")
                    elif k == "after":
                        self.error("syntax", "syntax.options", v.pos, "'after' was removed: ordering comes from data",
                                   "make the second call depend on the first's result (e.g. Resources: stopped.StoppingInstances.collect(.InstanceId)); mutations without a data dependency run one at a time in source order")
                    elif k in ("region", "profile"):
                        t = self.type_of(v, s.as_pure())
                        if T.strip_null(t) is not T.STRING and t is not T.ERROR:
                            self.error("types", "type.option", v.pos, f"option '{k}' must be a string, got {t}")
                        elif T.is_nullable(t):
                            self.warn("types", "type.nullableToRequired", v.pos, f"option '{k}' is {t}; a Null at runtime stops with a 'data' error naming the element")
                    else:
                        self.error("syntax", "syntax.options", v.pos, f"unknown call option '{k}'", "options: tolerate, region, profile")
        self.effects.append(EffectSite(op, e, tolerate, 1 if s.depth == 0 else None, s.depth))
        return T.nullable(op.output) if tolerate else op.output

    def _check_params(self, pt, op, e: OpCall):
        if not isinstance(pt, T.TRecord):
            if pt is not T.ERROR:
                self.error("types", "type.params", e.params.pos, f"parameters must be a record, got {pt}")
            return
        if op.input is None:
            if pt.fields:
                self.error("catalog", "catalog.unknownParameter", e.params.pos, f"{op.id} takes no parameters")
            return
        exprs = dict(e.params.fields) if isinstance(e.params, RecordE) else {}
        for k, t in pt.fields.items():
            expected = op.input.fields.get(k)
            if isinstance(exprs.get(k), ListE) and not exprs[k].items:
                self.error("catalog", "catalog.emptyListParameter", exprs[k].pos, f"'{k}' is []; an empty list is never a meaningful parameter and AWS rejects it",
                           "omit optional parameters; only the required ones must be present")
                continue
            if expected is None:
                if k in getattr(op, "runtime_owned", ()):
                    self.error("catalog", "catalog.runtimeOwned", e.params.pos, f"'{k}' is a pagination member owned by the runtime; pagination is automatic and complete",
                               "remove it; narrow the result with filters instead")
                else:
                    self.error("catalog", "catalog.unknownParameter", e.params.pos, f"'{k}' is not a parameter of {op.id}",
                               "parameters: " + ", ".join(sorted(op.input.fields)))
                continue
            if not T.assignable(t, expected):
                if T.assignable(t, expected, relaxed=True):
                    self.warn("types", "type.nullableToRequired", e.params.pos,
                              f"'{k}' is {T.describe(t, 1)} but {op.id} requires {T.describe(expected, 1)}; passing it as is is correct: a Null at runtime stops with a 'data' error naming the element",
                              "there is no default operator; a Null here is a real absence and must be visible")
                else:
                    self.error("types", "type.parameter", e.params.pos, f"'{k}' is {T.describe(t, 1)} but {op.id} expects {T.describe(expected, 1)}", None, t)
        required = getattr(op, "required", None)
        for k, t in op.input.fields.items():
            missing = k not in pt.fields
            if missing and (k in required if required is not None else (not T.is_nullable(t) and not isinstance(t, T.TList))):
                self.error("catalog", "catalog.missingParameter", e.params.pos, f"{op.id} requires parameter '{k}' ({t})")

    # ── stdlib ───────────────────────────────────────────────────────────────

    def _method(self, e: MethodCall, s):
        target = self.type_of(e.target, s)
        name = e.name
        if name in TEST_FNS:
            return self.error("syntax", "syntax.predicateOutside", e.pos, f"'{name}' is a predicate test and is only valid inside where/any/all")
        if name == "for":
            return self._for(e, target, s)
        if name in STR_FNS:
            return self._string_fn(e, target, s)
        if name in TIME_FNS:
            return self._time_fn(e, target, s)
        if target is T.ERROR:
            return T.ERROR
        if not isinstance(target, T.TList):
            return self.error("types", "type.notList", e.pos, f"'.{name}()' needs a list but the value is {target}",
                              "the list may be Null (optional member or tolerated call); keep it nullable in a shape, or compact() the outer list first" if T.is_nullable(target) else None, target)
        elem = target.element
        es = s.with_implicit(elem)

        def path(i, what="a path from the element (e.g. .Name)", optional_for_scalars=False):
            if not e.args and optional_for_scalars:
                if T.is_scalar(T.strip_null(elem)):
                    return elem  # identity path: the element itself
                self.error("syntax", "syntax.arity", e.pos, f"'{name}' needs {what} when the elements are {elem}; omit the path only for a list of scalars")
                return None
            if len(e.args) != i + 1:
                self.error("syntax", "syntax.arity", e.pos, f"'{name}' takes {what}" + (" (optional for a list of scalars)" if optional_for_scalars else ""))
                return None
            a = e.args[i]
            if not isinstance(a, ExprArg) or not _path_like(a.expr):
                self.error("syntax", "syntax.argKind", a.pos, f"'{name}' needs {what}; a path starts with '.' and refers to the element")
                return None
            return self.type_of(a.expr, es)

        def count_arg(i):
            a = e.args[i] if i < len(e.args) else None
            if not isinstance(a, ExprArg) or not isinstance(a.expr, (Lit, Ref)):
                self.error("syntax", "syntax.argKind", e.pos, f"'{name}' needs a count first: {name}(5, .path)")
                return False
            t = self.type_of(a.expr, s.as_pure())
            if t is not T.INT and t is not T.ERROR:
                self.error("types", "type.count", a.pos, f"'{name}' count must be an int, got {t}")
            return True

        def pred(i):
            if i >= len(e.args):
                self.error("syntax", "syntax.arity", e.pos, f"'{name}' needs a predicate")
                return False
            a = e.args[i]
            if not isinstance(a, PredArg):
                self.error("syntax", "syntax.argKind", a.pos, f"'{name}' needs a predicate such as .State == \"running\"")
                return False
            self._predicate(a.pred, es)
            return True

        def no_args():
            if e.args:
                self.error("syntax", "syntax.arity", e.pos, f"'{name}()' takes no argument")

        if name == "project":
            if len(e.args) != 1 or not isinstance(e.args[0], ExprArg):
                return self.error("syntax", "syntax.arity", e.pos, "'project' takes one path or one shape { field: .path }")
            x = e.args[0].expr
            if isinstance(x, RecordE):
                self._shape(x)
            t = self.type_of(x, es)
            if not isinstance(x, RecordE) and not _path_like(x):
                self.error("syntax", "syntax.argKind", e.args[0].pos, "'project' takes a path (.field) or a shape ({ field: .path }); found another expression")
            if isinstance(t, T.TList):
                self.warn("types", "type.nestedList", e.pos, f"project yields list[{t}]; use flat(...) if you want one list")
            return T.TList(t)
        if name == "flat":
            t = path(0)
            if t is None:
                return T.ERROR
            if t is T.ERROR:
                return T.TList(T.ERROR)
            if isinstance(t, T.TList):
                return T.TList(t.element)
            return self.error("types", "type.flatNotList", e.pos, f"flat needs a list-typed path but the path is {t}",
                              "the member is optional; use ?. plus compact()" if T.is_nullable(t) else "use project(...) for a non-list path", t)
        if name == "flatten":
            no_args()
            if isinstance(elem, T.TList):
                return T.TList(elem.element)
            return self.error("types", "type.flattenNotNested", e.pos, f"flatten needs list[list[T]] but the value is {target}", None, target)
        if name == "where":
            return target if pred(0) else T.ERROR
        if name == "compact":
            no_args()
            if T.is_nullable(elem):
                return T.TList(T.strip_null(elem))
            self.warn("types", "type.needlessCompact", e.pos, f"compact on {target} changes nothing")
            return target
        if name == "distinct":
            if not e.args:
                if not T.is_equatable(elem):
                    self.error("types", "type.notEquatable", e.pos, f"distinct needs equatable elements, not {elem}")
                return target
            t = path(0, "a path")
            if t is None:
                return T.ERROR
            if not T.is_equatable(t):
                self.error("types", "type.notEquatable", e.pos, f"distinct path must be equatable, not {t}")
            return T.TList(t)
        if name == "concat":
            if len(e.args) != 1 or not isinstance(e.args[0], ExprArg):
                return self.error("syntax", "syntax.arity", e.pos, "'concat' takes one list")
            t = self.type_of(e.args[0].expr, s, target)
            if t is T.ERROR:
                return target
            j = T.join(target, t)
            if isinstance(j, T.TList):
                return j
            return self.error("types", "type.concat", e.pos, f"cannot concat {target} with {t}", None, t)
        if name == "group":
            k = path(0)
            if k is None:
                return T.ERROR
            kb = T.strip_null(k)
            if not (T.is_scalar(kb) or kb is T.ERROR):
                self.error("types", "type.groupKey", e.pos, f"group key must be a scalar, not {k}")
            return T.TList(T.TRecord({"key": k, "items": target}))
        if name == "single":
            no_args()
            return T.nullable(elem)
        if name == "count":
            no_args()
            return T.INT
        if name == "sum":
            t = path(0, "a numeric path", optional_for_scalars=True)
            if t is None:
                return T.ERROR
            b = T.strip_null(t)
            if not T.is_numeric(b) and b is not T.ERROR:
                self.error("types", "type.numeric", e.pos, f"sum needs a numeric path, not {t}")
            return T.INT if b is T.INT else T.NUMBER
        if name in ("min", "max"):
            t = path(0, "an ordered scalar path", optional_for_scalars=True)
            if t is None:
                return T.ERROR
            b = T.strip_null(t)
            if not T.is_ordered(b) and b is not T.ERROR:
                self.error("types", "type.ordered", e.pos, f"{name} needs an ordered scalar path, not {t}")
            return T.nullable(b)
        if name == "avg":
            t = path(0, "a numeric path", optional_for_scalars=True)
            if t is None:
                return T.ERROR
            b = T.strip_null(t)
            if not T.is_numeric(b) and b is not T.ERROR:
                self.error("types", "type.numeric", e.pos, f"avg needs a numeric path, not {t}")
            return T.nullable(T.NUMBER)
        if name in ("top", "bottom"):
            if not count_arg(0):
                return T.ERROR
            if len(e.args) == 1 and T.is_scalar(T.strip_null(elem)):
                kt = elem
            else:
                kt = path(1, "an ordered scalar path to rank by")
                if kt is None:
                    return T.ERROR
            kb = T.strip_null(kt)
            if not T.is_ordered(kb) and kb is not T.ERROR:
                self.error("types", "type.ordered", e.pos, f"{name} needs an ordered scalar key, not {kt}")
            return T.TList(T.TRecord({"rank": T.INT, "value": elem}))
        if name == "collect":
            t = path(0, "a path", optional_for_scalars=True)
            if t is None:
                return T.ERROR
            if isinstance(t, T.TList):
                self.warn("types", "type.nestedList", e.pos, f"collect adds one list level: list[{t}]; use flat(...) to flatten first")
            return T.TList(T.strip_null(t))
        if name in ("any", "all"):
            return T.BOOL if pred(0) else T.ERROR
        return self.error("syntax", "syntax.unknownFunction", e.pos, f"'{name}' is not a TOWL function")

    def _shape(self, r: RecordE):
        for _, v in r.fields:
            if isinstance(v, RecordE):
                self._shape(v)
            elif isinstance(v, (OpCall, BlockE)):
                self.error("syntax", "syntax.pureLayer", v.pos, "a shape leaf must be a path, a name, a literal, or a nested shape")
            elif isinstance(v, MethodCall) and v.name == "for":
                self.error("syntax", "syntax.pureLayer", v.pos, "for is not allowed inside a shape")

    def _for(self, e: MethodCall, target, s):
        if s.pure:
            self.error("syntax", "syntax.pureLayer", e.pos, "for is not allowed inside a path, shape, predicate, or call arguments")
        if len(e.args) != 1 or not isinstance(e.args[0], LambdaArg):
            return self.error("syntax", "syntax.argKind", e.pos, "'for' takes exactly one binder: for x in xs")
        lam = e.args[0]
        if target is T.ERROR:
            self.type_of(lam.body, s.bind(lam.param, T.ERROR).deeper())
            return T.TList(T.ERROR)
        if not isinstance(target, T.TList):
            return self.error("types", "type.notList", e.pos, f"'for' needs a list but the value is {target}", None, target)
        if lam.param in s.bindings:
            self.error("names", "names.duplicate", lam.pos, f"'{lam.param}' shadows a name in scope")
        before = len(self.effects)
        inner = s.bind(lam.param, target.element).deeper()
        body_t = self.type_of(lam.body, inner)
        if len(self.effects) > before:
            width = self._static_length(e.target)
            self.waves[id(e)] = width
            for site in self.effects[before:]:
                if site.depth == inner.depth:
                    site.static_width = width
            if inner.depth > self.max_depth:
                self.error("effects", "effects.depth", e.pos, f"for with calls nested {inner.depth} deep exceeds the limit of {self.max_depth}")
            if width is not None and width > self.max_width:
                self.error("effects", "for.staticWidthExceeded", e.pos, f"for over {width} elements with calls exceeds the width limit of {self.max_width}")
        if lam.param not in self.referenced:
            self.warn("names", "names.unusedElement", lam.pos, f"'{lam.param}' is unused; the body does not depend on the element")
        return T.TList(body_t)

    def _static_length(self, x) -> Optional[int]:
        """TOWL §7: static length is inductive over literals, project, for, concat."""
        if isinstance(x, ListE):
            return len(x.items)
        if isinstance(x, Ref):
            return self.lengths.get(x.name)
        if isinstance(x, MethodCall):
            if x.name in ("project", "for"):
                return self._static_length(x.target)
            if x.name == "concat":
                a = self._static_length(x.target)
                b = self._static_length(x.args[0].expr) if x.args and isinstance(x.args[0], ExprArg) else None
                return a + b if a is not None and b is not None else None
        return None

    def _time_fn(self, e: MethodCall, target, s):
        base = T.strip_null(target)
        if base is not T.TIMESTAMP and base is not T.ERROR:
            return self.error("types", "type.timestamp", e.pos, f"'{e.name}' needs a timestamp but the value is {target}",
                              "timestamps come from the well-known input now (input now: timestamp) or from a timestamp-typed member", target)
        if e.name.startswith("minus_"):
            if len(e.args) != 1 or not isinstance(e.args[0], ExprArg):
                return self.error("syntax", "syntax.arity", e.pos, f"'{e.name}' takes one int argument")
            t = self.type_of(e.args[0].expr, s.as_pure())
            if t is not T.INT and t is not T.ERROR:
                self.error("types", "type.int", e.args[0].pos, f"'{e.name}' argument must be an int, not {t}")
        elif e.args:
            self.error("syntax", "syntax.arity", e.pos, f"'{e.name}()' takes no argument")
        out = T.STRING if e.name == "date" else T.TIMESTAMP
        return T.nullable(out) if T.is_nullable(target) else out

    def _string_fn(self, e: MethodCall, target, s):
        base = T.strip_null(target)
        if base is not T.STRING and base is not T.ERROR:
            return self.error("types", "type.string", e.pos, f"'{e.name}' needs a string but the value is {target}", None, target)
        if e.name in ("after_last", "before_first"):
            if len(e.args) != 1 or not isinstance(e.args[0], ExprArg):
                return self.error("syntax", "syntax.arity", e.pos, f"'{e.name}' takes one string argument")
            t = self.type_of(e.args[0].expr, s.as_pure())
            if T.strip_null(t) is not T.STRING and t is not T.ERROR:
                self.error("types", "type.string", e.args[0].pos, f"'{e.name}' argument must be a string, not {t}")
        elif e.args:
            self.error("syntax", "syntax.arity", e.pos, f"'{e.name}()' takes no argument")
        return T.nullable(T.STRING) if T.is_nullable(target) else T.STRING

    # ── predicates ───────────────────────────────────────────────────────────

    def _predicate(self, pr: Pred, es):
        if isinstance(pr, (AndP, OrP)):
            for t in pr.terms:
                self._predicate(t, es)
        elif isinstance(pr, NotP):
            self._predicate(pr.term, es)
        elif isinstance(pr, Cmp):
            self._operand_ok(pr.left)
            self._operand_ok(pr.right)
            l = self.type_of(pr.left, es)
            r = self.type_of(pr.right, es)
            if l is T.ERROR or r is T.ERROR:
                return
            lb, rb = T.strip_null(l), T.strip_null(r)
            if pr.op in ("==", "!="):
                if not T.is_equatable(l) or not T.is_equatable(r):
                    self.error("types", "type.notEquatable", pr.pos, f"cannot compare {l} with {r}")
                elif (l is T.NULL and not T.is_nullable(r)) or (r is T.NULL and not T.is_nullable(l)):
                    self.error("types", "type.compare", pr.pos, f"null can only be compared with a nullable value, not {r if l is T.NULL else l}",
                               "use .present()/.absent() on an optional member")
                elif l is not T.NULL and r is not T.NULL and T.join(lb, rb) is None:
                    self.error("types", "type.compare", pr.pos, f"cannot compare {l} with {r}", None, l)
            else:
                if not T.is_ordered(lb) or not T.is_ordered(rb) or T.join(lb, rb) is None:
                    self.error("types", "type.ordered", pr.pos, f"'{pr.op}' needs two ordered values of one type, got {l} and {r}")
        elif isinstance(pr, InP):
            self._operand_ok(pr.left)
            l = self.type_of(pr.left, es)
            r = self.type_of(pr.right, es, T.TList(T.strip_null(l)))
            if l is T.ERROR or r is T.ERROR:
                return
            if not isinstance(r, T.TList):
                self.error("types", "type.in", pr.pos, f"'in' needs a list on the right, got {r}")
            elif T.join(T.strip_null(l), r.element) is None:
                self.error("types", "type.in", pr.pos, f"'in' compares {l} against list[{r.element}]")
        elif isinstance(pr, TestP):
            t = self.type_of(pr.operand, es)
            if pr.fn in ("present", "absent"):
                if not T.is_nullable(t) and t is not T.ERROR:
                    self.warn("types", "type.needlessPresent", pr.pos, f"'{pr.fn}()' on a non-nullable value is always {pr.fn == 'present'}")
            elif pr.fn == "empty":
                if not isinstance(T.strip_null(t), T.TList) and t is not T.ERROR:
                    self.error("types", "type.notList", pr.pos, f"'empty()' needs a list operand, got {t}")
            else:
                if T.strip_null(t) is not T.STRING and t is not T.ERROR:
                    self.error("types", "type.string", pr.pos, f"'{pr.fn}' needs a string operand, got {t}")
                if pr.arg is not None:
                    at = self.type_of(pr.arg, es)
                    if T.strip_null(at) is not T.STRING and at is not T.ERROR:
                        self.error("types", "type.string", pr.pos, f"'{pr.fn}' argument must be a string, got {at}")
        elif isinstance(pr, QuantP):
            t = self.type_of(pr.operand, es)
            if t is T.ERROR:
                return
            l = T.strip_null(t)
            if not isinstance(l, T.TList):
                self.error("types", "type.notList", pr.pos, f"'{'all' if pr.all else 'any'}' needs a list operand, got {t}")
                return
            self._predicate(pr.inner, es.with_implicit(l.element))


    def _operand_ok(self, x):
        """Predicate operands allow only Null/string continuations (TOWL §3 spath)."""
        y = x
        while isinstance(y, (Member, MethodCall)):
            if isinstance(y, MethodCall) and y.name not in STR_FNS | TIME_FNS:
                self.error("syntax", "syntax.predicateOperand", y.pos, f"'{y.name}' is not allowed inside a predicate; operands are paths with optional string or time functions",
                           "for list members use .L.empty(), .L.any(pred) or .L.all(pred); otherwise compute the value in a binding or shape first")
                return
            y = y.target


def _path_like(x) -> bool:
    if isinstance(x, Implicit):
        return True
    if isinstance(x, (Member, MethodCall)):
        return _path_like(x.target)
    return False


_PREDEFINED_INPUTS = {"now": T.TIMESTAMP, "today": T.STRING}  # always available; the runtime binds them
_WELL_KNOWN_INPUTS = {"region": "string"}  # bound by the host when declared


def _nearest(name, candidates, n=3):
    import difflib

    return difflib.get_close_matches(name, list(candidates), n=n, cutoff=0.6)
