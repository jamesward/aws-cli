# AWS CLI Code Mode — TOWL v3 Profile, Product, and Implementation Specification

Status: draft / request for comment  
Code Mode profile version: `3`  
Language dependency: [`TOWL v3`](TOWL_SPEC.md)  
Implementation status: `awscli/customizations/codemode/` implements this specification (TOWL v3). The archived [v1 profile](CODE_MODE_V1-SPEC.md) describes the earlier JSON-plan prototype, which has been removed; v1 plans are rejected with a pointer here.

Code Mode lets an agent (or a person) describe a bounded AWS workflow as one small TOWL program, see a typed review of exactly which operations it will call, and execute it deterministically without a model in the loop. TOWL owns the language, types, and execution model. This document owns everything AWS-specific: the catalog derived from botocore, the CLI surface, policy, budgets, error classification, result rendering, help, and the implementation plan.

If this document conflicts with TOWL v3, TOWL controls and Code Mode rejects the unsupported feature rather than reinterpreting it.

---

## 1. Product

### 1.1 Problem

An agent using ordinary CLI tool calls performs one operation per model turn, receives full payloads into its context, and reasons between calls. Multi-region, multi-service, fan-out, and aggregate tasks cost many turns and tokens, cannot be reviewed as a whole before effects happen, and have no structural limit on fan-out. Code Mode replaces the loop with one program: authored in one turn, validated and typed before any call, executed with derived concurrency, and returned as one complete result.

### 1.2 Goals

- One-shot authoring from API knowledge: the program for a typical task fits in ten lines.
- Nothing runs before a typed review that lists every operation, its effect class, and its multiplicity.
- Validation diagnostics precise enough that a second attempt is right: location, inferred type, one fix.
- No partial data ever reaches the agent as a result; failures return a report that lets the next program resume without repeating work.
- Read-only by default; mutations are explicit, gated, and reported.
- Statelessness: every command receives a complete source and stores nothing.

### 1.3 Non-goals

General scripting, persistence, credential management beyond the CLI's, long-running or resumable processes, provider-agnostic catalogs (this profile is AWS), and any language feature not in TOWL v3.

### 1.4 Flow

```text
agent: aws codemode operation search "running instances"      -> ranked operations
agent: aws codemode schema ec2.DescribeInstances               -> exact typed schema, merged paged output, error codes
agent: aws codemode validate --plan file://p.towl              -> typed review (no AWS calls)
user/policy: approve                                           -> mutations, multiplicity, credential scopes visible
agent: aws codemode run --plan file://p.towl                   -> complete result | failure envelope
agent (on failure): rewrite; bind previous completed values as inputs; run again
```

---

## 2. Command surface

```text
aws codemode help                                         # plain-text authoring guide for TOWL v3 + AWS profile
aws codemode operation search <query>... [--limit n]     # ranked operations with one-line descriptions
aws codemode schema <service.Operation>...                # exact input/output schema, paging, effect class, error codes
aws codemode validate --plan <src> [--input ...]          # parse, type, effects; zero AWS calls; full report
aws codemode run      --plan <src> [--input ...] [flags]  # revalidate, preflight inputs, approve, execute

<src>            = file://path | fileb://path | - (stdin) | inline text starting with "towl"
--input NAME=VAL = literal JSON | @file.json | @file.json:<jmespath>       (repeat the flag once per input)
```

`validate` is the only static review command and returns the complete report. `run` always repeats parsing, validation, and policy for its own source; there is no remembered validation, plan hash, or run history.

### 2.1 Inputs

`--input name=value` binds a declared `input`. `@file.json:<jmespath>` selects a value from a JSON file with a JMESPath expression; this is how a failure envelope's `completed`, `fanout[0].completed[].value`, `fanout[0].interrupted`/`not_started`, or `mutations` sections feed a resume program:

```text
aws codemode run --plan file://resume.towl \
  --input done=@prev.json:'fanout[0].completed[].value' \
  --input remaining=@prev.json:'fanout[0].[interrupted, not_started][]' 
```

Inputs are type-checked against the declared type at preflight. Undeclared inputs, missing inputs, and type mismatches are preflight errors; no call is made.

### 2.2 Well-known inputs

`now` and `today` are predefined by TOWL §5 and need no declaration. If a program declares an input with one of these names and types and the caller does not bind it, Code Mode binds it:

| Input | Type | Value |
|---|---|---|
| `now` | `timestamp` | run start, UTC |
| `today` | `string` | run start date, `YYYY-MM-DD` |
| `region` | `string` | effective CLI region |

Any other name — including the account ID — must be bound by the caller (`--input account_id=…`); Code Mode never injects an operation call that is not a lexical call site in the program. Time windows are computed from `now` inside the program (`StartTime: now.minus_days(4), EndTime: now`; TOWL §9.4), so a caller never has to compute dates in a shell.

### 2.3 Source handling

Code Mode strips one leading/trailing Markdown fence when present. No other repair is performed; TOWL's own tolerances (trailing commas, comments, quote styles) already cover the model's habits.

### 2.4 Agent tool surface (MCP)

The same processor is exposed as an MCP server so that an agent's entire tool belt for AWS work is three tools: discovery happens inside a search tool rather than by loading a catalog into the prompt, one program does all fetching, and the agent pays tokens only for discovery, the program, and the compact envelope (origin: Appendix A).

| Tool | Input | Output |
|---|---|---|
| `codemode_plan_helper` | `queries: list[string]` (one per *capability*: verb + object), `limit?`, `include_guide?` (default true; false on a repeat search) | the TOWL v3 authoring guide (§7.1) plus the matching operations with typed input/merged-output schemas, effect class, error codes; `unmatched`: the queries no operation satisfies, with a note that the catalog is fully listed and searching again finds nothing; a bounded sample of other operation names |
| `codemode_validate` | `program: ProgramForm`, `inputs?: object` | the validation report and typed rendering (§4.2); never throws |
| `codemode_run` | `program: ProgramForm`, `inputs?: object`, `allow_mutations?: bool` | the success or failure envelope; policy rejections are returned as envelopes, never thrown |

`ProgramForm` is the structured program form of TOWL §3.2 — bindings as `{ name, value }`, `{ name, call, args?, options?, then? }` with `call` a `"service.Operation"` string and `args` a real JSON object (`{"$": "expr"}` for references), or `{ name, for: { over, as, bindings, result } }` — so the tool's input schema teaches the skeleton and AWS parameter payloads are JSON, not JSON inside a string. The CLI accepts the text form; both render to the same program.

The tool descriptions MUST carry this guidance: call the helper **once** with all capability queries; search by what an operation *does*, never by the task's subject (library, bucket, instance names are arguments, not operations); a capability the helper reports as `unmatched` does not exist — never search for it again, design without it; pass the program as the structured form, never as one escaped multi-line string; when a result is empty, read `nodes` (per-node element counts) before changing the program. Diagnostics carry `where` (the binding or result they belong to) in addition to line and column.

Two requirements on the helper: it MUST report unmatched queries explicitly, and every piece of authoring guidance — the guide, per-operation notes, examples — MUST be generated from the installed catalog and MUST NOT name an operation, namespace, or capability the catalog lacks. Advice that depends on a capability (for example "pass long text to a summarizing operation") is emitted only when such an operation exists and names it; otherwise the guidance says the capability is absent and what to do instead. (The failure loops behind both are in Appendix A.)

---

## 3. AWS catalog for TOWL v3

The catalog is generated from the installed botocore models and the AWS CLI's paginator and waiter configuration. It is immutable for a given CLI build and identified by the CLI version in every report.

### 3.1 Services and operations

- Service namespace (the first argument of `call`): the botocore service ID in CLI spelling (`ec2`, `s3`, `sts`, `cloudwatch-logs` → `logs` per CLI aliasing). Because it is a string literal, service names are not reserved and may be used as binding names (TOWL §5).
- Operation: exact API PascalCase name (`DescribeInstances`). CLI kebab spelling is accepted by `schema` and `search` for lookup but is not valid in programs; the diagnostic supplies the PascalCase name.
- `service.Shape` type names are botocore shape names qualified by service (`ec2.Instance`).

### 3.2 Input shapes

The `args` of `call("service", "Operation", args, options)` are checked against the modeled input shape: required members present, all members known, no empty-list values, types assignable (TOWL §4, including the nullable-to-required relaxation with its runtime `data` check), enums checked against the model's enum set, blobs typed `string` (base64), timestamps typed `timestamp` (a string literal in a timestamp position is typed by TOWL's literal rule).

Members owned by the runtime are **not authorable** and are hidden from `schema`: paginator cursor and page-size members (`NextToken`, `MaxResults`, `Marker`, and service-specific equivalents named by the paginator configuration). Authoring one is a `catalog.runtimeOwned` error naming the rule. Idempotency-token and checksum members remain authorable; when absent botocore populates them.

### 3.3 Output shapes and member optionality

botocore models almost every response member as optional. Applying TOWL's optional rule literally would make every path nullable, so the AWS profile classifies members:

| Modeled member | TOWL typing | Rationale |
|---|---|---|
| list-typed | **defaulted** `list[T]`, default `[]` | absence and emptiness are indistinguishable to callers; enables `.Reservations.flat(.Instances)` |
| map-typed | **defaulted** `list[{ key: K, value: V }]`, default `[]` | TOWL has no map type; lookup is `.where(.key == "Name").single()?.value` |
| documented absence | catalog-typed | where a provider documents that an absent member *means* a value (S3 `LocationConstraint` absent = `us-east-1`), the catalog SHOULD normalize it to that value and type the member non-nullable; programs have no default operator (TOWL §9.2) |
| structure, scalar, blob, timestamp | **optional** `T \| Null` unless `required` in the model | genuine absence |
| enum | **optional** `string \| Null` with the enum documented | |

`schema` prints the effective typing, so the author sees `Reservations: list[Reservation]` and `Platform: string | Null`.

### 3.4 Pagination

If the operation has a paginator configuration, the catalog marks it `paged` and generates the **merged output shape** from the output shape by an exhaustive rule: output cursor members (`output_token`) are removed; each `result_key` member (always list-typed) is concatenated across pages as a bag; every other member — whether or not listed in `non_aggregate_keys` — is retained with its type and takes its value from the first page. The merged shape is a closed, context-independent type; demanded-field projection (§3.8) affects decoding only, never types, schemas, or reports. Programs see only the merged shape. The runtime paginates to completion under budget (§5.3). There is no way to request a single page; a program that needs fewer items narrows its filters.

### 3.5 Effect classification

Each operation is `read` or `mutate`, decided by `aws_profile_metadata.effect_for`: a reviewed override table first (`sts.AssumeRole` is `read`; `ec2.CreateTags` is `mutate`), then the verb family of the operation name (`Describe`, `List`, `Get`, `Lookup`, `Search`, `Query`, `Batch Get`… are `read`); anything else, including an unrecognized verb, is `mutate`. Unknown is never `read`. The classification is shown by `schema` and in the effect table; the override table is the place a misclassification is corrected, and it is the subject of the golden-set test (§9).

### 3.6 Error codes and classes

For each operation the catalog lists modeled error shapes plus the service-wide common errors, and classifies codes:

The classes and actions are TOWL §12.4's; Code Mode supplies the AWS code mapping:

| TOWL class | AWS codes (examples) |
|---|---|
| `transient` | `Throttling`, `ThrottlingException`, `RequestLimitExceeded`, `TooManyRequestsException`, HTTP 5xx, `RequestTimeout`, connection errors — retried by the runtime; only exhaustion is reported |
| `validation` | `ValidationException`, `ValidationError`, `InvalidParameterValue`, `InvalidParameterCombination`, `MalformedPolicyDocument`, `InvalidFilter` |
| `authorization` | `AccessDenied`, `AccessDeniedException`, `UnauthorizedOperation`, `AuthFailure` |
| `availability` | `OptInRequired`, `UnsupportedOperation`, endpoint resolution failure |
| `absence` | `NoSuchBucketPolicy`, `NoSuchTagSet`, `NoSuchLifecycleConfiguration`, `ServerSideEncryptionConfigurationNotFoundError`, `ResourceNotFoundException`, any code containing `NotFound` or `NoSuch` |
| `state` | `IncorrectInstanceState`, `DependencyViolation`, `ResourceInUseException`, `ConditionalCheckFailedException` |
| `other` | anything else |

The classifier is deterministic: exact code table first, then the `NotFound`/`NoSuch` substring rule, then `other`. Substring rather than suffix matching matters because authors copy codes from operation documentation, where spellings such as `NotFoundError` appear; a code the classifier does not recognize cannot be tolerated. A failed `mutate` call is reported as TOWL class `mutation` with the AWS classification in `error.aws.class`. `openErrorCodes` is true for every AWS operation because service error models are incomplete: a `tolerate` code not modeled for the operation is accepted with a warning when the classifier places it in `absence`, `authorization`, `availability`, or `state`, and is a validation error otherwise.

### 3.7 Options

Core: `tolerate` (TOWL §6.2). AWS profile options:

| Key | Type | Meaning |
|---|---|---|
| `region` | `string` | endpoint region for this call; default is the CLI's effective region |
| `profile` | `string` | credential profile; rejected unless `--allow-profile-override` |

Options are a separate record from `args`, so an API member that happens to be named like an option (Glue models a `Region` member) needs no aliasing rule.

### 3.8 Predicate pushdown and demanded fields

A `where(pred)` immediately following a call (or following `flat` of a call's list member) MAY be pushed into the request when the catalog has an exact mapping from the predicate's path to a server-side filter (EC2 `Filters`, tag filters, and other reviewed mappings) and the comparison is equality or `in` against literals. Pushed predicates are still evaluated client-side afterwards, so results are identical either way; pushdown only reduces transfer. The effect table shows `pushed` per predicate that was actually pushed. Filters the author wrote directly in `args` are ordinary parameters, not pushdown.

A processor MAY decode only the members a program references; types, schemas, and reports are unaffected either way. Neither pushdown nor demanded-field decoding is implemented today (§8); every predicate is evaluated client-side and whole responses are decoded, which is always correct.

---

## 4. Validation and review

### 4.1 Pipeline

```text
parse -> names -> catalog -> types -> effects -> policy
```

Phases 1–4 are TOWL §11; all diagnostics are collected in one pass. Phase 5 builds the effect table. Phase 6 (Code Mode policy) applies §6 gates and is reported separately: a program can be `valid` yet `not approvable` under current flags, and the report says which flag would change that.

### 4.2 Report rendering

The report is JSON (TOWL §13.1) plus a **typed rendering**: the canonical pretty-print of the program with the inferred type of every binding and the effect of every call as trailing comments:

```text
regions = ["us-east-1", "us-west-2", "eu-west-1"]                       // list[string]

for r in regions                                                          // wave ×3; result: list[{ region: string, count: int, ids: list[string] }]
  insts = call("ec2", "DescribeInstances", { Filters: [...] }, { region: r })   // insts: list[ec2.Instance]; ec2.DescribeInstances read paged ×3
            .Reservations.flat(.Instances)
  { region: r, count: insts.count(), ids: insts.collect(.InstanceId) }
```

This rendering — not the raw source — is what the reviewer approves. `validate` prints it after the diagnostics; `run` prints it before the approval prompt.

### 4.3 Diagnostics that matter most

| Code | Trigger | Message shape |
|---|---|---|
| `type.nestedList` | `project(.Instances)` where a flat list is later required, or `collect` over a list-typed path | shows `list[list[T]]`, suggests `flat(.Instances)` or `flatten()` |
| `type.nullableAccess` | `.m` on `T \| Null` | names `?.m`; the result stays nullable |
| `syntax.removed` / `syntax.forForm` / `syntax.lambda` / `syntax.callForm` | forms borrowed from other languages (TOWL §3): `.or(...)`, `xs.map(...)`, `xs.each(...)`, `x => ...`, `for (x in xs)`, `service.Op(...)` | one fix each: keep the value nullable; `for x in xs` with its body forms; `call("service", "Op", { ... })` |
| `syntax.indent` / `syntax.resultNotLast` / `syntax.tab` / `syntax.brace` | layout that disagrees with structure (TOWL §3.1); a binding after a body's result; a tab; `{ a = ... }` | names the block and columns; says the result must be last; says braces enclose records and bindings go on their own lines |
| `catalog.runtimeOwned` | authored `NextToken`/`MaxResults` | explains pagination is automatic |
| `catalog.unknownMember` | misspelled or CLI-spelled member | lists the nearest API-spelled members |
| `names.unreferenced` | binding not reachable from the result | explains "all effects flow into the result" |
| `effects.mutationWithoutGate` | `mutate` call without `--allow-mutations` (policy phase) | names the flag |
| `for.staticWidthExceeded` | literal source longer than `--max-width` | shows width and budget |
| `catalog.unknownNamespace` | `call("x", ...)` where `x` is not a service (in either program form) | names the nearest services; never reported as "op is not a function" |
| `type.notRecord` | `.Member` on a bare scalar result | names the operation that returned the scalar and says to drop `.Member` |
| `type.nullableToRequired` (warning) | `T \| Null` supplied where the operation requires `T`, at any depth (`Dimensions: [{ Value: b.Name }]`) | shows both types one level deep; a runtime Null is a `data` error naming the path (`Dimensions[0].Value`) |
| `syntax.arity` on `xs.max()` | aggregator without a path on a list of records | says which path kind is needed and that the path is optional only for a list of scalars |
| `type.timestamp` | `.minus_days` on a non-timestamp | says timestamps come from the predefined input `now` or a timestamp member |
| `catalog.emptyListParameter` | `Param: []` | says AWS rejects it and to omit optional parameters |
| `catalog.literalLooksLikeName` (warning) | a string parameter equal to a binding name | shows the reference form; pairs with `names.unreferenced` when the binding is then unused |
| `budget` / `MaxResultBytes` (runtime) | result over `--max-result-bytes` | says to project fewer fields or narrow the list; `accounting.result_bytes` is reported on success |

---

## 5. Execution

### 5.1 Scheduling

TOWL §12.1: dispatch when arguments are values; `for` unrolls when its source is a value; independent work overlaps. Code Mode adds one policy barrier: **all `read` calls that do not depend on a mutation complete before the first `mutate` call is dispatched**, and `mutate` calls without a data dependency between them execute one at a time (in source order at the top level; in admission order inside a wave) — this is the ordering a program gets for "stop, then tag" when it does not thread the first result into the second call. Read-after-mutate happens only through a data dependency and carries a report note that visibility is eventually consistent. The barrier is applied at binding granularity: a binding containing any mutation waits for every mutation-independent binding.

### 5.2 Runtime ownership

The executor owns client creation and caching (by service, region, credential scope), per-endpoint concurrency and adaptive backpressure, botocore retries with jitter, pagination, idempotency-token injection, cancellation, and accounting. Programs cannot set any of these.

### 5.3 Budgets

| Budget | Default | Flag | On exceed |
|---|---|---|---|
| operation calls (physical requests) | 500 | `--max-calls` | stop, class `budget` |
| wave width (elements per `for` with calls) | 200 | `--max-width` | static: validation error; dynamic: stop before dispatch |
| nested wave depth | 2 | (fixed) | validation error |
| items per paged call | 100 000 | `--max-items` | stop, class `budget` (botocore paginators merge pages; the item cap is the enforceable bound) |
| concurrency | 8 | `--max-concurrency` | n/a |
| result bytes | 1 MiB | `--max-result-bytes` | stop, class `budget` |
| wall time | 300 s | `--timeout` | stop, class `budget` |

A budget stop produces the failure envelope; there is no truncated success.

### 5.4 Gates and approval

- Read-only by default; any `mutate` call site requires `--allow-mutations`.
- `profile` option requires `--allow-profile-override`.
- Interactive `run` shows the typed rendering and effect table and asks for confirmation when the program mutates, when any wave is dynamic, when the static call estimate exceeds `--approval-call-threshold` (default 50), or when more than one literal region is used. Non-interactive runs without `--yes` stop with `status: policy_rejected` (exit 252) and make no calls. `--yes` skips only the prompt.

### 5.5 Errors and the failure envelope

TOWL §12.4 and TOWL §13.3 apply. Code Mode maps the AWS code to the TOWL class per §3.6 (with the `mutation` override), and adds `error.aws` with the raw AWS error code, its AWS classification, message, request ID, and HTTP status. Transient retries are botocore's and are not itemized. The stop protocol, primary-error selection, and grace period (5 s) are TOWL's.

Credential and endpoint configuration failures raised before a request is sent (botocore `NoCredentialsError`, `ProfileNotFound`, `NoRegionError`, SSO token errors, endpoint connection errors) are a Code Mode-specific class `configuration` with action `configure`: the program is fine and nothing about it should change; the CLI exits 253.

The `action` field is the agent's cue:

| action | meaning for the agent |
|---|---|
| `rerun` | transient exhaustion; the same program is fine |
| `configure` | credentials, profile, region, or endpoint configuration; the same program is fine once the CLI is configured |
| `rewrite` | the program is wrong (input, provider validation, state, cardinality, data, other) |
| `tolerate-candidate` | narrow the scope (drop the region/resource) or declare `tolerate` at that call, using `fanout[].failed` |
| `budget` | narrow the scope or raise the flag with the user |
| `mutation` | some mutations succeeded; bind `mutations` as an input and exclude them |

---

## 6. Result rendering

Success and failure envelopes are TOWL §13.2 and TOWL §13.3 JSON on stdout; the approval prompt and the typed rendering shown before it go to stderr. `validate` prints the typed rendering as text by default and the JSON report with `--output json`; `run` always prints the JSON envelope (the `--output table` question is open, §10). Lists are in canonical order (TOWL §12.5).

---

## 7. Discovery and help

### 7.1 `aws codemode help`

Plain text, renderer-free, generated from the installed catalog so it cannot drift from the executor — and so it never names an operation or capability the catalog lacks (§2.4). Sections: purpose; the five-step agent workflow; program shape and layout (TOWL §3.1); values, `call`, members, `for`; the list functions, aggregators, string and time functions with types; the type vocabulary and the Null rules; authoring rules; three worked examples (fan-out per region, tolerated absence, two independent calls joined by a `for`); the success and failure envelopes with the error-action table. Text form only — the structured form (TOWL §3.2) is for tool arguments (§2.4) and is not taught by the CLI. Target length: what a model needs in-context to author correctly in one turn — under 90 lines; the eval in §8 measures the outcome.

### 7.2 `operation search`

Ranked over service, operation, and documentation text, with read verbs (`list`/`describe`/`get`/…) treated as one class, noise words (`aws`, `my`, `all`, …) ignored, a tie-break toward core services, and a stable preference for shorter operation names. Each hit prints `service.Operation  [read|mutate, paged?]  (cli: service:operation)`, the first sentence of its documentation, and its `params`/`required`/`returns` lines in TOWL type syntax — enough that `schema` is only needed for nested shapes. Queries that match nothing are listed under `unmatched`.

### 7.3 `schema`

Exact, lossless, concise: input members with types, a `required:` line naming the parameters that must be present (the others are omitted, never passed as `[]` or `null`), runtime-owned members listed separately, the (merged) output shape in TOWL type syntax, effect class, paging, modeled error codes, and referenced shapes. Shared shapes are printed once and referenced by name, and a shape name is itself a valid identifier (`aws codemode schema cloudwatch.Dimension`) because authors look up what they see in a type. An operation whose result is a bare scalar (`string`, `json`) is rendered as "`string` (a bare value, not a record)" with a note that the call's value is the result and there is no wrapper member: models generalize the `{ result: … }` shape of neighbouring operations to one that lacks it, and the note prevents that.

---

## 8. Implementation design

Modules under `awscli/customizations/codemode/`:

| Module | Responsibility |
|---|---|
| `syntax.py` | lexer, AST, recursive-descent parser for the TOWL v3 grammar; `free_refs` |
| `types.py` | type vocabulary, assignability, join, runtime normalization, `describe` |
| `aws_catalog.py` | botocore → services (namespaces), operations, member-policy typing, merged paged output, effect class, error-code classification, search summaries |
| `aws_profile_metadata.py` | reviewed effect classification (verb families + overrides) |
| `check.py` | names, catalog, types, effects, static widths; all diagnostics in one pass |
| `program_form.py` | the structured JSON form: structural checks, rendering to text with `where` mapping |
| `service.py` | the stateless pipeline for both program forms |
| `runtime.py` | dependency scheduling with the read-before-mutate barrier, waves and admission, `tolerate`, stop semantics, envelopes; `ClientAwsCalls` (botocore paginators, error mapping) |
| `render.py` | typed rendering and validation report |
| `schema.py` | capability search and exact schemas in TOWL type syntax |
| `source.py` | `--plan` and `--input` resolution |
| `tests/agent/codemode/eval.py` | agent evaluation: runs `cases.json` tasks through `claude -p` with a shim `aws` on PATH, no credentials; checks that the agent's final program validates, uses the expected operations/effects/text, avoids forbidden text (`.or(`, `=>`, runtime-owned members, `codemode run`) and stays within a call budget |
| `command.py` | CLI commands, policy gates, approval, generated help |

Dependency graph and waves are derived from the AST (`free_refs`, checker `waves`); there is no separate lowering artifact. Predicate pushdown (§3.8) is not implemented yet; every predicate is evaluated client-side, which is always correct.

---

## 9. Tests

Present (`tests/unit/customizations/codemode/`):

- **Language** (`test_core.py`, fake catalog): parsing incl. layout diagnostics, every stdlib function, Null rules, predicates, aggregators, time functions, the structured form, waves and budgets, scheduling permutations (TOWL invariant 14), the barrier and mutation serialization, tolerate, the failure envelope's `completed`/`fanout` sections, inputs and resume.
- **AWS profile** (`test_aws.py`, real botocore models, fake transport): member typing policy (§3.3), merged paged shapes, error-code classification and the `mutation` override, the paginated `ListBuckets` form, required-parameter and shape rendering in `schema`, search ranking, CLI commands (gates, exit codes, `--input` binding), the six worked examples' typed results.
- **Agent evaluation** (`tests/agent/codemode/eval.py`): eight tasks through `claude -p` (§8); pass means the agent's final program validates with the expected operations within a call budget.

Planned: effect classification against a reviewed golden set; recorded-response fixtures per error class; an opt-in integration run against a real account (examples 1–3 read-only; example 5 under `--allow-mutations`).

---

## 10. Decisions and open items

- Decisions taken in this profile: pagination is invisible and complete; no partial success; exit on all errors except declared `tolerate` codes; `tolerate` limited to non-transient, non-validation classes; list/map members defaulted, others optional; read-before-mutate barrier; mutations serialized unless dependent; no journal — resume via inputs bound from the failure envelope; `now`/`today` predefined, `region` bound when declared; v1 JSON plans rejected with a pointer here (Appendix A).
- Open: whether `--output table` should support nested records via flattening; whether `account_id` as a well-known input should require confirmation as a read call; the reviewed golden set for effect classification; catalog normalization of documented absences (§3.3) is specified but not implemented; non-paginated operations that still take a cursor parameter return one page and should be marked or paged.

---

## 11. Conformance fixture

The AWS programs in TOWL §14 (examples 1–6; example 7 is an MCP-catalog fixture and belongs to the Kotlin implementation) constitute the Code Mode fixture set, with these AWS-profile expectations:

| Example | Expected effect table | Expected type |
|---|---|---|
| 1 | `ec2.DescribeInstances read paged ×3` | `list[{ region: string, count: int, ids: list[string] }]` (`InstanceId` is optional; `collect` skips `Null`) |
| 2 | `s3.ListBuckets read paged ×1; s3.GetBucketPolicy read ×dynamic(≤200) tolerate NoSuchBucketPolicy`; one nullable-to-required warning on `Bucket` | `list[{ bucket: string \| Null, policy: string \| Null }]` |
| 3 | `ec2.DescribeVolumes read paged ×1` | `list[{ az: string \| Null, volumes: int, gib: int }]` |
| 4 | `ec2.DescribeInstances read paged ×dynamic(≤200)`; two inputs required | as 1 |
| 5 | `ec2.StopInstances mutate ×1; ec2.CreateTags mutate ×1` — the second depends on the first's result; requires `--allow-mutations` | `{ stopped: list[string], tagged: {} }` |
| 6 | `ec2.DescribeInstances read paged ×1; ec2.DescribeVolumes read paged ×1` — one wave of two independent calls; `for` is pure fan-in | `list[{ id: string \| Null, volumes: int, gib: int }]` |

The typed results above are asserted by `test_aws.py` against the installed botocore models; examples 2, 5, and 6 also run against a fake transport.

---

## Appendix A. History and learnings (informative)

- **v1 → v3.** The v1 prototype was a JSON plan language (`let`/`source`/`forEach` nodes, a function registry) behind the same command names. v3 replaced it without migration: v1 plans are small and are re-authored; the structured-form check rejects them (`towl` must be 3; the v1 members are unknown) with a pointer to this document.
- **The MCP tool shape came from the Kotlin v1 implementation** (`hello-spring-ai-bedrock`, `synth/towl/*`, Spring AI + Bedrock, javadoc MCP tools). Its whole tool belt was a capability search returning the guide plus matching operations, validate, and run; every data-fetching call ran inside the interpreter. It also supplied the catalog rules that made TOWL provider-neutral (JSON-Schema-to-type mapping, `string`/`json` for untyped tool output, `readOnlyHint` → `read`, per-node accounting). Its v3 port keeps the three tools' contracts.
- **Two failure loops in that prototype** produced the §2.4 requirements. An agent that received an empty match kept rephrasing until the helper reported *unmatched* queries explicitly. A guide that named an operation the catalog lacked (`llm.summarize`, disabled for the test) sent the agent searching for it; guidance is now generated from the installed catalog and names only what exists.
- **The first live AWS trials** (top-5 largest buckets) lost turns to language gaps rather than agent mistakes — no time arithmetic, aggregators only on records, no top-N, the parameter relaxation stopping at the top level — and then produced a silent wrong answer when `b.BucketRegion.or("us-east-1")` defaulted seven regions. The chain of responses (time functions, scalar-list aggregators, `top`/`bottom`, deep relaxation, envelope accounting of defaults, then removal of the default operator, then predefined `now`) is told in TOWL §16. Two Code Mode-specific findings: S3 returns `BucketRegion` only for the paginated `ListBuckets` request form, so the runner forces a page size for that operation; and the auto-prompt index files S3 API operations under `s3api`, which made them invisible to search until the catalog mapped the command name.
- **Search ranking** was tuned from transcripts: `amplifybackend.ListS3Buckets` outranked `s3.ListBuckets` until a service-name match counted like an operation-name match; "list AWS regions" missed `ec2.DescribeRegions` until read verbs became one class and `aws` a stop word.
- **Subcommand help** originally fell through to the CLI's man-page renderer, which fails without `groff`; the codemode commands now print plain text.
- **Options and API members.** Three Glue operations model a member literally named `Region`; keeping options in a separate record from `args` avoided an aliasing rule.
- **The agent evaluation** (`tests/agent/codemode/eval.py`) was introduced when transcripts showed the guide itself teaching a bug (`.or("")` on names): guidance is part of the conformance surface and is tested like the checker. After the `for`/layout revision, seven of eight tasks validated on the first attempt; the remaining retry causes (a dedented `.flatten()` after a laid-out body, `.empty() == false`, an undeclared `now`) each became a language or guide change.
