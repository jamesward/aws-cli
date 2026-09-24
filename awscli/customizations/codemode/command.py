"""AWS CLI ``aws codemode`` command group (CODE_MODE-SPEC.md §§2, 4–7) for TOWL v3."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from awscli.customizations.commands import BasicCommand

from . import render
from .aws_catalog import AwsCatalog
from .runtime import ClientAwsCalls, Limits, Runtime
from .schema import SchemaService, render_schema_text
from .service import TowlService
from .source import load_inputs, load_plan_source
from .syntax import TowlError

EXIT_INVALID = 252
EXIT_CONFIG = 253
EXIT_AWS = 254


def register_codemode(cli):
    cli.register("building-command-table.main", CodeModeCommand.add_command)


def _print(value):
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")


def invalid_report(exc: TowlError) -> dict:
    out = {"valid": False, "diagnostics": [d.to_dict() for d in exc.diagnostics]}
    if getattr(exc, "source", None):
        out["source"] = exc.source
    return out


def plain_usage(command, path):
    """Renderer-free help for one subcommand: description, arguments, examples (no groff, no pager)."""
    lines = [f"aws {path}", "", "  " + command.DESCRIPTION, ""]
    positional = [a for a in command.ARG_TABLE if a.get("positional_arg")]
    options = [a for a in command.ARG_TABLE if not a.get("positional_arg")]
    usage = f"aws {path}" + "".join(f" <{a['name']}>{'...' if a.get('nargs') in ('+', '*') else ''}" for a in positional)
    for a in options:
        piece = f"--{a['name']}" + ("" if a.get("action") == "store_true" else " <value>")
        usage += f" {piece}" if a.get("required") else f" [{piece}]"
    # wrap the usage line at flag boundaries
    words, cur, wrapped = usage.split(" "), "", []
    for w in words:
        if len(cur) + len(w) + 1 > 100 and cur:
            wrapped.append(cur)
            cur = "    " + w
        else:
            cur = (cur + " " + w) if cur else w
    wrapped.append(cur)
    lines += ["USAGE"] + ["  " + w for w in wrapped] + [""]
    if command.ARG_TABLE:
        lines.append("ARGUMENTS")
        for a in command.ARG_TABLE:
            flag = f"<{a['name']}>" if a.get("positional_arg") else f"--{a['name']}"
            extra = []
            if a.get("default") not in (None, False):
                extra.append(f"default {a['default']}")
            if a.get("required"):
                extra.append("required")
            lines.append(f"  {flag:<28} {a.get('help_text', '')}" + (f" ({', '.join(extra)})" if extra else ""))
        lines.append("")
    examples = getattr(command, "EXAMPLES", ())
    if examples:
        lines.append("EXAMPLES")
        lines += ["  " + e for e in examples]
        lines.append("")
    lines.append("See `aws codemode help` for the TOWL v3 authoring guide.")
    return "\n".join(lines) + "\n"


class _Base(BasicCommand):
    PATH = "codemode"

    def __call__(self, args, parsed_globals):
        if args and args[-1] == "help" or args == ["--help"]:
            sys.stdout.write(plain_usage(self, self.PATH))
            return 0
        return super().__call__(args, parsed_globals)

    def _catalog(self):
        return AwsCatalog(self._session)

    def _service(self):
        return TowlService(self._catalog(), max_width=getattr(self, "_max_width", 200))


class OperationSearchCommand(_Base):
    NAME = "search"
    PATH = "codemode operation search"
    DESCRIPTION = "Search operations by capability (verb + resource); returns concise ranked signatures in TOWL type syntax."
    EXAMPLES = ['aws codemode operation search "ec2 running instances" "ebs volumes" "caller identity" --limit 3',
                "aws codemode operation search \"s3 bucket policy\" --output json"]
    ARG_TABLE = [
        {"name": "query", "positional_arg": True, "nargs": "+",
         "help_text": "One query per capability the task needs; batch them all in one invocation."},
        {"name": "limit", "cli_type_name": "integer", "default": 4, "help_text": "Maximum operations per query."},
    ]

    def _run_main(self, parsed_args, parsed_globals):
        response = SchemaService(self._catalog()).search(list(parsed_args.query), limit=parsed_args.limit, brief=True, depth=0)
        if getattr(parsed_globals, "output", None) == "json":
            _print(response)
        else:
            sys.stdout.write(render_schema_text(response))
        return 0


class OperationCommand(BasicCommand):
    NAME = "operation"
    DESCRIPTION = "Progressive operation discovery: search by capability, then request exact schemas."
    SUBCOMMANDS = [{"name": "search", "command_class": OperationSearchCommand}]

    def __call__(self, args, parsed_globals):
        if not args or args == ["help"]:
            sys.stdout.write("aws codemode operation search <query>... [--limit n]\n\n  " + OperationSearchCommand.DESCRIPTION +
                             "\n  Run `aws codemode operation search help` for details.\n")
            return 0
        return super().__call__(args, parsed_globals)


class SchemaCommand(_Base):
    NAME = "schema"
    PATH = "codemode schema"
    DESCRIPTION = "Exact schemas for service.Operation identifiers (or service.Shape for a named record): parameter and result types, required parameters, effect, paging, error codes, referenced shapes."
    EXAMPLES = ["aws codemode schema ec2.DescribeInstances ec2.DescribeVolumes", "aws codemode schema sts.GetCallerIdentity --output json"]
    ARG_TABLE = [
        {"name": "identifier", "positional_arg": True, "nargs": "+", "help_text": "One or more exact service.Operation (or service.Shape) identifiers."},
    ]

    def _run_main(self, parsed_args, parsed_globals):
        response = SchemaService(self._catalog()).exact(list(parsed_args.identifier), depth=3)
        if getattr(parsed_globals, "output", None) == "json":
            _print(response)
        else:
            sys.stdout.write(render_schema_text(response))
        return 0 if response["count"] == len(parsed_args.identifier) else EXIT_INVALID


_PLAN_ARGS = [
    {"name": "plan", "required": True, "help_text": "Program text starting with 'towl 3', file://path, or - (stdin). The structured JSON form (TOWL 3.1) is also accepted."},
    {"name": "input", "action": "append", "nargs": "+", "help_text": "Bind a declared input: name=<json>, name=@file.json, or name=@file.json:<jmespath> (repeat the flag for several inputs)."},
    {"name": "max-width", "cli_type_name": "integer", "default": 200, "help_text": "Maximum elements in a fan-out with calls."},
]


class ValidateCommand(_Base):
    NAME = "validate"
    PATH = "codemode validate"
    DESCRIPTION = "Parse and type-check a program; prints the typed rendering and effect table. Performs zero AWS calls."
    ARG_TABLE = _PLAN_ARGS
    EXAMPLES = ["aws codemode validate --plan file://plan.towl", "aws codemode validate --plan 'towl 3 call(\"sts\", \"GetCallerIdentity\").Account' --output json",
                "cat plan.towl | aws codemode validate --plan -"]

    def _run_main(self, parsed_args, parsed_globals):
        self._max_width = parsed_args.max_width
        try:
            source = load_plan_source(parsed_args.plan)
            checked = self._service().validate(source.text)
        except TowlError as e:
            _print(invalid_report(e))
            return EXIT_INVALID
        report = {"valid": True, **render.report(checked), "policy": _policy(checked, parsed_args, require_flags=False)}
        if getattr(parsed_globals, "output", None) == "json":
            _print(report)
        else:
            sys.stdout.write(render.typed(checked) + "\n\n")
            for e in checked.effects:
                sys.stdout.write("effect: " + render.effect_text(e) + "\n")
            for w in checked.warnings:
                sys.stdout.write(f"warning: {w}\n")
            sys.stdout.write(f"result: {checked.result_type}\n")
            if report["policy"]["approvalReasons"]:
                flags = (" --allow-mutations" if report["policy"]["mutations"] else "") + " --yes"
                sys.stdout.write("approval required (" + "; ".join(report["policy"]["approvalReasons"]) + f"): run with{flags} after review\n")
        return 0


def _policy(checked, parsed_args, require_flags=True):
    reasons = []
    mutations = [e for e in checked.effects if e.op.effect != "read"]
    allowed = getattr(parsed_args, "allow_mutations", False)
    if mutations:
        reasons.append(f"{len(mutations)} mutating or unknown-effect call site(s): " + ", ".join(e.op.id for e in mutations))
    # read-only programs within the static threshold need no approval: runtime budgets bound dynamic reach (Code Mode §5.4)
    static_calls = sum(e.static_width or 0 for e in checked.effects)
    threshold = getattr(parsed_args, "approval_call_threshold", 50)
    if static_calls > threshold:
        reasons.append(f"static call estimate {static_calls} exceeds threshold {threshold}")
    return {
        "mutations": len(mutations),
        "mutationsAllowed": allowed if require_flags else None,
        "approvalReasons": reasons,
        "approvalRequired": bool(reasons),
    }


class RunCommand(_Base):
    NAME = "run"
    PATH = "codemode run"
    DESCRIPTION = "Type-check, apply policy, and execute a program. Returns the complete result or a failure envelope."
    EXAMPLES = ["aws codemode run --plan file://plan.towl",
                "aws codemode run --plan file://stop.towl --allow-mutations --yes",
                "aws codemode run --plan file://resume.towl --input done=@prev.json:'fanout[0].completed[].value' --input remaining=@prev.json:'fanout[0].[interrupted, not_started][]'"]
    ARG_TABLE = _PLAN_ARGS + [
        {"name": "yes", "action": "store_true", "help_text": "Skip the confirmation prompt; does not bypass gates."},
        {"name": "allow-mutations", "action": "store_true", "help_text": "Authorize mutating or unknown-effect operations."},
        {"name": "allow-losses", "action": "store_true", "help_text": "Dispatch mutations even when earlier steps lost elements to absent values (see 'losses'); only after the user accepts them."},
        {"name": "strict", "action": "store_true", "help_text": "Stop on every error, including failed reads that would otherwise become losses."},
        {"name": "allow-profile-override", "action": "store_true", "help_text": "Permit the per-call profile option."},
        {"name": "max-concurrency", "cli_type_name": "integer", "default": 8, "help_text": "Concurrent AWS requests."},
        {"name": "max-calls", "cli_type_name": "integer", "default": 500, "help_text": "Maximum operation calls in one run; exceeding it stops the program (class budget)."},
        {"name": "max-items", "cli_type_name": "integer", "default": 100000, "help_text": "Maximum items across pages for one paged call; exceeding it stops the program."},
        {"name": "max-result-bytes", "cli_type_name": "integer", "default": 1048576, "help_text": "Maximum size of the result value; exceeding it stops the program."},
        {"name": "timeout", "cli_type_name": "integer", "default": 300, "help_text": "Wall-clock budget in seconds."},
        {"name": "approval-call-threshold", "cli_type_name": "integer", "default": 50, "help_text": "Static call estimate above which a confirmation is required (or --yes)."},
    ]

    def _run_main(self, parsed_args, parsed_globals):
        self._max_width = parsed_args.max_width
        try:
            source = load_plan_source(parsed_args.plan)
            inputs = load_inputs(parsed_args.input)
            checked = self._service().validate(source.text)
        except TowlError as e:
            _print(invalid_report(e))
            return EXIT_INVALID
        report = {"valid": True, **render.report(checked)}
        policy = _policy(checked, parsed_args)
        report["policy"] = policy
        if policy["mutations"] and not parsed_args.allow_mutations:
            _print({"status": "policy_rejected", "reason": "mutating or unknown-effect operations require --allow-mutations", "validation": report})
            return EXIT_INVALID
        if not parsed_args.allow_profile_override and any(render._has_option(e.call, "profile") for e in checked.effects):
            _print({"status": "policy_rejected", "reason": "the per-call profile option requires --allow-profile-override", "validation": report})
            return EXIT_INVALID
        if policy["approvalRequired"] and not parsed_args.yes:
            approved = False
            if sys.stdin.isatty():
                sys.stderr.write(render.typed(checked) + "\n\n" + "\n".join("- " + r for r in policy["approvalReasons"]) + "\nExecute this program? [y/N] ")
                sys.stderr.flush()
                approved = sys.stdin.readline().strip().lower() in ("y", "yes")
            if not approved:
                _print({"status": "policy_rejected", "reason": "; ".join(policy["approvalReasons"]) + "; review with validate and rerun with --yes", "validation": report})
                return EXIT_INVALID
        well_known = {"now": datetime.now(timezone.utc).isoformat(), "today": datetime.now(timezone.utc).date().isoformat(),
                      "region": getattr(parsed_globals, "region", None) or self._session.get_config_variable("region")}
        for inp in checked.program.inputs:
            if inp.name in well_known and inp.name not in inputs:
                inputs[inp.name] = well_known[inp.name]
        calls = ClientAwsCalls(self._session, getattr(parsed_globals, "profile", None), max_items=parsed_args.max_items)
        limits = Limits(parsed_args.max_concurrency, parsed_args.max_width, parsed_args.max_calls, parsed_args.max_result_bytes, float(parsed_args.timeout))
        envelope = Runtime(self._catalog(), calls, limits, allow_losses=getattr(parsed_args, "allow_losses", False),
                           strict=getattr(parsed_args, "strict", False)).execute(checked, inputs)
        _print(envelope)
        if envelope["status"] == "ok":
            return 0
        return EXIT_CONFIG if envelope.get("error", {}).get("class") == "configuration" else EXIT_AWS


def render_codemode_help(catalog=None) -> str:
    """Plain-text authoring guide for TOWL v3 over AWS (Code Mode §7.1). Generated text: names no absent capability."""
    return "\n".join([
        "AWS CLI CODE MODE — TOWL v3 AUTHORING GUIDE",
        "",
        "PURPOSE",
        "  TOWL v3 is a small typed expression language for ONE workflow of AWS operations plus pure",
        "  transforms. You write one program; it is type-checked before anything runs, then executed",
        "  deterministically with derived concurrency. There is no model in the execution loop.",
        "",
        "AGENT WORKFLOW",
        "  1. aws codemode operation search <capability>...   one query per capability (verb + resource), all at once",
        "  2. aws codemode schema service.Operation ...         exact parameter/result TYPES for the chosen operations",
        "  3. write ONE program                                using only those operations, parameters, and members",
        "  4. aws codemode validate --plan <src>                typed rendering + effect table; zero AWS calls; fix every diagnostic",
        "  5. aws codemode run --plan <src> [--allow-mutations] [--yes] [--input name=value]   (--yes when validate lists approval reasons)",
        "     A runtime error stops the program with a failure envelope (completed, failed, never started); fix or narrow and rerun.",
        "",
        "PROGRAM",
        '  towl 3 "what this does"',
        "  input name: type              # optional; host-supplied values (list[string], { id: string }); input region: string is bound by the CLI",
        "  name = expr                   # bindings, each bound once; every binding must feed the result",
        "  expr                          # exactly one result expression, LAST — its value is the answer; keep it small",
        "  Layout: one item per line; braces are only records; a for body is the lines indented under its for line (2 spaces,",
        "  no tabs); a line starting with '.' continues the previous line. --plan takes inline text, file://path, or - (stdin).",
        "VALUES  \"str\"  12  1.5  true  [a, b]  { key: value }      (there is no null)",
        "CALLS (the ONLY effects)  call(\"service\", \"Operation\", { Param: value, ... }, { region: \"us-west-2\" })",
        "  Service and operation are string literals; args and options may be omitted: call(\"sts\", \"GetCallerIdentity\").",
        "  Args are schema-checked; pagination is automatic and complete (never NextToken/MaxResults). A failed read needs no handling:",
        "  'does not exist' (NoSuch*, *NotFound) makes it ABSENT; denied, not enabled, or over --max-items drops its list element (a loss);",
        "  other errors stop, and so does a for where EVERY element is denied (--strict: every error stops).",
        "  A call using another call's result runs after it; independent mutations run one at a time, in source order.",
        "  Calls may NOT appear inside paths, predicates, or args: bind them first.",
        "FAN-OUT (the only binder)  for x in list <body> -> list[body type]; bodies are independent and run concurrently.",
        "  Body: a record/expression on the for line (for b in buckets { bucket: b.Name }), or bindings then the result on",
        "  lines indented under it (result LAST). Bind a for to post-process it (per = for ...; per.flatten()). No lambdas",
        "  ('=>'), no .map/.each, no filters in the header: filter the source (for x in xs.where(...)).",
        "LIST FUNCTIONS take a PATH from the element (.Field.Sub) or a PREDICATE, never a function:",
        "  .collect(.Field) -> list[T]      one value per element;  one record per element: for i in insts { id: i.InstanceId }",
        "  .flat(.Instances) -> list[T]     flattens one list-typed member per element (use this, not collect, for a flat list)",
        "  .flatten()                       list[list[T]] -> list[T]",
        "  .where(pred)                     pred: .A == \"x\"  .A != 1  .N < 5  .A in [\"x\",\"y\"]  .A.present()  .A.absent()  .L.empty()",
        "                                   .A.contains(\"s\") .A.starts_with(\"s\") .A.ends_with(\"s\")  .L.any(pred) .L.all(pred)  && || ! ( )",
        "  .distinct()  .distinct(.Key)  .concat(otherList)  .group(.Key) -> list[{ key, items }]  .single() -> T",
        "AGGREGATES  .count() -> int  .sum(.N) .avg(.N)  .min(.N) .max(.N)  .any(pred) .all(pred)",
        "  .top(5, .Size) / .bottom(5, .Size) -> list[{ rank: int, value: T }]   the n largest/smallest by a key (rank is data; lists are unordered)",
        "STRINGS  s.after_last(\".\")  s.before_first(\"/\")  s.lower()  s.upper()  (no truncation).   On a list of scalars the path may be omitted: xs.sum() xs.top(3)",
        "TIME  now (timestamp) and today (\"YYYY-MM-DD\") are predefined; no declaration needed: now.minus_days(4)  .minus_hours(6)  .minus_minutes(30)  .start_of_day()",
        "      .start_of_month()  .date() -> \"YYYY-MM-DD\".  A string literal where a timestamp parameter is expected is a timestamp.",
        "TYPES  string int number bool timestamp json list[T] { field: T } service.Shape.  No type is nullable; members are",
        "  x.Field, and schemas mark the ones a provider may leave out as Field?: T (you write x.Field either way).",
        "ABSENCE  A member the provider left out, a read of something that does not exist, single()/min()/max()/avg() of nothing is ABSENT.",
        "  You write no null handling. A function of an absent value is absent; a record field may be absent (shown as null);",
        "  a list never holds one: that element is DROPPED (a for body, where, group key, aggregator path) and",
        "  reported under 'losses'. An absent value in a call's args or options STOPS the run (class data): filter first,",
        "  e.g. for b in buckets.where(.BucketRegion.present()).  x.present() / x.absent() test absence (also as bool values).",
        "",
        "RULES",
        "  - Read each operation's returns type: a record -> access members; a bare value -> the call's value IS the result.",
        "  - Search by what an operation DOES (ids, names, regions are parameter values). Filter before you fan out.",
        "  - group vs flatten is a type: .collect(.Instances) gives list[list[...]]; .flat(.Instances) gives list[...].",
        "  - Order of list elements is not meaningful; there are no first/take/sort functions. Keep the result SMALL.",
        "  - Report 'losses' with the result. If a budget or loss makes you narrow the scope, tell the user what was excluded.",
        "",
        "EXAMPLES",
        '  towl 3 "Running instances per region"',
        '  for r in ["us-east-1", "us-west-2", "eu-west-1"]',
        '    insts = call("ec2", "DescribeInstances", { Filters: [{ Name: "instance-state-name", Values: ["running"] }] }, { region: r })',
        "              .Reservations.flat(.Instances)",
        "    { region: r, count: insts.count(), ids: insts.collect(.InstanceId) }   // list[{ region: string, count: int, ids: list[string] }]",
        "",
        '  towl 3 "Bucket policies"   (no policy: policy is null; denied: the bucket is a loss)',
        '  for b in call("s3", "ListBuckets").Buckets { bucket: b.Name, policy: call("s3", "GetBucketPolicy", { Bucket: b.Name }).Policy }',
        "",
        '  towl 3 "Top 10 largest S3 objects"   (a denied bucket is dropped and listed in losses)',
        '  per = for b in call("s3", "ListBuckets").Buckets',
        '    objs = call("s3", "ListObjectsV2", { Bucket: b.Name }, { region: b.BucketRegion }).Contents',
        "    for o in objs { bucket: b.Name, key: o.Key, size: o.Size }",
        "  per.flatten().top(10, .size)",
        "",
        '  towl 3 "Attached storage per running instance"   (two independent calls run concurrently; the for joins them)',
        '  insts = call("ec2", "DescribeInstances", { Filters: [{ Name: "instance-state-name", Values: ["running"] }] }).Reservations.flat(.Instances)',
        '  vols  = call("ec2", "DescribeVolumes").Volumes',
        "  for i in insts",
        "    attached = vols.where(.Attachments.any(.InstanceId == i.InstanceId))",
        "    { id: i.InstanceId, gib: attached.sum(.Size) }",
        "",
        "RESULTS",
        "  ok:    { status, type, value, losses[]: { node, line, reason, origin, count, sample }, absorbed, effects, nodes, accounting }",
        "  error: { status, error: { class, code, message, operation, line, element, action }, completed, losses, fanout[]: { completed, failed, interrupted, not_started }, mutations }",
        "  action: rerun (transient) | rewrite (program bug) | narrow (drop the denied region/resource, or fix access) | budget | mutation",
        "          | losses (no mutation ran because data was lost: filter explicitly, or --allow-losses once the user accepts them)",
    ])


class CodeModeCommand(BasicCommand):
    NAME = "codemode"
    DESCRIPTION = "Author, discover, validate, review, and execute stateless TOWL v3 AWS programs."
    SUBCOMMANDS = [
        {"name": "operation", "command_class": OperationCommand},
        {"name": "schema", "command_class": SchemaCommand},
        {"name": "validate", "command_class": ValidateCommand},
        {"name": "run", "command_class": RunCommand},
    ]

    def __call__(self, args, parsed_globals):
        if args == ["help"]:
            sys.stdout.write(render_codemode_help())
            return 0
        return super().__call__(args, parsed_globals)
