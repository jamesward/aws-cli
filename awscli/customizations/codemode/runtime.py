"""TOWL v3 runtime (TOWL_SPEC.md §§12–13) for the AWS profile (CODE_MODE-SPEC.md §5).

Values are plain JSON plus ``Absent`` (TOWL §9.2): a function of an absent value is absent, record fields may
be absent, lists never hold one — an element that would is dropped and recorded in the loss log — and an absent
value in a call's args or options stops the run. Top-level bindings run as soon as their dependencies are values; ``for``
bodies with calls run as a wave. A failed read is classified (TOWL §12.4): ``absence`` yields an absent value,
``authorization``/``availability`` an *unknown* one that drops its enclosing element, anything else stops; a wave
that loses every element to unknowns stops too. A stopped run returns the failure envelope with only complete values. No mutation is
dispatched while losses exist unless losses are allowed. Pagination is complete
and invisible (botocore paginators); transient retries are botocore's.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from . import types as T
from .aws_catalog import classify_error
from .syntax import (
    STR_FNS, TIME_FNS, AndP, BlockE, Cmp, ExprArg, Implicit, InP, LambdaArg, ListE, Lit, Member, MethodCall, NotP, OpCall,
    OrP, QuantP, RecordE, Ref, TestP, free_refs,
)


class OperationError(Exception):
    """Raised by AwsCalls; ``code`` is classified to decide whether the failure is absorbed or stops the run."""

    def __init__(self, code, message, aws=None):
        super().__init__(message)
        self.code = code
        self.aws = aws or {}


class Stop(Exception):
    def __init__(self, cls, code, message, op=None, pos=None, element=None, has_element=False, aws=None):
        super().__init__(message)
        self.cls, self.code, self.op, self.pos, self.element, self.has_element, self.aws = cls, code, op, pos, element, has_element, aws or {}


class Absent:
    """A runtime absence (TOWL §9.2). Not a value a program can write; carries where it first became absent.

    ``origin`` is one of ``{kind: member, member, line, col}``, ``{kind: error, operation, code, class, line}``,
    ``{kind: empty, function, line}``. Record fields may hold an Absent (serialized as null); lists never do.
    An *unknown* Absent (a read that could not be done) makes any record holding it unknown, so it always drops
    its element instead of becoming a null field."""

    __slots__ = ("origin", "unknown")

    def __init__(self, origin, unknown=False):
        self.origin, self.unknown = origin, unknown

    def __repr__(self):
        return f"Absent({self.origin}{', unknown' if self.unknown else ''})"


def is_absent(v) -> bool:
    return isinstance(v, Absent)


def describe_origin(o) -> str:
    if o.get("kind") == "member":
        return f"member '{o['member']}' was absent (line {o['line']})"
    if o.get("kind") == "error":
        return f"{o['operation']} failed with {o['code']} ({o['class']}, line {o['line']})"
    if o.get("kind") == "budget":
        return f"{o['operation']} returned more than {o['value']} items, over --{o['limit']} (line {o['line']}); rerun with a higher --{o['limit']} to include it"
    if o.get("kind") == "empty":
        return f"{o['function']}() of an empty list (line {o['line']})"
    return "a value was absent"


class AwsCalls:
    """Injected effect boundary: one complete (paginated, decoded) result per logical call."""

    def invoke(self, op, params, options):  # pragma: no cover - interface
        raise NotImplementedError


# Operations whose *unpaginated* request form returns less data than the paginated one. Per the S3 API reference,
# `ListBuckets` includes `BucketRegion` per bucket only when the request carries `max-buckets` (and AWS rejects the
# unpaginated form for accounts with a raised bucket quota). botocore only sends the limit key when a PageSize is set.
_FORCED_PAGE_SIZE = {("s3", "ListBuckets"): 1000}


class ClientAwsCalls(AwsCalls):
    """Live botocore adapter; clients are created lazily and never touched by validate."""

    def __init__(self, session, profile=None, max_items=100_000):
        self.session = session
        self.profile = profile
        self.max_items = max_items
        self.sleep = time.sleep
        self._clients = {}
        self._lock = threading.Lock()

    def _client(self, service, region, profile, retries=True):
        key = (service, region, profile, retries)
        with self._lock:
            if key not in self._clients:
                session = self.session
                if profile:
                    from awscli.botocore.session import Session
                    session = Session(profile=profile)
                kwargs = {"region_name": region} if region else {}
                if not retries:
                    from awscli.botocore.config import Config
                    kwargs["config"] = Config(retries={"max_attempts": 1})
                self._clients[key] = session.create_client(service, **kwargs)
            return self._clients[key]

    def invoke(self, op, params, options):
        from awscli.botocore import xform_name
        method = xform_name(op.name)
        region, profile = options.get("region"), options.get("profile") or self.profile
        if op.effect != "read" and not _has_idempotency_token(op):
            # TOWL §12.2: never retry a mutation that may already have been applied
            return self._invoke_mutation_once(op, self._client(op.namespace, region, profile, retries=False), method, params)
        client = self._client(op.namespace, region, profile)
        try:
            if op.paged and client.can_paginate(method):
                paginator = client.get_paginator(method)
                config = {"MaxItems": self.max_items}
                page_size = _FORCED_PAGE_SIZE.get((op.namespace, op.name))
                if page_size:
                    config["PageSize"] = page_size
                result = paginator.paginate(PaginationConfig=config, **params).build_full_result()
                # build_full_result adds a synthetic NextToken (and echoes the provider cursor) only when MaxItems cut the iteration short
                if result.get("NextToken") or any(result.get(k) for k in op.paginator.output_members):
                    raise Stop("budget", "MaxItems", f"{op.id} has more than {self.max_items} items; narrow the request", op)
            else:
                result = getattr(client, method)(**params)
        except Stop:
            raise
        except Exception as exc:  # botocore ClientError and friends
            response = getattr(exc, "response", None) or {}
            error = response.get("Error", {}) if isinstance(response, dict) else {}
            code = error.get("Code") or type(exc).__name__
            cls = "configuration" if type(exc).__name__ in _CONFIG_ERRORS else classify_error(code)
            raise OperationError(code, error.get("Message") or str(exc), {
                "code": code, "class": cls, "message": error.get("Message") or str(exc),
                "requestId": (response.get("ResponseMetadata") or {}).get("RequestId") if isinstance(response, dict) else None,
                "httpStatus": (response.get("ResponseMetadata") or {}).get("HTTPStatusCode") if isinstance(response, dict) else None,
            })
        if isinstance(result, dict):
            result = {k: v for k, v in result.items() if k != "ResponseMetadata"}
        return _jsonable(result)

    def _invoke_mutation_once(self, op, client, method, params, attempts=5):
        """A mutation without an idempotency token: retried only on failures that prove it was not applied
        (throttling, a connection that was never established); anything after the request may have been received
        is reported as possibly applied."""
        import random
        for attempt in range(attempts):
            try:
                result = getattr(client, method)(**params)
                if isinstance(result, dict):
                    result = {k: v for k, v in result.items() if k != "ResponseMetadata"}
                return _jsonable(result)
            except Exception as exc:  # noqa: BLE001
                response = getattr(exc, "response", None) or {}
                error = response.get("Error", {}) if isinstance(response, dict) else {}
                code = error.get("Code") or type(exc).__name__
                not_applied = code in _THROTTLING_CODES or type(exc).__name__ in _NOT_SENT_ERRORS
                if not_applied and attempt + 1 < attempts:
                    self.sleep(min(20.0, 0.2 * 2 ** attempt) * (0.5 + random.random() / 2))
                    continue
                status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode") if isinstance(response, dict) else None
                possibly = not not_applied and not (isinstance(status, int) and 400 <= status < 500)
                cls = "configuration" if type(exc).__name__ in _CONFIG_ERRORS and not possibly else classify_error(code)
                message = (error.get("Message") or str(exc)) + ("; the request may have been applied" if possibly else "")
                raise OperationError(code, message, {
                    "code": code, "class": cls, "message": message, "possiblyApplied": possibly,
                    "requestId": (response.get("ResponseMetadata") or {}).get("RequestId") if isinstance(response, dict) else None,
                    "httpStatus": status,
                })


def _has_idempotency_token(op) -> bool:
    shape = getattr(op, "input_shape", None)
    members = getattr(shape, "members", None) or {}
    return any((getattr(m, "metadata", None) or {}).get("idempotencyToken") for m in members.values())


_THROTTLING_CODES = {"Throttling", "ThrottlingException", "ThrottledException", "RequestLimitExceeded", "TooManyRequestsException",
                     "RequestThrottled", "RequestThrottledException", "SlowDown", "ProvisionedThroughputExceededException"}
_NOT_SENT_ERRORS = {"EndpointConnectionError", "ConnectTimeoutError"}  # no connection, so no request was received


_CONFIG_ERRORS = {
    "NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError", "ProfileNotFound", "NoRegionError",
    "TokenRetrievalError", "SSOTokenLoadError", "UnauthorizedSSOTokenError", "InvalidConfigError", "ConfigNotFound",
    "EndpointConnectionError", "ConnectTimeoutError",
}


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        import base64
        return base64.b64encode(v).decode("ascii")
    return v


class Limits:
    def __init__(self, max_concurrency=8, max_width=200, max_calls=500, max_result_bytes=1024 * 1024, wall_seconds=300.0, stop_grace_seconds=5.0):
        self.max_concurrency, self.max_width, self.max_calls = max_concurrency, max_width, max_calls
        self.max_result_bytes, self.wall_seconds, self.stop_grace_seconds = max_result_bytes, wall_seconds, stop_grace_seconds


class _Env:
    __slots__ = ("names", "implicit", "has_implicit", "element", "in_each", "args")

    def __init__(self, names, implicit=None, has_implicit=False, element=None, in_each=False, args=False):
        self.names, self.implicit, self.has_implicit, self.element, self.in_each = names, implicit, has_implicit, element, in_each
        self.args = args  # evaluating call args/options: lists keep Absent so the call can stop on it (TOWL §6.1)

    def bind(self, n, v):
        return _Env({**self.names, n: v}, self.implicit, self.has_implicit, self.element, self.in_each, self.args)

    def with_element(self, v):
        return _Env(self.names, v, True, self.element, self.in_each, self.args)

    def for_element(self, n, v):
        return _Env({**self.names, n: v}, self.implicit, self.has_implicit, v, True, self.args)

    def for_args(self):
        return _Env(self.names, self.implicit, self.has_implicit, self.element, self.in_each, True)


class _Run:
    def __init__(self, checked, inputs, limits, allow_losses=False, strict=False):
        self.c, self.inputs, self.limits, self.allow_losses, self.strict = checked, inputs, limits, allow_losses, strict
        # bodies of nested waves block on their children, so the pool must never be the limit: the
        # semaphore bounds provider concurrency; threads are cheap and created on demand
        self.pool = ThreadPoolExecutor(max_workers=4096)
        self.calls = threading.Semaphore(max(1, limits.max_concurrency))
        self.mutation_lock = threading.Lock()  # mutations are serialized (Code Mode §5.1)
        self.stopped = threading.Event()
        self.failures: List[Stop] = []
        self.lock = threading.Lock()
        self.call_count = 0
        self.in_flight_mutations = 0
        self.nodes: Dict[str, dict] = {}
        self.effects: Dict[str, dict] = {}
        self.absorbed: List[dict] = []
        self.mutations: List[dict] = []
        self.fanouts: Dict[str, dict] = {}
        self.completed: Dict[str, Any] = {}
        self.losses: Dict[tuple, dict] = {}
        self.waves = 0
        self.result_bytes = 0
        self.started = time.monotonic()

    def loss(self, node, pos, absent: "Absent", element=None, has_element=False, reason="absent"):
        """Record one dropped or skipped element (TOWL §12.6). Samples are the canonically smallest three, so the
        log is identical under every schedule (invariant 14)."""
        origin = absent.origin
        if absent.unknown:
            reason = "budget" if origin.get("kind") == "budget" else "unknown"
        key = (node, pos.line, pos.col, reason, _canonical(origin))
        sample = _abbrev(element) if has_element else None
        with self.lock:
            rec = self.losses.get(key)
            if rec is None:
                rec = self.losses[key] = {"node": node, "line": pos.line, "col": pos.col, "reason": reason, "origin": dict(origin),
                                          "count": 0, "sample": []}
            rec["count"] += 1
            if has_element:
                s = rec["sample"]
                c = _canonical(sample)
                if all(_canonical(x) != c for x in s):
                    s.append(sample)
                    s.sort(key=_canonical)
                    del s[3:]

    def loss_list(self):
        return sorted(self.losses.values(), key=lambda l: (l["line"], l["col"], l["node"], _canonical(l["origin"])))

    def loss_count(self):
        return sum(l["count"] for l in self.losses.values())

    def fail(self, s: Stop):
        with self.lock:
            self.failures.append(s)
        self.stopped.set()
        raise s

    def primary(self) -> Optional[Stop]:
        cands = [f for f in self.failures if f.cls != "cancelled"] or list(self.failures)
        if not cands:
            return None
        return min(cands, key=lambda f: (f.pos.line if f.pos else 1 << 30, f.pos.col if f.pos else 1 << 30, _canonical(f.element) if f.has_element else ""))

    def node(self, kind, pos):
        key = f"{kind}@{pos.line}:{pos.col}"
        with self.lock:
            return self.nodes.setdefault(key, {"node": kind, "line": pos.line})

    def bump(self, m, key, by=1):
        with self.lock:
            m[key] = m.get(key, 0) + by


class Runtime:
    def __init__(self, catalog, calls: AwsCalls, limits: Optional[Limits] = None, allow_losses: bool = False, strict: bool = False):
        self.catalog, self.calls, self.limits, self.allow_losses, self.strict = catalog, calls, limits or Limits(), allow_losses, strict

    # ── entry ────────────────────────────────────────────────────────────────

    def execute(self, checked, given: Dict[str, Any]) -> Dict[str, Any]:
        given = dict(given)
        for name in getattr(checked, "implicit_inputs", ()):  # predefined inputs (TOWL §5)
            given.setdefault(name, _predefined_value(name))
        problems = self._input_problems(checked, given)
        if problems:
            return {
                "status": "error",
                "error": {"class": "input", "code": "Input", "message": "; ".join(problems), "action": "rewrite"},
                "completed": {}, "fanout": [], "mutations": [], "losses": [],
                "accounting": {"calls": 0, "waves": 0, "wall_ms": 0},
            }
        run = _Run(checked, given, self.limits, self.allow_losses, self.strict)
        started = time.monotonic()
        value, exc = None, None
        try:
            value = self._top_level(run)
        except Exception as e:  # noqa: BLE001 - every failure becomes the envelope
            exc = e
        run.pool.shutdown(wait=False)
        deadline = time.monotonic() + self.limits.stop_grace_seconds
        while time.monotonic() < deadline and any(not f.done() for f in getattr(run, "_futures", [])):
            time.sleep(0.02)
        mdeadline = time.monotonic() + 600
        while run.in_flight_mutations > 0 and time.monotonic() < mdeadline:
            time.sleep(0.02)
        ms = int((time.monotonic() - started) * 1000)
        stop = run.primary()
        if stop is None and exc is None and is_absent(value):
            stop = Stop("data", "AbsentResult",
                        f"the program's result is absent: {describe_origin(value.origin)}; a result is a value — return a record "
                        "(e.g. { policy: ... }) to report an absence as a null field, or test it with .present()",
                        None, checked.program.result.pos)
        if stop is None and exc is None:
            value = _normalize_order(value)
            run.result_bytes = len(_canonical(value).encode("utf-8"))
            if run.result_bytes > self.limits.max_result_bytes:
                stop = Stop("budget", "MaxResultBytes",
                            f"the result is {run.result_bytes} bytes, over the limit of {self.limits.max_result_bytes}; return fewer fields or narrow the list before returning",
                            None, checked.program.result.pos)
        if stop is None and exc is None:
            return self._success(run, value, ms)
        if stop is None:
            stop = Stop("other", type(exc).__name__, str(exc) or repr(exc))
        return self._failure(run, stop, ms)

    def _input_problems(self, checked, given):
        declared = {i.name: i for i in checked.program.inputs}
        implicit = set(getattr(checked, "implicit_inputs", ()))
        problems = [f"input '{k}' is not declared" for k in given if k not in declared and k not in implicit]
        for d in declared.values():
            if d.name not in given:
                problems.append(f"input '{d.name}' ({d.type}) was not provided")
            elif not T.conforms(given[d.name], d.type):
                problems.append(f"input '{d.name}' does not match {d.type}")
        return problems

    # ── top level: dependency scheduling with the read-before-mutate barrier ──

    def _top_level(self, run: _Run):
        p = run.c.program
        names = {b.name for b in p.bindings}
        deps = {b.name: free_refs(b.expr) & names for b in p.bindings}
        mutating = {b.name for b in p.bindings if _has_mutation(b.expr, run.c)}

        def depends_on_mutation(n, seen=frozenset()):
            return any(d in mutating or (d not in seen and depends_on_mutation(d, seen | {n})) for d in deps[n])

        # barrier: every read that does not depend on a mutation completes before the first mutation;
        # mutations run one at a time in source order (Code Mode §5.1)
        independent_reads = {b.name for b in p.bindings if b.name not in mutating and not depends_on_mutation(b.name)}
        last_mutation = None
        for b in p.bindings:
            if b.name in mutating:
                deps[b.name] = deps[b.name] | independent_reads | ({last_mutation} if last_mutation else set())
                last_mutation = b.name
        futures: Dict[str, Future] = {}
        placeholders = {b.name: Future() for b in p.bindings}  # tasks await these; filled by the real tasks

        def task(b):
            def go():
                try:
                    scope = dict(run.inputs)
                    for d in deps[b.name]:
                        scope[d] = placeholders[d].result()
                    if run.stopped.is_set():
                        raise Stop("cancelled", "Stopped", f"stopped before '{b.name}' started", None, b.pos)
                    v = self._eval(b.expr, _Env(scope), run)
                    run.completed[b.name] = v
                    placeholders[b.name].set_result(v)
                    return v
                except BaseException as exc:  # noqa: BLE001 - propagate to dependents
                    placeholders[b.name].set_exception(exc)
                    raise
            return go

        for b in p.bindings:
            futures[b.name] = run.pool.submit(task(b))
        run._futures = list(futures.values())
        env = dict(run.inputs)
        for b in p.bindings:
            env[b.name] = _unwrap(futures[b.name])
        return self._eval(p.result, _Env(env), run)

    # ── evaluation ───────────────────────────────────────────────────────────

    def _eval(self, e, env: _Env, run: _Run):
        if isinstance(e, Lit):
            return e.value
        if isinstance(e, Ref):
            return env.names.get(e.name)
        if isinstance(e, Implicit):
            return env.implicit
        if isinstance(e, RecordE):
            out = {}
            for k, v in e.fields:
                x = self._eval(v, env, run)
                if is_absent(x) and x.unknown and not env.args:
                    return x  # an unknown field makes the record unknown: it never becomes a null that claims absence
                out[k] = x  # an absent field stays absent (carry)
            return out
        if isinstance(e, ListE):
            out = []
            for v in e.items:
                x = self._eval(v, env, run)
                if is_absent(x) and not env.args:
                    run.loss("list", v.pos, x, env.element, env.in_each)  # lists never hold absent values
                    continue
                out.append(x)
            return out
        if isinstance(e, BlockE):
            inner = env
            for b in e.bindings:
                inner = inner.bind(b.name, self._eval(b.expr, inner, run))
            return self._eval(e.result, inner, run)
        if isinstance(e, Member):
            t = self._eval(e.target, env, run)
            if is_absent(t):
                return t
            v = t.get(e.name) if isinstance(t, dict) else None
            if is_absent(v):
                return v
            mt = run.c.type_of(e)
            if v is None:
                if isinstance(mt, T.TList):
                    return []  # a defaulted list member (TOWL §10)
                return Absent({"kind": "member", "member": e.name, "line": e.pos.line, "col": e.pos.col})
            return T.normalize(v, mt)
        if isinstance(e, OpCall):
            return self._call(e, env, run)
        if isinstance(e, MethodCall):
            return self._method(e, env, run)
        raise Stop("other", "Internal", f"cannot evaluate {type(e).__name__}", None, e.pos)

    def _call(self, e: OpCall, env: _Env, run: _Run):
        op = run.c.ops[id(e)]
        aenv = env.for_args()
        params = self._eval(e.params, aenv, run)
        args = dict(params) if isinstance(params, dict) else {}
        options = {}
        if isinstance(e.options, RecordE):
            for k, v in e.options.fields:
                if k in ("region", "profile"):
                    options[k] = self._eval(v, aenv, run)
        # an absent value never reaches an operation (TOWL §6.1): stop, naming the parameter and the origin
        for where, value in (("parameter", args), ("option", options)):
            found = _find_absent(value)
            if found:
                path, absent = found
                o = absent.origin
                hint = f"; to call only where it exists, filter first: .where(.{o['member']}.present())" if o.get("kind") == "member" else ""
                run.fail(Stop("data", "AbsentArgument",
                              f"{where} '{path}' of {op.id} is absent at runtime: {describe_origin(o)}; an absent value is never passed to an operation{hint}",
                              op, e.pos, env.element, env.in_each))
        empty = _find_empty(args)
        if empty:
            run.fail(Stop("data", "EmptyArgument",
                          f"parameter '{empty}' of {op.id} is an empty list at runtime; an empty list is never passed to an operation, because "
                          "providers often read it as 'no restriction'. Filter first so the call runs only when there is something to pass, "
                          "or fan out over the list", op, e.pos, env.element, env.in_each))
        mutate = op.effect != "read"
        if mutate and run.losses and not run.allow_losses:
            n = run.loss_count()
            run.fail(Stop("losses", "LossesBeforeMutation",
                          f"{op.id} was not dispatched: {n} element(s) were lost to absent values before it (see 'losses'); a mutation acts only on "
                          "complete data. Handle the absence in the program (.present()/.absent() filters) or rerun with --allow-losses once the user accepts the losses",
                          op, e.pos, env.element, env.in_each))
        if run.stopped.is_set():
            raise Stop("cancelled", "Stopped", f"stopped before {op.id} was dispatched", op, e.pos)
        if time.monotonic() - run.started > self.limits.wall_seconds:
            run.fail(Stop("budget", "Timeout", f"wall time budget of {self.limits.wall_seconds}s exceeded at {op.id}", op, e.pos))
        with run.lock:
            run.call_count += 1
            count = run.call_count
        if count > self.limits.max_calls:
            run.fail(Stop("budget", "MaxCalls", f"call budget of {self.limits.max_calls} exceeded at {op.id}", op, e.pos))
        eff = run.effects.setdefault(f"{op.id}@{e.pos.line}", {"line": e.pos.line, "operation": op.id, "effect": op.effect})
        run.bump(eff, "calls")
        run.calls.acquire()
        if mutate:
            run.mutation_lock.acquire()
        try:
            if run.stopped.is_set():
                raise Stop("cancelled", "Stopped", f"stopped before {op.id} was dispatched", op, e.pos)
            if mutate:
                with run.lock:
                    run.in_flight_mutations += 1
            try:
                raw = self.calls.invoke(op, _plain(args), _plain(options))
            except Stop as s:  # adapter-raised budget stops are run failures too
                if s.cls == "budget" and s.code == "MaxItems" and not mutate and env.in_each and not run.strict:
                    # a per-call budget inside a fan-out element drops that element as a loss (TOWL §12.3)
                    return Absent({"kind": "budget", "operation": op.id, "limit": "max-items",
                                   "value": getattr(self.calls, "max_items", None), "line": e.pos.line}, unknown=True)
                run.fail(Stop(s.cls, s.code, str(s), op, e.pos, env.element, env.in_each, s.aws))
        except OperationError as x:
            cls = x.aws.get("class") if x.aws.get("class") == "configuration" else ("mutation" if mutate else classify_error(x.code))
            # a failed read is absorbed by class (TOWL §12.4): absence -> absent; authorization/availability -> unknown,
            # which needs an enclosing list element to drop; everything else, a mutation, or --strict stops
            absorb = not run.strict and (cls == "absence" or (cls in ("authorization", "availability") and env.in_each))
            if absorb:
                with run.lock:
                    run.absorbed.append({"line": e.pos.line, "operation": op.id, "code": x.code, "class": cls, "element": _plain(env.element)})
                return Absent({"kind": "error", "operation": op.id, "code": x.code, "class": cls, "line": e.pos.line}, unknown=cls != "absence")
            run.fail(Stop(cls, x.code, str(x), op, e.pos, env.element, env.in_each, x.aws))
        except Stop:
            raise
        except Exception as x:  # noqa: BLE001
            run.fail(Stop("mutation" if mutate else "other", type(x).__name__, str(x) or repr(x), op, e.pos, env.element, env.in_each))
        finally:
            if mutate:
                with run.lock:
                    run.in_flight_mutations = max(0, run.in_flight_mutations - 1)
                run.mutation_lock.release()
            run.calls.release()
        if mutate:
            with run.lock:
                run.mutations.append({"line": e.pos.line, "operation": op.id, "element": _plain(env.element), "params": _plain(args),
                                      "options": _plain(options), "response": raw})
        return T.normalize(raw, op.output)

    def _method(self, e: MethodCall, env: _Env, run: _Run):
        target = self._eval(e.target, env, run)
        name = e.name
        if name in ("present", "absent"):
            if is_absent(target) and target.unknown:
                return target  # a read that could not be done does not say whether the value exists
            return is_absent(target) == (name == "absent")
        if is_absent(target):
            return target  # carry: a function of an absent value is absent (TOWL §9.2)
        if name == "for":
            return self._for(e, target, env, run)
        if name in STR_FNS or name in TIME_FNS:
            args = [self._eval(a.expr, env, run) for a in e.args if isinstance(a, ExprArg)]
            bad = next((a for a in args if is_absent(a)), None)
            if bad is not None:
                return bad
            return (_string_fn if name in STR_FNS else _time_fn)(name, target, args)
        lst = target if isinstance(target, list) else []
        n = run.node(name, e.pos)
        run.bump(n, "in", len(lst))

        def path_of(el, i=0):
            if i >= len(e.args):
                return el  # identity path: a list of scalars
            return self._eval(e.args[i].expr, env.with_element(el), run)

        def keyed(i=0):
            """(value, element) for every element whose path is present; the others are skipped and recorded."""
            out = []
            for el in lst:
                v = path_of(el, i)
                if is_absent(v):
                    run.loss(name, e.pos, v, el, True)
                else:
                    out.append((v, el))
            return out

        def pred(el, i=0):
            return self._pred(e.args[i].pred, env.with_element(el), run)

        def empty(fn):
            return Absent({"kind": "empty", "function": fn, "line": e.pos.line})

        if name == "flat":
            out = [x for v, _ in keyed() for x in (v if isinstance(v, list) else [])]
        elif name == "flatten":
            out = [x for el in lst for x in (el if isinstance(el, list) else [])]
        elif name == "where":
            out = []
            for el in lst:
                r = pred(el)
                if r is True:
                    out.append(el)
                elif is_absent(r):
                    run.loss("where", e.pos, r, el, True, reason="undecided")
        elif name == "distinct":
            seen, out = set(), []
            for v, _ in (keyed() if e.args else [(el, el) for el in lst]):
                k = _canonical(v)
                if k not in seen:
                    seen.add(k)
                    out.append(v)
        elif name == "concat":
            other = self._eval(e.args[0].expr, env, run)
            if is_absent(other):
                return other
            out = lst + (other if isinstance(other, list) else [])
        elif name == "group":
            groups: Dict[str, dict] = {}
            for k, el in keyed():
                groups.setdefault(_canonical(k), {"key": k, "items": []})["items"].append(el)
            out = list(groups.values())
        elif name == "single":
            if len(lst) > 1:
                run.fail(Stop("cardinality", "NotSingle", f"single() found {len(lst)} elements", None, e.pos, env.element, env.in_each))
            out = lst[0] if lst else empty("single")
        elif name == "count":
            out = len(lst)
        elif name == "sum":
            vals = [v for v, _ in keyed() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            out = sum(vals) if vals else 0
        elif name in ("min", "max"):
            vals = [v for v, _ in keyed()]
            out = (min if name == "min" else max)(vals, key=_ord_key) if vals else empty(name)
        elif name == "avg":
            vals = [v for v, _ in keyed() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            out = (sum(vals) / len(vals)) if vals else empty("avg")
        elif name == "collect":
            out = [v for v, _ in keyed()]
        elif name in ("top", "bottom"):
            count = self._eval(e.args[0].expr, env, run)
            if is_absent(count):
                return count
            count = count if isinstance(count, int) and not isinstance(count, bool) else 0
            ks = keyed(1) if len(e.args) > 1 else [(el, el) for el in lst]
            order = _neg_key if name == "top" else _ord_key
            ks.sort(key=lambda kv: (order(kv[0]), _canonical(kv[1])))  # ties broken by canonical element order
            out = [{"rank": i + 1, "value": el} for i, (k, el) in enumerate(ks[:count])]
        elif name in ("any", "all"):
            out = _fold(name == "all", (pred(el) for el in lst))
        else:
            run.fail(Stop("other", "Internal", f"unknown function {name}", None, e.pos))
        if isinstance(out, list):
            run.bump(n, "out", len(out))
        elif out is not None and not is_absent(out):
            n["out"] = out if isinstance(out, (bool, int, float)) else 1
        return out

    def _for(self, e: MethodCall, target, env: _Env, run: _Run):
        lam: LambdaArg = e.args[0]
        items = target if isinstance(target, list) else []
        n = run.node("for", e.pos)
        run.bump(n, "in", len(items))

        def keep(i, v):
            """A body whose result is absent drops its element (TOWL §7, §9.2)."""
            if is_absent(v):
                run.loss("for", e.pos, v, items[i], True)
                return False
            return True

        if id(e) not in run.c.waves:
            out = []
            for i, it in enumerate(items):
                v = self._eval(lam.body, env.for_element(lam.param, it), run)
                if keep(i, v):
                    out.append(v)
            run.bump(n, "out", len(out))
            return out
        if len(items) > self.limits.max_width:
            run.fail(Stop("budget", "MaxWidth", f"for over {len(items)} elements exceeds the width limit of {self.limits.max_width}", None, e.pos))
        if run.stopped.is_set():
            raise Stop("cancelled", "Stopped", "stopped before wave started", None, e.pos)
        with run.lock:
            run.waves += 1
        started = [False] * len(items)
        done = [False] * len(items)
        results: List[Any] = [None] * len(items)
        failures: List[Optional[Stop]] = [None] * len(items)

        def body(i, item):
            if run.stopped.is_set():
                return
            started[i] = True
            try:
                results[i] = self._eval(lam.body, env.for_element(lam.param, item), run)
                done[i] = True
            except Stop as s:
                failures[i] = s

        futs = [run.pool.submit(body, i, it) for i, it in enumerate(items)]
        for f in futs:
            try:
                f.result()
            except Exception:  # noqa: BLE001
                pass
        own = [i for i, f in enumerate(failures) if f is not None and f.cls != "cancelled"]
        if any(f is not None for f in failures):
            run.fanouts[str(e.pos)] = {
                "line": e.pos.line,
                "completed": [{"element": _plain(items[i]), "value": _plain(results[i])} for i in range(len(items)) if done[i] and not is_absent(results[i])],
                "failed": [{"element": _plain(items[i]), "code": failures[i].code, "message": str(failures[i])} for i in own],
                "interrupted": [_plain(items[i]) for i in range(len(items)) if started[i] and not done[i] and i not in own],
                "not_started": [_plain(items[i]) for i in range(len(items)) if not started[i]],
            }
            for i in range(len(items)):
                if done[i]:
                    keep(i, results[i])
            first = own[0] if own else next(i for i, f in enumerate(failures) if f is not None)
            raise failures[first]
        if run.stopped.is_set():
            raise Stop("cancelled", "Stopped", "stopped during wave", None, e.pos)
        unknown = [i for i in range(len(items)) if is_absent(results[i]) and results[i].unknown and results[i].origin.get("kind") in ("error", "budget")]
        if items and len(unknown) == len(items):
            # every element failed to be read: the program or its credentials are wrong, not the data (TOWL §12.4)
            first = min(unknown, key=lambda i: _canonical(items[i]))
            o = results[first].origin
            cls, code = (o["class"], o["code"]) if o["kind"] == "error" else ("budget", "MaxItems")
            run.fanouts[str(e.pos)] = {"line": e.pos.line, "completed": [], "interrupted": [], "not_started": [],
                                       "failed": [{"element": _plain(items[i]), "code": code if results[i].origin["kind"] != "error" else results[i].origin["code"],
                                                   "message": describe_origin(results[i].origin)} for i in unknown]}
            run.fail(Stop(cls, code,
                          f"every one of the {len(items)} elements of this for failed to be read (first: {describe_origin(o)}); a failure that affects everything "
                          "stops instead of returning an empty result: check permissions, regions, or limits, or narrow the source list",
                          next((x for x in run.c.ops.values() if x.id == o["operation"]), None), e.pos, items[first], True))
        out = [results[i] for i in range(len(items)) if keep(i, results[i])]
        run.bump(n, "out", len(out))
        return out

    # ── predicates: three-valued (TOWL §8.3); an Absent result means undecided and carries the origin ──

    def _pred(self, pr, env: _Env, run: _Run):
        if isinstance(pr, AndP):
            return _fold(True, (self._pred(t, env, run) for t in pr.terms))
        if isinstance(pr, OrP):
            return _fold(False, (self._pred(t, env, run) for t in pr.terms))
        if isinstance(pr, NotP):
            r = self._pred(pr.term, env, run)
            return r if is_absent(r) else not r
        if isinstance(pr, Cmp):
            l, r = self._eval(pr.left, env, run), self._eval(pr.right, env, run)
            if is_absent(l):
                return l
            if is_absent(r):
                return r
            if pr.op == "==":
                return _eq(l, r)
            if pr.op == "!=":
                return not _eq(l, r)
            c = (_ord_key(l) > _ord_key(r)) - (_ord_key(l) < _ord_key(r))
            return {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0}[pr.op]
        if isinstance(pr, InP):
            l, r = self._eval(pr.left, env, run), self._eval(pr.right, env, run)
            if is_absent(l):
                return l
            if is_absent(r):
                return r
            return any(_eq(x, l) for x in (r if isinstance(r, list) else []))
        if isinstance(pr, TestP):
            v = self._eval(pr.operand, env, run)
            if is_absent(v) and v.unknown:
                return v
            if pr.fn == "present":
                return not is_absent(v)
            if pr.fn == "absent":
                return is_absent(v)
            if is_absent(v):
                return v
            if pr.fn == "empty":
                return not v
            a = self._eval(pr.arg, env, run) if pr.arg is not None else ""
            if is_absent(a):
                return a
            if not isinstance(v, str) or not isinstance(a, str):
                return False
            return {"contains": a in v, "starts_with": v.startswith(a), "ends_with": v.endswith(a)}[pr.fn]
        if isinstance(pr, QuantP):
            l = self._eval(pr.operand, env, run)
            if is_absent(l):
                return l
            return _fold(pr.all, (self._pred(pr.inner, env.with_element(x), run) for x in (l if isinstance(l, list) else [])))
        return False

    # ── envelopes ────────────────────────────────────────────────────────────

    def _success(self, run: _Run, value, ms):
        return {
            "status": "ok",
            "type": str(run.c.result_type),
            "value": value,
            "losses": run.loss_list(),
            "absorbed": sorted(run.absorbed, key=lambda t: (t["line"], _canonical(t))),
            "effects": sorted(run.effects.values(), key=lambda x: x["line"]),
            "nodes": sorted(run.nodes.values(), key=lambda x: x["line"]),
            "accounting": {"calls": run.call_count, "waves": run.waves, "wall_ms": ms, "result_bytes": run.result_bytes,
                           "absorbed": len(run.absorbed), "losses": run.loss_count()},
        }

    def _failure(self, run: _Run, s: Stop, ms):
        return {
            "status": "error",
            "error": {
                "class": s.cls, "code": s.code, "message": str(s), "operation": s.op.id if s.op else None,
                "line": s.pos.line if s.pos else None, "element": _plain(s.element) if s.has_element else None,
                "action": _action(s.cls), "aws": s.aws or None,
            },
            "errors_also": [
                {"class": f.cls, "code": f.code, "message": str(f), "operation": f.op.id if f.op else None,
                 "line": f.pos.line if f.pos else None, "element": _plain(f.element) if f.has_element else None}
                for f in run.failures if f is not s and f.cls != "cancelled"
            ],
            "completed": {k: {"type": str(run.c.binding_types.get(k)), "value": _normalize_order(v)} for k, v in sorted(run.completed.items())},
            "losses": run.loss_list(),
            "fanout": sorted(run.fanouts.values(), key=lambda x: x["line"]),
            "mutations": list(run.mutations),
            "accounting": {"calls": run.call_count, "waves": run.waves, "wall_ms": ms, "losses": run.loss_count()},
        }


def _action(cls):
    return {"transient": "rerun", "cancelled": "rerun", "authorization": "narrow", "availability": "narrow",
            "absence": "narrow", "budget": "budget", "mutation": "mutation", "configuration": "configure",
            "losses": "losses"}.get(cls, "rewrite")


def _fold(conj: bool, results):
    """Kleene and (conj) / or over True, False, and Absent (undecided): the deciding value wins, else the first
    undecided, else the identity."""
    undecided = None
    for r in results:
        if is_absent(r):
            undecided = undecided or r
        elif bool(r) != conj:
            return not conj
    return undecided if undecided is not None else conj


def _find_absent(v, path=""):
    """First (path, Absent) inside a call's args/options structure."""
    if is_absent(v):
        return (path or "<value>", v)
    if isinstance(v, dict):
        for k, x in v.items():
            got = _find_absent(x, f"{path}.{k}" if path else k)
            if got:
                return got
    elif isinstance(v, list):
        for i, x in enumerate(v):
            got = _find_absent(x, f"{path}[{i}]")
            if got:
                return got
    return None


def _find_empty(v, path=""):
    """First path of an empty list inside a call's args (TOWL §6.1)."""
    if isinstance(v, list):
        if not v:
            return path or "<value>"
        for i, x in enumerate(v):
            got = _find_empty(x, f"{path}[{i}]")
            if got:
                return got
    elif isinstance(v, dict):
        for k, x in v.items():
            got = _find_empty(x, f"{path}.{k}" if path else k)
            if got:
                return got
    return None


def _plain(v):
    """JSON value: absent record fields become null (TOWL §12.5)."""
    if is_absent(v):
        return None
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def _abbrev(el):
    """A loss sample: records are shortened to their scalar members."""
    el = _plain(el)
    if isinstance(el, dict):
        return {k: v for k, v in el.items() if isinstance(v, (str, int, float, bool))}
    if isinstance(el, list):
        return f"<list of {len(el)}>"
    return el


def _has_mutation(expr, checked) -> bool:
    found = []

    def walk(x):
        if isinstance(x, OpCall):
            op = checked.ops.get(id(x))
            if op is not None and op.effect != "read":
                found.append(True)
            walk(x.params)
        elif isinstance(x, Member):
            walk(x.target)
        elif isinstance(x, RecordE):
            for _, v in x.fields:
                walk(v)
        elif isinstance(x, ListE):
            for v in x.items:
                walk(v)
        elif isinstance(x, BlockE):
            for b in x.bindings:
                walk(b.expr)
            walk(x.result)
        elif isinstance(x, MethodCall):
            walk(x.target)
            for a in x.args:
                if isinstance(a, ExprArg):
                    walk(a.expr)
                elif isinstance(a, LambdaArg):
                    walk(a.body)

    walk(expr)
    return bool(found)


def _unwrap(f: Future):
    return f.result()


def _string_fn(name, s, args):
    if not isinstance(s, str):
        return None
    if name == "after_last":
        sep = args[0] if args and isinstance(args[0], str) else ""
        i = s.rfind(sep)
        return s if not sep or i < 0 else s[i + len(sep):]
    if name == "before_first":
        sep = args[0] if args and isinstance(args[0], str) else ""
        i = s.find(sep)
        return s if not sep or i < 0 else s[:i]
    if name == "lower":
        return s.lower()
    if name == "upper":
        return s.upper()
    return s


def _eq(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(_canonical, a)) == sorted(map(_canonical, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return _canonical(a) == _canonical(b)
    return a == b


def _predefined_value(name):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z") if name == "now" else now.date().isoformat()


def _neg_key(v):
    """Descending order key with a stable, canonical tie-break (top(n, ...))."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, -float(v), "")
    if isinstance(v, str):
        return (1, 0.0, "".join(chr(0x10FFFF - ord(c)) for c in v))
    return (2, 0.0, _canonical(v))


def _time_fn(name, v, args):
    if not isinstance(v, str):
        return None
    from datetime import datetime, timedelta, timezone
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    n = args[0] if args and isinstance(args[0], int) and not isinstance(args[0], bool) else 0
    if name == "minus_days":
        t = t - timedelta(days=n)
    elif name == "minus_hours":
        t = t - timedelta(hours=n)
    elif name == "minus_minutes":
        t = t - timedelta(minutes=n)
    elif name == "start_of_day":
        t = t.replace(hour=0, minute=0, second=0, microsecond=0)
    elif name == "start_of_month":
        t = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif name == "date":
        return t.date().isoformat()
    return t.isoformat().replace("+00:00", "Z")


def _ord_key(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, float(v), "")
    if isinstance(v, str):
        return (1, 0.0, v)
    return (2, 0.0, _canonical(v))


def _canonical(v) -> str:
    """RFC 8785 (JCS) serialization (TOWL §12.5): object keys sorted by UTF-16 code units, no whitespace, strings as
    JSON.stringify writes them, numbers in the ECMAScript Number-to-String form. Absent values serialize as null."""
    if v is None or is_absent(v):
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _es_number(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_canonical(x) for x in v) + "]"
    if isinstance(v, dict):
        keys = sorted(v, key=lambda k: str(k).encode("utf-16-be"))
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + _canonical(v[k]) for k in keys) + "}"
    return json.dumps(str(v), ensure_ascii=False)


def _es_number(x: float) -> str:
    """ECMAScript Number::toString for a finite double (RFC 8785 §3.2.2.3), from Python's shortest round-trip repr."""
    import decimal
    import math
    if not math.isfinite(x):
        return "null"  # not representable in JSON; never produced from provider data
    if x == 0:
        return "0"
    sign, digits, exp = decimal.Decimal(repr(x)).normalize().as_tuple()
    d = "".join(map(str, digits))
    k, n = len(d), len(d) + exp  # k significant digits; the decimal point sits after n of them
    if k <= n <= 21:
        out = d + "0" * (n - k)
    elif 0 < n <= 21:
        out = d[:n] + "." + d[n:]
    elif -6 < n <= 0:
        out = "0." + "0" * (-n) + d
    else:
        e = n - 1
        out = (d if k == 1 else d[0] + "." + d[1:]) + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return ("-" if sign else "") + out


def _normalize_order(v):
    """Lists are bags: serialize elements in canonical order (TOWL §12.5); absent fields become null."""
    if is_absent(v):
        return None
    if isinstance(v, dict):
        return {k: _normalize_order(x) for k, x in v.items()}
    if isinstance(v, list):
        return sorted((_normalize_order(x) for x in v), key=_canonical)
    return v
