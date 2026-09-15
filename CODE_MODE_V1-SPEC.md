# AWS CLI Code Mode — TOWL Profile, Product, and Implementation Specification

Status: archived v1 profile; describes the current Python prototype  
Code Mode profile version: `v1`  

> This document is frozen. The active Code Mode specification is [`CODE_MODE-SPEC.md`](CODE_MODE-SPEC.md), which targets TOWL v3. The Python implementation under `awscli/customizations/codemode/` was rewritten to v3; the v1 prototype described here no longer exists in the tree.
Language dependency: archived [`TOWL v1`](TOWL_V1_SPEC.md)

Code Mode is an AWS CLI feature that validates and executes TOWL documents against the AWS operation catalog. This profile and the current Python prototype remain pinned to TOWL v1; [`TOWL_SPEC.md`](TOWL_SPEC.md) defines the breaking IR-first TOWL v2 calculus. Code Mode MUST reject v2 plans until a new profile/registry/implementation migrates atomically under the v2 migration contract. TOWL owns the provider-neutral wire grammar and semantics. This document owns:

- the AWS operation catalog and Code Mode TOWL profile;
- Smithy/botocore-derived types, result subjects, pagination, relationships, and lowering;
- the concrete Code Mode function and aggregator registries;
- `aws codemode operation search|schema|validate|run` and plan-source handling;
- AWS credentials, regions, mutation policy, retries, budgets, and result rendering;
- Python implementation types and module boundaries; and
- agent-facing help, discovery, rollout, and AWS integration tests.

If this document conflicts with the archived TOWL v1 contract it depends on, `TOWL_V1_SPEC.md` controls and Code Mode must reject the unsupported profile feature rather than redefine it. TOWL v2 does not govern this v1 profile until an explicit migration.

---

## 1. Product problem and goals

### 1.1 Problem

An agent using ordinary AWS CLI tools must repeatedly select one operation, receive its full output, reason over it, and issue the next operation. Long workflows suffer from model round trips, repeated payload cost, context exhaustion, and no single artifact a user can review before effects.

Code Mode changes the interaction:

1. the agent discovers only the AWS schemas it needs;
2. it emits one TOWL document;
3. the CLI validates the complete plan and returns its static review report;
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
- hidden AWS calls during validation;
- general branching, waits, timers, compensation, or human tasks;
- persisted plans, execution history, or result cache;
- endpoint, TLS, signing, retry, credential, filesystem, or process control from a plan.

### 1.4 Users and flow

**Agent author.** Calls `aws codemode help` once, batch-searches `schema`, writes TOWL, repairs validation errors, reviews the validation report, and summarizes the result.

**Human reviewer.** Reviews the `validate` report, including operations, regions, credential scopes, mutations, logical work, physical estimates, bounds, and pushdown warnings.

**Automation.** Supplies a plan explicitly, consumes JSON diagnostics/results, and opts into mutations or profile overrides through invocation flags.

### 1.5 Product risks and mitigations

| Risk                                             | Mitigation                                                                          |
|--------------------------------------------------|-------------------------------------------------------------------------------------|
| model predates Code Mode                         | minimal pointer skill, AWS help topic, optional in-band discovery                   |
| installed help and executor drift                | help/schema/functions generated from the installed binary and registries            |
| hallucinated operation or argument               | batched local schema lookup, strict validation, did-you-mean diagnostics            |
| wasted turns on weak keyword queries             | progressive lexical relaxation, vocabulary bridge, never-empty orientation          |
| unsound pushdown changes results                 | residual default and differential tests before enabling each mapping                |
| function registry becomes an escape language     | closed typed pure descriptors; no user code, I/O, recursion, or hidden context      |
| saved plan silently changes under a new registry | mandatory exact `{name,version}` selector; immutable retained versions; no fallback |
| runaway region/resource/page fan-out             | static/symbolic validation report, hard budgets, global and per-endpoint governors  |
| concurrency changes result order                 | TOWL input-order traversal results, write-once bindings, monoid property tests      |
| throttling from overlapping calls                | adaptive backpressure and botocore retry under one global governor                  |
| silent truncation produces wrong answers         | mandatory partial status and structured truncation diagnostics                      |
| unexpected mutation or identity                  | read-only default, explicit mutation/profile flags, prominent validation manifest   |
| result remains too large for a model             | derived pruning, explicit result shaping, streaming folds, byte/item limits         |
| implementation accumulates hidden state          | no persistence/cache/replay package; caller owns documents and envelopes            |

### 1.6 Success criteria

- Representative multi-operation tasks complete in two model turns: plan and summarize.
- Independent reads approach dependency-critical-path latency rather than serial latency.
- An agent given only the pointer reaches installed help and produces a valid plan without an external grammar.
- Operation discovery fits in one batched search invocation, followed by one exact multi-operation schema retrieval; exploratory schema turns are tracked as defects.
- First-attempt validity target is at least 80%, and at least 95% within one repair turn.
- Total model tokens fall by an order of magnitude on large fan-out tasks, including authoring-help cost.
- Validation invokes no AWS operations.
- No execution path performs an effect outside the resolved operation manifest and approved policy.
- No truncated or partially failed run can be rendered with `ok` status.

---

## 2. Command surface and plan sources

```text
aws codemode help                       # complete renderer-free plain-text authoring guide
aws codemode operation search <query>... [--limit n]  # ranked descriptions
aws codemode schema <service:operation>...                # exact complete schemas
aws codemode validate --plan <src>     # validation + complete static review report; zero AWS calls
aws codemode run      --plan <src>     # revalidate, approve if needed, execute

<src> = inline JSON | file://path | -
```

There is no separate `explain` or `--dry-run` phase. `validate` is the sole static review command and returns the complete resolved report. `run` always repeats the complete parse, TOWL validation, AWS-profile validation, and Code Mode policy checks for its own source.

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

Each command receives a complete source and stores nothing. There is no remembered validation, plan hash, replay command, run history, or hidden plan file. A caller that wants persistence stores the TOWL document and result envelope itself. The document's mandatory registry requirement makes that saved artifact independent of whichever registry version later becomes the default; it runs only while the exact required version remains installed.

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

This metadata drives source binding, paths, `result`, stream-stage legality, generated result schemas, and the validation report.

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

TOWL defines registry contracts but no functions. Code Mode publishes the language-environment family `aws-code-mode`; this profile defines exact registry version `v1`. Every Code Mode plan therefore contains:

```jsonc
"registry": {"name": "aws-code-mode", "version": "v1"}
```

The installed registry resolver MAY retain several immutable Code Mode versions concurrently. It resolves the authored pair exactly before registry-dependent parsing; it never substitutes the current version. Reusing one `(name, version)` with a different semantic content fingerprint is an installation-integrity error. Deprecation advisories MAY be updated independently; they never alter signatures, laws, or evaluators and carry a separate advisory fingerprint. If the exact version is unavailable, validation fails with code `registry.versionUnavailable`, the authored requirement path, and available installed versions. `schema`, generated plan schemas, and authoring help always report the selector they describe.

Code Mode v1 installs the following closed function and aggregator registries as one `LanguageEnvironment`. Per TOWL, `ref`, `input`, `env`, `call`, and `path` are reserved keys and are never registrable names, and the function and aggregator name spaces are disjoint. The generated TOWL schema, `help expressions`, validator, normalizer, evaluator, and lowerer consume the same selected descriptors. The generated document schema encodes the closed block union as `oneOf` variants with `additionalProperties: false`, requires the exact registry selector, and enumerates only that version's legal application keys.

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

Descriptors declare arity, types, complements, associativity, commutativity, idempotence, and optional TOWL `Deprecation` metadata. Semantic roles are normative for this registry: `eq` is `relationRole=equality`; `lt`/`lte`/`gt`/`gte` are `relationRole=ordering`; `and` is `booleanRole=conjunction`; `or` is `booleanRole=disjunction`; and `not` is `booleanRole=negation`. No role is inferred from spelling. Code Mode v1 currently deprecates none of these functions. Future signatures or laws require a new immutable registry version; later migration guidance may update only the non-semantic advisory snapshot and its advisory fingerprint.

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
| `collect(x)`              | free monoid; preserves elements      | ordered list |
| `avg(x)`                  | product `(sum,count)`                | number       |
| `distinctCount(x)`        | set union                            | number       |
| `groupBy(key, aggregate)` | map of nested aggregate accumulators | map          |

Each descriptor supplies `(prepare, monoid, present)`, law metadata, and optional structured deprecation metadata. Property tests verify identity, associativity, and commutativity where completion order may vary. Code Mode v1 currently deprecates none of these constructors.

Named product composition (`{name: aggregate, ...}`) is TOWL core semantics, not a Code Mode registry constructor. Code Mode supplies only the leaf/combinator descriptors listed above. `collect(x)` adds one list layer and never flattens a list-valued `x`; validation emits `aggregator.collectNested` when that easily-misread shape occurs.

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

## 6. Progressive operation and schema discovery

The CLI contains no language or embedding model. Discovery is lexical, deterministic, and explicitly two-step so an agent does not request full schemas before it knows which operations are relevant.

```text
# Step 1: ranked operation descriptions; batch every capability query
aws codemode operation search "ec2 regions" "ec2 running instances" "caller identity" --limit 4

# Step 2: complete schemas for exact selected identifiers
aws codemode schema ec2:describe-regions ec2:describe-instances sts:get-caller-identity
```

### 6.1 Operation search

`aws codemode operation search <query>... [--limit n]` is the only keyword-discovery interface.

- Queries describe capabilities—service/resource plus verb—not task-specific resource values.
- Several queries in one invocation produce one plain-text response with explicit query boundaries.
- Results contain ranked exact identifiers, first operation descriptions, effects, result cardinality/subject, paging presence, and match tier/score; they contain no argument or field schema trees.
- `--limit` is the only search-specific flag and defaults to 4 per query.
- Ranking is deterministic: all terms in operation name; all terms in name plus first documentation sentence; vocabulary expansion; any-term coverage; then bounded fuzzy/acronym matching. Ties prefer reads, paginated list/describe operations, service-name matches, and shorter names.
- Weak queries return orientation and alternative descriptions, never an implicit full-schema promotion.
- Plain text is the default; explicit global `--output json` returns the same search records structurally.

The agent reads descriptions, chooses the operations whose semantics fit the task, and proceeds once to exact schema retrieval. It does not repeatedly fetch schemas while still exploring names.

### 6.2 Exact schema retrieval

`aws codemode schema <service:operation>...` accepts one or more exact identifiers selected from operation search.

- Every identifier MUST contain the service qualifier. AWS schema lookup never guesses global operation identity.
- Resolution is exact after ordinary AWS CLI/API spelling normalization. There is no keyword fallback, automatic promotion, match limit, or typo execution.
- Unknown or malformed identifiers fail with a diagnostic directing the agent back to `operation search`.
- Each selected operation returns its complete depth-5 input and logical-result schema, required members, effects, One/Optional/Many cardinality, subject path, paging metadata, and selected registry identity/deprecation state.
- There are no schema-specific `--brief`, `--full`, `--depth`, `--fields`, `--queries`, `--service`, or `--list` flags. If one exact response is too large, the agent requests fewer exact operations in separate calls; Code Mode never reduces content silently.
- Plain text is the default; explicit global `--output json` returns the same lossless schema data structurally.

### 6.3 Concise lossless signature format

```text
ec2:describe-instances
  effects=read result=Many at Reservations[].Instances[] paged=true
  paging: input=NextToken output=NextToken pageSize=MaxResults result=Reservations
  args:
    InstanceIds?: list
      item: string
  fields:
    InstanceId?: string
    InstanceType?: string<InstanceType> enum ref InstanceType-values-...
    State?: structure
      Name?: string<InstanceStateName> enum[pending | running | ...]

  definitions (...):
    Filter-...:
      ...
  enum definitions (...):
    InstanceType-values-...: a1.medium | a1.large | ...
```

The renderer uses compact type notation, `!`/`?` required markers, complete enum values, full normalized descriptions, and indentation. Repeated exact shape subtrees across selected operations are replaced by deterministic `ref <definition>` entries and one trailing `definitions` lookup; repeated exact enum sets independently use `enum ref <definition>` and `enum definitions`. Explicit JSON uses `$ref`/`enumRef` plus `definitions`/`enumDefinitions`. Generic modeled names that only repeat the wire type render once (`string`, `integer`, `boolean`) rather than as `string<String>`; meaningful domain names remain. Expanding references reconstructs the original schema exactly.

No content is clipped: there is no character bound, property cap, enum cap, description truncation, or fallback compaction. The only reductions are explicit operation selection in step 2 and shared-definition normalization, which is lossless.

### 6.4 Validation as schema feedback

Diagnostics include stable pointers, codes, expected/actual types, candidates, and help topics:

```text
error let.inst.source.instance.call.operation
      unknown operation 'ec2:DescribeInstance'; did you mean DescribeInstances?

error let.lookup.source.tool.call.service
      service omitted; operation 'search' is ambiguous
      candidates: docs:search, issues:search

error let.inst.filter.and[0].eq[0].path
      'Encrypted' is not a field of ec2:DescribeInstances records

warning registry.version deprecated [registry.deprecated]
        aws-code-mode:v1 is deprecated since v2; removal v4; migrate to aws-code-mode:v3

warning let.inst.filter.oldPredicate [function.deprecated]
        oldPredicate is deprecated since registry v2; replacement: newPredicate
```

Validation and schema lookup together form the repair channel for the authoring agent. Deprecation warnings are structured `{code,severity,path,since,message,replacement?,removal?}` records. Code Mode emits one warning for the selected registry and one per referenced deprecated descriptor, deduplicated across repeated uses; the same records appear in the validation report and run output.

---

## 7. Validation and static review

### 7.1 Validation pipeline

```text
source preprocessing
  -> strict envelope parse and exact registry requirement resolution
  -> registry-dependent TOWL parse
  -> TOWL name/scope/type/deprecation validation
  -> AWS catalog/profile resolution
  -> AWS capability support checks
  -> Code Mode policy checks
  -> ValidatedAwsPlan
  -> ValidationReport
```

Invalid TOWL, unsupported AWS profile capability, policy rejection, configuration failure, and execution failure remain distinct diagnostic classes. Validation invokes no AWS operations.

### 7.2 Validation report

`aws codemode validate --plan <src>` returns one JSON static-review report containing:

- `valid` and all errors, warnings, and deprecations;
- authored and resolved registry name, version, semantic/advisory fingerprints;
- resolved operations, effect classes, regions, and credential scopes;
- inferred symbol/cardinality/shape table;
- dependency waves and nested traversals;
- logical tasks and physical request estimates;
- pagination and global bounds;
- pushed versus residual predicates;
- batching/repartitioning strategy;
- mutations and required opt-in flags; and
- result-shape and truncation estimates.

Unknown dynamic widths are rendered symbolically rather than guessed. Valid-but-surprising collection shapes produce warnings: `traversal.nestedMany` for a Many-valued traversal body (`Many<List<T>>`) and `aggregator.collectNested` when `collect` adds a layer around list-valued elements. Invalid plans return `valid:false` with every safely available diagnostic/context field; valid plans return the complete report that a reviewer and `run` approval prompt consume. There is no separate public static-analysis phase or command.

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
- Every mutation, region, and credential scope appears in the validation report.
- Unknown effect classification is treated as mutation-risk.
- No file/process/socket operation exists in the TOWL document.
- `file://` expansion applies only to the outer `--plan` source, not operation arguments inside the plan.

### 8.6 Approval and invocation overrides

Interactive `run` renders the same validation report and asks for confirmation when a plan mutates, has an upper request estimate above 50, or spans multiple credential scopes. `--approval-call-threshold <n>` changes the threshold. In a non-interactive process, a required confirmation without `--yes` is `policy.rejected` and performs no AWS calls. `--yes` skips only the prompt; it does not bypass validation or policy gates.

`--on-error strict` changes every TOWL error policy to `fail` for automation. The override is recorded in the validation report and result diagnostics.

---

## 9. Concrete result and process interface

Result JSON is written to stdout; progress is written to stderr.

```jsonc
{
  "status": "partial",
  "result": {},
  "errors": [],
  "truncations": [],
  "diagnostics": [
    {
      "code": "function.deprecated",
      "severity": "warning",
      "path": "let.example.filter.oldPredicate",
      "since": "v2",
      "message": "Use newPredicate",
      "replacement": "newPredicate",
      "removal": "v4"
    }
  ],
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
    "languageRegistry": {
      "name": "aws-code-mode",
      "version": "v1",
      "fingerprint": "sha256:...",
      "advisoryFingerprint": "sha256:..."
    }
  }
}
```

Statuses are `ok`, `partial`, or `error`. Any truncation makes status `partial`. Errors identify binding, traversal item when applicable, operation, region, provider code, message, retryability, and phase. The context object echoes every explicit or descriptor-declared `env` value referenced by the validated plan. Catalog and resolved language-registry fingerprints are mandatory for reproducibility. The flat `diagnostics` array includes every registry/function/aggregator deprecation found during the run's mandatory revalidation, including successful `ok` runs; truncations remain a separate array because they force `partial` status.

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
RegistryRequirement = (RegistryName, RegistryVersion)
Plan        = (TowlVersion, RegistryRequirement, Description, Inputs, Block)
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
ResolvedRegistry = (LanguageEnvironment, RegistryIdentity, DeprecationDiagnostics)
ResolvedFunction = (Descriptor, Signature, ContextDependencies, Deprecation?)
Result      = Expr | Apply(ResolvedAggregator, Args) | Record(Map[Name,Result])
Aggregator  = Apply(ResolvedAggregator, Args) | Product(Map[Name,Aggregator])
ResolvedAggregator = (Descriptor, Signature, Deprecation?)
```

The parser first reads the version/registry envelope, resolves the exact installed `LanguageEnvironment`, then pattern-matches the closed block union using that version's names. There is no role-inference pass, and a member from a foreign role is a parse error naming the matched-nearest variant. The parser constructs only frozen, normalized domain types. `ServiceName`, `OperationName`, `OperationId`, `RegionName`, `Path`, and budget values are distinct wrappers rather than interchangeable strings.

`parse(document, registryResolver) -> Either[NonEmpty[Diagnostic], ParsedAwsPlan]` performs exact registry selection and structural decoding. `validate(parsedPlan, catalogs, policy) -> ValidationReport | ValidatedAwsPlan` performs name, catalog, type, capability, deprecation, graph, and policy checks; it returns the report publicly and retains the validated type internally for `run`. Downstream lower/report/execute functions accept only the validated type.

### 10.2 Effect boundary

All side effects are behind injected interfaces:

```text
AwsCalls.invoke(resolvedCall) -> PageOrValue
ClockSnapshot                 -> immutable Code Mode env
Policy.authorize(manifest)    -> allow | reject
Progress.emit(event)
```

Parsing, graph construction, expression evaluation, normalization, pruning, lowering, folding, validation-report rendering, and result assembly are pure wherever possible.

### 10.3 Modules

```text
awscli/customizations/codemode/
  command.py       command registration and global args
  source.py        --plan source resolution and preprocessing
  towlast.py       TOWL JSON decoding into frozen nodes
  registry.py      immutable versioned environments, resolver, descriptors, and deprecations
  aws_catalog.py   Smithy/botocore operation catalog adapter
  types.py         inferred shapes, cardinality, paths
  graph.py         references, cycles, waves, effect manifest
  lower.py         pruning, pushdown, correlation batching/repartitioning
  validate.py      diagnostics and ValidatedAwsPlan construction
  explain.py       internal validation-report renderers (no public command)
  executor.py      scheduler, governors, invoker, streaming folds
  envelope.py      result/error/accounting serialization
```

No persistence module exists.

---

## 11. Test strategy

### 11.1 TOWL consumption tests

Code Mode runs the archived TOWL v1 conformance suite from `TOWL_V1_SPEC.md`, then profile tests for the unified validation report and:

- AWS-required service qualification;
- CLI/API name aliases and typo correction;
- result subject and One/Optional/Many inference;
- singular-stage and pagination capability rejection;
- `args.region` versus exact `args.Region`;
- exact `aws-code-mode` registry selection with old/new versions installed, unavailable-version failure, immutable semantic fingerprints, independently versioned advisory fingerprints, and no fallback;
- every registered function signature, explicit context dependency, Boolean/relation role, declared normalization law, and deprecation record;
- every registered aggregator, monoid law, and deprecation record;
- deduplicated registry/function/aggregator deprecation diagnostics in the validation report and run;
- scope, no shadowing, depth-2 traversal nesting, eager execution of all declared bindings, DAG waves, traversal outputs under each error policy, and order;
- strict JSON duplicate-key rejection and Code Mode preprocessing warnings;
- valid and invalid `validate` reports contain the complete Section 7 review surface;
- nested traversal/list-producing aggregation warnings and the canonical multi-region help plan;
- no public `explain` subcommand or `--dry-run` argument exists.

### 11.2 AWS differential tests

Every filter/projection capability entry has fixtures proving pushed and residual evaluation return equivalent logical results. Correlation batching/repartitioning is compared with per-element logical execution. Missing capability tests prove safe residual or unsupported behavior.

### 11.3 Integration tests

A fake `AwsCalls` covers concurrency, pagination, throttling, retries, cancellation, partial failure, budgets, mutation barriers, profile gates, idempotency defaults, client caching, and progress/result stream separation. Local mock endpoints provide end-to-end tests without production AWS effects.

---

## 12. Authoring help and discovery

### 12.1 `aws codemode help`

`aws codemode help` is the single agent bootstrap. It writes basic plain text directly to stdout, bypassing the AWS CLI man-page/groff renderer, and includes all information needed before task-specific schema lookup:

- purpose, safety model, statelessness, and the four-step authoring/review/run workflow;
- complete command surface and plan-source syntax;
- the progressive `operation search` → exact `schema` workflow, plain-text/lossless JSON modes, shared shape/enum definition lookups, concise scalar names, and no-clipping policy;
- selected and installed registry versions, semantic/advisory fingerprints, and deprecation behavior;
- complete Plan/Assemble/Express/Traverse grammar, references, applications, paths, AWS call rules, pagination, and errors;
- every selected function and aggregator with arity, semantic roles/laws, and deprecation guidance;
- validation-report/result semantics, a minimum plan, and a validated canonical multi-region pattern whose Assemble body returns one `{region, instances:list}` record per outer region without accidental list nesting.

The text is generated from the installed TOWL schema and exactly selected Code Mode registry, so it cannot drift from validation. It is one complete document, not a topic index, and is not JSON. `help-topics` does not exist. AWS operations are discovered with one batched `operation search`; complete signatures are then fetched once by exact identifier with `schema`.

### 12.2 Pointer skill

The external skill only tells older models that Code Mode exists and directs them to `aws codemode help`. It contains no independent grammar that can drift from the binary.

### 12.3 Other discovery

- `aws help topics` entry;
- targeted stderr hints after repeated related commands, if enabled;
- diagnostics linking to the relevant heading in `aws codemode help`; and
- help text carrying CLI, TOWL, profile, available registry versions, selected fingerprints, and deprecation records.

---

## 13. Rollout and open product questions

### 13.1 Rollout

1. Preview: parser, registry, AWS catalog, and full-report `validate` for read-only plans.
2. Execution: calls, references, DAG scheduling, pagination, limits, and result envelope.
3. Traversal/aggregation: `forEach`, correlation batching, list-input chunking, aggregate results, partial results.
4. Discovery: schema search, generated help, pointer skill, and agent benchmarks.
5. Mutations and organization policy behind explicit capability flags.

Code Mode should not advertise complete TOWL/profile conformance until every required construct is executable; preview builds expose supported capability labels. The current profile advertises only TOWL v1. A future TOWL v2 Code Mode profile requires a distinct profile/registry version, v1-to-v2 migration fixtures, regenerated help/schema, and implementation conformance; it never reuses `aws-code-mode:v1` with v2 semantics.

### 13.2 Open questions

- How should organizations distribute reviewed capability/effect overrides?
- Which additional Code Mode functions or aggregators have demonstrated tasks and sound typing?
- How should cross-account credential scopes be represented and reviewed?
- Which exit code should represent partial-but-useful output?
- Which inventory backends may safely replace live reads while preserving freshness diagnostics?
- Should a future profile permit typed LLM transductions inside a plan, with a separate nondeterministic budget and visible effect type?

---

## 14. AWS integration conformance fixture

This fixture is exhaustive rather than exemplary. It is a Code Mode AWS-profile test, not the TOWL core conformance document. It exercises AWS catalog resolution, region targeting, paging, filtering/pushdown, expressions, aggregators, traversal, correlation, list-input chunking, partial errors, mutations, and model-driven defaults.

```json
{
  "towl": "v1",
  "registry": {"name": "aws-code-mode", "version": "v1"},
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

- exact registry requirement `{name:"aws-code-mode",version:"v1"}`.
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

Language-family comparisons such as Arazzo, CWL, and Open Workflow DSL are in the informative rationale of `TOWL_SPEC.md`; v1-specific rationale remains in `TOWL_V1_SPEC.md`.

### 15.2 AWS-specific decisions and learnings

- Plans select exact immutable `aws-code-mode` registry versions so saved artifacts keep their vocabulary when newer versions are installed; semantic fingerprints verify behavior after selection, while separately fingerprinted advisory snapshots may evolve to guide migration without fallback or rewriting.
- The current Code Mode profile is explicitly pinned to archived TOWL v1; TOWL v2 is a breaking IR migration, not an in-place grammar reinterpretation.
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
