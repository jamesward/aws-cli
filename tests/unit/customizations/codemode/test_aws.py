"""AWS profile tests against the vendored botocore models (no network) and the CLI commands."""

import json

import pytest

from awscli.customizations.codemode import types as T
from awscli.customizations.codemode.aws_catalog import AwsCatalog, OperationSpec, Unknown
from awscli.customizations.codemode.command import CodeModeCommand, RunCommand, ValidateCommand, render_codemode_help
from awscli.customizations.codemode.runtime import AwsCalls, ClientAwsCalls, Limits, OperationError, Runtime
from awscli.customizations.codemode.schema import SchemaService, render_schema_text
from awscli.customizations.codemode.service import TowlService
from awscli.customizations.codemode.source import load_inputs, load_plan_source
from awscli.customizations.codemode.syntax import TowlError


@pytest.fixture(scope="module")
def catalog():
    return AwsCatalog(services=["ec2", "s3", "sts", "iam"])


@pytest.fixture(scope="module")
def service(catalog):
    return TowlService(catalog)


# ── catalog typing (Code Mode §3) ─────────────────────────────────────────────


def test_operation_identity_effect_and_paging(catalog):
    op = catalog.operation("ec2", "DescribeInstances")
    assert isinstance(op, OperationSpec) and op.id == "ec2.DescribeInstances" and op.effect == "read" and op.paged
    assert set(op.runtime_owned) == {"NextToken", "MaxResults"}
    assert catalog.operation("ec2", "describe-instances") is op, "kebab spelling resolves to the same spec"
    assert isinstance(catalog.resolve("ec2", "DescribeInstancez"), Unknown)
    assert catalog.operation("ec2", "TerminateInstances").effect == "mutate"
    assert catalog.operation("sts", "GetCallerIdentity").effect == "read"


def test_member_policy_lists_defaulted_others_optional(catalog):
    op = catalog.operation("ec2", "DescribeInstances")
    out = op.output
    assert set(out.fields) == {"Reservations"}, "merged output drops the NextToken cursor"
    res = out.fields["Reservations"]
    assert isinstance(res, T.TList) and isinstance(res.element.fields["Instances"], T.TList)
    inst = res.element.fields["Instances"].element
    assert str(inst) == "ec2.Instance"
    assert str(inst.fields["InstanceId"]) == "string | Null"
    assert str(inst.fields["Tags"]) == "list[ec2.Tag]"
    assert str(inst.fields["State"]) == "ec2.InstanceState | Null"
    inp = op.input
    assert "NextToken" not in inp.fields and str(inp.fields["Filters"]) == "list[ec2.Filter]" and str(inp.fields["DryRun"]) == "bool | Null"


def test_required_input_members_and_error_codes(catalog):
    gbp = catalog.operation("s3", "GetBucketPolicy")
    assert str(gbp.input.fields["Bucket"]) == "string" and gbp.open_error_codes
    assert str(gbp.output) == "s3.GetBucketPolicyOutput" and str(gbp.output.fields["Policy"]) == "string | Null"
    tag = catalog.operation("ec2", "CreateTags")
    assert str(tag.output) == "{}"


def test_map_members_become_key_value_lists(catalog):
    op = catalog.operation("iam", "ListAccountAliases")
    assert isinstance(op.output.fields["AccountAliases"], T.TList)
    # a modeled map: sts AssumeRole has none; use lambda-free check on iam SimulatePrincipalPolicy context? keep generic:
    lam = AwsCatalog(services=["lambda"]).operation("lambda", "GetFunctionConfiguration")
    env = lam.output.fields["Environment"]
    variables = T.strip_null(env).fields["Variables"]
    assert isinstance(variables, T.TList) and set(variables.element.fields) == {"key", "value"}


# ── the spec's worked examples type-check against real models (TOWL §14) ────

EXAMPLES = {
    "regions": ('''towl 3 "Running instances per region"
regions = ["us-east-1", "us-west-2", "eu-west-1"]
for r in regions
  insts = call("ec2", "DescribeInstances", { Filters: [{ Name: "instance-state-name", Values: ["running"] }] }, { region: r })
            .Reservations.flat(.Instances)
  { region: r, count: insts.count(), ids: insts.collect(.InstanceId) }''', "list[{ region: string, count: int, ids: list[string] }]"),
    "bucket_policies": ('''towl 3 "Bucket policies"
for b in call("s3", "ListBuckets").Buckets { bucket: b.Name, policy: call("s3", "GetBucketPolicy", { Bucket: b.Name }, { tolerate: ["NoSuchBucketPolicy"] })?.Policy }''',
                        "list[{ bucket: string | Null, policy: string | Null }]"),
    "volumes_by_az": ('''towl 3 "Volume storage by availability zone"
call("ec2", "DescribeVolumes").Volumes.group(.AvailabilityZone).project({ az: .key, volumes: .items.count(), gib: .items.sum(.Size) })''',
                      "list[{ az: string | Null, volumes: int, gib: int }]"),
    "join": ('''towl 3 "Attached storage per running instance"
insts = call("ec2", "DescribeInstances", { Filters: [{ Name: "instance-state-name", Values: ["running"] }] }).Reservations.flat(.Instances)
vols  = call("ec2", "DescribeVolumes").Volumes
for i in insts
  attached = vols.where(.Attachments.any(.InstanceId == i.InstanceId))
  { id: i.InstanceId, volumes: attached.count(), gib: attached.sum(.Size) }''',
             "list[{ id: string | Null, volumes: int, gib: int }]"),
    "mutations": ('''towl 3 "Stop then tag"
stopped = call("ec2", "StopInstances", { InstanceIds: ["i-0123"] })
tagged  = call("ec2", "CreateTags", { Resources: stopped.StoppingInstances.collect(.InstanceId), Tags: [{ Key: "state", Value: "stopped" }] })
{ stopped: stopped.StoppingInstances.collect(.InstanceId), tagged: tagged }''', "{ stopped: list[string], tagged: {} }"),
}


@pytest.mark.parametrize("name", list(EXAMPLES))
def test_spec_examples_type_check(service, name):
    src, expected = EXAMPLES[name]
    assert str(service.validate(src).result_type) == expected


def test_required_list_parameters_are_enforced(service):
    with pytest.raises(TowlError) as e:
        service.validate('towl 3 call("ec2", "TerminateInstances").TerminatingInstances')
    assert [d.code for d in e.value.diagnostics] == ["catalog.missingParameter"]
    with pytest.raises(TowlError) as e:
        service.validate('towl 3 call("ec2", "DescribeVolumes").Volumes.sum(1)')
    assert [d.code for d in e.value.diagnostics] == ["syntax.argKind"]
    with pytest.raises(TowlError) as e:
        service.validate('towl 3 call("ec2", "DescribeVolumes").Volumes.where(.Size == null && null == 1)')
    assert [d.code for d in e.value.diagnostics] == ["type.compare"]


def test_read_after_mutation_does_not_deadlock_and_mutations_serialize(catalog, service):
    import threading
    order, lock = [], threading.Lock()

    class Calls(AwsCalls):
        active = 0
        overlap = False

        def invoke(self, op, params, options):
            mutate = op.effect != "read"
            with lock:
                if mutate:
                    Calls.active += 1
                    Calls.overlap = Calls.overlap or Calls.active > 1
                order.append(op.id)
            import time
            time.sleep(0.02)
            with lock:
                if mutate:
                    Calls.active -= 1
            if op.id == "ec2.StopInstances":
                return {"StoppingInstances": [{"InstanceId": i} for i in params["InstanceIds"]]}
            if op.id == "ec2.CreateTags":
                return {}
            return {"Reservations": [{"Instances": [{"InstanceId": "i-1"}]}]}

    src = '''towl 3
stopped = call("ec2", "StopInstances", { InstanceIds: ["i-1"] })
after = call("ec2", "DescribeInstances", { InstanceIds: stopped.StoppingInstances.collect(.InstanceId) }).Reservations.flat(.Instances)
tagged = for k in ["a", "b"] call("ec2", "CreateTags", { Resources: ["i-1"], Tags: [{ Key: k, Value: "v" }] })
{ after: after.count(), tagged: tagged.count() }'''
    out = Runtime(catalog, Calls(), Limits(max_concurrency=8)).execute(service.validate(src), {})
    assert out["status"] == "ok", out
    assert order[0] == "ec2.StopInstances" and order.index("ec2.DescribeInstances") < order.index("ec2.CreateTags")
    assert Calls.overlap is False, "mutations never overlap each other"


def test_pagination_members_are_rejected(service):
    with pytest.raises(TowlError) as e:
        service.validate('towl 3 call("ec2", "DescribeInstances", { MaxResults: 5 }).Reservations')
    assert [d.code for d in e.value.diagnostics] == ["catalog.runtimeOwned"]


# ── runtime over real models with a fake AwsCalls ────────────────────────────


class FakeAws(AwsCalls):
    def __init__(self):
        self.calls = []

    def invoke(self, op, params, options):
        self.calls.append((op.id, params, options))
        if op.id == "ec2.DescribeInstances":
            r = options.get("region")
            if r == "eu-west-1":
                raise OperationError("OptInRequired", "region not enabled", {"code": "OptInRequired", "class": "availability"})
            return {"Reservations": [{"Instances": [{"InstanceId": f"i-{r}", "State": {"Name": "running"}, "Tags": []}]}]}
        if op.id == "s3.ListBuckets":
            return {"Buckets": [{"Name": "a"}, {"Name": "b"}]}
        if op.id == "s3.GetBucketPolicy":
            if params["Bucket"] == "b":
                raise OperationError("NoSuchBucketPolicy", "no policy")
            return {"Policy": "{}"}
        raise AssertionError(op.id)


def test_runtime_regions_with_failure_envelope(catalog, service):
    checked = service.validate(EXAMPLES["regions"][0])
    out = Runtime(catalog, FakeAws(), Limits()).execute(checked, {})
    assert out["status"] == "error"
    assert out["error"]["class"] == "availability" and out["error"]["code"] == "OptInRequired" and out["error"]["element"] == "eu-west-1"
    assert out["error"]["action"] == "tolerate-candidate"
    fan = out["fanout"][0]
    assert [f["element"] for f in fan["failed"]] == ["eu-west-1"]
    assert {c["element"] for c in fan["completed"]} | set(fan["interrupted"]) | set(fan["not_started"]) == {"us-east-1", "us-west-2"}
    assert set(out["completed"]) == {"regions"} and "value" not in out


def test_runtime_tolerate_over_real_models(catalog, service):
    checked = service.validate(EXAMPLES["bucket_policies"][0])
    out = Runtime(catalog, FakeAws(), Limits()).execute(checked, {})
    assert out["status"] == "ok"
    assert {r["bucket"]: r["policy"] for r in out["value"]} == {"a": "{}", "b": None}
    assert out["tolerated"][0]["code"] == "NoSuchBucketPolicy"


def test_client_adapter_maps_botocore_errors_to_operation_errors(catalog):
    class Resp(Exception):
        response = {"Error": {"Code": "UnauthorizedOperation", "Message": "nope"}, "ResponseMetadata": {"RequestId": "r1", "HTTPStatusCode": 403}}

    class Client:
        def can_paginate(self, m):
            return False

        def describe_instances(self, **kw):
            raise Resp()

    class Session:
        def create_client(self, *a, **k):
            return Client()

    adapter = ClientAwsCalls(Session())
    with pytest.raises(OperationError) as e:
        adapter.invoke(catalog.operation("ec2", "DescribeInstances"), {}, {"region": "us-east-1"})
    assert e.value.code == "UnauthorizedOperation" and e.value.aws["class"] == "authorization" and e.value.aws["httpStatus"] == 403


def test_client_adapter_forces_the_paginated_form_of_s3_list_buckets(catalog):
    """S3 returns BucketRegion only for paginated ListBuckets requests (those carrying max-buckets)."""
    seen = {}

    class Pages:
        def build_full_result(self):
            return {"Buckets": [{"Name": "a", "BucketRegion": "us-west-2"}]}

    class Paginator:
        def paginate(self, PaginationConfig=None, **params):
            seen.update(PaginationConfig)
            return Pages()

    class Client:
        def can_paginate(self, m):
            return True

        def get_paginator(self, m):
            return Paginator()

    class Session:
        def create_client(self, *a, **k):
            return Client()

    adapter = ClientAwsCalls(Session())
    out = adapter.invoke(catalog.operation("s3", "ListBuckets"), {}, {})
    assert seen["PageSize"] == 1000 and out["Buckets"][0]["BucketRegion"] == "us-west-2"
    seen.clear()
    adapter.invoke(catalog.operation("ec2", "DescribeInstances"), {}, {"region": "us-east-1"})
    assert "PageSize" not in seen  # EC2 rejects MaxResults together with identifier filters; never forced


def test_configuration_failures_are_their_own_class(catalog, service):
    class NoCreds(AwsCalls):
        def invoke(self, op, params, options):
            raise OperationError("NoCredentialsError", "Unable to locate credentials", {"code": "NoCredentialsError", "class": "configuration"})

    out = Runtime(catalog, NoCreds(), Limits()).execute(service.validate('towl 3 call("sts", "GetCallerIdentity").Account'), {})
    assert out["error"]["class"] == "configuration" and out["error"]["action"] == "configure"


# ── search / schema (Code Mode §7) ───────────────────────────────────────────


def test_search_returns_v3_signatures_and_unmatched(catalog):
    s = SchemaService(catalog)
    r = s.search(["ec2 running instances", "frobnicate widgets"], limit=2, brief=True)
    hit = r["queries"][0]["matches"][0]
    assert hit["operation"] == "ec2.DescribeInstances" and hit["paged"] and hit["effect"] == "read"
    assert hit["params"].startswith("{ InstanceIds: list[string]") and "Reservations: list[ec2.Reservation]" in hit["returns"]
    assert r["unmatched"] == ["frobnicate widgets"] and "does not exist" in r["note"]
    text = render_schema_text(r)
    assert "ec2.DescribeInstances  [read, paged]" in text and "unmatched: frobnicate widgets" in text


def test_s3_api_operations_are_searchable_and_service_names_rank(catalog):
    # the CLI index files S3 API operations under `s3api`; `aws s3` has only cp/ls/sync-style custom commands
    ids = {s.id for s in catalog.operation_summaries("s3")}
    assert "s3:list-buckets" in ids and "s3:get-bucket-policy-status" in ids and "s3:cp" not in ids
    s = SchemaService(catalog)
    r = s.search(["list s3 buckets", "get bucket policy status"], limit=2, brief=True)
    assert r["queries"][0]["matches"][0]["operation"] == "s3.ListBuckets"
    assert r["queries"][1]["matches"][0]["operation"] == "s3.GetBucketPolicyStatus"


def test_search_treats_read_verbs_as_one_class_and_ignores_noise_words(catalog):
    """'list AWS regions' must surface ec2.DescribeRegions: list/describe/get are one capability and 'AWS' is noise."""
    s = SchemaService(catalog)
    r = s.search(["list AWS regions", "get running instances"], limit=3, brief=True)
    assert "ec2.DescribeRegions" in [m["operation"] for m in r["queries"][0]["matches"]]
    assert r["queries"][1]["matches"][0]["operation"] == "ec2.DescribeInstances"


def test_exact_schema_lists_required_params_and_resolves_shapes(catalog):
    s = SchemaService(catalog)
    r = s.exact(["ec2.DescribeInstances", "ec2.Filter", "ec2.Nope"])
    ops = r["queries"][0]["matches"][0]
    assert "required" in ops and ops["required"] == []
    shape = r["queries"][1]["matches"][0]
    assert shape["shape"] == "ec2.Filter" and shape["fields"].startswith("{ Name: string | Null")
    assert "Unknown exact operation or shape" in r["queries"][2]["diagnostics"][0]
    text = render_schema_text(r)
    assert "ec2.Filter = { Name: string | Null" in text and "required: none" in text


def test_exact_schema_accepts_both_spellings(catalog):
    s = SchemaService(catalog)
    r = s.exact(["s3.GetBucketPolicy", "ec2:describe-volumes", "nope.Thing"])
    assert r["count"] == 2
    gbp = r["queries"][0]["matches"][0]
    assert gbp["params"] == "{ Bucket: string, ExpectedBucketOwner: string | Null }" and gbp["returns"] == "{ Policy: string | Null }"
    vol = r["queries"][1]["matches"][0]
    assert vol["runtimeOwned"] == ["NextToken", "MaxResults"]
    assert r["queries"][2]["diagnostics"]


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_help_is_plain_text_and_names_nothing_absent():
    text = render_codemode_help()
    for heading in ("AGENT WORKFLOW", "PROGRAM", "LIST FUNCTIONS", "RULES", "EXAMPLES", "RESULTS"):
        assert heading in text
    assert "summarize" not in text and "truncate" not in text and "llm." not in text
    assert "NextToken" in text
    assert '"bindings"' not in text and '{"$"' not in text, "the CLI guide teaches the text form only"
    assert len(text.splitlines()) < 120


def test_public_help_prints_plain_text(capsys):
    from types import SimpleNamespace
    assert CodeModeCommand(object())(["help"], SimpleNamespace()) == 0
    assert capsys.readouterr().out.startswith("AWS CLI CODE MODE")


def test_dedented_continuation_applies_to_the_for_and_tests_compare_with_booleans(catalog, service):
    src = '''towl 3
regions = call("ec2", "DescribeRegions").Regions.project(.RegionName).compact()
functions = for r in regions
              call("ec2", "DescribeVolumes", {}, { region: r }).Volumes.project({ az: .AvailabilityZone })
            .flatten()
insts = call("ec2", "DescribeInstances").Reservations.flat(.Instances)
{ f: functions.count(), e: insts.where(.Tags.empty() == false).count(), ne: insts.where(.Tags.empty() != true).count() }'''
    c = service.validate(src)
    assert str(c.result_type) == "{ f: int, e: int, ne: int }"


def test_repeated_input_flags_all_bind(catalog, capsys, monkeypatch):
    """`--input a=1 --input b=2` must bind both (argparse append + nargs); the parser layer, not _run_main, is under test."""
    from awscli.argparser import ArgTableArgParser
    from awscli.customizations.codemode.source import load_inputs
    from types import SimpleNamespace
    cmd = RunCommand(SimpleNamespace(emit=lambda *a, **k: None))
    parser = ArgTableArgParser(cmd.arg_table)
    parsed, remaining = parser.parse_known_args(["--plan", "towl 3", "--input", "a=1", "--input", "b=2", "--yes"])
    assert not remaining and load_inputs(parsed.input) == {"a": 1, "b": 2}
    parsed, _ = parser.parse_known_args(["--plan", "towl 3", "--input", "a=1", "b=2"])
    assert load_inputs(parsed.input) == {"a": 1, "b": 2}


def test_subcommand_help_is_plain_text_without_a_renderer(capsys):
    from types import SimpleNamespace
    for cls, path in ((ValidateCommand, "codemode validate"), (RunCommand, "codemode run")):
        cmd = cls(object())
        assert cmd(["help"], SimpleNamespace()) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"aws {path}") and "USAGE" in out and "--plan" in out and "EXAMPLES" in out and "groff" not in out
    out = None
    cmd = RunCommand(object())
    cmd(["help"], SimpleNamespace())
    out = capsys.readouterr().out
    assert "--allow-mutations" in out and "(default 50)" in out


def test_plan_source_forms(tmp_path):
    assert load_plan_source('towl 3 "x" 1').origin == "inline"
    assert load_plan_source('{"towl": 3}').origin == "inline"
    f = tmp_path / "p.towl"
    f.write_text("towl 3 1")
    assert load_plan_source(f"file://{f}").text == "towl 3 1"
    with pytest.raises(TowlError):
        load_plan_source("what")


def test_inputs_from_json_file_and_jmespath(tmp_path):
    prev = tmp_path / "prev.json"
    prev.write_text(json.dumps({"fanout": [{"completed": [{"element": "a", "value": {"region": "a"}}], "interrupted": ["b"], "not_started": ["c"]}]}))
    got = load_inputs([f"done=@{prev}:fanout[0].completed[].value", f"remaining=@{prev}:fanout[0].[interrupted, not_started][]", 'n=3', 'name=plain'])
    assert got == {"done": [{"region": "a"}], "remaining": ["b", "c"], "n": 3, "name": "plain"}


class _Globals:
    output = "json"
    region = "us-east-1"
    profile = None


def _cmd(cls, session, **kw):
    from types import SimpleNamespace
    cmd = cls(session)
    defaults = dict(plan=None, input=[], max_width=200, yes=True, allow_mutations=False, allow_profile_override=False,
                    max_concurrency=4, max_calls=500, max_items=1000, max_result_bytes=1 << 20, timeout=60, approval_call_threshold=50)
    defaults.update(kw)
    return cmd, SimpleNamespace(**defaults)


def test_validate_command_reports_and_rejects(catalog, capsys, monkeypatch):
    monkeypatch.setattr("awscli.customizations.codemode.command.AwsCatalog", lambda session: catalog)
    cmd, args = _cmd(ValidateCommand, object(), plan=EXAMPLES["regions"][0])
    assert cmd._run_main(args, _Globals()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["valid"] and out["result_type"] == EXAMPLES["regions"][1] and out["policy"]["approvalRequired"] is False
    cmd, args = _cmd(ValidateCommand, object(), plan='towl 3 call("ec2", "DescribeInstances", { MaxResults: 1 })')
    assert cmd._run_main(args, _Globals()) == 252
    assert json.loads(capsys.readouterr().out)["diagnostics"][0]["code"] == "catalog.runtimeOwned"


def test_run_command_gates_mutations_then_executes(catalog, capsys, monkeypatch):
    monkeypatch.setattr("awscli.customizations.codemode.command.AwsCatalog", lambda session: catalog)
    monkeypatch.setattr("awscli.customizations.codemode.command.ClientAwsCalls", lambda session, profile, max_items: FakeAws())

    class Session:
        def get_config_variable(self, name):
            return "us-east-1"

    cmd, args = _cmd(RunCommand, Session(), plan=EXAMPLES["mutations"][0])
    assert cmd._run_main(args, _Globals()) == 252
    assert json.loads(capsys.readouterr().out)["status"] == "policy_rejected"
    cmd, args = _cmd(RunCommand, Session(), plan=EXAMPLES["bucket_policies"][0])
    assert cmd._run_main(args, _Globals()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and {r["bucket"] for r in out["value"]} == {"a", "b"}
    cmd, args = _cmd(RunCommand, Session(), plan=EXAMPLES["regions"][0])
    assert cmd._run_main(args, _Globals()) == 254
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "OptInRequired"
