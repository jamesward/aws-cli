"""TOWL v3 runtime (TOWL_SPEC.md §§12–13) for the AWS profile (CODE_MODE-SPEC.md §5).

Values are plain JSON. Top-level bindings run as soon as their dependencies are values; ``for``
bodies with calls run as a wave. Every error stops the program except a declared ``tolerate`` code,
and a stopped run returns the failure envelope with only complete values. Pagination is complete
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
    """Raised by AwsCalls; ``code`` is what ``tolerate`` matches against."""

    def __init__(self, code, message, aws=None):
        super().__init__(message)
        self.code = code
        self.aws = aws or {}


class Stop(Exception):
    def __init__(self, cls, code, message, op=None, pos=None, element=None, has_element=False, aws=None):
        super().__init__(message)
        self.cls, self.code, self.op, self.pos, self.element, self.has_element, self.aws = cls, code, op, pos, element, has_element, aws or {}


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
        self._clients = {}
        self._lock = threading.Lock()

    def _client(self, service, region, profile):
        key = (service, region, profile)
        with self._lock:
            if key not in self._clients:
                session = self.session
                if profile:
                    from awscli.botocore.session import Session
                    session = Session(profile=profile)
                kwargs = {"region_name": region} if region else {}
                self._clients[key] = session.create_client(service, **kwargs)
            return self._clients[key]

    def invoke(self, op, params, options):
        from awscli.botocore import xform_name
        client = self._client(op.namespace, options.get("region"), options.get("profile") or self.profile)
        method = xform_name(op.name)
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
    __slots__ = ("names", "implicit", "has_implicit", "element", "in_each")

    def __init__(self, names, implicit=None, has_implicit=False, element=None, in_each=False):
        self.names, self.implicit, self.has_implicit, self.element, self.in_each = names, implicit, has_implicit, element, in_each

    def bind(self, n, v):
        return _Env({**self.names, n: v}, self.implicit, self.has_implicit, self.element, self.in_each)

    def with_element(self, v):
        return _Env(self.names, v, True, self.element, self.in_each)

    def for_element(self, n, v):
        return _Env({**self.names, n: v}, self.implicit, self.has_implicit, v, True)


class _Run:
    def __init__(self, checked, inputs, limits):
        self.c, self.inputs, self.limits = checked, inputs, limits
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
        self.tolerated: List[dict] = []
        self.mutations: List[dict] = []
        self.fanouts: Dict[str, dict] = {}
        self.completed: Dict[str, Any] = {}
        self.waves = 0
        self.result_bytes = 0
        self.started = time.monotonic()

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
    def __init__(self, catalog, calls: AwsCalls, limits: Optional[Limits] = None):
        self.catalog, self.calls, self.limits = catalog, calls, limits or Limits()

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
                "completed": {}, "fanout": [], "mutations": [],
                "accounting": {"calls": 0, "waves": 0, "wall_ms": 0},
            }
        run = _Run(checked, given, self.limits)
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
        if stop is None and exc is None:
            value = _normalize_order(value)
            run.result_bytes = len(_canonical(value).encode("utf-8"))
            if run.result_bytes > self.limits.max_result_bytes:
                stop = Stop("budget", "MaxResultBytes",
                            f"the result is {run.result_bytes} bytes, over the limit of {self.limits.max_result_bytes}; project fewer fields or narrow the list before returning",
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
            return {k: self._eval(v, env, run) for k, v in e.fields}
        if isinstance(e, ListE):
            return [self._eval(v, env, run) for v in e.items]
        if isinstance(e, BlockE):
            inner = env
            for b in e.bindings:
                inner = inner.bind(b.name, self._eval(b.expr, inner, run))
            return self._eval(e.result, inner, run)
        if isinstance(e, Member):
            t = self._eval(e.target, env, run)
            if t is None:
                return None
            v = t.get(e.name) if isinstance(t, dict) else None
            return T.normalize(v, run.c.type_of(e))
        if isinstance(e, OpCall):
            return self._call(e, env, run)
        if isinstance(e, MethodCall):
            return self._method(e, env, run)
        raise Stop("other", "Internal", f"cannot evaluate {type(e).__name__}", None, e.pos)

    def _call(self, e: OpCall, env: _Env, run: _Run):
        op = run.c.ops[id(e)]
        site = next(s for s in run.c.effects if s.call is e)
        params = self._eval(e.params, env, run)
        args = dict(params) if isinstance(params, dict) else {}
        options = {}
        if isinstance(e.options, RecordE):
            for k, v in e.options.fields:
                if k in ("region", "profile"):
                    options[k] = self._eval(v, env, run)
        if op.input is not None:
            required = getattr(op, "required", None)
            for k, t in op.input.fields.items():
                needed = k in required if required is not None else (not T.is_nullable(t) and not isinstance(t, T.TList))
                if needed and k in args and args[k] is None:
                    run.fail(Stop("data", "NullParameter", f"parameter '{k}' of {op.id} was Null at runtime", op, e.pos, env.element, env.in_each))
                if k in args and args[k] is not None:
                    bad = T.null_at_required(args[k], t, k)
                    if bad:
                        run.fail(Stop("data", "NullParameter", f"parameter '{bad}' of {op.id} was Null at runtime", op, e.pos, env.element, env.in_each))
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
        mutate = op.effect != "read"
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
                raw = self.calls.invoke(op, {k: v for k, v in args.items() if v is not None}, options)
            except Stop as s:  # adapter-raised budget stops are run failures too
                run.fail(Stop(s.cls, s.code, str(s), op, e.pos, env.element, env.in_each, s.aws))
        except OperationError as x:
            if x.code in site.tolerate:
                with run.lock:
                    run.tolerated.append({"line": e.pos.line, "operation": op.id, "code": x.code, "element": env.element})
                return None
            cls = x.aws.get("class") if x.aws.get("class") == "configuration" else ("mutation" if mutate else classify_error(x.code))
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
                run.mutations.append({"line": e.pos.line, "operation": op.id, "element": env.element, "params": args,
                                      "options": {"tolerate": list(site.tolerate), **options}, "response": raw})
        return T.normalize(raw, op.output)

    def _method(self, e: MethodCall, env: _Env, run: _Run):
        target = self._eval(e.target, env, run)
        name = e.name
        if name == "for":
            return self._for(e, target, env, run)
        if name in STR_FNS:
            return _string_fn(name, target, [self._eval(a.expr, env, run) for a in e.args if isinstance(a, ExprArg)])
        if name in TIME_FNS:
            return _time_fn(name, target, [self._eval(a.expr, env, run) for a in e.args if isinstance(a, ExprArg)])
        lst = target if isinstance(target, list) else []
        n = run.node(name, e.pos)
        run.bump(n, "in", len(lst))

        def path_of(el, i=0):
            if i >= len(e.args):
                return el  # identity path: a list of scalars
            return self._eval(e.args[i].expr, env.with_element(el), run)

        def pred(el, i=0):
            return self._pred(e.args[i].pred, env.with_element(el), run)

        if name == "project":
            out = [path_of(el) for el in lst]
        elif name == "flat":
            out = [x for el in lst for x in (path_of(el) if isinstance(path_of(el), list) else [])]
        elif name == "flatten":
            out = [x for el in lst for x in (el if isinstance(el, list) else [])]
        elif name == "where":
            out = [el for el in lst if pred(el)]
        elif name == "compact":
            out = [el for el in lst if el is not None]
        elif name == "distinct":
            seen, out = set(), []
            for el in lst:
                k = _canonical(path_of(el) if e.args else el)
                if k not in seen:
                    seen.add(k)
                    out.append(path_of(el) if e.args else el)
        elif name == "concat":
            other = self._eval(e.args[0].expr, env, run)
            out = lst + (other if isinstance(other, list) else [])
        elif name == "group":
            groups: Dict[str, dict] = {}
            for el in lst:
                k = path_of(el)
                groups.setdefault(_canonical(k), {"key": k, "items": []})["items"].append(el)
            out = list(groups.values())
        elif name == "single":
            if len(lst) > 1:
                run.fail(Stop("cardinality", "NotSingle", f"single() found {len(lst)} elements", None, e.pos, env.element, env.in_each))
            out = lst[0] if lst else None
        elif name == "count":
            out = len(lst)
        elif name == "sum":
            vals = [v for v in (path_of(el) for el in lst) if isinstance(v, (int, float)) and not isinstance(v, bool)]
            out = sum(vals) if vals else 0
        elif name in ("min", "max"):
            vals = [v for v in (path_of(el) for el in lst) if v is not None]
            out = (min if name == "min" else max)(vals, key=_ord_key) if vals else None
        elif name == "avg":
            vals = [v for v in (path_of(el) for el in lst) if isinstance(v, (int, float)) and not isinstance(v, bool)]
            out = (sum(vals) / len(vals)) if vals else None
        elif name == "collect":
            out = [v for v in (path_of(el) for el in lst) if v is not None]
        elif name in ("top", "bottom"):
            count = self._eval(e.args[0].expr, env, run)
            count = count if isinstance(count, int) and not isinstance(count, bool) else 0
            keyed = [(path_of(el, 1) if len(e.args) > 1 else el, el) for el in lst]
            keyed = [(k, el) for k, el in keyed if k is not None]
            order = _neg_key if name == "top" else _ord_key
            keyed.sort(key=lambda kv: (order(kv[0]), _canonical(kv[1])))  # ties broken by canonical element order
            out = [{"rank": i + 1, "value": el} for i, (k, el) in enumerate(keyed[:count])]
        elif name == "any":
            out = any(pred(el) for el in lst)
        elif name == "all":
            out = all(pred(el) for el in lst)
        else:
            run.fail(Stop("other", "Internal", f"unknown function {name}", None, e.pos))
        if isinstance(out, list):
            run.bump(n, "out", len(out))
        elif out is not None:
            n["out"] = out if isinstance(out, (bool, int, float)) else 1
        return out

    def _for(self, e: MethodCall, target, env: _Env, run: _Run):
        lam: LambdaArg = e.args[0]
        items = target if isinstance(target, list) else []
        n = run.node("for", e.pos)
        run.bump(n, "in", len(items))
        if id(e) not in run.c.waves:
            out = [self._eval(lam.body, env.for_element(lam.param, it), run) for it in items]
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
                "completed": [{"element": items[i], "value": results[i]} for i in range(len(items)) if done[i]],
                "failed": [{"element": items[i], "code": failures[i].code, "message": str(failures[i])} for i in own],
                "interrupted": [items[i] for i in range(len(items)) if started[i] and not done[i] and i not in own],
                "not_started": [items[i] for i in range(len(items)) if not started[i]],
            }
            first = own[0] if own else next(i for i, f in enumerate(failures) if f is not None)
            raise failures[first]
        if run.stopped.is_set():
            raise Stop("cancelled", "Stopped", "stopped during wave", None, e.pos)
        run.bump(n, "out", len(items))
        return results

    # ── predicates ───────────────────────────────────────────────────────────

    def _pred(self, pr, env: _Env, run: _Run) -> bool:
        if isinstance(pr, AndP):
            return all(self._pred(t, env, run) for t in pr.terms)
        if isinstance(pr, OrP):
            return any(self._pred(t, env, run) for t in pr.terms)
        if isinstance(pr, NotP):
            return not self._pred(pr.term, env, run)
        if isinstance(pr, Cmp):
            l, r = self._eval(pr.left, env, run), self._eval(pr.right, env, run)
            if pr.op == "==":
                return _eq(l, r)
            if pr.op == "!=":
                return not _eq(l, r)
            if l is None or r is None:
                return False
            c = (_ord_key(l) > _ord_key(r)) - (_ord_key(l) < _ord_key(r))
            return {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0}[pr.op]
        if isinstance(pr, InP):
            l, r = self._eval(pr.left, env, run), self._eval(pr.right, env, run)
            return any(_eq(x, l) for x in (r if isinstance(r, list) else []))
        if isinstance(pr, TestP):
            v = self._eval(pr.operand, env, run)
            if pr.fn == "present":
                return v is not None
            if pr.fn == "absent":
                return v is None
            if pr.fn == "empty":
                return not v
            a = self._eval(pr.arg, env, run) if pr.arg is not None else ""
            if not isinstance(v, str) or not isinstance(a, str):
                return False
            return {"contains": a in v, "starts_with": v.startswith(a), "ends_with": v.endswith(a)}[pr.fn]
        if isinstance(pr, QuantP):
            l = self._eval(pr.operand, env, run)
            l = l if isinstance(l, list) else []
            q = all if pr.all else any
            return q(self._pred(pr.inner, env.with_element(x), run) for x in l)
        return False

    # ── envelopes ────────────────────────────────────────────────────────────

    def _success(self, run: _Run, value, ms):
        return {
            "status": "ok",
            "type": str(run.c.result_type),
            "value": value,
            "tolerated": sorted(run.tolerated, key=lambda t: t["line"]),
            "effects": sorted(run.effects.values(), key=lambda x: x["line"]),
            "nodes": sorted(run.nodes.values(), key=lambda x: x["line"]),
            "accounting": {"calls": run.call_count, "waves": run.waves, "wall_ms": ms, "result_bytes": run.result_bytes,
                           "tolerated": len(run.tolerated)},
        }

    def _failure(self, run: _Run, s: Stop, ms):
        return {
            "status": "error",
            "error": {
                "class": s.cls, "code": s.code, "message": str(s), "operation": s.op.id if s.op else None,
                "line": s.pos.line if s.pos else None, "element": s.element if s.has_element else None,
                "action": _action(s.cls), "aws": s.aws or None,
            },
            "errors_also": [
                {"class": f.cls, "code": f.code, "message": str(f), "operation": f.op.id if f.op else None,
                 "line": f.pos.line if f.pos else None, "element": f.element if f.has_element else None}
                for f in run.failures if f is not s and f.cls != "cancelled"
            ],
            "completed": {k: {"type": str(run.c.binding_types.get(k)), "value": _normalize_order(v)} for k, v in sorted(run.completed.items())},
            "fanout": sorted(run.fanouts.values(), key=lambda x: x["line"]),
            "mutations": list(run.mutations),
            "accounting": {"calls": run.call_count, "waves": run.waves, "wall_ms": ms},
        }


def _action(cls):
    return {"transient": "rerun", "cancelled": "rerun", "authorization": "tolerate-candidate", "availability": "tolerate-candidate",
            "absence": "tolerate-candidate", "budget": "budget", "mutation": "mutation", "configuration": "configure"}.get(cls, "rewrite")


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
    return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_order(v):
    """Lists are bags: serialize elements in canonical order (TOWL §12.5)."""
    if isinstance(v, dict):
        return {k: _normalize_order(x) for k, x in v.items()}
    if isinstance(v, list):
        return sorted((_normalize_order(x) for x in v), key=_canonical)
    return v
