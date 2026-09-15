"""Language-level tests (parser, checker, runtime) against an in-memory catalog: no botocore, no network."""

import threading
import time

import pytest

from awscli.customizations.codemode import types as T
from awscli.customizations.codemode import render
from awscli.customizations.codemode.aws_catalog import classify_error
from awscli.customizations.codemode.runtime import AwsCalls, Limits, OperationError, Runtime
from awscli.customizations.codemode.service import TowlService
from awscli.customizations.codemode.syntax import BlockE, LambdaArg, MethodCall, OpCall, Parser, RecordE, TowlError


class FakeOp:
    def __init__(self, ns, name, inp, out, effect="read", paged=False, codes=()):
        self.namespace, self.name, self.input, self.output, self.effect, self.paged = ns, name, inp, out, effect, paged
        self.error_codes, self.open_error_codes, self.runtime_owned, self.description = frozenset(codes), True, ("NextToken",), f"fake {name}"

    id = property(lambda s: f"{s.namespace}.{s.name}")


SYMBOL = T.TRecord({"fqn": T.STRING, "link": T.STRING, "kind": T.nullable(T.STRING)}, name="docs.Symbol")
INSTANCE = T.TRecord({"InstanceId": T.nullable(T.STRING), "State": T.nullable(T.TRecord({"Name": T.nullable(T.STRING)})), "Tags": T.TList(T.TRecord({"Key": T.nullable(T.STRING), "Value": T.nullable(T.STRING)}))}, name="ec2.Instance")


class FakeCatalog:
    namespaces = frozenset({"ec2", "s3", "docs", "store"})

    def __init__(self):
        rec = T.TRecord
        self.ops = {o.id: o for o in [
            FakeOp("ec2", "DescribeInstances", rec({"Filters": T.TList(rec({"Name": T.nullable(T.STRING), "Values": T.TList(T.STRING)}))}),
                   rec({"Reservations": T.TList(rec({"Instances": T.TList(INSTANCE)}))}), paged=True),
            FakeOp("ec2", "DescribeVolumes", rec({}), rec({"Volumes": T.TList(rec({"VolumeId": T.nullable(T.STRING), "Size": T.nullable(T.INT), "AvailabilityZone": T.nullable(T.STRING),
                                                                                    "Attachments": T.TList(rec({"InstanceId": T.nullable(T.STRING)}))}))}), paged=True),
            FakeOp("ec2", "StopInstances", rec({"InstanceIds": T.TList(T.STRING)}), rec({"StoppingInstances": T.TList(rec({"InstanceId": T.nullable(T.STRING)}))}), effect="mutate"),
            FakeOp("s3", "ListBuckets", rec({}), rec({"Buckets": T.TList(rec({"Name": T.nullable(T.STRING)}))})),
            FakeOp("s3", "GetBucketPolicy", rec({"Bucket": T.STRING}), rec({"Policy": T.nullable(T.STRING)}), codes=("NoSuchBucketPolicy",)),
            FakeOp("docs", "get_latest_version", rec({"groupId": T.STRING, "artifactId": T.STRING}), rec({"result": T.STRING})),
            FakeOp("docs", "list_symbols", rec({"version": T.STRING}), rec({"result": T.TList(SYMBOL)})),
            FakeOp("docs", "get_doc", rec({"version": T.STRING, "link": T.STRING}), T.STRING),
            FakeOp("store", "Put", rec({"key": T.STRING, "value": T.STRING}), rec({"ok": T.BOOL}), effect="mutate"),
        ]}

    def operation(self, ns, name):
        return self.ops.get(f"{ns}.{name}")

    def resolve(self, ns, name):
        from awscli.customizations.codemode.aws_catalog import Unknown
        return self.ops.get(f"{ns}.{name}") or Unknown(tuple(o.name for o in self.ops.values() if o.namespace == ns)[:3])

    def error_class(self, op, code):
        return classify_error(code)


INSTANCES = {"us-east-1": [{"InstanceId": "i-1", "State": {"Name": "running"}}, {"InstanceId": "i-2", "State": {"Name": "running"}}],
             "us-west-2": [{"InstanceId": "i-3", "State": {"Name": "running"}}], "eu-west-1": []}


class FakeCalls(AwsCalls):
    def __init__(self):
        self.calls = []
        self.fail = lambda op, params, options: None
        self.latency = 0.0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def invoke(self, op, params, options):
        with self._lock:
            self.calls.append((op.id, params, options))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.latency:
                time.sleep(self.latency)
            err = self.fail(op, params, options)
            if err:
                raise err
            if op.id == "ec2.DescribeInstances":
                return {"Reservations": [{"Instances": INSTANCES.get(options.get("region", "us-east-1"), [])}]}
            if op.id == "ec2.DescribeVolumes":
                return {"Volumes": [{"VolumeId": "v-1", "Size": 8, "AvailabilityZone": "a", "Attachments": [{"InstanceId": "i-1"}]},
                                    {"VolumeId": "v-2", "Size": 100, "AvailabilityZone": "b", "Attachments": []}]}
            if op.id == "s3.ListBuckets":
                return {"Buckets": [{"Name": "alpha"}, {"Name": "beta"}]}
            if op.id == "s3.GetBucketPolicy":
                return {"Policy": "{}"}
            if op.id == "docs.get_latest_version":
                return {"result": "2.22.2"}
            if op.id == "docs.list_symbols":
                return {"result": [{"fqn": "com.x.PolymorphicTypeValidator", "link": "p.html", "kind": "class"}, {"fqn": "com.x.ObjectMapper", "link": "o.html", "kind": "class"},
                                   {"fqn": "com.x.SubTypeValidator", "link": "s.html", "kind": "interface"}]}
            if op.id == "docs.get_doc":
                return f"doc for {params['link']}"
            if op.id == "ec2.StopInstances":
                return {"StoppingInstances": [{"InstanceId": i} for i in params["InstanceIds"]]}
            if op.id == "store.Put":
                return {"ok": True}
            raise AssertionError(op.id)
        finally:
            with self._lock:
                self.in_flight -= 1


def service(catalog=None, **kw):
    return TowlService(catalog or FakeCatalog(), **kw)


def run(src, calls=None, catalog=None, inputs=None, **limits):
    catalog = catalog or FakeCatalog()
    calls = calls or FakeCalls()
    checked = TowlService(catalog).validate(src)
    return Runtime(catalog, calls, Limits(**limits)).execute(checked, inputs or {}), calls


def errors(src, catalog=None, **kw):
    with pytest.raises(TowlError) as e:
        service(catalog, **kw).validate(src)
    return [d.code for d in e.value.diagnostics if d.severity == "error"], e.value.diagnostics


REGIONS = '''towl 3 "Running instances per region"
regions = ["us-east-1", "us-west-2", "eu-west-1"]
for r in regions
  insts = call("ec2", "DescribeInstances", { Filters: [{ Name: "instance-state-name", Values: ["running"] }] }, { region: r })
            .Reservations.flat(.Instances)
  { region: r, count: insts.count(), ids: insts.collect(.InstanceId) }'''


# ── parser ─────────────────────────────────────────────────────────────────


def test_parses_spec_example_shapes():
    p = Parser(REGIONS, FakeCatalog.namespaces).program()
    assert [b.name for b in p.bindings] == ["regions"]
    m = p.result
    assert isinstance(m, MethodCall) and m.name == "for"
    body = m.args[0].body
    assert isinstance(body, BlockE) and isinstance(body.bindings[0].expr.target.target, OpCall) or isinstance(body, BlockE)


def test_braces_are_records_and_blocks_are_laid_out():
    ns = FakeCatalog.namespaces
    assert isinstance(Parser("towl 3 { a: 1 }", ns).program().result, RecordE)
    assert isinstance(Parser("towl 3 {}", ns).program().result, RecordE)
    for bad in ("towl 3 { a = 1  { b: a } }", "towl 3 { a = 1; b = 2; { b: a, c: b } }"):
        with pytest.raises(TowlError) as e:
            Parser(bad, ns).program()
        assert e.value.diagnostics[0].code == "syntax.brace"
    # a for body: bindings then result on indented lines; the block ends at its result
    p = Parser("towl 3\nxs = [1]\nfor x in xs\n  a = x\n  b = 2\n  { a: a, b: b }\n", ns).program()
    body = p.result.args[0].body
    assert isinstance(body, BlockE) and [b.name for b in body.bindings] == ["a", "b"]
    # same-line bodies: a record, or any single expression; ';' still separates one-line items
    assert isinstance(Parser("towl 3 xs = [1]; for x in xs { a: x }", ns).program().result.args[0].body, RecordE)
    assert isinstance(Parser("towl 3 for x in [[1]] x.count()", ns).program().result.args[0].body, MethodCall)
    # layout diagnostics
    for src, code in (
        ("towl 3\nfor x in [1]\n  { a: x }\n  y = 1\ny", "syntax.resultNotLast"),
        ("towl 3\nxs = [1]\nfor x in xs\n{ a: x }", "syntax.indent"),
        ("towl 3\nfor x in [1]\n  a = 1\n   { a: a }", "syntax.indent"),
        ("towl 3\nx = 1\n{ a: x }\ny = 2", "syntax.trailing"),
        ("towl 3\nfor x in [1]\n\t{ a: x }", "syntax.tab"),
    ):
        with pytest.raises(TowlError) as e:
            Parser(src, ns).program()
        assert e.value.diagnostics[0].code == code, (src, e.value.diagnostics[0])
    # the Python reflex `for x in xs:` is accepted; layout is not checked inside brackets
    assert Parser("towl 3 for x in [1]: { a: x }", ns).program()
    assert Parser("towl 3 { a: for x in [1]\n{ b: x } }", ns).program()


@pytest.mark.parametrize("bad", ["towl 2 \"x\" 1", "towl 3 xs.map(x => x)", "towl 3 for x in xs x", "towl 3 xs.each(x => x)", "towl 3 for (x in xs) { a: x }", "towl 3 xs.first()",
                                 "towl 3 x = 1", "towl 3 1 2", "towl 3 s3.ListBuckets({})", "towl 3 call(s3, \"ListBuckets\")", "towl 3 xs.where(.a.or(1) == 1)",
                                 'towl 3 call("ec2", "DescribeInstances").Reservations.project(call("ec2", "DescribeVolumes"))'])
def test_general_purpose_forms_are_rejected(bad):
    with pytest.raises(TowlError):
        service().validate(bad)


def test_empty_predicate_and_arithmetic_hint():
    out, _ = run('towl 3 call("ec2", "DescribeVolumes").Volumes.where(.Attachments.empty()).collect(.VolumeId)')
    assert out["value"] == ["v-2"]
    assert errors('towl 3 call("ec2", "DescribeVolumes").Volumes.where(.Size.empty())')[0] == ["type.notList"]
    _, diags = errors("towl 3 x = 1 + 2\nx")
    assert diags[0].code == "syntax.badChar" and "no arithmetic" in diags[0].fix


def test_aggregations_are_not_allowed_in_predicates():
    assert errors('towl 3 call("ec2", "DescribeInstances").Reservations.where(.Instances.count() > 1)')[0] == ["syntax.predicateOperand"]


def test_unknown_namespace_is_named_as_such():
    codes, diags = errors('towl 3 call("llm", "summarize", { text: "t" })')
    assert codes == ["catalog.unknownNamespace"]
    assert "'llm' is not a service" in diags[0].message


# ── checker ────────────────────────────────────────────────────────────────


def test_types_the_regions_example():
    c = service().validate(REGIONS)
    assert str(c.result_type) == "list[{ region: string, count: int, ids: list[string] }]"
    assert [(e.op.id, e.static_width) for e in c.effects] == [("ec2.DescribeInstances", 3)]
    assert list(c.waves.values()) == [3]
    assert not c.warnings


def test_project_versus_flat_is_a_visible_type():
    c = service().validate('towl 3 r = call("ec2", "DescribeInstances").Reservations\n{ nested: r.project(.Instances), flat: r.flat(.Instances) }')
    assert str(c.result_type) == "{ nested: list[list[ec2.Instance]], flat: list[ec2.Instance] }"
    assert [w.code for w in c.warnings] == ["type.nestedList"]


def test_nullable_access_and_string_functions():
    codes, _ = errors('towl 3 call("ec2", "DescribeInstances").Reservations.flat(.Instances).project(.State.Name)')
    assert codes == ["type.nullableAccess"]
    c = service().validate('towl 3 call("ec2", "DescribeInstances").Reservations.flat(.Instances).project(.State?.Name.upper())')
    assert str(c.result_type) == "list[string | Null]"


def test_tolerate_rules():
    c = service().validate('towl 3 call("s3", "GetBucketPolicy", { Bucket: "b" }, { tolerate: ["NoSuchBucketPolicy"] })?.Policy')
    assert str(c.result_type) == "string | Null"
    for code, expect in (("Throttling", "catalog.tolerateClass"), ("ValidationException", "catalog.tolerateClass")):
        codes, _ = errors(f'towl 3 call("s3", "GetBucketPolicy", {{ Bucket: "b" }}, {{ tolerate: ["{code}"] }})')
        assert codes == [expect]
    c = service().validate('towl 3 call("s3", "GetBucketPolicy", { Bucket: "b" }, { tolerate: ["NotFoundError"] })')
    assert [w.code for w in c.warnings] == ["catalog.unmodeledErrorCode"]


def test_all_diagnostics_in_one_pass_with_where_for_structured_form():
    src = '{"towl":3,"bindings":[{"name":"a","call":"docs.get_latest_version","args":{"groupId":"g"}},{"name":"b","value":"nothing.here"},{"name":"c","value":"1"}],"result":"{ a: a.result, b: b }"}'
    codes, diags = errors(src)
    assert {"catalog.missingParameter", "names.undefined", "names.unreferenced"} <= set(codes)
    assert {d.where for d in diags} >= {"binding 'a'", "binding 'b'", "binding 'c'"}


def test_bare_scalar_result_names_the_operation_in_the_fix():
    codes, diags = errors('towl 3 call("docs", "get_doc", { version: "1", link: "x" }).result')
    assert codes == ["type.notRecord"]
    assert 'call("docs", "get_doc") returns the string itself' in diags[0].fix and "drop '.result'" in diags[0].fix


def test_runtime_owned_and_unknown_parameters():
    assert errors('towl 3 call("ec2", "DescribeInstances", { NextToken: "x" }).Reservations')[0] == ["catalog.runtimeOwned"]
    assert errors('towl 3 call("s3", "GetBucketPolicy", { Bukcet: "x" }).Policy')[0] == ["catalog.unknownParameter", "catalog.missingParameter"]


def test_empty_literals_and_literal_looks_like_name():
    assert str(service().validate('towl 3 ["a"].concat([])').result_type) == "list[string]"
    assert errors("towl 3 []")[0] == ["type.emptyLiteral"]
    codes, diags = errors('towl 3 ver = call("docs", "get_latest_version", { groupId: "g", artifactId: "a" }).result\ncall("docs", "list_symbols", { version: "ver" }).result')
    assert "names.unreferenced" in codes and any(d.code == "catalog.literalLooksLikeName" for d in diags)


def test_static_width_budget_is_checked_at_validation():
    src = 'towl 3 for r in ["a", "b", "c"] call("ec2", "DescribeInstances", {}, { region: r }).Reservations'
    assert str(service().validate(src).result_type).startswith("list[list[")
    assert errors(src, max_width=2)[0] == ["for.staticWidthExceeded"]


def test_structured_form_renders_and_reports_structural_slips():
    codes, diags = errors('{"towl":3,"bindings":[{"name":"a","value":"1","call":"x.y"},{"name":"b","args":{}},{"name":"c","for":{"over":"xs"}},{"name":"d","map":{"over":"xs","as":"x","result":"x"}}],"syms":"x","result":""}')
    msgs = " | ".join(d.message for d in diags)
    for frag in ("exactly one of value, call, for", "args/options/then belong to a call binding", "for.as", "'map' is now 'for'", "'syms' is not a program member", "'result' is required"):
        assert frag in msgs, frag
    # the structured form renders to the text form: call(...) and for x in xs
    from awscli.customizations.codemode.program_form import render
    text = render({"towl": 3, "bindings": [
        {"name": "vols", "call": "ec2.DescribeVolumes", "then": ".Volumes"},
        {"name": "r", "for": {"over": "vols", "as": "v", "bindings": [{"name": "d", "call": "ec2.DescribeInstances", "args": {"Filters": [{"Name": "volume", "Values": [{"$": "v.VolumeId"}]}]}, "options": {"region": "us-east-1"}}], "result": "{ id: v.VolumeId, n: d.Reservations.count() }"}},
    ], "result": "r"}).source
    assert 'vols = call("ec2", "DescribeVolumes").Volumes' in text and "r = for v in vols\n" in text
    assert 'd = call("ec2", "DescribeInstances", { Filters: [{ Name: "volume", Values: [v.VolumeId] }] }, { region: "us-east-1" })' in text
    assert service().validate(text).result_type is not None


# ── runtime ────────────────────────────────────────────────────────────────


def test_runs_the_regions_example_as_a_wave_with_accounting():
    out, calls = run(REGIONS)
    assert out["status"] == "ok"
    assert out["type"] == "list[{ region: string, count: int, ids: list[string] }]"
    by_region = {r["region"]: r for r in out["value"]}
    assert by_region["us-east-1"]["count"] == 2 and by_region["us-east-1"]["ids"] == ["i-1", "i-2"] and by_region["eu-west-1"]["ids"] == []
    assert len(calls.calls) == 3 and {c[2]["region"] for c in calls.calls} == {"us-east-1", "us-west-2", "eu-west-1"}
    assert out["accounting"]["calls"] == 3 and out["accounting"]["waves"] == 1 and out["accounting"]["result_bytes"] > 0
    assert any(n["node"] == "for" and n["in"] == 3 for n in out["nodes"])


def test_independent_bindings_run_concurrently_and_join():
    calls = FakeCalls()
    calls.latency = 0.15
    src = '''towl 3
insts = call("ec2", "DescribeInstances").Reservations.flat(.Instances)
vols  = call("ec2", "DescribeVolumes").Volumes
for i in insts
  attached = vols.where(.Attachments.any(.InstanceId == i.InstanceId))
  { id: i.InstanceId, volumes: attached.count(), gib: attached.sum(.Size) }'''
    started = time.monotonic()
    out, _ = run(src, calls)
    assert out["status"] == "ok"
    assert calls.max_in_flight == 2, "both reads were in flight together"
    assert time.monotonic() - started < 0.28
    assert {r["id"]: r for r in out["value"]} == {"i-1": {"id": "i-1", "volumes": 1, "gib": 8}, "i-2": {"id": "i-2", "volumes": 0, "gib": 0}}


def test_canonical_order_regardless_of_concurrency():
    a, _ = run(REGIONS, max_concurrency=8)
    b, _ = run(REGIONS, max_concurrency=1)
    assert a["value"] == b["value"]


def test_group_aggregate_single_and_strings():
    out, _ = run('''towl 3
vols = call("ec2", "DescribeVolumes").Volumes
{ by_az: vols.group(.AvailabilityZone).project({ az: .key, n: .items.count(), gib: .items.sum(.Size) }),
  big: vols.where(.Size > 50).single()?.VolumeId, none: vols.where(.Size > 500).single(), up: "ab".upper() }''')
    assert out["status"] == "ok"
    assert out["value"]["by_az"] == [{"az": "a", "n": 1, "gib": 8}, {"az": "b", "n": 1, "gib": 100}]
    assert out["value"]["big"] == "v-2" and out["value"]["none"] is None and out["value"]["up"] == "AB"
    many, _ = run('towl 3 call("ec2", "DescribeVolumes").Volumes.single()')
    assert many["error"]["class"] == "cardinality"


def test_tolerate_yields_null_and_is_reported():
    calls = FakeCalls()
    calls.fail = lambda op, p, o: OperationError("NoSuchBucketPolicy", "none") if op.id == "s3.GetBucketPolicy" and p["Bucket"] == "beta" else None
    out, _ = run('towl 3 for b in call("s3", "ListBuckets").Buckets { bucket: b.Name, policy: call("s3", "GetBucketPolicy", { Bucket: b.Name }, { tolerate: ["NoSuchBucketPolicy"] })?.Policy }', calls)
    assert out["status"] == "ok"
    assert {r["bucket"]: r["policy"] for r in out["value"]} == {"alpha": "{}", "beta": None}
    assert out["tolerated"] == [{"line": 1, "operation": "s3.GetBucketPolicy", "code": "NoSuchBucketPolicy", "element": {"Name": "beta"}}]


def test_untolerated_failure_stops_and_returns_only_complete_values():
    calls = FakeCalls()
    calls.fail = lambda op, p, o: OperationError("UnauthorizedOperation", "denied", {"code": "UnauthorizedOperation"}) if o.get("region") == "us-west-2" else None
    out, _ = run(REGIONS, calls)
    assert out["status"] == "error"
    err = out["error"]
    assert (err["class"], err["code"], err["action"], err["operation"]) == ("authorization", "UnauthorizedOperation", "tolerate-candidate", "ec2.DescribeInstances")
    assert err["element"] == "us-west-2" and err["aws"]["code"] == "UnauthorizedOperation"
    assert set(out["completed"]) == {"regions"}
    fan = out["fanout"][0]
    assert [f["element"] for f in fan["failed"]] == ["us-west-2"]
    assert len(fan["completed"]) + len(fan["failed"]) + len(fan["interrupted"]) + len(fan["not_started"]) == 3
    assert "value" not in out


def test_resume_program_binds_inputs_and_rejects_bad_inputs():
    src = '''towl 3 "resume"
input done: list[{ region: string, count: int, ids: list[string] }]
input remaining: list[string]
done.concat(for r in remaining { region: r, count: 0, ids: ["none"] })'''
    out, _ = run(src, inputs={"done": [{"region": "us-east-1", "count": 2, "ids": ["i-1"]}], "remaining": ["eu-west-1"]})
    assert out["status"] == "ok" and len(out["value"]) == 2
    bad, _ = run(src, inputs={"done": "nope"})
    assert bad["status"] == "error" and bad["error"]["class"] == "input"


def test_mutations_barrier_ordering_and_failure_class():
    calls = FakeCalls()
    out, calls = run('''towl 3
insts = call("ec2", "DescribeInstances").Reservations.flat(.Instances)
stopped = call("ec2", "StopInstances", { InstanceIds: ["i-1"] })
put = call("store", "Put", { key: "k", value: "v" })
{ n: insts.count(), stopped: stopped.StoppingInstances.collect(.InstanceId), ok: put.ok }''', calls)
    assert out["status"] == "ok"
    ids = [c[0] for c in calls.calls]
    # reads before mutations; mutations without a data dependency one at a time in source order (no `after` exists)
    assert ids.index("ec2.DescribeInstances") < ids.index("ec2.StopInstances") < ids.index("store.Put")
    assert errors('towl 3 a = call("ec2", "StopInstances", { InstanceIds: ["i-1"] })  call("store", "Put", { key: "k", value: "v" }, { after: a })')[0] == ["syntax.options", "names.unreferenced"]
    assert len(out.get("effects", [])) == 3
    calls = FakeCalls()
    calls.fail = lambda op, p, o: OperationError("IncorrectInstanceState", "busy") if op.id == "ec2.StopInstances" else None
    bad, _ = run('towl 3 call("ec2", "StopInstances", { InstanceIds: ["i-1"] }).StoppingInstances', calls)
    assert bad["error"]["class"] == "mutation" and bad["error"]["action"] == "mutation"


def test_budgets_stop_before_dispatch_and_result_bytes():
    out, calls = run('towl 3 for b in call("s3", "ListBuckets").Buckets call("s3", "GetBucketPolicy", { Bucket: b.Name })', max_width=1)
    assert out["status"] == "error" and out["error"]["code"] == "MaxWidth" and len(calls.calls) == 1
    out, _ = run('towl 3 call("ec2", "DescribeVolumes").Volumes', max_result_bytes=32)
    assert out["error"]["code"] == "MaxResultBytes" and out["error"]["action"] == "budget"


def test_scalar_aggregates_top_and_time_functions():
    src = '''towl 3
input now: timestamp
input sizes: list[int]
vols = call("ec2", "DescribeVolumes").Volumes
{ total: sizes.sum(), biggest: sizes.max(), top2: sizes.top(2), n: sizes.collect().count(),
  largest: vols.top(1, .Size), smallest: vols.bottom(1, .Size),
  since: now.minus_days(4), day: now.start_of_day(), month: now.start_of_month().date() }'''
    c = service().validate(src)
    assert str(c.binding_types["vols"]) == "list[{ VolumeId: string | Null, Size: int | Null, AvailabilityZone: string | Null, Attachments: list[{ InstanceId: string | Null }] }]"
    assert "top2: list[{ rank: int, value: int }]" in str(c.result_type) and "since: timestamp" in str(c.result_type) and "month: string" in str(c.result_type)
    out, _ = run(src, inputs={"now": "2026-09-14T23:06:17Z", "sizes": [3, 9, 1]})
    assert out["status"] == "ok", out
    v = out["value"]
    assert v["total"] == 13 and v["biggest"] == 9 and v["n"] == 3
    assert v["top2"] == [{"rank": 1, "value": 9}, {"rank": 2, "value": 3}]
    assert v["largest"][0]["value"]["VolumeId"] == "v-2" and v["smallest"][0]["value"]["VolumeId"] == "v-1"
    assert v["since"] == "2026-09-10T23:06:17Z" and v["day"] == "2026-09-14T00:00:00Z" and v["month"] == "2026-09-01"
    assert errors('towl 3 call("ec2", "DescribeVolumes").Volumes.max()')[0] == ["syntax.arity"]
    assert errors('towl 3 ("x").minus_days(1)')[0] == ["type.timestamp"]


def test_null_has_no_default_operator_and_compares_false():
    # ordered comparisons accept nullable operands; Null is never <, >, or == a value
    out, _ = run('towl 3 input xs: list[{ n: int | Null }] { big: xs.where(.n > 1).count(), eq: xs.where(.n == 2).count(), absent: xs.where(.n.absent()).count() }',
                 inputs={"xs": [{"n": None}, {"n": 2}, {"n": 5}]})
    assert out["status"] == "ok" and out["value"] == {"big": 2, "eq": 1, "absent": 1}
    assert out["accounting"]["tolerated"] == 0 and "defaulted" not in out
    codes, diags = errors('towl 3 input x: { a: string | Null } x.a.or("d")')
    assert codes == ["syntax.removed"] and ".or' was removed" in diags[0].message
    # a nullable list needs ?. and stays nullable; there is no .or([])
    src = '''towl 3
for v in call("ec2", "DescribeVolumes").Volumes
  a = call("ec2", "DescribeInstances", { Filters: [{ Name: "volume", Values: [v.VolumeId] }] }, { tolerate: ["InvalidInstanceID.NotFound"] })
  { id: v.VolumeId, reservations: a?.Reservations }'''
    c = service().validate(src)
    assert "reservations: list[{ Instances: list[ec2.Instance] }] | Null" in str(c.result_type)
    codes, diags = errors(src.replace("a?.Reservations }", "a?.Reservations.count() }"))
    assert codes == ["type.notList"] and "compact()" in diags[0].fix
    assert errors('towl 3 call("ec2", "DescribeInstances", { Filters: [] })')[0] == ["catalog.emptyListParameter"]


def test_now_and_today_are_predefined_and_a_declaration_wins():
    c = service().validate("towl 3 { t: now.minus_days(1), d: today, k: now.start_of_day() }")
    assert c.implicit_inputs == ["now", "today"] and str(c.result_type) == "{ t: timestamp, d: string, k: timestamp }"
    out, _ = run("towl 3 { d: today }")
    assert out["status"] == "ok" and len(out["value"]["d"]) == 10
    out, _ = run("towl 3 input now: timestamp { t: now }", inputs={"now": "2026-01-01T00:00:00Z"})
    assert out["value"] == {"t": "2026-01-01T00:00:00Z"}  # a declared input is bound by the caller as before
    assert str(service().validate('towl 3 now = "x" { t: now }').result_type) == "{ t: string }"  # a binding shadows the predefined name
    assert errors("towl 3 { r: region }")[0] == ["names.undefined"]  # region still needs a declaration


def test_service_names_are_ordinary_names_and_call_options_may_be_nullable():
    out, _ = run('towl 3 s3 = call("ec2", "DescribeVolumes").Volumes  { ec2: s3.count() }')
    assert out["status"] == "ok" and out["value"] == {"ec2": 2}
    c = service().validate('towl 3 input r: { name: string | Null } call("ec2", "DescribeVolumes", {}, { region: r.name }).Volumes')
    assert [w.code for w in c.warnings] == ["type.nullableToRequired"]
    codes, diags = errors('towl 3 call("s33", "DescribeVolumes")')
    assert codes == ["catalog.unknownNamespace"] and "did you mean: s3" in diags[0].fix


def test_typed_rendering():
    c = service().validate(REGIONS)
    typed = render.typed(c)
    assert "// regions: list[string]" in typed and "wave ×3" in typed and "ec2.DescribeInstances read paged ×3" in typed
    assert "result: list[{ region: string, count: int, ids: list[string] }]" in typed


def test_error_classifier():
    assert classify_error("NoSuchBucketPolicy") == "absence" and classify_error("NotFoundError") == "absence"
    assert classify_error("ThrottlingException") == "transient" and classify_error("AccessDenied") == "authorization"
    assert classify_error("OptInRequired") == "availability" and classify_error("Weird") == "other"
