# AWS CLI Code Mode — TOWL Profile, Product, and Implementation Specification

Status: draft / request for comment  
Code Mode profile version: `v1`  
Language dependency: [`TOWL v1`](TOWL_SPEC.md)

Code Mode is an AWS CLI feature that validates and executes TOWL documents against the AWS operation catalog. TOWL owns the provider-neutral wire grammar and semantics. This document owns:

- the AWS operation catalog and Code Mode TOWL profile;
- Smithy/botocore-derived types, result subjects, pagination, relationships, and lowering;
- the concrete Code Mode function and aggregator registries;
- `aws codemode schema|validate|explain|run` and plan-source handling;
- AWS credentials, regions, mutation policy, retries, budgets, and result rendering;
- Python implementation types and module boundaries; and
- agent-facing help, discovery, rollout, and AWS integration tests.

If this document conflicts with TOWL core semantics, TOWL controls the language and Code Mode must reject the unsupported profile feature rather than redefine it.

---

## 1. Product problem and goals

### 1.1 Problem

An agent using ordinary AWS CLI tools must repeatedly select one operation, receive its full output, reason over it, and issue the next operation. Long workflows suffer from model round trips, repeated payload cost, context exhaustion, and no single artifact a user can review before effects.

Code Mode changes the interaction:

1. the agent discovers only the AWS schemas it needs;
2. it emits one TOWL document;
3. the CLI validates and explains the complete plan;
4. the user or policy approves it;
5. a deterministic local executor performs all calls without an LLM in the loop; and
6. one compact result returns to the summarizing model.

### 1.2 Functional goals

- One planning turn and one compact result.
- A standalone, reviewable TOWL document.
- AWS operation, argument, response-path, cardinality, and relationship validation from installed models.
- Output-to-input binding and data-driven fan-out.
- Parallel execution of independent reads derived from TOWL references.
- Runtime-owned concurrency, pagination, batching, retries, and hard budgets.
- Sound server-side filter/projection lowering with residual local evaluation.
- Streaming aggregation and loud truncation.
- Structured partial success and per-element errors.
- Multi-region queries as the canonical traversal example.
- Read-only default with explicit mutation and credential-scope gates.
- No CLI-side plan history, replay, cache, or persistence.

### 1.3 Non-goals

- durable or server-side orchestration;
- arbitrary scripts or expression languages;
- hidden AWS calls during validation or explanation;
- general branching, waits, timers, compensation, or human tasks;
- persisted plans, execution history, or result cache;
- endpoint, TLS, signing, retry, credential, filesystem, or process control from a plan.

### 1.4 Users and flow

**Agent author.** Calls `aws codemode help` and `schema`, writes TOWL, repairs validation errors, and summarizes the result.

**Human reviewer.** Reviews `explain`, including operations, regions, credential scopes, mutations, logical work, physical estimates, bounds, and pushdown warnings.

**Automation.** Supplies a plan explicitly, consumes JSON diagnostics/results, and opts into mutations or profile overrides through invocation flags.

### 1.5 Product risks and mitigations

| Risk                                         | Mitigation                                                                     |
|----------------------------------------------|--------------------------------------------------------------------------------|
| model predates Code Mode                     | minimal pointer skill, AWS help topic, optional in-band discovery              |
| installed help and executor drift            | help/schema/functions generated from the installed binary and registries       |
| hallucinated operation or argument           | batched local schema lookup, strict validation, did-you-mean diagnostics       |
| wasted turns on weak keyword queries         | progressive lexical relaxation, vocabulary bridge, never-empty orientation     |
| unsound pushdown changes results             | residual default and differential tests before enabling each mapping           |
| function registry becomes an escape language | closed typed pure descriptors; no user code, I/O, recursion, or hidden context |
| runaway region/resource/page fan-out         | static/symbolic explanation, hard budgets, global and per-endpoint governors   |
| concurrency changes result order             | TOWL input-order traversal results, write-once bindings, monoid property tests |
| throttling from overlapping calls            | adaptive backpressure and botocore retry under one global governor             |
| silent truncation produces wrong answers     | mandatory partial status and structured truncation diagnostics                 |
| unexpected mutation or identity              | read-only default, explicit mutation/profile flags, prominent explain manifest |
| result remains too large for a model         | derived pruning, explicit result shaping, streaming folds, byte/item limits    |
| implementation accumulates hidden state      | no persistence/cache/replay package; caller owns documents and envelopes       |

### 1.6 Success criteria

- Representative multi-operation tasks complete in two model turns: plan and summarize.
- Independent reads approach dependency-critical-path latency rather than serial latency.
- An agent given only the pointer reaches installed help and produces a valid plan without an external grammar.
- Schema discovery for one task fits in one batched invocation; repeated lookup turns are tracked as defects.
- First-attempt validity target is at least 80%, and at least 95% within one repair turn.
- Total model tokens fall by an order of magnitude on large fan-out tasks, including authoring-help cost.
- No validation or explanation path invokes AWS.
- No execution path performs an effect outside the resolved operation manifest and approved policy.
- No truncated or partially failed run can be rendered with `ok` status.

---

## 2. Command surface and plan sources

```text
aws codemode help [<topic>]            # authoring instructions; --output json
aws codemode schema <query>...         # AWS catalog discovery
aws codemode validate --plan <src>     # TOWL + AWS profile + policy validation
aws codemode explain  --plan <src>     # static render; zero AWS calls
aws codemode run      --plan <src>     # revalidate, approve if needed, execute

<src> = inline JSON | file://path | -
```

There is no `--dry-run`; `explain` is the dry run. `run` always repeats the complete parse, TOWL validation, AWS-profile validation, and Code Mode policy checks for its own source.

### 2.1 Source precedence

| Value                           | Meaning                           |
|---------------------------------|-----------------------------------|
| starts with `{` after trimming  | inline JSON                       |
| `file://path` or `fileb://path` | AWS CLI paramfile expansion       |
| `-`                             | explicit stdin                    |
| omitted                         | error: source required            |
| any other value                 | error with source-format guidance |

A plan is never read from ambient stdin unless `--plan -` is explicit.

### 2.2 Preprocessing boundary

Code Mode may strip one leading/trailing Markdown fence, comments, trailing commas, or one obvious single-plan envelope when the repair is mechanically unambiguous. Every repair emits a warning. The resulting value must be strict TOWL JSON; TOWL itself does not contain these tolerances.

### 2.3 Stateless lifecycle

Each command receives a complete source and stores nothing. There is no remembered validation, plan hash, replay command, run history, or hidden plan file. A caller that wants persistence stores the TOWL document and result envelope itself.

---

## 3. Code Mode AWS profile for TOWL

### 3.1 AWS operation identity

TOWL makes `call.service` an optional catalog qualifier. The Code Mode AWS catalog requires it because AWS operation names are not a stable global namespace.

```jsonc
"call": {
  "service": "ec2",
  "operation": "describe-instances"
}
```

Service and operation names accept CLI spelling or API spelling and normalize to the installed model identity. The validated plan stores resolved service and operation models; execution performs no string lookup.

### 3.2 Effective call schema

The AWS profile validates `call.args` against the modeled operation input plus one reserved client selector:

- lowercase `args.region` selects the botocore client/endpoint and is removed before request construction;
- all other members validate against the operation input shape;
- argument names accept exact API spelling or unambiguous CLI-style spelling and normalize to API members;
- response paths always use API-native member spelling; and
- exact `args.Region` remains a modeled API member for the three current Glue metadata operations that declare it.

Reserved lowercase `region` resolves before CLI-style aliases, so both endpoint and API member can be supplied without ambiguity.

Code Mode additionally supports a profile call option `profile`, a literal credential-scope override. It is rejected unless the invocation enables profile overrides. This is an AWS host option, not TOWL core.

### 3.3 AWS result subjects and cardinality

Each operation has generated `OperationResult(cardinality, subjectPath, shape)` metadata:

- singular operations default to `One<OutputShape>`;
- reviewed optional singular subjects produce `Optional<T>`;
- resource-list or paginator metadata may identify `Many<Record>` at a record path; and
- ambiguous list-bearing outputs remain `One<OutputShape>`.

Examples:

```text
sts:GetCallerIdentity  -> One<GetCallerIdentityOutput>
ec2:DescribeInstances -> Many<Instance> at Reservations[].Instances[]
```

This metadata drives source binding, paths, `result`, stream-stage legality, generated result schemas, and explanation.

### 3.4 AWS pagination capability

`paginate` is optional and omission is always valid. Presence requires `Many` plus an installed paginator capability. The profile obtains cursor, limit, result, and page-size members from the AWS CLI paginator configuration. Plan authors never name cursors.

An omitted policy on a paged operation means all pages subject to Code Mode budgets. `maxItems`, `maxPages`, and `pageSize` narrow that policy. Truncation is always reported.

Paginator cursor and modeled page-limit members are runtime-owned. Authors may not set them directly in `args`; validation points to `paginate`. Schema signatures list them under paging metadata rather than authorable arguments.

Modeled idempotency-token and checksum members are authorable ordinary inputs. When absent, botocore may populate them from model traits; when present, the authored value wins and remains stable across retries. Signing timestamps, SDK headers, and transport state are never authorable. Capability generation rejects any filter/projection mapping that targets pagination, idempotency, checksum, signing, or header-owned members.

### 3.5 AWS environment registry

Code Mode registers immutable `env` values including:

- `now`, `nowMillis`, and `today`;
- relative and boundary times such as `ago.d90` and `startOf.day`;
- `runId`;
- effective region; and
- when already available without an undeclared call, account/partition/identity facts.

Pagination cursors, credentials, signing timestamps, retries, endpoint state, process environment, files, and arbitrary clock/random reads are unavailable. Botocore owns modeled idempotency tokens, checksums, signing, and headers.

### 3.6 AWS effects

The profile classifies operations as read or mutating from Smithy traits/resource lifecycle metadata plus reviewed overrides. Unknown classification is unsafe by default. Tool annotations or name prefixes alone never authorize a call as read-only.

---

## 4. Code Mode function and aggregator registries

TOWL defines registry contracts but no functions. Code Mode v1 installs the following closed registries. Per TOWL, `ref`, `input`, `env`, `call`, and `path` are reserved keys and are never registrable names, and the function and aggregator name spaces are disjoint. The generated TOWL schema, `help expressions`, validator, normalizer, evaluator, and lowerer consume the same descriptors. The generated document schema encodes the closed block union as `oneOf` variants with `additionalProperties: false`, so role mixtures fail schema validation before parsing, and registered function/aggregator names are enumerated as the only legal application keys.

### 4.1 Value functions

| Function   | Signature                                 |
|------------|-------------------------------------------|
| `casefold` | string → string                           |
| `age`      | (timestamp, reference timestamp) → number |
| `date`     | string/timestamp → timestamp              |
| `number`   | numeric string/number → number            |
| `size`     | string/list/map → number                  |
| `cidr`     | string → cidr                             |
| `cidrSize` | cidr → number                             |

### 4.2 Relations and Boolean functions

| Family      | Functions                            |
|-------------|--------------------------------------|
| equality    | `eq`, `ne`                           |
| strings     | `startsWith`, `endsWith`, `contains` |
| ordering    | `lt`, `lte`, `gt`, `gte`             |
| existence   | `present`, `absent`                  |
| collections | `setEq`, `setNe`, `subset`           |
| Boolean     | n-ary `and`, n-ary `or`, unary `not` |

Descriptors declare arity, types, complements, associativity, commutativity, and idempotence where applicable. Semantic roles are normative for this registry: `eq` is `relationRole=equality`; `lt`/`lte`/`gt`/`gte` are `relationRole=ordering`; `and` is `booleanRole=conjunction`; `or` is `booleanRole=disjunction`; and `not` is `booleanRole=negation`. No role is inferred from spelling.

Scalar relations over a many-valued path are existential. Literal arrays on the right of equality/string relations are alternatives. `setEq`, `setNe`, and `subset` operate on whole collections.

### 4.3 Normalization and AWS pushdown

Code Mode may apply complement rules, associative flattening, canonical sorting, same-path equality alternatives, and bounded CNF conversion according to registry laws.

A predicate is ordinarily pushable when one argument is a bare source-path node, its relation descriptor has an equivalent operation mapping, and its other arguments are record-independent. Functions around the record-dependent argument remain residual unless an exact AWS mapping exists.

### 4.4 Aggregator registry

| Constructor               | Accumulator                          | Output       |
|---------------------------|--------------------------------------|--------------|
| `count()`                 | sum of ones                          | number       |
| `sum(x)`                  | numeric sum                          | number       |
| `min(x)`, `max(x)`        | semilattice                          | scalar       |
| `collect(x)`              | free monoid                          | ordered list |
| `avg(x)`                  | product `(sum,count)`                | number       |
| `distinctCount(x)`        | set union                            | number       |
| `groupBy(key, aggregate)` | map of nested aggregate accumulators | map          |

Each descriptor supplies `(prepare, monoid, present)` and law metadata. Property tests verify identity, associativity, and commutativity where completion order may vary.

Named product composition (`{name: aggregate, ...}`) is TOWL core semantics, not a Code Mode registry constructor. Code Mode supplies only the leaf/combinator descriptors listed above.

---

## 5. AWS lowering and optimization

### 5.1 Demanded fields

Code Mode walks every TOWL reference in arguments/options, filters, dedup keys, correlation atoms, aggregators, and results. The union is the demanded field set for each binding.

Demanded fields are pruned immediately after decoding and, where AWS supports projection inputs, lowered into the request. Plans never author a separate projection stage.

### 5.2 Server-filter capability table

For each operation, reviewed metadata maps response paths and Code Mode functions to AWS filter/ID parameters with equivalent semantics. Missing entries safely degrade to residual evaluation.

The capability table records:

- response path;
- allowed relation descriptors;
- request member/filter name;
- scalar/list/batch limits;
- case and missing-value semantics; and
- differential fixtures proving equivalence.

When an authored argument and a lowered predicate target the same request member, the lowerer may append only when the member is a list-valued conjunction whose service semantics make appending equivalent to logical AND. Scalar collisions and any unproved merge are validation errors naming both writers; the lowerer never overwrites authored input. Multiple lowered clauses targeting one list member combine only under the mapping's `mergeSemantics=conjunction` and are deduplicated canonically. Multiple writers to a scalar member must normalize to the same value or fail. Every AWS capability entry carries TOWL's encoding, value-limit, case/missing, and merge-semantics fields.

### 5.3 Pseudo-paths

The AWS profile may expose typed pseudo-paths, notably `tag:<name>`, mapping `{Key,Value}` tag lists to one logical value and to provider filter names where sound.

### 5.4 Correlated traversal

TOWL permits direct equality correlation. Code Mode requires call-source correlation to lower to an AWS filter or modeled ID parameter. The lowerer may batch outer keys into multi-value requests and repartition responses into the original logical per-element results. It must preserve tuple semantics and traversal order.

Bound-collection correlation builds a hash index once. O(n×m) repeated scans are forbidden.

### 5.5 Metadata derivation

Four generated datasets back the profile:

1. result cardinality, subject paths, shapes, paginator capability, and modeled list-input element limits (`inputLimits`, e.g. `GetParameters.Names` = 10);
2. filter/projection capability mappings;
3. resource relationships and identifiers; and
4. read/mutation effect classification.

Smithy selectors and service models generate the baseline at build time. Reviewed overrides add or remove entries where models are silent. Differential tests gate every pushdown mapping. Missing filterability stays residual; missing relationships require explicit author input; unknown effects are denied by default.

---

## 6. Agent-facing schema service

The CLI contains no language or embedding model. Schema discovery must therefore be lexical, deterministic, compact, and batchable over the service models already on disk.

```text
aws codemode schema ec2:DescribeInstances
aws codemode schema "running instances" "list volumes" "get caller identity"
aws codemode schema --service ec2 --list
```

### 6.1 Lookup modes

- Exact `service:operation` lookup returns a full signature.
- Keyword lookup behaves like `apropos`, using operation names, service names, documentation terms, acronyms, synonyms, and curated task hints.
- Several queries in one invocation produce one result document.
- An unambiguous hit auto-promotes to its full signature.
- Naming-style or unique typo corrections return the corrected signature with a diagnostic rather than forcing another turn.
- A weak query returns orientation and next queries, never a useless empty result.
- `--queries file://queries.json` or stdin accepts a JSON array of queries.
- Keyword results default to `--limit 4` per query; `--brief` forces summaries and `--full` forces signatures.

Keyword ranking is deterministic: all terms in operation name; all terms in name plus first documentation sentence; all terms after vocabulary expansion; any-term coverage; then bounded fuzzy/acronym matching. Ties prefer reads, paginated list/describe operations, service-name matches, and shorter operation names. Every result reports the matched tier.

### 6.2 Signature format

```text
ec2:DescribeInstances
  effects: read
  result: Many<ec2.Instance> at Reservations[].Instances[]
  paging (runtime-owned): NextToken/MaxResults
  args: Filters, InstanceIds, ...
  fields: InstanceId, InstanceType, State.Name, Placement.AvailabilityZone, Tags, ...
  filterable:
    State.Name eq -> instance-state-name
    tag:<name> eq -> tag:<name>
  relationships:
    VpcId -> ec2:DescribeVpcs.VpcId
    SubnetId -> ec2:DescribeSubnets.SubnetId
```

Compact signatures are preferred to raw model JSON. Defaults are shape depth 2 and at most 40 fields per structure. `--depth <0..5>` changes depth; `--fields <comma-separated paths>` forces up to 100 requested paths while retaining the depth bound. `--full` is equivalent to depth 5 with at most 200 fields per structure and is mutually exclusive with `--brief`, `--depth`, and `--fields`. `--brief` emits one-line operation summaries. Each query has a hard 20,000-character output bound and reports omitted counts.

### 6.3 Validation as schema feedback

Diagnostics include stable pointers, codes, expected/actual types, candidates, and help topics:

```text
error let.inst.source.instance.call.operation
      unknown operation 'ec2:DescribeInstance'; did you mean DescribeInstances?

error let.lookup.source.tool.call.service
      service omitted; operation 'search' is ambiguous
      candidates: docs:search, issues:search

error let.inst.filter.and[0].eq[0].path
      'Encrypted' is not a field of ec2:DescribeInstances records
```

Validation and schema lookup together form the repair channel for the authoring agent.

---

## 7. Validate and explain

### 7.1 Validation pipeline

```text
source preprocessing
  -> strict TOWL parse
  -> TOWL name/scope/registry/type validation
  -> AWS catalog/profile resolution
  -> AWS capability support checks
  -> Code Mode policy checks
  -> ValidatedAwsPlan
```

Invalid TOWL, unsupported AWS profile capability, policy rejection, configuration failure, and execution failure remain distinct diagnostic classes. No stage before execution invokes AWS.

### 7.2 Explain output

`aws codemode explain --plan <src>` reports:

- resolved operations, effect classes, regions, and credential scopes;
- inferred symbol/cardinality/shape table;
- dependency waves and nested traversals;
- logical tasks and physical request estimates;
- pagination and global bounds;
- pushed versus residual predicates;
- batching/repartitioning strategy;
- mutations and required opt-in flags; and
- result-shape and truncation estimates.

Unknown dynamic widths are rendered symbolically rather than guessed. `explain` makes zero AWS calls.

---

## 8. Execution and security policy

### 8.1 Scheduling

TOWL references determine readiness. Independent AWS reads may overlap. Pure bindings run whenever dependencies permit. Every binding declared in an entered block executes even when it is not referenced by the returned `result`; this is why the effect manifest must include unconsumed bindings.

Code Mode adds an AWS policy barrier:

- all independent AWS reads complete before the first mutation;
- mutations order only through data references; and
- AWS read-after-mutation is rejected because sequencing does not guarantee visibility under eventual consistency.

This is Code Mode policy, not a TOWL dependency edge.

### 8.2 Runtime ownership

The executor owns:

- global and per-service/endpoint concurrency;
- adaptive backpressure;
- botocore retries and jitter;
- client creation and thread-safe caching by service, effective region, and credential scope;
- paginator cursors;
- idempotency-token injection;
- cancellation; and
- logical/physical accounting.

Plans cannot set concurrency, retries, endpoints, TLS, signing, CA bundles, or credentials.

### 8.3 Budgets

| Budget                | Default                              | Flag                 |
|-----------------------|--------------------------------------|----------------------|
| physical AWS requests | 500                                  | `--max-calls`        |
| concurrency           | 8, hard ceiling 32                   | `--max-concurrency`  |
| items per binding     | 100,000                              | `--max-items`        |
| result bytes          | 256 KB                               | `--max-result-bytes` |
| wall time             | 300 seconds                          | `--timeout`          |
| expression work       | implementation node/size/time bounds | none                 |

Hitting a budget stops cleanly and produces a loud partial/truncated outcome.

### 8.4 Error handling

| Class         | Examples                                  | Handling                        |
|---------------|-------------------------------------------|---------------------------------|
| invalid       | bad grammar, path, type, function         | reject before effects           |
| unsupported   | missing AWS correlation/paging capability | reject before effects           |
| policy        | mutation or profile override not enabled  | reject before effects           |
| configuration | credentials/region/profile unavailable    | fail before affected call       |
| authorization | AccessDenied, SCP denial                  | TOWL `onError`                  |
| throttling    | throttling/request limit                  | retry, then `onError`           |
| transient     | 5xx, timeout, endpoint failure            | retry, then `onError`           |
| provider      | not found or operation-specific error     | `onError`                       |
| budget        | calls/items/bytes/time                    | partial with diagnostics        |
| cancellation  | dependency failure or Ctrl-C              | structured cancellation/partial |

### 8.5 Capability gates

- Read-only by default; mutations require `--allow-mutations`.
- Per-call `profile` requires `--allow-profile-override`.
- Every mutation, region, and credential scope appears in `explain`.
- Unknown effect classification is treated as mutation-risk.
- No file/process/socket operation exists in the TOWL document.
- `file://` expansion applies only to the outer `--plan` source, not operation arguments inside the plan.

### 8.6 Approval and invocation overrides

Interactive `run` renders the explanation and asks for confirmation when a plan mutates, has an upper request estimate above 50, or spans multiple credential scopes. `--approval-call-threshold <n>` changes the threshold. In a non-interactive process, a required confirmation without `--yes` is `policy.rejected` and performs no AWS calls. `--yes` skips only the prompt; it does not bypass validation or policy gates.

`--on-error strict` changes every TOWL error policy to `fail` for automation. The override is recorded in explanation and result diagnostics.

---

## 9. Concrete result and process interface

Result JSON is written to stdout; progress is written to stderr.

```jsonc
{
  "status": "partial",
  "result": {},
  "errors": [],
  "diagnostics": {
    "truncated": [],
    "warnings": []
  },
  "accounting": {
    "logicalTasks": 17,
    "awsRequests": 4,
    "pages": 4,
    "retries": 1,
    "durationMs": 3900
  },
  "context": {
    "runId": "...",
    "now": "2026-09-05T13:00:00Z",
    "ago.d90": "2026-06-07T13:00:00Z",
    "regions": ["us-east-1"]
  },
  "versions": {
    "towl": "v1",
    "codeModeProfile": "v1",
    "awsCatalog": "...",
    "functionRegistry": "...",
    "aggregatorRegistry": "..."
  }
}
```

Statuses are `ok`, `partial`, or `error`. Any truncation makes status `partial`. Errors identify binding, traversal item when applicable, operation, region, provider code, message, retryability, and phase. The context object echoes every explicit or descriptor-declared `env` value referenced by the validated plan. Catalog and registry fingerprints are mandatory for reproducibility.

Progress lines contain the binding/task name so interleaved parallel work remains readable:

```text
[codemode] plan: 2 bindings, est. 18-52 requests, read-only
[codemode] binding 'regions' -> ec2:DescribeRegions (us-east-1)
[codemode] binding 'inst' traverses 17 item(s) (concurrency=8)
[codemode] binding 'inst' [ap-east-1] !! AuthFailure (collected)
[codemode] done: partial, 21 requests, 3.9s
```

Ctrl-C cancels in-flight work and exits 130 after emitting the best structured partial outcome possible.

Exit codes follow the AWS CLI return-code conventions used by Code Mode:

| Code | Meaning                                                        |
|------|----------------------------------------------------------------|
| 0    | complete `ok` result                                           |
| 1    | useful `partial` result, including truncation                  |
| 130  | interrupted                                                    |
| 252  | invalid TOWL, unsupported AWS profile, or rejected plan policy |
| 253  | invalid configuration, credentials, region, or profile         |
| 254  | AWS service error caused run failure                           |
| 255  | other failure                                                  |

---

## 10. Python implementation design

Python representation is an implementation of TOWL plus the AWS profile; it is not a second language definition.

### 10.1 Parsed types

```text
Plan        = (TowlVersion, Description, Inputs, Block)
Block       = Traverse(ForEach, OnError)
            | Express(Producer, Stages, Result?, OnError)
            | Assemble(Let: Map[Name,Block], Result?)
Map1[N,A]   = Map[N,A] where size = 1
ForEach     = Map1[Name,(From:Expr, Body:Block)]
Producer    = Call | Expr
Call        = (Service?, Operation, Args, AwsCallOptions, Paginate?)
AwsCallOptions = (Profile?)
CallResult  = Cardinality<Shape>
Paging      = NotPaged | Paged(CursorSpec)
Stages      = (Filter:Expr[Boolean]?, Dedup:List[Expr]?)
Expr        = Ref | ImplicitPath | Input | Env | Literal | Apply(ResolvedFunction, Args)
ResolvedFunction = (Descriptor, Signature, ContextDependencies)
Result      = Expr | Apply(ResolvedAggregator, Args) | Record(Map[Name,Result])
Aggregator  = Apply(ResolvedAggregator, Args) | Product(Map[Name,Aggregator])
```

The parser pattern-matches the closed block union directly; there is no role-inference pass, and a member from a foreign role is a parse error naming the matched-nearest variant. The parser constructs only frozen, normalized domain types. `ServiceName`, `OperationName`, `OperationId`, `RegionName`, `Path`, and budget values are distinct wrappers rather than interchangeable strings.

`parse(document, catalogs, registries) -> Either[NonEmpty[Diagnostic], ValidatedAwsPlan]` performs all structural, name, catalog, registry, type, capability, graph, and policy-independent checks. Downstream lower/explain/execute functions accept only the validated type.

### 10.2 Effect boundary

All side effects are behind injected interfaces:

```text
AwsCalls.invoke(resolvedCall) -> PageOrValue
ClockSnapshot                 -> immutable Code Mode env
Policy.authorize(manifest)    -> allow | reject
Progress.emit(event)
```

Parsing, graph construction, expression evaluation, normalization, pruning, lowering, folding, explanation, and result assembly are pure wherever possible.

### 10.3 Modules

```text
awscli/customizations/codemode/
  command.py       command registration and global args
  source.py        --plan source resolution and preprocessing
  towlast.py       TOWL JSON decoding into frozen nodes
  registry.py      Code Mode function/aggregator registries
  aws_catalog.py   Smithy/botocore operation catalog adapter
  types.py         inferred shapes, cardinality, paths
  graph.py         references, cycles, waves, effect manifest
  lower.py         pruning, pushdown, correlation batching/repartitioning
  validate.py      diagnostics and ValidatedAwsPlan construction
  explain.py       human and JSON renderers
  executor.py      scheduler, governors, invoker, streaming folds
  envelope.py      result/error/accounting serialization
```

No persistence module exists.

---

## 11. Test strategy

### 11.1 TOWL consumption tests

Code Mode runs the TOWL conformance suite from `TOWL_SPEC.md`, then profile tests for:

- AWS-required service qualification;
- CLI/API name aliases and typo correction;
- result subject and One/Optional/Many inference;
- singular-stage and pagination capability rejection;
- `args.region` versus exact `args.Region`;
- every registered function signature, explicit context dependency, Boolean/relation role, and declared normalization law;
- every registered aggregator and monoid law;
- scope, no shadowing, depth-2 traversal nesting, eager execution of all declared bindings, DAG waves, traversal outputs under each error policy, and order;
- strict JSON duplicate-key rejection and Code Mode preprocessing warnings.

### 11.2 AWS differential tests

Every filter/projection capability entry has fixtures proving pushed and residual evaluation return equivalent logical results. Correlation batching/repartitioning is compared with per-element logical execution. Missing capability tests prove safe residual or unsupported behavior.

### 11.3 Integration tests

A fake `AwsCalls` covers concurrency, pagination, throttling, retries, cancellation, partial failure, budgets, mutation barriers, profile gates, idempotency defaults, client caching, and progress/result stream separation. Local mock endpoints provide end-to-end tests without production AWS effects.

---

## 12. Authoring help and discovery

### 12.1 `aws codemode help`

| Topic         | Contents                                                           |
|---------------|--------------------------------------------------------------------|
| default       | purpose, commands, minimum plan, topic index                       |
| `plan`        | TOWL blocks, declarations, refs, cardinality, AWS call profile     |
| `expressions` | Code Mode function registry, signatures, laws, and pushdown notes  |
| `combiners`   | Code Mode aggregator registry and streaming requirements           |
| `passing`     | inline, `file://`, stdin, and shell quoting                        |
| `errors`      | diagnostics, `onError`, partial results, truncation, exit codes    |
| `schema`      | lookup syntax, batching, signatures, vocabulary                    |
| `limits`      | budgets, defaults, flags, and policy gates                         |
| `examples`    | single call, DAG, multi-region traversal, correlation, aggregation |

Grammar, function, and aggregator material is generated from the installed TOWL schema and Code Mode registries. AWS signatures are generated from installed profile metadata.

### 12.2 Pointer skill

The external skill only tells older models that Code Mode exists and directs them to `aws codemode help`. It contains no independent grammar that can drift from the binary.

### 12.3 Other discovery

- `aws help topics` entry;
- targeted stderr hints after repeated related commands, if enabled;
- diagnostics linking directly to help topics; and
- machine-readable help carrying CLI, TOWL, profile, and registry versions.

---

## 13. Rollout and open product questions

### 13.1 Rollout

1. Preview: parser, registry, AWS catalog, `validate`, and `explain` for read-only plans.
2. Execution: calls, references, DAG scheduling, pagination, limits, and result envelope.
3. Traversal/aggregation: `forEach`, correlation batching, list-input chunking, aggregate results, partial results.
4. Discovery: schema search, generated help, pointer skill, and agent benchmarks.
5. Mutations and organization policy behind explicit capability flags.

Code Mode should not advertise complete TOWL/profile conformance until every required construct is executable; preview builds expose supported capability labels.

### 13.2 Open questions

- How should organizations distribute reviewed capability/effect overrides?
- Which additional Code Mode functions or aggregators have demonstrated tasks and sound typing?
- How should cross-account credential scopes be represented and reviewed?
- Should `explain` be required before non-interactive `run --yes`?
- Which exit code should represent partial-but-useful output?
- Which inventory backends may safely replace live reads while preserving freshness diagnostics?
- Should a future profile permit typed LLM transductions inside a plan, with a separate nondeterministic budget and visible effect type?

---

## 14. AWS integration conformance fixture

This fixture is exhaustive rather than exemplary. It is a Code Mode AWS-profile test, not the TOWL core conformance document. It exercises AWS catalog resolution, region targeting, paging, filtering/pushdown, expressions, aggregators, traversal, correlation, list-input chunking, partial errors, mutations, and model-driven defaults.

```json
{
  "towl": "v1",
  "description": "Quarterly account hygiene sweep (grammar conformance fixture)",
  "inputs": {
    "env": {
      "type": "string",
      "default": "prod"
    },
    "staleDays": {
      "type": "integer",
      "default": 90
    },
    "logPrefix": {
      "type": "string",
      "default": "/aws/lambda/"
    },
    "paramNames": {
      "type": "array",
      "default": []
    },
    "metadata": {
      "type": "document",
      "default": {
        "eq": [
          1,
          2
        ]
      }
    }
  },
  "let": {
    "identity": {
      "source": {
        "call": {
          "service": "sts",
          "operation": "get-caller-identity"
        }
      },
      "result": {
        "Account": {
          "path": "Account"
        },
        "Arn": {
          "path": "Arn"
        }
      }
    },
    "regions": {
      "source": {
        "call": {
          "service": "ec2",
          "operation": "describe-regions"
        }
      },
      "filter": {
        "eq": [
          {
            "path": "OptInStatus"
          },
          [
            "opt-in-not-required",
            "opted-in"
          ]
        ]
      }
    },
    "insts": {
      "forEach": {
        "region": {
          "from": {
            "ref": "regions",
            "path": "RegionName"
          },
          "source": {
            "call": {
              "service": "ec2",
              "operation": "describe-instances",
              "args": {
                "region": {
                  "ref": "region"
                }
              },
              "paginate": {
                "maxItems": 5000,
                "pageSize": 1000
              }
            }
          },
          "filter": {
            "and": [
              {
                "eq": [
                  {
                    "casefold": [
                      {
                        "path": "tag:Env"
                      }
                    ]
                  },
                  {
                    "casefold": [
                      {
                        "input": "env"
                      }
                    ]
                  }
                ]
              },
              {
                "or": [
                  {
                    "eq": [
                      {
                        "path": "State.Name"
                      },
                      [
                        "running",
                        "stopped"
                      ]
                    ]
                  },
                  {
                    "startsWith": [
                      {
                        "path": "InstanceType"
                      },
                      "t2."
                    ]
                  }
                ]
              },
              {
                "not": [
                  {
                    "present": [
                      {
                        "path": "tag:Ephemeral"
                      }
                    ]
                  }
                ]
              },
              {
                "gt": [
                  {
                    "age": [
                      {
                        "path": "LaunchTime"
                      },
                      {
                        "env": "now"
                      }
                    ]
                  },
                  {
                    "input": "staleDays"
                  }
                ]
              },
              {
                "lt": [
                  {
                    "size": [
                      {
                        "path": "Tags"
                      }
                    ]
                  },
                  10
                ]
              }
            ]
          },
          "result": {
            "region": {
              "ref": "region"
            },
            "byType": {
              "groupBy": [
                {
                  "path": "InstanceType"
                },
                {
                  "count": []
                }
              ]
            }
          }
        }
      },
      "onError": "collect"
    },
    "staleByRegion": {
      "source": {
        "ref": "insts"
      },
      "filter": {
        "present": [
          {
            "path": "ok"
          }
        ]
      },
      "result": {
        "region": {
          "path": "ok.region"
        },
        "byType": {
          "path": "ok.byType"
        }
      }
    },
    "instErrors": {
      "source": {
        "ref": "insts"
      },
      "filter": {
        "present": [
          {
            "path": "error"
          }
        ]
      },
      "result": {
        "region": {
          "path": "error.item"
        },
        "code": {
          "path": "error.code"
        },
        "retryable": {
          "path": "error.retryable"
        }
      }
    },
    "vols": {
      "source": {
        "call": {
          "service": "ec2",
          "operation": "describe-volumes",
          "paginate": {}
        }
      },
      "filter": {
        "and": [
          {
            "eq": [
              {
                "path": "Encrypted"
              },
              false
            ]
          },
          {
            "gte": [
              {
                "path": "Size"
              },
              100
            ]
          },
          {
            "lte": [
              {
                "path": "Size"
              },
              16384
            ]
          },
          {
            "ne": [
              {
                "path": "State"
              },
              [
                "deleting",
                "error"
              ]
            ]
          },
          {
            "subset": [
              {
                "path": "Attachments[].State"
              },
              [
                "attached",
                "attaching"
              ]
            ]
          },
          {
            "or": [
              {
                "setNe": [
                  {
                    "path": "Tags[].Key"
                  },
                  [
                    "Owner"
                  ]
                ]
              },
              {
                "and": [
                  {
                    "subset": [
                      {
                        "path": "Tags[].Key"
                      },
                      [
                        "Owner",
                        "Env",
                        "Team"
                      ]
                    ]
                  },
                  {
                    "not": [
                      {
                        "setEq": [
                          {
                            "path": "Tags[].Key"
                          },
                          [
                            "Owner",
                            "Env",
                            "Team"
                          ]
                        ]
                      }
                    ]
                  }
                ]
              }
            ]
          },
          {
            "endsWith": [
              {
                "path": "SnapshotId"
              },
              "0"
            ]
          },
          {
            "contains": [
              {
                "path": "AvailabilityZone"
              },
              "us-"
            ]
          }
        ]
      },
      "dedup": [
        {
          "path": "VolumeId"
        }
      ]
    },
    "volStats": {
      "source": {
        "ref": "vols"
      },
      "result": {
        "n": {
          "count": []
        },
        "bytes": {
          "sum": [
            {
              "number": [
                {
                  "path": "Size"
                }
              ]
            }
          ]
        },
        "biggest": {
          "max": [
            {
              "path": "Size"
            }
          ]
        },
        "smallest": {
          "min": [
            {
              "path": "Size"
            }
          ]
        },
        "meanSize": {
          "avg": [
            {
              "path": "Size"
            }
          ]
        },
        "zones": {
          "distinctCount": [
            {
              "path": "AvailabilityZone"
            }
          ]
        },
        "ids": {
          "collect": [
            {
              "path": "VolumeId"
            }
          ]
        },
        "byZone": {
          "groupBy": [
            {
              "path": "AvailabilityZone"
            },
            {
              "count": []
            }
          ]
        }
      }
    },
    "volSnap": {
      "forEach": {
        "volume": {
          "from": {
            "ref": "vols"
          },
          "source": {
            "call": {
              "service": "ec2",
              "operation": "describe-snapshots",
              "args": {
                "OwnerIds": [
                  "self"
                ]
              },
              "paginate": {
                "maxItems": 20000
              }
            }
          },
          "filter": {
            "eq": [
              {
                "path": "VolumeId"
              },
              {
                "ref": "volume",
                "path": "VolumeId"
              }
            ]
          },
          "result": {
            "volume": {
              "ref": "volume",
              "path": "VolumeId"
            },
            "size": {
              "ref": "volume",
              "path": "Size"
            },
            "snapshots": {
              "collect": [
                {
                  "path": "SnapshotId"
                }
              ]
            },
            "newest": {
              "max": [
                {
                  "date": [
                    {
                      "path": "StartTime"
                    }
                  ]
                }
              ]
            }
          }
        }
      }
    },
    "volStatus": {
      "source": {
        "call": {
          "service": "ec2",
          "operation": "describe-volume-status",
          "args": {
            "VolumeIds": {
              "ref": "vols",
              "path": "VolumeId"
            }
          }
        }
      },
      "result": {
        "VolumeId": {
          "path": "VolumeId"
        },
        "Status": {
          "path": "VolumeStatus.Status"
        }
      },
      "onError": "skip"
    },
    "logGroups": {
      "source": {
        "call": {
          "service": "logs",
          "operation": "describe-log-groups",
          "args": {
            "logGroupNamePrefix": {
              "input": "logPrefix"
            }
          },
          "paginate": {
            "maxItems": 200,
            "pageSize": 50
          }
        }
      },
      "filter": {
        "and": [
          {
            "gt": [
              {
                "path": "storedBytes"
              },
              0
            ]
          },
          {
            "absent": [
              {
                "path": "retentionInDays"
              }
            ]
          }
        ]
      },
      "result": {
        "logGroupName": {
          "path": "logGroupName"
        },
        "storedBytes": {
          "path": "storedBytes"
        }
      }
    },
    "params": {
      "source": {
        "call": {
          "service": "ssm",
          "operation": "get-parameters",
          "args": {
            "Names": {
              "input": "paramNames"
            },
            "WithDecryption": false
          }
        }
      },
      "result": {
        "Name": {
          "path": "Name"
        },
        "Type": {
          "path": "Type"
        }
      },
      "onError": "skip"
    },
    "perRegionNet": {
      "forEach": {
        "region": {
          "from": {
            "ref": "regions",
            "path": "RegionName"
          },
          "let": {
            "vpcs": {
              "source": {
                "call": {
                  "service": "ec2",
                  "operation": "describe-vpcs",
                  "args": {
                    "region": {
                      "ref": "region"
                    }
                  }
                }
              },
              "filter": {
                "gte": [
                  {
                    "cidrSize": [
                      {
                        "cidr": [
                          {
                            "path": "CidrBlock"
                          }
                        ]
                      }
                    ]
                  },
                  16
                ]
              }
            },
            "subnetsByVpc": {
              "forEach": {
                "vpc": {
                  "from": {
                    "ref": "vpcs"
                  },
                  "source": {
                    "call": {
                      "service": "ec2",
                      "operation": "describe-subnets",
                      "args": {
                        "region": {
                          "ref": "region"
                        }
                      }
                    }
                  },
                  "filter": {
                    "eq": [
                      {
                        "path": "VpcId"
                      },
                      {
                        "ref": "vpc",
                        "path": "VpcId"
                      }
                    ]
                  },
                  "result": {
                    "vpc": {
                      "ref": "vpc",
                      "path": "VpcId"
                    },
                    "cidr": {
                      "ref": "vpc",
                      "path": "CidrBlock"
                    },
                    "subnet": {
                      "path": "SubnetId"
                    }
                  }
                }
              }
            }
          },
          "result": {
            "region": {
              "ref": "region"
            },
            "subnets": {
              "ref": "subnetsByVpc"
            }
          }
        }
      }
    },
    "tagVols": {
      "forEach": {
        "volId": {
          "from": {
            "ref": "vols",
            "path": "VolumeId"
          },
          "source": {
            "call": {
              "service": "ec2",
              "operation": "create-tags",
              "args": {
                "Resources": [
                  {
                    "ref": "volId"
                  }
                ],
                "Tags": [
                  {
                    "Key": "HygieneReview",
                    "Value": {
                      "env": "today"
                    }
                  }
                ]
              }
            }
          },
          "result": {
            "volId": {
              "ref": "volId"
            }
          }
        }
      },
      "onError": "fail"
    },
    "taggedOk": {
      "source": {
        "ref": "tagVols"
      },
      "result": {
        "volId": {
          "path": "volId"
        }
      }
    },
    "runbook": {
      "forEach": {
        "taggedVol": {
          "from": {
            "ref": "taggedOk",
            "path": "volId"
          },
          "source": {
            "call": {
              "service": "ssm",
              "operation": "start-automation-execution",
              "args": {
                "DocumentName": "AWS-CreateSnapshot",
                "Parameters": {
                  "VolumeId": [
                    {
                      "ref": "taggedVol"
                    }
                  ]
                }
              }
            }
          }
        }
      },
      "onError": "fail"
    }
  },
  "result": {
    "runId": {
      "env": "runId"
    },
    "identity": {
      "ref": "identity"
    },
    "asOf": {
      "env": "now"
    },
    "window": {
      "env": "ago",
      "path": "d90"
    },
    "staleInstances": {
      "ref": "staleByRegion"
    },
    "regionErrors": {
      "ref": "instErrors"
    },
    "volumes": {
      "ref": "volStats"
    },
    "volumeSnapshots": {
      "ref": "volSnap"
    },
    "volumeStatus": {
      "ref": "volStatus"
    },
    "logGroups": {
      "ref": "logGroups"
    },
    "parameters": {
      "ref": "params"
    },
    "network": {
      "ref": "perRegionNet"
    },
    "metadata": {
      "input": "metadata"
    }
  }
}
```

### 14.1 Coverage expectations

- 15 top-level `let` bindings.
- Every Code Mode v1 function and aggregator constructor appears at least once.
- `identity` exercises a singular `One` call.
- `regions` and regional calls exercise `args.region`.
- `insts`, `vols`, and `logGroups` exercise pagination bounds.
- `perRegionNet` and `volSnap` exercise nested/correlated traversal.
- `vols` mixes pushable and residual predicates.
- `volStatus` and `params` exercise `Many`-to-`List` materialization with catalog `inputLimits` chunking (`DescribeVolumeStatus.VolumeIds`, `GetParameters.Names`); nothing is batched by the author.
- `insts` feeds `staleByRegion`/`instErrors` through tagged `ok`/`error` collect elements; `fail`/`skip` traversals are consumed unwrapped.
- source elements are implicit: bare `path` nodes address the current element, while `ref` appears only for `let` values and traversal variables.
- `volSnap` derives the newest snapshot through an explicit `max` aggregate leaf beside a `collect`; no positional index paths exist.
- `volStats` exercises product and grouped aggregate results; no `fold` keyword or `do` wrapper exists.
- `tagVols` and `runbook` exercise mutation policy and data-derived ordering as stage-free Express bodies: mutation results are unnamed, and `tagVols` maps its `result` from the traversal variable alone.
- `onError` covers fail, skip, and collect.
- Top-level result assembly covers env, inputs, singular values, streams, folds, and tagged collect elements.

The fixture requires generated coverage instrumentation so registry or grammar changes fail CI rather than leaving stale prose.

---

## 15. AWS product alternatives and decisions

### 15.1 Why not other AWS approaches

| Alternative                        | Why it does not replace Code Mode                                                |
|------------------------------------|----------------------------------------------------------------------------------|
| shell/Python/SDK script            | arbitrary authority and source-code review burden                                |
| `--query` plus pipes               | shapes one response; no typed operation graph, fan-out, or cross-call errors     |
| many-small-tools MCP loop          | still one model turn per tool and repeated intermediate payloads                 |
| aliases/wizards/prebuilt workflows | safe but cannot cover open-ended composition                                     |
| Step Functions / ASL               | deployed durable state machines, IAM roles, explicit control/concurrency         |
| SSM Automation                     | managed sequential runbooks, scripts, authored JSONPath outputs                  |
| Cloud Custodian                    | excellent single-resource policy/filter/action model, not arbitrary call DAG     |
| Terraform                          | desired-state lifecycle and required state, not one-shot query results           |
| BPMN                               | business-process lifecycle and diagram interchange, not typed AWS query lowering |

Language-family comparisons such as Arazzo, CWL, and Open Workflow DSL are in the informative rationale of `TOWL_SPEC.md`.

### 15.2 AWS-specific decisions and learnings

- Region targeting is `args.region`, consumed by the AWS client layer; exact `args.Region` remains available to modeled Glue inputs.
- AWS requires `service` qualification even though TOWL permits omission.
- Pagination is optional and allowed only for generated paginator capability.
- Result cardinality comes from generated subject metadata rather than assuming every call is a stream.
- Missing pushdown metadata stays residual; missing correlation support is rejected rather than repeated full scans.
- Read-before-mutate and no-read-after-mutate are Code Mode policy, not TOWL scheduling semantics.
- Botocore supplies modeled idempotency tokens per logical task and reuses them across retries.
- Modeled list-member limits surface as catalog `inputLimits` driving processor-side chunking; authored `batch` was removed from the language.
- Markdown-fence tolerance belongs to source preprocessing, not the TOWL grammar.
- Prototype experience showed that runtime-owned concurrency, correlation repartitioning, compact results, and one short grammar/example materially improve agent reliability.

---

End of AWS CLI Code Mode v1 draft.
