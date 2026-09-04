# Code Mode: agent-authored, single-turn command workflows

Status: draft / request for comment
Owner: AWS CLI
Scope: `aws` CLI v2 (`awscli/customizations/codemode/`), plus a minimal discovery skill

---

# Part I — Solution-agnostic

## 1. Problem

An AI agent that answers a question about a user's AWS environment usually needs many API calls,
and today each call costs a full LLM turn: the model emits one command, the harness runs it, the
full response is appended to the context, and the model is invoked again. A question like
_"which running EC2 instances are tagged `Env=prod`, grouped by region and instance type?"_ becomes
1 + N turns (one per region), each carrying kilobytes of `describe-instances` JSON into context.

This produces four compounding problems:

1. **Latency.** Serial LLM round-trips dominate wall-clock time even though the underlying API calls
   are independent and could run concurrently.
2. **Cost.** Intermediate payloads are paid for repeatedly: every prior tool result is re-sent as
   input on each subsequent turn.
3. **Context exhaustion.** Large list/describe responses evict the information the agent actually
   needs. The agent then re-fetches, making it worse.
4. **Unreviewability.** The user sees an unbounded stream of individual commands. There is no single
   artifact to approve before anything runs, and no way to know how many calls will happen or whether
   any of them mutate state.

The pattern that fixes all four is well known outside of agents: describe the whole computation once,
then execute it. What is missing is a way for an agent to express _"run these commands, in this
shape, feeding these outputs into those inputs"_ that is (a) safe to execute without an LLM in the
loop, (b) small enough to plan against without shipping the entire AWS API surface into the prompt,
and (c) legible enough for a human to approve.

### 1.1 Why not the obvious answers

| Approach                                             | Why it falls short                                                                                                                                                                                                                                                               |
|------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Let the agent write a bash/Python script             | Maximum expressiveness, minimum reviewability. Arbitrary code means arbitrary side effects (filesystem, network, `curl`, `rm`), non-portable shell assumptions, and a review burden equivalent to code review. Sandboxing it is a much larger project than the problem warrants. |
| Chain with `--query` (JMESPath) and shell pipes      | `--query` filters a single response. It cannot bind one command's output into another's input, cannot fan out, and shell glue reintroduces the script problem.                                                                                                                   |
| Ship a "many small tools" MCP server                 | Reduces prompt size per tool but does not reduce turns: still one LLM turn per call.                                                                                                                                                                                             |
| Pre-built, hand-written workflows (aliases, wizards) | Reviewable and safe, but the set of useful workflows is unbounded and unpredictable. The agent must be able to compose novel ones.                                                                                                                                               |
| Server-side orchestration (e.g. Step Functions)      | Correct shape, wrong deployment model: requires cloud resources, IAM setup, and deploy latency for a read-only question asked in a terminal.                                                                                                                                     |

The gap is a **declarative, data-only workflow document** that the agent authors, the user reviews,
and the CLI executes locally.

## 2. Goals

### 2.1 Functional goals

- **G1 — One turn.** An agent describes a multi-command workflow in a single response; the workflow
  executes with no model in the loop; the agent receives one compact result.
- **G2 — Reviewable plan.** The workflow is a standalone artifact a human can read, diff, edit,
  version, re-run, and approve before execution. The runtime can explain it (what runs, how many
  calls, which regions, what mutates) without calling any API.
- **G3 — Plannable with bounded prompt cost.** The agent can discover exactly the schema it needs —
  operations, parameters, output shapes, pagination and server-side-filter facts — without the full
  CLI/API surface being placed in its context. The authoring instructions themselves come from the
  CLI's own help, versioned with the executor, not from a document that can drift out of sync with it.
- **G4 — Output → input binding.** A binding can consume any previous binding's value through a typed
  reference, with filtering and projection expressed in a closed, validatable vocabulary and aggregation
  handled by declared combiners rather than by expressions.
- **G5 — Derivable parallelism, two axes.** All concurrency is *derived from the plan's data
  dependencies*, never stated by the plan.
  - **G5a — Fan-out (horizontal).** A binding declared as "run once per element of this collection" runs
    its tasks concurrently. Multi-region queries are the canonical case.
  - **G5b — DAG (vertical).** Two steps that do not reference each other's outputs are independent and
    run concurrently. The plan is a DAG, not a list: document order carries no meaning at all, and
    a plan that answers a question from three unrelated `describe` calls should cost one round-trip,
    not three. Independence is inferred from the absence of output → input binding, subject to the
    safety rules in §4.2.
- **G6 — Parallel fold.** A fan-out binding can declare a per-item projection and an associative
  combiner, so results are reduced as they arrive rather than materialized. This makes
  "count/sum/group across N regions or M pages" cheap in memory and small in output.
- **G7 — Pagination is a first-class, bounded concern.** Paginated operations are handled by the
  runtime with explicit limits, and truncation is always reported, never silent.
- **G8 — Filters pushed down.** The system actively steers the agent toward server-side filtering and
  reserves client-side expressions for what the service cannot do.
- **G9 — Partial success is a real outcome.** Per-binding and per-item error policies, with a result
  envelope that carries both the data that succeeded and a structured account of what failed.

### 2.2 Safety and trust goals

- **G10 — No ambient authority.** The interpreter can call CLI/API operations and evaluate pure
  expressions. It cannot read or write files, spawn processes, open sockets, or reach the host
  runtime. Impure inputs (clock, randomness, identity) are supplied by the runtime through an
  explicit binding that is reported back with the result, rather than fetched by plan code.
- **G11 — Read-only by default.** Mutating operations require an explicit opt-in flag at run time,
  and are called out prominently in the plan explanation.
- **G12 — Bounded blast radius.** Hard ceilings on total calls, concurrency, result bytes, wall-clock
  time, and items retrieved — enforced by the runtime, not negotiable by the plan.
- **G13 — Stateless and self-describing.** The runtime persists nothing: no run store, no cache, no
  history, no replay. Every invocation is self-contained — parse, validate, execute, report — and the
  audit trail is the output itself: the resolved impure inputs, every operation invoked, and the error
  summary all travel in the result envelope and the progress stream.
  Retention is the caller's decision, not the CLI's.

### 2.3 Non-goals (v1)

- General-purpose programming: no user-defined functions, recursion, unbounded loops, or
  Turing-complete control flow. Long-running or stateful orchestration belongs in Step Functions.
- Conditional branching beyond "skip this if the collection is empty". No `goto`, no dynamically
  constructed work.
- Interactive steps, prompts, or waiting on human input mid-run.
- Cross-run state of any kind: no run store, no history, no cache, no replay, no resume of a partially
  completed plan, no scheduling or triggers. A re-run is a fresh invocation of the same plan.
- Replacing individual `aws` commands. A single command stays a single command; Code Mode is for
  workflows.
- Emitting a workflow *from natural language inside the CLI* (i.e. the CLI does not call a model).
  Planning is the agent's job; the CLI provides schema, validation, explanation, and execution.

## 3. Users and flows

**Agent (primary author).** Has a task and, at most, a pointer telling it this command exists; it reads
the CLI's own help for the format (§4.5). Needs to discover the handful of operations relevant to the task, emit a plan, get it validated cheaply, and
receive a small result.

**Interactive human (reviewer).** Sees a rendered plan before anything runs; approves, edits, or
rejects. Cares about: how many API calls, against which accounts/regions, does anything mutate, what
will it cost me in time.

**Automation / CI (re-user).** Takes a plan that worked once, parameterizes it, and re-runs it on a
schedule with no model involved at all. Plans are ordinary artifacts that can live in a repo.

### 3.1 Happy path

```
task ──▶ agent ──▶ [schema discovery]  (0..k cheap, local, offline lookups)
                      │
                      ▼
                   plan document (single LLM output)
                      │
                      ├──▶ validate      (static: names, types, expressions, refs, mutation check)
                      ├──▶ explain       (human-readable render + call estimate; no API calls)
                      ├──▶ approve       (interactive gate, or --yes / policy-driven)
                      ▼
                   execute               (concurrent, bounded, no LLM in the loop)
                      │
                      ▼
                   result envelope       (small; single tool result back to the agent)
```

### 3.2 Repair path

Validation failures are returned as structured, actionable diagnostics (unknown operation with
did-you-mean candidates; missing required parameter; a reference to an undefined binding; type
mismatch). The agent gets at most a small, bounded number of repair turns — this is still far cheaper
than N execution turns, because no API payloads enter the context.

## 4. Conceptual architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│ Authoring surface (what the agent is given)                            │
│  · Skill: workflow grammar, expression allowlist, patterns, pitfalls   │
│  · Schema lookup: operations by name or keyword, compactly, offline    │
└───────────────────────────┬────────────────────────────────────────────┘
                            │ plan document (JSON)
┌───────────────────────────▼────────────────────────────────────────────┐
│ Plan pipeline                                                          │
│  Parser ──▶ Normalizer ──▶ Static validator ──▶ Explainer/Estimator    │
│   (parse, don't validate: an invalid plan cannot become a run)          │
└───────────────────────────┬────────────────────────────────────────────┘
                            │ normalized plan + approval
┌───────────────────────────▼────────────────────────────────────────────┐
│ Executor                                                               │
│  Scheduler (DAG from data deps)  Expression sandbox (pure, data-only)  │
│  Invoker (auth, region, retry, pagination)  Reducer (streaming fold)   │
│  Governor (budgets: calls, bytes, time, concurrency)                   │
└───────────────────────────┬────────────────────────────────────────────┘
                            │
                            ▼
       result envelope (stdout)  +  progress and diagnostics (stderr)
                     nothing is written to disk
```

### 4.1 One binding form, a chain of stages

A workflow is a set of named blocks and locally named values. Each block may establish one source, traverse one
collection, and apply a canonical chain of optional stages:

```
traverse? ──▶ source ──▶ filter? ──▶ dedup? ──▶ fold? ──▶ result/bind
```

- **traverse** — optional. A reference yields a collection, and the binding evaluates once per element.
  Absent ⇒ one evaluation. This is the only source of fan-out, and it is derived from data, so the runtime
  knows the width before the first call.
- **source** — the one effectful producer (an API call) or a pure dereference. the sole `source` map key names each produced record for every later stage and `result`; there is no implicit current record. A call yields a
  stream, and pagination bounds that stream rather than changing downstream semantics.
- **transform** — optional filter and deduplicate stages. Each references explicitly named values.
  Applied before retention, together with derived field pruning, they let a binding pull four fields out of a
  40 MB page and never hold the page. Which fields those are is inferred from consumer references (§11.2).
- **fold** — optional aggregation into a single named value. the sole `fold` map key binds the aggregate for `result`.
  Because aggregation is expressed as a monoid with declared laws (§10.2), the runtime may fold partial results
  as they arrive in bounded memory.
- **bind** — the block's result becomes available to other bindings by its `let` name.

Width × pages is where a naive agent-written workflow explodes; making both the runtime's responsibility,
with projection and folding available to collapse them, is the core of the design.

Note what is *not* in this list: there is no taxonomy of binding kinds. A single form plus composable
operators is what lets "fold one paginated call" and "fold across a fan-out" be the same mechanism, and
"fan out over a correlation" require nothing new.

### 4.2 Dependency and concurrency model

A plan is a **DAG, not a script.** `let` is an unordered set that happens to be written as an array;
execution order is derived, and two steps with no path between them may run at the same time.

**Edges are derived, not declared.** An edge `a → b` exists when any expression in `b` references
another binding's value. Every position participates: `args`, `region`, `forEach.<name>.from`, stage keys,
`result`. Nothing else creates an edge — in particular, adjacency in `let` does not, and there is
no construct for declaring an edge without data behind it (§4.2.3).

Because a block may nest (§9.2.1), the graph is a **tree of DAGs**: each block has its own dependency graph
over its own bindings, and a nested block is a single node in its parent's graph. Scope is one-directional, so
edges never cross block boundaries — an inner binding may read outer values, but nothing outside a block can
name what is inside it. Waves are therefore nested rather than flat, and the analysis recurses without
special cases.

```
                      ┌── volumes ──┐
        regions ──────┤             ├── result
                      └── snapshots ┘

  wave 1: regions
  wave 2: volumes, snapshots        (independent: neither references the other)
```

This is the parallelism an agent gets for free from writing the obvious plan. A question like
_"summarize my account: instance count, unattached volumes, and public snapshots"_ is three unrelated
reads; as a DAG they cost one wave instead of three.

The two axes compose: an independent set of steps runs concurrently, and each of those steps may
itself fan out. Total in-flight work is `Σ(fan-out width of each running binding)`, which is why the
concurrency governor is global rather than per-binding (§13.1).

#### 4.2.1 When is independence safe?

Absence of data dependency is *sufficient* for reads and *insufficient* for writes.

- **Read ∥ read — always safe.** Two operations with no observable effect cannot interfere. Order is
  unobservable, so any schedule is correct.
- **Read ∥ write, write ∥ write — not inferable.** Data flow cannot see effect-based dependencies. If
  one step creates a resource and another lists resources of that kind, there is a real dependency
  that no expression reference reveals; running them concurrently makes the result depend on timing.
  API-level eventual consistency makes this worse, not better: even a correctly ordered
  create-then-describe may not observe its own write.

Therefore: **mutating AWS calls run after every AWS read call.** Pure bindings are not assigned to a phase;
they run whenever their data dependencies are ready, including between two mutations to inspect the first
mutation's outcome. A read call may not depend transitively on a mutation — read-after-write remains forbidden
for the eventual-consistency reason above.

The scheduler therefore delays mutation call tasks until all independent read call tasks complete, then orders
mutations only through data dependencies. Nothing about this is authored: effect classification comes from the
operation (§13.4), while ordering comes from references.

The rule is derivable *and* it matches the shape real plans have: find the stale volumes, then tag them. And it
needs no ordering construct, because if two mutations genuinely need sequencing then one's input derives from
the other's outcome ("snapshot the volumes that tagged successfully"), and if it doesn't, they are independent.
An apparent need to declare order without a data relationship is a sign that the plan is sequential-imperative
rather than dataflow — the thing this design exists to avoid.

What this gives up is **read-after-write inside one plan**. That is the right thing to give up: it was never
reliable anyway, for the eventual-consistency reason above, and it is exactly the point where a checkpoint and a
second invocation are wanted rather than a silent race. This makes the safe case fast and
the dangerous case boring, and it is the reason the read-only classification of §13.4 is load-bearing
for more than just the mutation gate.

#### 4.2.2 Determinism

The *result* must not depend on the schedule. Three rules enforce it:

1. Aggregator monoids are associative (and commutative where completion order is nondeterministic).
2. Collecting fan-out results into a list preserves `forEach` input order, not completion order.
3. Independent bindings cannot observe each other by construction (no shared mutable state; each value is
   written once, when its binding completes).
4. The document has no positional semantics (§9.1.1), so two plans differing only in ordering are the same
   plan and cannot execute differently.

Consequence: re-running an unchanged plan against an unchanged environment yields byte-identical
`result`, regardless of how the scheduler interleaved the calls. Only `diagnostics` (durations, retry
counts, error ordering) may vary.

#### 4.2.3 Why there is no explicit ordering construct

An earlier draft had an `after` field: an edge with no data behind it, for "dependencies real but invisible to
data flow". Each of its three motivating cases turned out not to survive scrutiny.

| Claimed use                              | Why it fails                                                                                                                                                                                        |
|------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Spacing calls against a rate-limited API | Contradicts §13.1 — concurrency and backpressure are the runtime's, and a plan cannot know the right value anyway.                                                                                  |
| Sequencing a write before a read         | Either the read consumes the write's output, in which case it is already a data dependency, or it does not, in which case eventual consistency means ordering does not deliver visibility (§4.2.1). |
| Ordering two mutations                   | If ordering matters, one's input derives from the other's outcome; if it does not, they are independent.                                                                                            |

Removing it also removed a positional rule, a validation rule, and a barrier-insertion pass. The remaining
ordering mechanisms are the two that are grounded in something: references, and the read-then-mutate phase
split.

### 4.3 Sublanguage requirements

Whatever expresses "which records, which fields, and how many results become one" must be:

- **Closed, not open.** A fixed vocabulary of operations, so every construct can be enumerated in `help`,
  checked by a validator, and exhaustively handled by a compiler. An open language — one where new behaviour
  arrives as a function call or a lambda — cannot offer any of the three.
- **Weaker than computation.** No recursion, no user-defined functions, no unbounded iteration. Guardrails
  that must be *configured* (stack depth, timeouts) are evidence the language is too strong for the job.
- **Decomposable by concern.** Filtering, aggregation, and dataflow are different problems with different
  algebras (§10). Keeping them separate is what lets each stay small: aggregation in the combiner algebra is
  precisely why the query algebra needs no grouping, and no arithmetic to support it.
- **Analysable for translation.** The system, not the author, should decide which predicates a service can
  evaluate. That requires a normal form the compiler can split into independent clauses — a property of
  algebraic structure, not of syntax.
- **Schema-describable.** The surface, including its closed expression AST, has a JSON Schema, so authoring
  becomes constrained generation rather than free-text generation and syntactic validity stops being a failure
  mode. An opaque string expression cannot offer this; a structured term algebra can.
- **Unambiguous about references.** Naming another binding's value should be *data*, not a string to be
  parsed, so dataflow can be read off the document. Dependencies discovered by analysing a language can be
  missed; a missed dependency is a race, not a diagnostic.

Impure needs that arise from real arguments — "start time = 24h ago", "my account id" — are runtime-provided
bindings resolved once per run and echoed back with the result (§12), not language features. Modeled
idempotency tokens are populated by botocore itself (§12), so they require no expression source.

### 4.4 Schema provisioning strategy (the hard part)

The AWS API surface is far too large to inline: hundreds of services, tens of thousands of
operations, deeply nested shapes. Four tiers, in increasing specificity and decreasing frequency:

- **Tier 0 — the CLI's own help.** The workflow grammar, expression allowlist, budgets, error
  policies, and worked patterns live in `codemode help`, shipped and versioned with the binary that
  will execute the plan. Fixed size, read once per session, contains *no* service schema. An external
  skill is **not** the teaching surface (see §4.5) — it only tells the agent that this command exists.
- **Tier 1 — keyword lookup.** "Which operation lists X?" Returns a deterministically ranked,
  one-line-per-operation index, with pagination and server-filter flags. Costs hundreds of tokens, not
  millions. Retrieval is lexical — there is no model in the CLI — so the design constraint is recall and
  turn elimination, not ranking quality: many queries per invocation, never an empty result, and
  promotion straight to Tier 2 when the top hit is unambiguous (§15.1).
- **Tier 2 — describe.** Compact, pruned signature for a specific operation: parameters (required,
  types, enums), the response members that matter, pagination facts, and which parameters filter
  server-side. Pruned by depth and rendered in a compact signature notation rather than full JSON
  Schema.
- **Tier 3 — validation feedback.** The authoritative schema is applied by the validator, not by the
  prompt. The agent is *allowed* to guess from its pre-training and be corrected precisely and
  cheaply. This is what makes the whole thing affordable: the expensive artifact (full schema) stays
  on the local disk, where it already is.

Design consequence: **schema lookups must be local, offline, and free.** The CLI already ships the
service models it needs; the schema service is a projection over them, not a network service.

### 4.5 Discovery vs instruction

There are two distinct problems, and conflating them produces a stale, oversized skill.

**Instruction — how to author a plan.** This is large, precise, and changes with the binary: grammar,
expression allowlist, budgets, error policies, examples. It belongs in the CLI's own help output,
because that is the only copy guaranteed to describe *the executor that will actually run the plan*.
A skill file, a blog post, or a model's pre-training can all disagree with the installed binary; help
cannot.

**Discovery — that this capability exists at all.** A model whose training predates the feature has no
idea the command exists, and will confidently fall back to N single commands. Nothing in the
instruction material helps, because the agent never looks for it. This is the *only* problem the
external skill needs to solve, and it needs roughly three sentences to do it: what the capability is,
when it is the right choice, and the one command to run to learn the rest.

Requirements that follow:

- **D1 — Help is the authoritative, self-contained instruction surface.** An agent that has read only
  the help output can author a valid plan. No external document is required at author time.
- **D2 — Help is written for a machine reader as well as a human.** Structured into addressable
  topics, exhaustive where it claims to be (§10.2), token-budgeted, and available as JSON.
- **D3 — Help is versioned with the executor.** Same artifact, same release, no drift, no
  compatibility matrix between prose and behaviour.
- **D4 — The pointer is minimal and stable.** The trigger material must be small enough to sit in
  every session's context permanently, and must not repeat anything from help — otherwise it will
  eventually contradict it.
- **D5 — Multiple discovery paths, since one will always be missing.** Skill pointer, `aws help`
  topic, and in-band hints from the CLI itself.

## 5. Feature requirements

| ID   | Requirement                                                                                                                        |
|------|------------------------------------------------------------------------------------------------------------------------------------|
| F1   | Plan is a single, self-contained, declarative document; JSON or equivalent.                                                        |
| F2   | Every value identifier is introduced as a map key (`let`, `source`, `forEach`, `fold`) and resolved through one lexical namespace. |
| F3   | Data access and computation use a closed typed expression algebra; no templates, interpolation, or user-defined functions.         |
| F4   | Fan-out declared as "run per element of this collection"; no explicit concurrency.                                                 |
| F4b  | Bindings form a DAG from data references; independent read-only bindings run concurrently.                                         |
| F4c  | Mutating AWS calls wait for all AWS reads; pure bindings follow data dependencies; read-call-after-mutation is rejected.           |
| F4d  | `explain` renders the DAG as execution waves, showing what runs in parallel.                                                       |
| F5   | Per-binding region/profile override, so fan-out over regions or accounts is expressible.                                           |
| F6   | Field pruning is derived from consumer usage, not authored; applied before retention and pushed down where supported.              |
| F7   | Combiners are declared monoids; laws (associativity, identity, commutativity) license streaming and out-of-order folds.            |
| F8   | Pagination policy is a set of optional item/page/size bounds, with mandatory truncation reporting.                                 |
| F9   | The interpreter decides which predicates the service evaluates and which are residual; the author never encodes filter dialects.   |
| F10  | Per-binding error policy: fail the run / skip the item / collect the error and continue.                                           |
| F11  | Result envelope with status (ok / partial / failed), result, and structured diagnostics.                                           |
| F12  | Static validation against a JSON Schema plus the service models, with actionable diagnostics and suggestions.                      |
| F12b | Schema lookup accepts multiple queries per invocation, results labelled and budgeted per query.                                    |
| F12c | Schema lookup never returns an empty result; it relaxes and reports which relaxation matched.                                      |
| F12d | Unambiguous name near-misses are corrected in place and answered, not rejected with a suggestion.                                  |
| F13  | `explain`: human-readable render, execution waves, and call estimate without any API call.                                         |
| F14  | Read-only classification of every operation; mutations gated behind an explicit flag.                                              |
| F15  | Runtime budgets: max calls, max concurrency, max result bytes, max wall clock, max items.                                          |
| F16  | `help` is self-contained and authoritative: an agent that has read only the help output can author a valid plan.                   |
| F16b | Every standard function and combiner is enumerated in `help`, generated from the same typed tables the parser enforces.            |
| F16c | `help` addressable by topic and emittable as JSON, for machine consumption.                                                        |
| F17  | Progress reporting on stderr; result on stdout; the two never mix.                                                                 |
| F18  | Stateless: nothing persisted between invocations; the envelope and progress stream are the audit trail.                            |
| F18b | `validate`, `explain`, and `run` each require an explicit plan source; `run` always re-validates.                                  |
| F25  | One block construct: local bindings and an expression are orthogonal optional parts, not alternative forms.                        |
| F26  | Nesting is `let` inside `let`, bounded in depth, with static one-directional scope and no shadowing.                               |
| F27  | No positional semantics: name-keyed maps wherever items have names; two plans differing only in order are identical.               |
| F28  | Parser infers One/Optional/Many cardinality and Smithy/structural shape for every symbol; only external inputs declare types.      |
| F19  | Plans accept named inputs so a reviewed plan can be re-run with different parameters.                                              |
| F20  | Idempotent re-planning: same task + same schema ⇒ stable plan shape (no hidden nondeterminism in the format).                      |
| F21  | Discoverable by models that predate the feature: minimal skill pointer, `aws help` topic, in-band hints.                           |
| F22  | Plan supplied inline, from stdin, or from a file; no filesystem write access required to run one.                                  |
| F23  | Parser tolerates mechanically unambiguous model-output artifacts (code fences, comments) with a warning.                           |

## 6. Risks

| Risk                                                  | Mitigation                                                                                                                      |
|-------------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| Model predates the feature and never tries it         | Discovery is a separate, minimal artifact (§18.3) plus in-band paths (§18.4); measured in phase 3.                              |
| Instructions drift from the installed executor        | Help ships with the binary and is generated from the enforced allowlists; skill carries no grammar.                             |
| Agent hallucinates operations/parameters              | Tier 1/2 schema lookups + strict validation with did-you-mean; bounded repair loop.                                             |
| Agent burns turns guessing keyword terms              | No empty results ever (§15.4); many queries per call; auto-promotion to full signature (§15.5).                                 |
| Wrong pushdown returns a silently wrong answer        | Conservative default (residual); curated table gated on differential tests (§11.2); `explain` shows the split.                  |
| DSL cannot express a needed shape                     | Gaps are filled by adding an algebra operator, never by adding computation to the query language (§10.6).                       |
| Lexical-only retrieval has poor recall                | Curated synonym/acronym/intent table applied before matching; progressive relaxation; `--service --list`.                       |
| Bespoke DSL has no prior familiarity                  | JSON Schema enables constrained generation; worked examples in `help`; diagnostics name the enumerated alternatives.            |
| Runaway fan-out (regions × accounts × pages)          | Estimate before running; hard budgets; require approval above thresholds.                                                       |
| Concurrent steps interfere via effects                | Reads never interfere; mutations run in a later phase (§4.2.1), ordered only by data.                                           |
| Result depends on scheduling (nondeterminism)         | Associative combiners, input-order collection, write-once bindings; property tests on fold order.                               |
| Overlapping steps multiply throttling                 | Global concurrency bound plus per-service and per-endpoint caps; adaptive backoff lowers it.                                    |
| Silent truncation produces a confidently wrong answer | First-class envelope field, partial status, and summarizer guidance make incompleteness explicit.                               |
| Predicates too complex to review                      | `explain` renders the lowered form as equivalent CLI commands; residual clauses are listed explicitly.                          |
| Plan performs mutations the user did not expect       | Read-only default; mutation list shown at the top of `explain`; per-run opt-in.                                                 |
| Sublanguage becomes an escape vector                  | Closed typed function vocabulary; no user functions, recursion, host access, or effects — nothing to sandbox (§4.3).            |
| Result too large to help the agent                    | `result` kept small and `fold` encouraged; envelope byte cap with explicit truncation; `explain` warns on likely-large results. |

## 7. Success criteria

- A representative benchmark of multi-call tasks (multi-region inventory, tag audit, cost-by-tag,
  log-group sweep, security-group exposure check) completes in **2 LLM turns** (plan + summarize),
  versus N+1 today.
- Independent reads in those tasks overlap: measured wall-clock is close to the critical path of the
  dependency graph, not the sum of the steps.
- An agent given only the pointer skill reaches `codemode help` and produces a valid plan on tasks
  that warrant one, without the plan format appearing anywhere in its prompt beforehand.
- Schema discovery for a task costs **at most one turn**: one batched `schema` invocation covering every
  operation the plan needs. Zero-result responses, and repeat lookup turns for the same task, are
  tracked as defects against §15.1.
- Total tokens for those tasks drop by an order of magnitude; wall-clock drops proportionally to
  achievable concurrency. The manual read at author time is counted against this budget (§18.2).
- Plan validity on first attempt above a target rate (aim: ≥80% with Tier 1/2 lookups available),
  and ≥95% within one repair turn.
- No execution path exists that performs an effect outside the declared operation set.

---

# Part II — Proposed solution: `aws codemode`

## 8. Overview

A new CLI command group, `aws codemode`, which carries its own authoring documentation, plus a
three-sentence pointer skill (delivered by the existing `aws configure agent-toolkit` mechanism) whose
only job is to make a model that predates the feature aware that it exists.

```
aws codemode help [<topic>]            # THE authoring instructions (see §18); --output json
aws codemode schema <query>...         # look up operations: by name, or by keywords
aws codemode validate  --plan <src>    # static check; structured diagnostics
aws codemode explain   --plan <src>    # human render + call estimate (no API calls)
aws codemode run       --plan <src>    # execute

<src> = inline JSON | file://path | -  (stdin)   -- required, see §8.1
```

`run` always re-validates its own source before executing, and (interactively, or above risk
thresholds) explains + confirms. There is no `--dry-run` flag: `explain` **is** the dry run. It parses,
validates, and estimates without contacting any service, which is the entire content a `--dry-run` flag
would have had. A separate command rather than a mode means one fewer decision for the caller, one fewer
way to get the flag wrong, and an exit code that means "the plan is sound" rather than "the plan is sound
and also I did nothing".

The command group is **stateless** (G13): nothing is written to disk, nothing is remembered between
invocations, and there is no run history, cache, or replay. Every invocation carries its own plan.

A plan contains **no general-purpose expression language**. It is three small algebras (§10) — workflow,
combiners, and a closed typed pure-expression algebra — each represented as a JSON AST with a JSON Schema, so
a plan can be schema-validated, type-checked against the service models, and lowered to server-side request
parameters where the API supports them. There is no opaque expression string, recursion, or user-defined code.

### 8.1 Plan sources

`--plan` accepts three sources, resolved by an explicit ordered rule so there is never any doubt about
how a value was interpreted:

| Value                            | Source                                           | Primary user                                            |
|----------------------------------|--------------------------------------------------|---------------------------------------------------------|
| starts with `{` (after trimming) | **inline JSON** in the argument itself           | agents in sandboxes without filesystem write access     |
| `file://path`, `fileb://path`    | file, via the CLI's existing paramfile expansion | humans, CI, plans checked into a repo                   |
| `-`                              | **stdin**, read to EOF                           | agents invoking through a shell; pipelines; large plans |
| omitted                          | **error**: the source is required                | —                                                       |
| anything else                    | **error**, with a targeted diagnostic            | —                                                       |

Notes on each:

- **Inline is the default form** because that is how every other structured parameter in the CLI works
  (`--filters '[{"Name":…}]'`); `file://` is the opt-in indirection. Getting this backwards — bare paths
  meaning files — would be inconsistent with the rest of the CLI.
- **A bare path is a helpful error, not a mystery.** `--plan plan.json` fails with
  `not JSON and not a file:// URI; did you mean file://plan.json?`, because that mistake is inevitable
  and a one-line correction is cheaper than a confusing JSON parse error.
- **`file://` reuses `awscli/paramfile.py`** as-is, so `~` and environment-variable expansion behave the
  same as everywhere else in the CLI, and `file:///dev/stdin` works on platforms that have it.
- **No implicit stdin.** Omitting `--plan` is an error, not a silent read from the terminal. Pipelines
  write `--plan -` explicitly (`generate-plan | aws codemode run --plan -`). This costs one token and
  removes an entire class of confusion: a command that hangs waiting on a TTY, and any ambiguity about
  which plan a given invocation actually ran.
- Inline plans land in shell history and process listings. Plans are not secrets, but they do carry
  resource identifiers, and inline is also bounded by the platform's argument-length limit — both are
  reasons to prefer stdin or `file://` for anything large.

#### 8.1.1 Shell quoting

Because plans contain no `${…}` templates (§9.2), the substitution hazard that would otherwise dominate this
section is gone: there is no `$` in a plan for `bash`, `zsh`, or PowerShell to expand. What remains is
ordinary JSON quoting, and `help` presents the safe forms in this order:

```bash
# 1. stdin heredoc with a quoted delimiter: no escaping, no length limit
aws codemode run --plan - <<'PLAN'
{ "codemode": "v1", … }
PLAN

# 2. single-quoted inline
aws codemode run --plan '{"codemode":"v1", …}'

# 3. a file
aws codemode run --plan file://plan.json
```

Dropping the template syntax was motivated by validation and DAG extraction (§9.2); eliminating a whole
class of quoting failure is a larger practical benefit than either.

#### 8.1.2 Parse tolerance

The parser accepts strict JSON plus three documented tolerances, each reported as a warning so the
behaviour stays visible:

- **Markdown code fences** stripped from a leading/trailing ```` ``` ````/```` ```json ```` pair. Models
  emit them constantly — the prototype needed exactly this (§16) — and failing on a fence costs a turn
  to remove a character sequence the CLI can remove itself.
- **`//` and `/* */` comments**, and **trailing commas**, both tolerated and stripped.
- **A single-element array or an object wrapped in an obvious envelope** (`{"plan": {...}}`) unwrapped
  when the inner object is a valid plan.

Nothing else is tolerated: no YAML, no JSON5 unquoted keys, no single-quoted strings. The line is drawn
at *mechanically unambiguous* repairs to model output formatting, never at ambiguous syntax. Parse
errors report line and column, plus the offending token.

#### 8.1.3 Every invocation is self-contained

`--plan <src>` is **required** by `validate`, `explain`, and `run`. Nothing is remembered between
invocations: there is no stored plan, no cached validation result, no run history, and no replay
(G13). Each command re-parses, re-normalizes, and re-validates from its own source, every time.

Two consequences worth stating outright:

- **`run` always re-validates.** It never trusts an earlier `validate` invocation, because it has no way
  to know one happened or that the plan is the same one. `run` on an invalid plan fails at validation,
  before any call, always.
- **A pipe is consumed once.** `explain --plan -` followed by `run --plan -` cannot both read the same
  stream. The review-then-execute flow therefore uses a source that can be read twice — `file://` or
  inline — or stays inside a single invocation, where `run` renders the explanation and asks for
  confirmation itself (§13.4) from the plan it already holds in memory. The single invocation is the
  better flow anyway: it is the only one where the thing reviewed and the thing executed are provably the
  same bytes.

## 9. Plan document

### 9.1 One construct: a block

There is one structural concept. A **block** is either an expression or a `let`-block. A binding is a block
*at a name*, so `Block` is the only structural type in the document and the plan is simply a block.

```jsonc
Plan     = { codemode, description, inputs? } & Block

Block    = { forEach?,                              // Map1<Name,{from,batch?,do}> — traverse (§9.1.2)
             let?,                                  // local bindings
             source?, filter?, dedup?, fold?,            // explicit binder and stages (§10.1)
             result?,                               // the block's value (§10.4)
             onError? }

let      : Map[Name, Block]           // names → blocks
```

`let` and `source` are **orthogonal, not alternatives**. In any language with `let`, a block has both local
bindings and a body; an earlier draft forced a choice between them, which is why nesting needed a special
source kind and why three rules existed to police the combinations.

Every declaration uses a **name-keyed map**. `let` may contain many entries; producers that introduce exactly
one local value use `Map1<Name,A>` — a map with exactly one entry:

```text
Map1<Name,A> = Map<Name,A> where size = 1
```

- `source: { region: Producer }` names each produced record `region`.
- `forEach: { region: {from, batch?, do} }` names the current element or batch `region` inside `do`.
- `fold: { stats: Aggregator }` names the aggregate `stats` for `result`.
- `let: { regions: Block, … }` names each block's complete value for sibling and descendant blocks.

The identifier is always the object key; `as` does not exist. This gives uniqueness through JSON object
structure, stable diagnostic paths (`let.regions.source.region`), and one rule for what `{"ref":"name"}` means.
The operator still determines scope and cardinality, but never the declaration syntax.

`forEach` carries `from`, optional `batch`, and `do` under its name because those fields are meaningless apart.
It is higher-order: §10.1 types it as `Map1<Name, {from:Expr, do:Block}>`, and `do` is the function body. Stage
keys remain flat because each is independently meaningful whenever a block has a source. There is no implicit
current record.

Making the body explicit distinguishes two levels that were previously conflated:

| Key                      | Level                                                |
|--------------------------|------------------------------------------------------|
| the `forEach` map key    | one element or batch, visible inside `do`            |
| everything inside `do`   | per element                                          |
| `onError`                | how an element's failure is handled by the traversal |
| `let` on the outer block | evaluated **once**, visible to every element         |
| `let` inside `do`        | evaluated **per element**                            |

The last pair is a capability the implicit form could not express at all.

A block is a **flat record of optional keys**. There is no `pipeline` wrapper: once the stages are named keys
in a canonical order, a container whose only job is grouping them carries no information. The same flattening
already happens one level up — `Plan` is a `Block` with `codemode`, `description`, and `inputs` added — so the
document is a flat record at every level while the *type* (§17.1) groups the keys that belong together.

**The block's value**, in order of precedence: `result` if present; else the stages' output if `source` is
present; else the **unique sink** of `let` — the binding nothing else references. Several sinks and no `result`
is an error, because the answer is genuinely a combination and guessing would be wrong.

**Scope inside a block** is the enclosing scope, plus the block's own `let` names, plus — if `source` is
present — the records flowing through the stages.

**Not every combination of those keys is meaningful.** A flat record of ten optional keys admits 2¹⁰ shapes, of
which roughly a quarter are legal — so the validity structure is worth stating as structure rather than as a
list of prohibitions. A block plays exactly **one of three roles**, decided by which value-provider it has:

| Key                       | Traverse (`forEach`)      | Express (`source`) | Assemble (neither)               |
|---------------------------|---------------------------|--------------------|----------------------------------|
| `forEach`                 | required                  | —                  | —                                |
| `source`                  | —                         | required           | —                                |
| `filter`, `dedup`, `fold` | — they belong inside `do` | yes, independently | —                                |
| `let`                     | yes                       | yes                | yes                              |
| `result`                  | — use `do.result`         | shapes the stream  | yes; defaults to the unique sink |
| `onError`                 | yes, per element          | yes                | — makes no calls of its own      |

Reading the table as rules: stage keys require `source`, because they transform a stream and without a source
there is none. `forEach` and `source` are mutually exclusive, because a traversing block takes its value from
`forEach.do` and each element's source belongs inside it. `onError` requires one of the two, because an Assemble
block has no failures of its own to police — its bindings each carry their own policy. And the three roles are
exhaustive, which is the same statement as "a block needs at least one of `source`, `let`, or `result`".

Two rules are about names rather than shape:

- **Names are unique along the scope chain.** Keys from `let`, `source`, `forEach`, and `fold` share one lexical
  namespace (§9.2), so
  shadowing is rejected and a name means one thing in a plan.
- **Duplicate keys are rejected** in `let`, `inputs`, and `args` alike. JSON parsers disagree about
  duplicates and Python's silently keeps the last, so parsing uses a hook that reports them.

**The roles are a union, and it needs no tag.** Presence of `forEach` versus `source` versus neither
discriminates the branches, so the generated JSON Schema is a `oneOf` over three branches while the document
stays exactly as flat as it looks here — nothing on the wire changes, and invalid combinations become
unrepresentable rather than diagnosed (§17.1). This also sharpens the criterion above: *within* the Express
branch the four stage keys really are independently meaningful, so flat is right for them. What needed structure
was never the stages but the branch discrimination, which an earlier draft wrote as prose.

There is no projection *stage*. Which fields to **carry** is derived from what consumers use (§11.2); which
fields to **name and expose** is `result`. The first is a dataflow fact, the second is authorial intent, and an
earlier draft conflated them in a single `fields` operator.

#### 9.1.1 No positional semantics

Every collection in the plan is keyed by name unless its items genuinely have no name, and **nothing in the
document depends on position**:

| Field                        | Form                | Why                                                                       |
|------------------------------|---------------------|---------------------------------------------------------------------------|
| `let`, `inputs`, `args`      | map                 | items have natural unique names                                           |
| `source`, `forEach`, `fold`  | `Map1<Name,A>`      | each introduces exactly one named value                                   |
| stage keys on a block        | named keys          | composition order is canonical (§10.1), not authored                      |
| `filter`                     | one `Expr<boolean>` | conjunction is the `and` function, never implied by a container           |
| `and` / `or` arguments       | array               | n-ary commutative function arguments; the normalizer sorts them           |
| `dedup`                      | array               | an unkeyed set of identity expressions                                    |
| arrays inside `args`         | array               | dictated by the service model (`Filters`, `Values`), not by the DSL       |
| function arguments generally | array               | order is semantically meaningful except where the function is commutative |

So two plans that differ only in ordering are the **same plan**. The normalizer sorts unkeyed collections
canonically, which makes review, diffing, and `explain` output stable, and removes the last way that
reformatting a document could change what it does. Mutating AWS calls, which in an earlier draft took their
ordering from declaration position, now wait for all AWS read calls (§4.2.1) — derived, not authored.

#### 9.1.2 The block in full, by role

Shared parts first, then exactly one of three roles. The document is flat — the role headings are the schema's
`oneOf` branches (§17.1), not nesting.

```jsonc
Plan  = { "codemode": "v1", "description": <string>, "inputs": Inputs?, …Block }

Block = { "let":    { <name>: Block, … }?,        // shared — local bindings, evaluated once
          …Role }                                 // exactly one of the three below


// A declaration is always a map key. Exactly-one producers use Map1.
Map1<Name,A> = { <name>: A }   // exactly one property

// ── Role 1 · Traverse ── evaluate a block once per element ────────────────────────────────
{ "forEach": { <element>: { "from": Expr, "batch": <int>?, "do": Block } },  // Map1
  "onError": OnError? }          // failures are handled per element; shaping belongs in `do.result`

// ── Role 2 · Express ── one explicitly named stream, transformed ──────────────────────────
{ "source":  { <record>: { "call": Call } | Expr },       // Map1
  "filter":  Expr<boolean>?,                               // fixed order: filter → dedup → fold
  "dedup":   [ Expr, … ]?,
  "fold":    { <aggregate>: Aggregator }?,                 // Map1
  "result":  PureTransform?,
  "onError": OnError? }

// ── Role 3 · Assemble ── a value built from bindings ──────────────────────────────────────
{ "result": PureTransform? }      // omitted ⇒ the unique sink of `let`; see §10.4
                               // needs `let`, `result`, or both — a block with neither has no value
```

The leaf vocabularies, each closed:

```jsonc
Call       = { "service": <name>, "operation": <name>,          // §9.3.1
               "args":     { <param>: InputValue, … }?,             // the operation's input shape
               "region":   Expr?,                                // which endpoint
               "profile":  Literal?,                             // which identity; gated (§13.4)
               "paginate": { "maxItems": <int>?, "maxPages": <int>?, "pageSize": <int>? }? }

Expr       = Literal                                      // the single constant representation
           | { "ref": <name>, "path": Path? }          // lexical scope; all record access is explicit
           | { "input": <name>, "path": Path? }        // caller
           | { "env": <name>, "path": Path? }          // runtime (§12)
           | { Function: [ Expr, … ] }                  // typed application (§10.3)

Function   = casefold | age | date | number | size | cidr | cidrSize
           | eq | ne | startsWith | endsWith | contains
           | lt | lte | gt | gte | present | absent | setEq | setNe | subset
           | and | or | not

InputValue = Expr | [ InputValue, … ] | { <member>: InputValue, … }
Predicate  = Expr<boolean>

Aggregator = { "count": [] }
           | { (sum | min | max | collect | avg | distinctCount): [ Expr ] }
           | { "groupBy": [ Expr, Aggregator ] }
           | { <field>: Aggregator, … }                     // product of aggregators (§10.2)

PureTransform = Expr
              | { <name>: PureTransform, … }                 // recursive named product
              | { "record": { <name>: PureTransform, … } }   // explicit product when shorthand is ambiguous

Path       = <member> ( "." <member> | "[]" | "[" <int> "]" )*   // plus `tag:<name>` pseudo-paths
Inputs     = { <name>: { "type": "string" | "number" | "integer" | "boolean" | "array" | "document",
                         "default": Literal? } }
OnError    = "fail" | "skip" | "collect"
```

Three things this shape makes visible that a flat listing did not:

- **What is shared and what is exclusive.** `let` applies to any block; `onError` applies only where calls
  happen; stages exist only in Express.
- **Where each `result` points.** The same key means "over the envelope list", "element-wise on a stream", and
  "assembled from bindings" in the three roles — the same projection grammar applied to three different subjects.
- **How little there is.** Three stages, four expression sources, twenty-four standard functions across value,
  relation, and Boolean families, eight aggregator constructors, three error policies. Everything else is
  composition.

#### 9.1.3 Normative grammar invariants

This section consolidates the grammar contract. Implementations **MUST** enforce these rules before constructing
`ValidPlan`; later sections provide rationale and lowering details.

1. **Identifiers are map keys.** `let` is `Map<Name,Block>`. `source`, `forEach`, and `fold` are
   `Map1<Name,A>` (exactly one property). No declaration stores its name in an `as`, `id`, or positional field.
2. **One lexical namespace.** Keys introduced by `let`, `source`, `forEach`, and `fold` share a lexical
   namespace, are unique along the scope chain, and may not shadow one another. `{"ref":"name"}` resolves one
   symbol or is an error.
3. **Types are inferred, not duplicated.** Only `inputs` declare types. Every other symbol receives
   `Cardinality<Shape>` metadata from Smithy models and algebraic inference (§9.2.2).
4. **Block roles are a tagless union.** Presence of `forEach`, `source`, or neither selects Traverse, Express,
   or Assemble. Traverse and Express are mutually exclusive. Stage keys and `onError` are invalid on Assemble;
   stage keys require Express.
5. **Only semantic sequences are arrays.** Maps hold named values. `and`/`or` arguments and `dedup` keys are
   arrays but canonically sorted because order has no meaning. Other function arguments preserve order.
6. **Raw JSON is the only constant representation.** There is no `const` node. Exact expression-shaped object
   literals in expression contexts are supplied through typed `document` inputs (§10.3).
7. **Pure applications have one shape.** Every standard value function, relation, and Boolean connective is
   `{function:[arguments]}`. There is no parallel `key/value`, `left/right`, `op/args`, `transform`,
   `caseInsensitive`, or `apply` grammar.
8. **Field access is always rooted.** Standalone `{"path":…}` is invalid. Paths occur only on `ref`, `input`, or
   `env`, so no implicit `this`/current record exists.
9. **Stage order is fixed.** Express stages are `filter → dedup → fold`, regardless of document key order.
   Each may appear at most once. A `fold` consumes the stream; `fold.<name>` is the value available to `result`.
10. **Traverse bodies are explicit.** `forEach.<name>` contains `from`, optional `batch`, and `do`. Per-element
    shaping belongs in `do.result`; a Traverse block has no outer `result`.
11. **Correlation is constrained.** V1 has no join/attach stage. An outer traversal name may affect a nested
    query only through direct equality correlation atoms. Call correlations must be server-pushable; bound
    collection correlations must be indexable (§10.1.1).
12. **Result is a pure product.** `result` is an `Expr` or recursive named record product. There is no result
    field-list shorthand, so raw arrays always mean arrays. Omitted Assemble result means the unique `let` sink.
13. **Pruning is derived.** Plans never author projected field sets. The lowerer unions every referenced path,
    filter/fold/correlation dependency, and result demand (§11.2).
14. **Pagination is bounds, not modes.** `maxItems`, `maxPages`, and `pageSize` are independent optional bounds;
    absent means unbounded subject to runtime budgets. Cursor members are forbidden in `args`.
15. **Ordering is dataflow only.** No `after` or positional ordering exists. Mutating AWS calls wait for AWS
    reads and are ordered among themselves only by references; pure bindings follow dependencies (§4.2.1).
16. **Platform machinery is not syntax.** There is no idempotency-token, random, credential, endpoint, retry,
    or pagination-cursor expression source. Botocore/runtime own those values (§12).

#### 9.1.4 The floor

The smallest thing that is a plan: a version, a description, and something that produces a value.

```json
{
  "codemode": "v1",
  "description": "enabled regions",
  "source": {"region": {"call": {"service": "ec2", "operation": "describe-regions"}}}
}
```

Three keys, and nothing else is required because everything else has a derivation or a default:

| Absent           | What happens                                                                                             |
|------------------|----------------------------------------------------------------------------------------------------------|
| `result`         | the block's value is the call's records, unshaped — so the answer is whole records                       |
| pruning          | nothing inside the plan consumes this binding, and the consumer is the caller, so no field can be pruned |
| `paginate`       | absent means unbounded pagination, still capped by the run budget (§13.3)                                |
| `filter`, `fold` | absent means absent; there is no implicit behaviour to know about                                        |
| `onError`        | `fail` — a single call that fails fails the plan                                                         |

Two things worth noticing about the floor.

First, it is barely longer than `aws ec2 describe-regions`, which is the point: the ramp starts at zero, so
there is no threshold below which a plan is the wrong tool. Second, the *only* thing missing that a reviewer
would want is `result` — a plan with no `result` returns whole records, which is precisely why terminal shaping
is the one projection that has to be authored (§10.4) rather than derived.

#### 9.1.5 One step up

Add a filter and a shape, and it is still one flat object:

```json
{
  "codemode": "v1",
  "description": "running instances in us-east-1",
  "source": {
    "instance": {"call": {"service": "ec2", "operation": "describe-instances", "region": "us-east-1"}}
  },
  "filter": {"eq": [{"ref": "instance", "path": "State.Name"}, "running"]},
  "result": {
    "InstanceId": {"ref": "instance", "path": "InstanceId"},
    "InstanceType": {"ref": "instance", "path": "InstanceType"}
  }
}
```

No `let` and no implicit record — five top-level keys, one of which is the answer's shape, and the `filter` will be lowered
to `--filters Name=instance-state-name,Values=running` without the author knowing the parameter's name (§11.2).
Three consequences matter more than the brevity:

- **Code Mode becomes a superset of a single command rather than a mode with a threshold.** The agent never
  has to judge "is this big enough for a plan?" — a one-call plan is valid, not an error. The skill's
  quantitative trigger (§18.3) remains useful guidance, but misjudging it costs nothing.
- **There is a smooth ramp**: floor → add a filter and a result → add a binding → add `forEach`. No rewrite at
  any boundary, which is exactly where an agent would otherwise burn a turn.
- **The machinery applies to single calls too.** Pagination bounds and loud truncation diagnostics have no
  equivalent in `aws … --query`, which cannot report that a workflow-level budget was hit.

#### 9.1.6 Why blocks rather than step kinds

An earlier draft had three step kinds (`call`, `fanOut`, `join`). That conflated **one effect** (`call`),
**one combinator** (fan-out = traverse), and **one pure operation** (`join`) as siblings, so fields repeated
across them and nothing composed. With one block form and a canonical chain of stages:

- `fold` after a single paginated `call` counts one bucket's objects — inexpressible when combining was a
  fan-out-only field.
- `forEach` over a correlated stream needs no new kind.
- `dedup` is a stage rather than a flag.
- Multi-step work per element is a nested block rather than an impossibility (§9.2.1).

A-normal form is what keeps this authorable: one source, one stage record, references to other bindings.
Flat is what a model emits and edits reliably, and named keys are what diagnostics can point at
(`let.inst.filter.and[0].eq`). Sharing is why names exist at all: plans are DAGs, so if two bindings
both read `vpcs`, nesting would duplicate the subtree and therefore duplicate the API calls. Named bindings
are common subexpression elimination, stated rather than inferred.

### 9.2 References, scope, and nesting

There is no expression template syntax. An expression is a raw literal, a named value reference, or a typed
standard-function application. References are a closed union discriminated by their key:

```jsonc
{ "ref":   "regions", "path": "RegionName" }  // a name in lexical scope: a binding …
{ "ref":   "region" }                         // … or a traversal element, by its `forEach` map key
{ "input": "tagKey" }                         // a plan parameter
{ "env":   "ago", "path": "h24" }             // a runtime-provided value (§12)
```

Every reference has the **same shape**: the key names something, and the optional `path` navigates within it.
There are no sentinel values — no `null` standing for "no path", no boolean standing for "the whole thing" — and
`path` means one thing everywhere. An earlier draft had an `item` kind that overloaded its value slot as the
path, so `{"item": null}` meant the element and `{"item": "Name"}` meant a field of it; naming traversal
elements removes both the sentinel and the kind.

There is **one lexical namespace and two external namespaces.** `ref` names anything in lexical scope — a
binding or a value introduced by a `source`, `forEach`, or `fold` map key — and
the resolver looks it up rather than the author declaring which construct introduced it. That is sound because
names are unique along the scope chain (§9.2.1), so `{"ref":"region"}` cannot be ambiguous. `input` and `env`
stay separate because they come from the caller and runtime respectively; merging them would let a plan shadow
a runtime value.

`path` is **navigation attached to a named value**, not an expression source: member names, `[]` flattening,
index and slice literals. There is deliberately no standalone `{"path":…}` and no implicit current record.
Every field access starts with `ref`, `input`, or `env`, so the value being traversed and the dependency it
creates are explicit. Paths contain no functions, operators, or comparisons and remain fully type-checkable
against the named value's shape.

A reference **navigates only**. It carries no filter and no projection, because a binding whose `source` is a
reference does both, with a name attached and a line in `explain`. That keeps one place for filtering, one for
shaping, and one for assembly.

| Previously considered                                                 | Now                                                                                        |
|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| edges extracted by walking an expression AST; a missed edge is a race | edges are `{"ref": …}` references — read off the document                                  |
| `"${ … }"` templates collided with shell `${}` expansion              | no `$` appears in a plan at all                                                            |
| literal-vs-expression needed a delimiter convention                   | raw JSON values are constants; exact source/function object shapes are expressions (§10.3) |
| paths validated at runtime                                            | paths type-checked against the block's known shape at parse time                           |

Semantics:

- **Binding.** A binding's value is available to other bindings *in the same block* by name. Under `forEach`
  it is a list of envelope records in the input order of the traversed collection, one per element. The element
  is carried under **the traversal's map key**, and the outcome under `value` or, with
  `onError: "collect"`, `error`:

  ```jsonc
  // for  "forEach": { "region": { "from": …, "do": … } }
  { "region": "us-east-1", "value": { … } }
  { "region": "ap-east-1", "error": { "code": "AuthFailure", "retryable": false } }
  ```

  These are ordinary fields reached by ordinary paths — there is nothing magic about them, which is why
  A downstream binding with `source: {entry:{"ref":"inst"}}` can partition envelopes with
  `{"present":[{"ref":"entry","path":"error"}]}` and shape them with explicit `entry` references.
  Naming the element field after the traversal key rather than calling it `item` is what makes such a projection
  self-explanatory: `{"region": "item"}` required knowing that "item" meant the traversed thing, while
  the explicit `{region, value}` product says it. Consequently a traversal key may not be `value` or `error`.
- **Ordering.** Nothing depends on document position (§9.1.1). Execution order comes from references and
  the read-then-mutate phase split (§4.2); the answer comes from `result` or the unique sink.
- **Empty collections.** `forEach` over an empty collection succeeds with an empty list — the identity case
  of the fold (§10.2), not a special branch.
- **`fold` scope.** A `fold` reduces its own block's stream: the pages of one call, or the records of one
  traversal element. To reduce across elements, fold in an enclosing block.

#### 9.2.1 Nesting is a block with local bindings

A traversal evaluates its whole block once per element, so multi-step per-element work needs nothing beyond the
block's own `let`:

```json
{
  "codemode": "v1",
  "description": "VPC and subnet inventory per enabled region",
  "let": {
    "regions": {
      "source": {"region": {"call": {"service": "ec2", "operation": "describe-regions"}}},
      "filter": {"eq": [{"ref": "region", "path": "OptInStatus"}, ["opt-in-not-required", "opted-in"]]}
    },
    "perRegion": {
      "forEach": {
        "region": {
          "from": {"ref": "regions", "path": "RegionName"},
          "do": {
            "let": {
              "vpcs": {
                "source": {
                  "vpc": {
                    "call": {
                      "service": "ec2",
                      "operation": "describe-vpcs",
                      "region": {"ref": "region"}
                    }
                  }
                }
              },
              "subnetsByVpc": {
                "forEach": {
                  "vpc": {
                    "from": {"ref": "vpcs"},
                    "do": {
                      "let": {
                        "subnets": {
                          "source": {
                            "subnet": {
                              "call": {
                                "service": "ec2",
                                "operation": "describe-subnets",
                                "region": {"ref": "region"}
                              }
                            }
                          },
                          "filter": {
                            "eq": [
                              {"ref": "subnet", "path": "VpcId"},
                              {"ref": "vpc", "path": "VpcId"}
                            ]
                          }
                        }
                      },
                      "result": {
                        "vpc": {"ref": "vpc", "path": "VpcId"},
                        "cidr": {"ref": "vpc", "path": "CidrBlock"},
                        "subnets": {"ref": "subnets", "path": "[].SubnetId"}
                      }
                    }
                  }
                }
              }
            },
            "result": {"region": {"ref": "region"}, "vpcs": {"ref": "subnetsByVpc"}}
          }
        }
      }
    }
  }
}
```

`vpcs` is evaluated once per region. `subnetsByVpc` is then a dependent traversal: each logical VPC element
queries `DescribeSubnets` with `subnet.VpcId = vpc.VpcId`. The lowerer must push that equality and may batch VPC
IDs into multi-value filters before repartitioning the response (§10.1.1), so the author expresses the
relationship without choosing between N requests and an eager full-region scan.

Composition stays **applicative** (§10.1): the block is a fixed value and only the argument varies, so the call
graph is still static and `explain` can still bound the call count — now as width × block cost.

Note what is *not* here: no `block` source kind, and no rule confining nesting to a traversal body. Nesting is
`let` inside `let`, which is what nesting means in any language that has bindings. An earlier draft needed both
because `let` and `source` were alternatives.

Two rules keep it from becoming an expression tree:

1. **Depth limit of 2.** Deeper is expressible by flattening and almost always means the plan wants
   restructuring; the parser rejects it rather than letting a model build a five-deep tree.
2. **Scope is static and runs inward only.** Everything visible where a block is written is visible inside it:
   the traversal variables of enclosing traversals, every binding of every enclosing block — including the
   siblings of the binding it is written in — plus `inputs` and `env`. What is *not* visible is the inside of
   another binding: a name local to `crawl`'s own `let` cannot be reached from outside `crawl`, only `crawl`'s
   value can. That is the only cross-scope violation, and detecting it is mechanical because references are
   data. Shadowing is rejected outright, so a name means one thing everywhere in a plan.

A reference outward is an ordinary dependency edge, which has a useful consequence: **a binding named from
inside a `do` is evaluated once**, before the traversal, and the same value is visible to every element. That is
how a lookup table is broadcast rather than re-fetched per element:

```json
{
  "let": {
    "amis": {
      "source": {
        "image": {
          "call": {"service": "ec2", "operation": "describe-images", "args": {"Owners": ["self"]}}
        }
      }
    },
    "perRegion": {
      "forEach": {
        "region": {
          "from": {"ref": "regions", "path": "RegionName"},
          "do": {
            "let": {
              "instances": {"source": {"instance": {"call": {"…": "…"}}}},
              "annotated": {
                "forEach": {
                  "instance": {
                    "from": {"ref": "instances"},
                    "do": {
                      "source": {"image": {"ref": "amis"}},
                      "filter": {
                        "eq": [
                          {"ref": "image", "path": "ImageId"},
                          {"ref": "instance", "path": "ImageId"}
                        ]
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

`amis` is fetched once and appears in an earlier wave in `explain`, not once per region. Where the helper is only
meaningful to one traversal, putting it in a `let` *on the traversing block* rather than in the enclosing block
scopes it accordingly — same single evaluation, narrower namespace.

And two conventions that keep it legible:

3. **Traversal elements are always named.** The `forEach` map key is required, so `{"ref": …}` is the only
   way to reach an element and nesting introduces no ambiguity to resolve. There is no implicit "innermost element" to reason
   about.
4. **Prefer the flattest form that expresses the computation.** One validator rule covers both directions of
   over-structuring: warn when a `let` has a single binding whose value is the block's value anyway, and when a
   nested `let` uses its traversal variable at most once and could be hoisted. The normalizer canonicalizes
   internally either way, so `explain` and the lowerer see one representation.

#### 9.2.2 Symbols and inferred types

The authored document declares names but does not declare types, except for external `inputs`. Parsing builds a
symbol table from the Smithy models and the algebra. Every declaration site is a map key:

| Declaration                | Symbol introduced | Inferred type                                               |
|----------------------------|-------------------|-------------------------------------------------------------|
| `let.regions: Block`       | `regions`         | output type of that block                                   |
| `source.region: Producer`  | `region`          | one record flowing through the Express stages               |
| `forEach.region: {from,…}` | `region`          | one element of `from`, or a bounded many value when batched |
| `fold.stats: Aggregator`   | `stats`           | one aggregate value with the aggregator's structural shape  |
| `inputs.env: {type,…}`     | `env`             | the explicitly declared external type                       |

The internal type metadata is intentionally small:

```text
ValueType = Cardinality<Shape>
Cardinality = One | Optional | Many
Shape = SmithyShapeId | StructuralRecord | Scalar | List | Map | Document | Unknown
```

`Many<T>` is ordered: service/pagination order or traversal input order is preserved. `dedup` adds a uniqueness
property but does **not** turn the value into a set, because deterministic ordering remains observable. Optional
Smithy members and paths that may not exist produce `Optional<T>`.

Representative inference:

```text
regions                  : Many<ec2.Region>         // let binding's block output
region                    : One<ec2.Region>          // source record inside the block
region.OptInStatus        : Optional<enum<string>>
subnetsByVpc              : Many<Envelope<vpc, Many<SubnetSummary>>>
stats                     : One<{objects:number, bytes:number}>
```

The wire reference remains compact:

```json
{"ref": "regions", "path": "RegionName"}
```

After parsing it is a resolved node, not a string lookup:

```text
ResolvedRef {
  symbol: SymbolId("regions"),
  path: RegionName,
  type: Many<string>,
  provenance: LetBinding
}
```

This metadata validates that `forEach.<name>.from` is iterable, function applications have the right argument types,
paths exist, `filter` returns Boolean, correlation keys are compatible, and `fold` consumes a stream. It also
drives field pruning and the generated result schema. No authored type annotation is needed because duplicating
the Smithy model would add tokens and create a second source of truth. `explain --output json` includes the
inferred symbol table so the agent and reviewer can inspect it.

### 9.3 Why structured `service` + `operation`, not a single string

`"aws": "ec2:describe-instances"` is more compact but stringly typed: the plan parser cannot reject a
malformed reference without splitting and re-parsing, and the same concept ends up written three ways
across the plan, skill, and diagnostics. Structured fields give a single canonical form, direct
validation, and unambiguous error locations. Operation names accept either CLI style
(`describe-instances`) or API style (`DescribeInstances`); the normalizer canonicalizes and the
normalized form is what `explain` shows.

Parameter names likewise accept CLI style (`--filters` → `filters`) or API style (`Filters`).
Response data is always API-native (`Reservations`, `InstanceType`), exactly as `--output json` is
today. That asymmetry is real, is what the model has been trained on, and is called out explicitly in
`help plan`; the normalizer removes the ambiguity from execution.

#### 9.3.1 What goes in a `call`, and what does not

A `call` has three distinct namespaces, and conflating them is the most likely early mistake:

| Part                            | Contents                                                           |
|---------------------------------|--------------------------------------------------------------------|
| `service`, `operation`          | what to invoke                                                     |
| `args`                          | the operation's input shape — **the only place API parameters go** |
| `region`, `profile`, `paginate` | which endpoint, which identity, and pagination policy              |

A `call` is always the value of a one-entry source map: `source: {record: {call:…}}`. The key is not a call
parameter; it binds each returned record for `filter`, `dedup`, `fold`, and `result`.

`region` is **not** an API parameter: no operation's input shape contains it, it selects the client and
endpoint, and it takes no part in pushdown or field derivation. It is lifted to its own key because varying it
is the canonical fan-out (§9.4); written in `args` it would simply fail validation against the model.

`paginate` is policy, not parameters. The mechanics — `NextToken`, `MaxResults`, `Marker`, `Limit` — belong to
the runtime, which takes them from the shipped paginator configuration (§11.1).

Everything else is `args`, where names accept CLI style (`filters`) or API style (`Filters`) and normalize to
API members (§9.3), and nested structures are literal JSON matching the input shape. At any leaf the author may
supply a literal or a record-independent `Expr` (`ref`, `input`, `env`, or a standard function application
over them). The block's `source` key is not in scope while constructing that same source request — it is
bound only after the call returns. Expressions participate in dependency and type analysis like they do
elsewhere.

#### 9.3.2 What a plan may vary, and what only the invocation may

The dividing line: **a plan may vary what changes the question; only the invocation may change trust and
transport.**

| Knob                                         | Where it may be set                              | Why                                                                                                                    |
|----------------------------------------------|--------------------------------------------------|------------------------------------------------------------------------------------------------------------------------|
| `region`                                     | in the plan, freely                              | changes which account-region is being asked about; every region contacted is listed in `explain`                       |
| `profile`                                    | in the plan, gated by `--allow-profile-override` | changes *identity*, so the blast radius differs from what the plan text suggests (§13.4)                               |
| endpoint URL                                 | **nowhere**                                      | a plan that could redirect API traffic to an arbitrary host is an exfiltration vector; excluding it is required by G10 |
| request signing, TLS verification, CA bundle | invocation only                                  | security posture is the operator's decision, never the document's                                                      |
| timeouts, retry mode, concurrency            | invocation only                                  | the governor owns them, and the right values are only knowable at run time (§13.1)                                     |
| output format, `--query`                     | not applicable                                   | the envelope is the output contract, and `--query` is not the execution path (§11.2)                                   |

#### 9.3.3 Who owns a parameter

Two rules settle what happens when the runtime and the author both have a claim on the same input member:

- **Pagination members are the runtime's.** `args` may not set `NextToken`, `MaxResults`, `Limit`, or `Marker`
  for a paginated operation. The validator rejects it and points at `paginate`, because two writers on the
  token chain is a correctness problem, not a style question.
- **Author-written filters merge with lowered ones.** Writing `args.Filters` directly is a legitimate escape
  hatch for a filter dimension the capability table does not model, so for **list-valued** filter parameters
  the lowerer *appends* its pushed clauses — `Filters` are AND-composed, so appending is sound — and `explain`
  shows both origins. For **scalar** parameters (`Prefix`, `FilterPattern`) a collision is an error naming both,
  since silently preferring one would change the answer.

### 9.4 Worked example: multi-region fan-out

Task: _"running instances tagged `Env=prod` across all enabled regions, grouped by instance type."_

```json
{
  "codemode": "v1",
  "description": "Running Env=prod instances per enabled region, counted by instance type",
  "let": {
    "regions": {
      "source": {"region": {"call": {"service": "ec2", "operation": "describe-regions"}}},
      "filter": {"eq": [{"ref": "region", "path": "OptInStatus"}, ["opt-in-not-required", "opted-in"]]}
    },
    "inst": {
      "forEach": {
        "region": {
          "from": {"ref": "regions", "path": "RegionName"},
          "do": {
            "source": {
              "instance": {
                "call": {
                  "service": "ec2",
                  "operation": "describe-instances",
                  "region": {"ref": "region"},
                  "paginate": {"maxItems": 5000}
                }
              }
            },
            "filter": {
              "and": [
                {"eq": [{"ref": "instance", "path": "tag:Env"}, "prod"]},
                {"eq": [{"ref": "instance", "path": "State.Name"}, "running"]}
              ]
            },
            "fold": {
              "instDoAggregate": {"groupBy": [{"ref": "instance", "path": "InstanceType"}, {"count": []}]}
            }
          }
        }
      },
      "onError": "collect"
    },
    "ok": {
      "source": {"entry": {"ref": "inst"}},
      "filter": {"present": [{"ref": "entry", "path": "value"}]},
      "result": {"region": {"ref": "entry", "path": "region"}, "value": {"ref": "entry", "path": "value"}}
    },
    "failed": {
      "source": {"entry": {"ref": "inst"}},
      "filter": {"present": [{"ref": "entry", "path": "error"}]},
      "result": {
        "region": {"ref": "entry", "path": "region"},
        "code": {"ref": "entry", "path": "error.code"}
      }
    }
  },
  "result": {"byRegion": {"ref": "ok"}, "failed": {"ref": "failed"}}
}
```

Four things the plan does not say, because they are derived:

- **Which fields to carry.** Nobody writes a projection. `regions` is consumed only through
  `path: "RegionName"`, so that is the derived set and the rest of each region record is dropped as pages
  arrive. `inst` folds by `InstanceType`, so only that field and the two filter keys are retained (§11.2).
- **Which filters are server-side.** The lowerer normalizes the conjunction and consults the filterability
  table: both clauses become request parameters
  (`Filters=[{tag:Env,[prod]}, {instance-state-name,[running]}]`). The author wrote predicates against the
  *response* shape; mapping `State.Name` to the `instance-state-name` filter name is the interpreter's job.
- **Where the records live.** `filter` and `fold` apply to instance records; the model says the record path is
  `Reservations[].Instances[]`, so nobody writes it.
- **Fan-out width, concurrency, page counts** — derived as in §4.2.

`tag:Env` is a documented **pseudo-path**: tags are a `{Key, Value}` list in every response and a first-class
filter dimension in most APIs, so treating `tag:<name>` as a path matches both how the agent thinks and how the
API filters. Cloud Custodian uses the same convention.

`ok` and `failed` are bindings over a reference, so they make no API calls — they are the partition of `inst`
that `onError: "collect"` makes necessary. Written as inline filters on the references inside `result` they
would work too, but as bindings they get names, appear in `explain`, and can be reused.

### 9.5 Worked example: correlated subquery with batched pushdown

Task: _"which running instances are in VPCs that have an internet gateway, and what are those VPCs named?"_

```json
{
  "codemode": "v1",
  "description": "Running instances in internet-facing VPCs, with VPC names",
  "let": {
    "igwVpcs": {
      "source": {
        "gateway": {"call": {"service": "ec2", "operation": "describe-internet-gateways", "paginate": {}}}
      },
      "dedup": [{"ref": "gateway", "path": "Attachments[].VpcId"}],
      "result": {"VpcId": {"ref": "gateway", "path": "Attachments[].VpcId"}}
    },
    "instances": {
      "source": {
        "instance": {"call": {"service": "ec2", "operation": "describe-instances", "paginate": {}}}
      },
      "filter": {"eq": [{"ref": "instance", "path": "State.Name"}, "running"]}
    },
    "vpcByInstance": {
      "forEach": {
        "instance": {
          "from": {"ref": "instances"},
          "do": {
            "source": {"vpc": {"call": {"service": "ec2", "operation": "describe-vpcs", "paginate": {}}}},
            "filter": {
              "and": [
                {"eq": [{"ref": "vpc", "path": "VpcId"}, {"ref": "instance", "path": "VpcId"}]},
                {
                  "eq": [{"ref": "vpc", "path": "VpcId"}, {"ref": "igwVpcs", "path": "[].VpcId"}]
                }
              ]
            },
            "result": {
              "id": {"ref": "instance", "path": "InstanceId"},
              "type": {"ref": "instance", "path": "InstanceType"},
              "vpc": {"ref": "instance", "path": "VpcId"},
              "vpcName": {"ref": "vpc", "path": "tag:Name"}
            }
          }
        }
      }
    }
  },
  "result": {"instances": {"ref": "vpcByInstance"}, "internetFacingVpcs": {"ref": "igwVpcs"}}
}
```

```
wave 1:  igwVpcs ∥ instances       (independent inventory calls)
wave 2:  vpcByInstance             (one logical VPC subquery per running instance)
```

Three things this example is doing at once:

- **The relationship is a query, not an eager correlation stage.** Each `do` asks `DescribeVpcs` for the VPC
  whose `VpcId` equals the outer instance's `VpcId`. That equality is visible in the normal filter algebra and
  lowers to the service's VPC-ID filter (§10.1.1).
- **Logical fan-out does not dictate physical calls.** The optimizer deduplicates and batches instance VPC IDs,
  intersects them with the `igwVpcs` set, issues as few `DescribeVpcs` requests as modeled limits permit, and
  partitions responses back into input-order instance envelopes. A naive one-request-per-instance plan is not
  an allowed lowering.
- **The two outer constraints remain distinct and optimizable.** `State.Name = running` pushes into
  `DescribeInstances`; membership in the internet-gateway VPC set pushes into the dependent `DescribeVpcs`
  query as a multi-value VPC-ID filter. The author states both relationships as ordinary typed expressions and
  never chooses a join strategy.

Because the block has two sinks — `vpcByInstance` and `igwVpcs` — `result` is required rather than defaulted
(§9.1). That is the rule working as intended: the answer genuinely combines them.

### 9.6 Worked example: folding without retaining

Task: _"how many objects and how many bytes in each bucket?"_

```json
{
  "codemode": "v1",
  "description": "Object count and total size per bucket",
  "let": {
    "buckets": {"source": {"bucketRecord": {"call": {"service": "s3api", "operation": "list-buckets"}}}},
    "stats": {
      "forEach": {
        "bucket": {
          "from": {"ref": "buckets", "path": "Name"},
          "do": {
            "source": {
              "object": {
                "call": {
                  "service": "s3api",
                  "operation": "list-objects-v2",
                  "args": {"Bucket": {"ref": "bucket"}},
                  "paginate": {"maxItems": 1000000, "pageSize": 1000}
                }
              }
            },
            "fold": {
              "statsDoAggregate": {
                "objects": {"count": []},
                "bytes": {"sum": [{"ref": "object", "path": "Size"}]},
                "largest": {"max": [{"ref": "object", "path": "Size"}]}
              }
            }
          }
        }
      },
      "onError": "collect"
    }
  }
}
```

No `result`: `stats` is the unique sink, so it *is* the answer. No projection either: the fold names `Size`,
so `Size` is the derived set and every other member of every object entry is discarded per page. A `count`
alone would derive the *empty* set, and the lowerer would request as little as the API allows.

The `fold` is a **product of aggregators** (§10.2): associative and commutative because each component is, so a
million keys fold into three numbers per bucket, page by page, with nothing retained. Adding
`"mean": {"avg": "Size"}` would work identically — `avg` carries a `present` stage that divides once at the
end, which is precisely why the abstraction has three parts rather than two.

## 10. Three algebras

The DSL is not one language. It is three small algebras with disjoint responsibilities, each closed,
each independently validatable, and each lowering to a different part of the execution model:

| Algebra      | Answers                                          | Operations                        | Lowers to                                                           |
|--------------|--------------------------------------------------|-----------------------------------|---------------------------------------------------------------------|
| **Workflow** | what calls, in what order, with what parallelism | `call`, `forEach`, implicit `zip` | the scheduler (§13.1)                                               |
| **Combiner** | how many results become one                      | aggregators over monoids (§10.2)  | a streaming fold (§10.2)                                            |
| **Query**    | which records, and which of their fields         | σ (`filter`) and π (`result`)     | server-side request params + residual client-side predicate (§11.2) |

The separation is what keeps each piece small. Grouping and aggregation are *not* in the query algebra —
they are combiners — so the query algebra never needs `group_by`, `sum`, or arithmetic, which is exactly
the machinery that made JMESPath and JSONata awkward to validate and impossible to push down. Each algebra
is weaker than a general expression language, and the three together cover the use cases.

### 10.1 Workflow algebra: one effect, pure combinators, applicative composition

One source has effects; everything else is pure. Each operator has a signature, and stages type-check by
composition over two shapes — a stream of records, or a single value:

```
source   : Map1[Name, Call | Expr] -> Stream[Record] | Stream[R] | One[T]
call     : Args                    -> Stream[Record]                 -- the only effect
filter   : Expr[Boolean]            -> Stream[R] -> Stream[R]
dedup    : List[Expr]              -> Stream[R] -> Stream[R]
prune    : Set[RefPath]            -> Stream[R] -> Stream[R']        -- derived, not authored (§11.2)
fold     : Map1[Name, Aggregator]   -> Stream[R] -> One[T]
forEach  : Map1[Name, (Expr, Batch?, Block)] -> Stream[Envelope<Name,T,Error>]
```

The stage keys sit directly on a block (§9.1) rather than inside a wrapper, and their composition order is
fixed by the algebra rather than by the document — `filter` → `dedup` → `fold` — so `filter` after `fold` is
not an ordering a plan can express at all.

#### 10.1.1 Correlated traversal: the only v1 correlation form

There is no general join or eager client-correlation stage. Correlation is expressed as a dependent subquery: a
`forEach.do`
source reads the outer traversal alias in its filter.

```json
{
  "forEach": {
    "vpc": {
      "from": {"ref": "vpcs"},
      "do": {
        "source": {"subnet": {"call": {"service": "ec2", "operation": "describe-subnets"}}},
        "filter": {"eq": [{"ref": "subnet", "path": "VpcId"}, {"ref": "vpc", "path": "VpcId"}]}
      }
    }
  }
}
```

The logical meaning is one subnet query per VPC. The physical execution is compiler-owned and must never be a
naive repeated scan.

A **correlation atom** is an `eq` application where one argument is a direct `ref`/`path` under the
`do.source` map key and the other is a direct `ref`/`path` under an enclosing traversal alias. V1 permits a
conjunction of such atoms plus ordinary predicates that do not reference the outer alias. An outer reference in
`or`, `not`, a non-equality relation, or an arbitrary value-function application is rejected as an
unoptimizable correlation.

Two lowering strategies are mandatory:

1. **Call source — server-dependent query.** Every correlation atom must map through the capability table to a
   server-side filter or modeled ID parameter. For one logical element the outer value becomes a request value.
   Where the service accepts multiple values, the optimizer batches outer keys into one request, then partitions
   the response back into the original per-element envelopes by the correlation key. Smithy list limits,
   paginator bounds, and run budgets determine batch size.
2. **Bound-collection source — indexed query.** The runtime builds `groupBy(matchKey, collect)` once and probes
   it for each outer key. Nested-loop O(n×m) execution is forbidden; inability to derive an index is a validation
   error.

For multiple equality atoms, an in-memory source uses a composite hash key. A call source may batch them only
when doing so preserves tuple semantics; otherwise it issues one logical request per element (still with every
atom pushed). It must never turn `(a=1 ∧ b=X) ∨ (a=2 ∧ b=Y)` into `a∈[1,2] ∧ b∈[X,Y]` without repartitioning and
residual verification, because that admits cross-pairs.

#### 10.1.2 Logical tasks versus physical requests

`forEach` still defines logical cardinality, ordering, per-element envelopes, and `onError`. Optimization may
fuse many logical tasks into fewer physical requests, but must preserve those semantics:

- a batched response is partitioned back to the originating outer elements;
- result envelopes remain in traversal input order, not response order;
- one failed batched request contributes the same failure to every logical element in that batch, after normal
  SDK retries;
- truncation is tracked per physical request and reflected on every affected logical envelope;
- `explain` reports both counts.

Example review output:

```text
subnetsByVpc
  logical: 17 dependent VPC queries
  correlation: subnet.VpcId = vpc.VpcId
  lowering: ec2:DescribeSubnets filter vpc-id
  batching: up to 100 VPC IDs per request
  physical estimate: 1-4 API calls
```

A correlated call whose equality cannot be pushed is rejected rather than degraded to repeated client scans:

```text
error  let.subnetsByVpc.forEach.do.filter
       correlation subnet.VpcId = vpc.VpcId cannot be pushed to
       ec2:DescribeSubnets; v1 forbids repeated residual scans
```

This keeps the authored grammar declarative: the plan states the dependent relationship; the interpreter
chooses per-element, batched semijoin, or indexed execution according to the model and capability table.

Pagination is not a separate concept: `call` yields a *stream* of records, and `paginate` bounds that stream.
Pages are an implementation detail of the source, which is why every downstream operator works identically
whether the call paginated or not.

**Composition is applicative, deliberately not monadic.** `forEach`'s body is a fixed expression with holes,
not an arbitrary function of the element. So the *shape* of the computation is static and only the *widths*
are data-dependent. That single property is what buys, rather than merely permits:

- **Parallelism** — applicative composition carries no ordering obligation, so §4.2's DAG parallelism is a
  consequence of the laws, not a claim about our scheduler. Independent bindings are `zip`, which stays
  implicit: independence *is* the absence of references.
- **Estimability** — the entire call graph is known before execution, so `explain` can bound call counts
  (F13). A real monadic bind, where data decides *what kind* of work comes next, would make estimation
  impossible.
- **No branching** — there is nowhere for a conditional to live, which is why §2.3 can list branching as a
  non-goal without it feeling arbitrary.
- **Renderability** — a plan is a value describing effects, so `explain` renders it without running it, and
  no `--dry-run` mode is needed.

Two properties follow from the algebra rather than from implementation care:

1. **Schedule independence.** Independent bindings commute, so any topological order yields the same result.
2. **No hidden dependencies.** A binding's inputs are exactly its references, because references are the only
   way to name another binding's value (§9.2). There is no ambient scope to capture.

### 10.2 Combiner algebra: one abstraction, named instances

A combiner is an **aggregator**: three parts, of which exactly one carries laws.

```
Aggregator[Record, Acc, Out] = ( prepare : Record -> Acc      -- pure map into the monoid
                               , monoid  : Monoid[Acc]        -- the only law-bearing part
                               , present : Acc -> Out )       -- applied once, at the end
```

`count` is not a sibling of `fold`; it is an aggregator, and so is everything else. The named vocabulary is a
derivation table, not a feature list:

| Named form                 | `prepare`                      | `monoid`            | `present`         |
|----------------------------|--------------------------------|---------------------|-------------------|
| `count`                    | `_ ↦ 1`                        | Sum                 | id                |
| `sum(p)`                   | `r ↦ r.p`                      | Sum                 | id                |
| `min(p)` / `max(p)`        | `r ↦ r.p`                      | Semilattice         | id                |
| `collect(p)`               | `r ↦ [r.p]`                    | Free monoid         | id                |
| `avg(p)`                   | `r ↦ (r.p, 1)`                 | Sum × Sum           | `(s,n) ↦ s / n`   |
| `distinctCount(p)`         | `r ↦ {r.p}`                    | Set union           | size              |
| `groupBy(k, agg)`          | `r ↦ { k(r): agg.prepare(r) }` | Map of `agg.monoid` | map `agg.present` |
| `{ "a": agg₁, "b": agg₂ }` | componentwise                  | product             | componentwise     |

Three things this buys that a table of eight named combiners did not:

1. **`avg` explains why `present` must exist.** A mean is not a monoid — two means cannot be combined — but
   `(sum, count)` is, with division at the end. Without a `present` stage, `avg` is either a special case or
   absent. With it, `avg` is a row of data.
2. **Laws are proved once.** Property tests check the monoid laws on the five base monoids (Sum,
   Semilattice, Free, Set-union, Map-of-M). Every derived aggregator inherits associativity and identity,
   because `prepare` is a pure map and `present` runs exactly once at the end.
3. **`groupBy` is an aggregator *combinator*.** It takes an aggregator and returns one over a map, so
   `groupBy(AvailabilityZone, avg(Size))` needs no new machinery. This is the rigorous form of "grouping
   lives in the combiner algebra, not the query algebra" — and it is why the query algebra needs no
   `group_by` and no arithmetic. Algebraically it is a **quotient followed by a fold**: partition the stream
   by the kernel of the key function, then fold each class. Stating it that way (following Agentics'
   quotient construction, §10.5) makes the streaming property obvious — the quotient is computed
   incrementally as keys are encountered, never materialized.

What the laws license, precisely:

- **Associativity** ⇒ fold `prepare`d values incrementally as pages arrive; memory is O(accumulator), not
  O(response). This is why §9.6 counts a million objects in two integers.
- **Commutativity** (where declared) ⇒ fold in *completion* order, not just input order. The free monoid
  (`collect`) is not commutative, so its inputs are folded in traversal order to preserve §4.2.2.
- **Identity** ⇒ an empty stream has an answer without a special case, which is what makes §9.2's
  empty-collection rule a consequence rather than a rule.
- **`present` is quarantined.** It is the only non-associative part, it runs once, and it never participates
  in the incremental fold — so the streaming story stays exact even for `avg`.

### 10.3 Typed pure expressions: one application grammar

Values, value functions, relations, and Boolean connectives are all one typed term algebra:

```jsonc
Expr = Literal                               // the only constant form
     | { "ref": Name, "path": Path? }        // lexical scope; source records are named
     | { "input": Name, "path": Path? }      // caller
     | { "env": Name, "path": Path? }        // runtime
     | { Function: [Expr, …] }                 // function application

Predicate = Expr<boolean>
```

Raw JSON is the **only** constant syntax: `false`, `32`, `"prod"`, arrays, and ordinary objects are constants.
There is no `const` wrapper. Expression objects are recognized only when they exactly match a source node
(`path`, `ref`, `input`, `env`) or a single standard-function key with an argument array.

This creates one unavoidable boundary for object literals: JSON data can itself have an expression-shaped
object such as `{"eq":[…]}`. In Smithy-typed `args`, the expected input shape wins — structures, maps, and
documents remain literal JSON, while expression nodes are accepted at typed leaves. In a pure-expression
context, an exactly expression-shaped object is an expression; if that exact object is intended as data, declare
it as a typed plan `input` and reference the input. This avoids a second constant syntax while preserving the
full JSON domain through inputs.

There is no separate predicate syntax. `and`, `or`, `not`, `eq`, and `casefold` are all typed functions applied
through the same single-key node:

```json
{
  "and": [
    {
      "eq": [{"casefold": [{"ref": "record", "path": "tag:Env"}]}, {"casefold": [{"input": "env"}]}]
    },
    {"present": [{"ref": "record", "path": "OwnerId"}]}
  ]
}
```

This single grammar removes three asymmetries from earlier drafts:

- no `key`/`value` or `left`/`right` distinction — arguments are positional according to a typed signature;
- no one-sided `transform` field or two-sided `caseInsensitive` flag — case-insensitivity is ordinary
  `eq(casefold(a), casefold(b))`;
- no special Boolean syntax versus relation syntax — both are `{function: [arguments]}`.

#### 10.3.1 Functions as implicit pure blocks

Each `Function<Args,Out>` is a typed, pure binding in an immutable standard environment. Algebraically, types
are objects and unary functions are morphisms in a small category: identity is an expression unchanged,
composition is nesting, and associativity lets the normalizer flatten and re-associate applications without
changing meaning.

```text
std.casefold : string -> string
std.size     : list<a> -> number
std.eq       : (a, a) -> boolean
std.and      : nonEmptyList<boolean> -> boolean
```

Thus `{"casefold":[x]}` is an application of the implicit `std.casefold` block, and nested applications are
function composition. The parsed plan stores typed standard-library identities rather than
untyped strings.

This does **not** make an arbitrary lexical block callable as `{"ref":"other"}` in function position in v1. Ordinary blocks
describe streams and effects, have no declared parameter list, and may make AWS calls; inferring parameters
from free references would hide dependencies and make the function algebra open, defeating exhaustive typing
and pushdown classification. A future pure function block could use this same interface only if it has explicit
typed parameters, one total result, no effects, and no free variables. Until then the implicit standard
environment gives the algebraic model without weakening the closed authored vocabulary.

The closed value-function vocabulary:

| Function   | Input                | Output    | Notes                                               |
|------------|----------------------|-----------|-----------------------------------------------------|
| `casefold` | string               | string    | deterministic Unicode case folding                  |
| `age`      | timestamp            | number    | elapsed days at run start; uses immutable `env.now` |
| `date`     | string or timestamp  | timestamp | parse / normalize a timestamp                       |
| `number`   | string or number     | number    | numeric normalization                               |
| `size`     | string, list, or map | number    | consumes the whole value; not pointwise             |
| `cidr`     | string               | cidr      | parse and normalize a network                       |
| `cidrSize` | cidr                 | number    | prefix length; composes with `cidr`                 |

Scalar functions lift pointwise over many-valued paths; `size` consumes the collection itself. Therefore
`cidrSize(cidr(path("Cidrs[]")))` has type `Many<number>`, while `size(path("Cidrs[]"))` has type
`One<number>`. Function evaluation failures are typed value errors handled by the containing block's
`onError`; they never silently become a failed predicate.

The same `Expr` grammar is reused by predicate arguments, projection map values, aggregator inputs and group
keys, correlated filter arguments, `dedup` keys, and argument leaves. This is the algebraic payoff: obtaining and
transforming a value is one concept rather than syntax owned by filters.

#### 10.3.2 Function families and arity

The expected type determines which closed function family a single application key belongs to:

| Family    | Functions                                     | Signature                                  |
|-----------|-----------------------------------------------|--------------------------------------------|
| value     | `casefold age date number size cidr cidrSize` | one value → one value                      |
| string    | `eq ne startsWith endsWith contains`          | two compatible values → boolean            |
| numeric   | `lt lte gt gte`                               | two numbers → boolean                      |
| existence | `present absent`                              | one value → boolean                        |
| set       | `setEq setNe subset`                          | two many-valued expressions → boolean      |
| Boolean   | `and or` / `not`                              | non-empty booleans / one boolean → boolean |

Arity is therefore part of the function type, not a parallel grammar production: `{"present":[a,b]}`,
`{"eq":[a]}`, and `{"not":[a,b]}` fail while parsing into `ValidPlan`.

#### 10.3.3 Boolean normalization

A filter is any `Expr<boolean>`. `and`/`or` argument arrays are n-ary but commutative, so their order carries no
meaning and the normalizer sorts them canonically. Boolean expressions nest arbitrarily because an application
argument is another expression.

Normalization:

1. Push `not` applications to relation applications by De Morgan, replacing function keys where a complement exists:
   `eq↔ne`, `lt↔gte`, `gt↔lte`, `present↔absent`, `setEq↔setNe`. Negated `startsWith`, `endsWith`, `contains`,
   or `subset` remains wrapped and residual-only.
2. Flatten nested applications of the same associative Boolean function and remove one-argument `and`/`or`.
   An `or` of `eq(path, literal)` terms with the same path collapses to one `eq` whose literal is an alternatives
   array — the server's `Values` form (§11.2).
3. Produce conjunctive normal form. CNF growth is capped; beyond the cap the original expression is evaluated
   client-side, so normalization is an optimization rather than a correctness requirement.

#### 10.3.4 Relation and cardinality semantics

A many-valued expression compared to a scalar by a scalar relation matches if **any** element satisfies it. A
literal array on the second argument means alternatives for `eq`, `ne`, and string relations: `eq` is any-of;
`ne` is none-of. Ordering functions reject alternatives because `lt(x,[10,20])` degenerates to `lt(x,20)`.

When both arguments are many, scalar relations are existential on both sides — their sets intersect. Use
`setEq`, `setNe`, or `subset` for relations over whole projections. A one-element constant array normalizes to
its element; a literal empty array warns as dead code (`eq` false, `ne` true).

#### 10.3.5 Typing and pushdown boundary

Applications type-check bottom-up. For example, `{"gt":[{"size":[{"ref":"record","path":"Tags"}]},"large"]}`
is rejected because the arguments become number and string;
`{"eq":[{"casefold":[{"ref":"record","path":"Name"}]},{"casefold":[{"input":"name"}]}]}` is valid because
both become strings.

Pushdown remains conservative:

- the ordinary pushable form is a relation application whose first argument is a direct `ref`/`path` to the
  block's `source` record name, whose function is supported by the operation, and whose second argument is a record-independent expression evaluable before
  the request;
- record-independent function applications may be pre-evaluated and do not inherently prevent pushdown;
- a function application around the record-dependent first argument makes the relation residual unless the
  capability table declares an exactly equivalent server function;
- therefore case-folded equality is residual against case-sensitive EC2 filters, with no special rule.

Paths have model-derived types, external values have declared types, every standard function has a closed
signature, and the result type of every application is known before execution. Invalid compositions fail while
constructing `ValidPlan`, before any AWS call.

### 10.4 Pure transforms: expressions and products

`Expr` is the atomic pure transform: a raw constant, a value source, or a typed standard-function application.
A `PureTransform<A,B>` closes expressions under named products, giving the structural transformation used by
every `result`:

```text
PureTransform<A,B> = Expr<A,B>
                   | Record<Map<Name, PureTransform<A,*>>>
```

Wire forms:

```jsonc
"result": {
  "VolumeId": {"ref": "volume", "path": "VolumeId"},
  "Size": {"ref": "volume", "path": "Size"}
}

"result": {
  "id":         {"ref": "volume", "path": "VolumeId"},
  "normalized": {"casefold": [{"ref": "volume", "path": "tag:Name"}]},
  "region":     {"ref": "region"},
  "kind":       "volume"
}

"result": {
  "summary": {
    "count": {"ref": "stats", "path": "objects"},
    "bytes": {"ref": "stats", "path": "bytes"}
  }
}
```

There is deliberately no list shorthand for field selection. A raw array is a constant everywhere; making
`["VolumeId","Size"]` mean paths in `result` would create a second meaning for arrays and force `const` back
into the grammar. The explicit record above is longer but obeys the one-representation rule.

An object matching an expression source (`path`, `ref`, `input`, `env`) or a single standard-function key with
an argument array is an `Expr`; any other object is a `Record`. `{"record":{…}}` explicitly disambiguates the
rare collision — for example, to emit one field literally named `eq` whose value is an array. A raw literal
object that exactly matches an expression node is supplied through a typed `input`, as §10.3 explains.

This is the top-level pure transform type shared by all three block roles:

- Traverse has no outer `result`; `do.result` shapes each element's value, and a downstream Express block
  shapes envelope records using its explicit source alias.
- Express applies it element-wise to the stream, or once to the value after `fold`.
- Assemble evaluates it against lexical scope; every field access is an explicit `ref`, and values may also
  come from constants, `input`, or `env`.

The same algebra explains the shapes used elsewhere rather than creating parallel mini-languages:

| Context                                  | Pure fragment it accepts                                                                             |
|------------------------------------------|------------------------------------------------------------------------------------------------------|
| predicate                                | one `Expr<boolean>` (§10.3)                                                                          |
| aggregator preparation and `groupBy` key | one `Expr`; the monoid folds its outputs (§10.2)                                                     |
| correlated filter arguments, `dedup` key | one `Expr`                                                                                           |
| `result`                                 | any `PureTransform`, including recursive record products                                             |
| argument leaf                            | one expression whose references are already in scope; the same block's `source` key is not yet bound |

Algebraically, `Record` is the product of its member transforms. Product is associative up to field naming; an
empty record is the unit; evaluation is pointwise and pure. This gives Assemble a positive definition rather
than "the role with neither `forEach` nor `source`": it is a `PureTransform` evaluated in an environment extended
by local `let` bindings. If `result` is omitted and `let` has a unique sink, the normalizer inserts
`{"ref":"<sink>"}`, so even the default is explicit after normalization.

There is no arbitrary lexical function application here. Computation is the closed typed `Function` environment
(§10.3.1), whose names resolve from the implicit standard environment. This keeps pure transforms
schema-validatable and their record-dependent parts classifiable for pushdown.

### 10.5 Prior art

The decomposition is not novel, which is the point:

- **Relational algebra** — σ/π/⋈/γ is exactly this split, with γ (grouping) separated from σ/π, which is
  the separation the steering above identifies.
- **Apache Calcite** — relational algebra as an IR, with rule-based pushdown into heterogeneous adapters.
  Our lowering is a much smaller instance of the same pattern.
- **Substrait** — a standardized, language-independent algebraic plan IR meant to be produced by one
  system and consumed by another; direct precedent for "algebra as the interchange format".
- **Smithy selectors** — a closed, comparator-based DSL for querying a *model* graph. Not usable as our
  data-filter surface (it matches shapes, not records), but the source of our comparator and
  projection semantics (§10.3), and the tool that *derives* our metadata tables (§15.8). The layer
  distinction is worth stating once so it is not re-litigated: selectors query the schema; `filter` queries
  the data described by the schema.
- **Cloud Custodian** — the closest applied precedent: `{key, op, value, value_type}` predicates with
  nestable `and`/`or`/`not`, a separate `reduce` filter for grouping/sorting/limiting, and a JSON Schema
  generated from the registries. A decade of use over the AWS API surface.
- **Agentics / logical transduction algebra** (Gliozzo et al., IBM, arXiv:2508.15610) — argues that agentic
  systems are fragile precisely because they *lack algebraic structure*, and supplies one: typed transductions
  between schemas, composed with products and quotients, with asynchronous map/reduce falling out of the
  algebra. Independent arrival at this spec's thesis, in a different domain (LLM-to-LLM rather than
  API-to-API). Its *quotient* construction is the cleaner account of grouping: `groupBy(k, agg)` is a
  quotient by the kernel of `k` followed by a fold (§10.2).
- **SPL** (Gong, arXiv:2607.07727) — a declarative language whose deterministic half (`SOLVE`, `ASSERT`) and
  probabilistic half (`GENERATE`, `EVALUATE`) compose in one specification, with the mode boundary visible in
  source. What transfers is the observation that a declarative specification is what makes optimizer rewrites
  possible at all, and that probabilistic and deterministic modes should have visibly different types. Worth
  noting a place where our position is *stronger* than theirs: SPL concedes that "SQL optimization rests on
  relational algebra equivalences over deterministic set operations, whereas GENERATE samples from a probability
  distribution", so its optimizer must stay at the workflow level. Our v1 plans contain no probabilistic step,
  so algebraic rewrites — CNF normalization, predicate pushdown, and field pruning — are available on the same
  footing as in a relational optimizer.
- **Free applicative / free monad interpreters** — the standard account of "programs as values,
  interpreted later", and the reason `zip` is parallelizable while bind is not.
- **Monoid-based aggregation** (Spark/Flink combiners, Cassandra CRDTs) — associativity as the licence for
  partial aggregation.

### 10.6 What is deliberately absent

| Absent                        | Why it is not needed                                                                                                      |
|-------------------------------|---------------------------------------------------------------------------------------------------------------------------|
| Arithmetic                    | aggregates are combiners; relative time is the `age` value function or an `env` binding (§12)                             |
| Grouping in queries           | the `groupBy` aggregator combinator (§10.2)                                                                               |
| String building               | argument values are literals or refs; nothing is templated                                                                |
| User-defined functions        | would make the algebras open, defeating validation and pushdown                                                           |
| Conditionals / branching      | empty fan-out covers the common case (§9.2); real branching is a non-goal                                                 |
| Sorting / limiting in queries | `paginate` bounds volume; ordering is a projection concern, added as a `sort`/`limit` operator only if examples demand it |

Every entry is a capability *moved* rather than lost — with one exception. Per-item string surgery (ARN
splitting, truncation) has no home in any of the three algebras, and stays with the summarizing model.

## 11. Pagination

### 11.1 Policy

```jsonc
"paginate": {
  "maxItems": 5000,     // stop after this many records; capped by the run budget
  "maxPages": 1,        // stop after this many requests
  "pageSize": 1000      // hint; clamped to the operation's legal range
}
```

Each bound is optional, and an absent bound means *unbounded, subject to the run budget* (§13.3). An earlier
draft had a `mode` enum — `all` / `first` / `limit` — which this replaces:

| Was                                  | Now                               |
|--------------------------------------|-----------------------------------|
| `{"mode": "all"}`                    | `{}`, or omit `paginate` entirely |
| `{"mode": "first"}`                  | `{"maxPages": 1}`                 |
| `{"mode": "limit", "maxItems": 200}` | `{"maxItems": 200}`               |

That removes an enum and the conditional rule that `maxItems` was required for one of its members, and it is
strictly more expressive: `maxPages: 2` had no spelling before.

- Whether an operation paginates, and what its tokens/limit keys are, comes from the shipped service
  models (the CLI's existing paginator configuration) — the plan does not describe pagination
  mechanics, only policy.
- An unbounded `paginate` is still bounded by the run budget for total items and bytes (§13.3). Hitting either
  the plan's bound or the budget stops that binding and records a truncation; it does not error.
- Derived pruning runs per page, before retention. Combined with `fold`, an unbounded paginated listing is
  memory-safe.
- Pagination is sequential per task (token chaining) and parallel across fan-out tasks.
- **Truncation is loud.** Any truncated binding appears in `diagnostics.truncated[]` with its name,
  the reason (`maxItems` / `bytes` / `time`), and the count retrieved. Status becomes `partial`.
  `help errors` instructs the agent to state incompleteness in its answer rather than presenting a
  truncated count as a total.

### 11.2 Lowering σ: what gets pushed to the service

The plan states a predicate against the response shape. The interpreter decides where it runs. This is the
one place the design accepts real implementation complexity, because the alternative is making every agent
learn several hundred filter dialects.

**Deriving the field set.** Before any lowering, the demanded fields of each binding are computed by walking
every `Expr` in the document: a `ref`/`path` names both a value and a field, while `input`/`env` sources
contribute dependency and type information plus any nested path. Function applications recurse through their
arguments. This covers filters (including outer correlation references), aggregator inputs and group keys, `dedup`,
argument leaves, and `result`. Because expressions are structured data (§10.3), this is a traversal rather than
an analysis — no over-approximation and no guessing.

Three properties matter:

- **Nobody authors a projection.** The set is complete by construction: a field a consumer names is in it, and
  a field nothing names cannot be needed. This is the standard column-pruning pass of a query planner
  (Calcite's field trimming, Spark's column pruning), available to us cheaply because the plan is data.
- **It prunes and it pushes.** The set bounds what is retained per page, and where the API supports projection
  (`ProjectionExpression`, `AttributesToGet`, `--include`) it is also what gets requested — so pruning reduces
  bytes on the wire, not just bytes in memory. A `fold: count` derives the *empty* set and requests the minimum
  the API allows.
- **A block's `result` is a consumer.** Terminal shaping is what keeps the answer small (§13.3), and it is the
  one projection that cannot be derived, since the consumer is outside the plan.

`dedup: []` means **the derived set** — deduplicating on fields nothing observes would be a distinction without
a difference. Explicit keys are always available when a narrower notion of identity is wanted.

**The lowering algorithm.**

1. Normalize the `filter` lattice to conjunctive normal form (§10.3). The result is a *set* of clauses.
2. For each clause, inspect its typed application and consult the **capability table**. The ordinary pushable
   atom is a supported relation whose first argument is a direct `ref`/`path` to the block's sole `source` record
   and whose second argument is a
   record-independent expression evaluable before the request. A function wrapped around the
   record-dependent first argument makes the atom residual unless the table declares an exactly equivalent
   server operation.

   A clause that is a **single atom** is the easy case. A clause that is a **disjunction** pushes only when
   the API can express that particular disjunction, and its normalized operand shape decides:

   | Clause | Pushable? |
   | --- | --- |
   | `tag:Env eq prod` | yes, if the table allows the key and operator |
   | `tag:Env eq prod` **or** `tag:Env eq staging` | yes — same key, so it becomes one filter with two `Values` (`Filters` are OR-within-values) |
   | `tag:Env eq prod` **or** `InstanceType eq t3.micro` | no — spans two keys, and `Filters` are AND-across-filters |
   | `not startsWith …` | no — negation of a comparator with no complement (§10.3) |

   The validator warns on the third shape, because a cross-key disjunction silently turns a cheap query into a
   full scan and the author may not have intended a disjunction at all.
3. Pushable clauses become request parameters — `Filters`, `TagFilters`, `Prefix`, `FilterPattern`, key
   conditions, whatever this API calls them. Inside `forEach.do`, equality clauses that reference the outer
   traversal alias are correlation atoms: call sources must push them and may batch their values; bound sources
   must build an index (§10.1.1).
4. Everything else becomes the **residual predicate**, evaluated in-process per page, before pruning and
   `fold` (§4.1) so residual filtering still reduces retained bytes.
5. The derived field set is lowered the same way where the API supports projection; otherwise pruning happens
   in-process, per page, before retention.

**Conservative by construction.** Unknown ⇒ residual. A filter name is never guessed. A clause whose
server-side semantics differ from client-side semantics is never pushed, even when a filter of that name
exists.

**Normalization is an optimization, never a correctness requirement.** CNF conversion of a
disjunction-of-conjunctions is exponential in the worst case, so the lowerer caps the clause count it will
produce. On exceeding the cap it abandons normalization for that predicate and evaluates the *original* tree
client-side — slower, but identical in result. This matters because it means a pathological predicate degrades
performance rather than failing, and no amount of nesting can produce a wrong answer or an error the author
cannot understand.

**Why the caution.** A wrong pushdown returns a wrong answer with no error, which is the worst failure mode
in this design. The specific traps:

| Trap                                                           | Consequence if pushed naively                             |
|----------------------------------------------------------------|-----------------------------------------------------------|
| EC2 filter values are case-sensitive and support `*` wildcards | `eq` silently changes matching semantics                  |
| Most APIs support only equality and set membership             | `gt`/`lt` must never lower to a filter                    |
| `Filters` compose AND-across-filters, OR-within-values         | any other boolean shape must not lower to them            |
| Some APIs ignore unrecognized filter names                     | a bad lowering returns *more* data and looks like success |
| absent vs null vs empty string                                 | `present`/`absent` rarely match server semantics          |

**Visible, not magic.** `explain` prints the split, so the reviewer sees exactly which predicates cost
bytes:

```
inst  ec2:DescribeInstances  (fan-out over 17 regions)
      pushed:    --filters Name=tag:Env,Values=prod Name=instance-state-name,Values=running
      residual:  none
      est. 17-51 calls

vols  ec2:DescribeVolumes
      pushed:    --filters Name=encrypted,Values=false
      residual:  Size > 100          (no server-side filter for Size)
      est. 1-8 calls, all pages fetched
```

`diagnostics.pushdown` reports the same in the envelope, so a cost surprise is observable rather than
inferred. `--require-pushdown` fails a plan that would page an entire collection to satisfy a residual
clause — useful in automation, where an accidental full scan is worse than an error.

**Keeping it honest.** Each capability-table entry carries a differential test: for recorded fixtures,
server-side and client-side evaluation of the same clause must return identical sets. An entry without a
passing differential test does not ship, which makes the table's growth self-limiting and its correctness
mechanical rather than reviewed.

**Not the CLI's `--query` mechanism.** `--query` runs in the output formatter *after* `build_full_result()`
joins every page (`awscli/formatter.py`), so lowering residuals to `--query` would reintroduce full
materialization. Residuals are evaluated in-process, per page. `--filters`/`--query` are the *review
vocabulary* of `explain`, not the execution path.

## 12. Runtime-provided and platform values

The expression algebra has no operation for reading a clock, entropy source, process environment, credential
store, or host. Values outside the plan enter through one of three deliberately different channels: an exposed
immutable run environment, model-driven request defaults, or execution-internal state that is never exposed.

### 12.1 Exposed immutable run environment

At run start the interpreter captures one immutable `env`, referenced as `{"env":"<name>"}` and echoed in the
result diagnostics when used:

| Binding                      | Value                                                             |
|------------------------------|-------------------------------------------------------------------|
| `now`                        | ISO-8601 timestamp, run start                                     |
| `nowMillis`                  | epoch millis, run start                                           |
| `today`                      | `YYYY-MM-DD`, run start, UTC                                      |
| `ago.<span>`                 | run start minus `m5 m15 h1 h6 h12 h24 d7 d30 d90`, as ISO-8601    |
| `agoMillis.<span>`           | the same instants as epoch millis                                 |
| `startOf.<unit>`             | boundary instants: `hour day week month quarter year` (UTC)       |
| `runId`                      | one UUIDv4 correlation value for this invocation                  |
| `region`                     | effective default region                                          |
| `accountId`, `arn`, `userId` | caller identity, resolved lazily through STS only when referenced |
| `partition`                  | `aws`, `aws-cn`, `aws-us-gov`                                     |

Most relative-time predicates need no binding because the `age` function covers them:

```jsonc
{"lt": [{"age": [{"ref": "instance", "path": "LaunchTime"}]}, 32]}
```

Bindings are for concrete instants in request arguments:

```jsonc
"args": { "StartTime": { "env": "ago", "path": "h24" }, "EndTime": { "env": "now" } }
```

The span list is deliberately enumerated. It covers common logs/metrics/cost windows and lets a reviewer know
the exact window without evaluating arithmetic. An unusual fixed instant is a plan `input`, which is more honest
than adding date arithmetic to the language.

`runId` is the only generic entropy exposed. It supports correlation and, where an API requires a caller-chosen
unique suffix rather than an idempotency member, may be passed directly or supplied as an input. There is no
random-number function or stream of UUIDs: repeatable per-element naming should derive from domain data, and
truly caller-controlled randomness belongs in `inputs`.

### 12.2 Model-driven request defaults

Some request values are platform responsibilities rather than plan inputs. **Idempotency tokens are the primary
case, but their scope is a logical request, not a plan run.** Reusing one token for different fan-out elements
whose parameters differ can cause deduplication of the wrong call or an `IdempotentParameterMismatch`. The
correct behavior is:

- one token per logical call task;
- the same token for every retry of that task;
- different tokens for different tasks, including fan-out elements.

Botocore already implements this from the service model. `OperationModel.idempotent_members` exposes input
members carrying the Smithy `idempotencyToken` trait, and the registered `before-parameter-build` handler injects
a UUID when a member is absent. Generated CLI documentation states: "This field is autopopulated if not
provided." SDK retries reuse the materialized request and therefore its token.

Code Mode must preserve that contract rather than add a grammar source:

1. Materialize each logical call task once.
2. Invoke botocore once and let its retry handler own attempts.
3. If the executor ever retries above botocore, reuse the already materialized request and token rather than
   rebuilding parameters.

If a caller needs a token stable across **separate Code Mode invocations**, it declares a normal plan `input`
and references it explicitly in `args`; botocore preserves a supplied member. Cross-invocation identity is thus
caller-owned, consistent with the stateless runtime.

Other model/client defaults follow the same rule: request checksums, content hashes, signing timestamps,
endpoint-derived headers, and SDK invocation identifiers are calculated by botocore and are not expression
sources. `explain` may say that a modeled default is automatic, but the plan does not provide it.

### 12.3 Execution-internal and intentionally unavailable values

| Value                                         | Visibility                | Reason                                                      |
|-----------------------------------------------|---------------------------|-------------------------------------------------------------|
| pagination cursor (`NextToken`, `Marker`)     | internal                  | the paginator owns the token chain (§11.1)                  |
| retry attempt / backoff state                 | diagnostics only          | exposing it would let results depend on transient failures  |
| service request IDs                           | response diagnostics only | useful for support, not request construction                |
| credentials, session tokens, signing keys     | never                     | secrets and ambient authority                               |
| endpoint URL, TLS settings, CA bundle         | invocation policy only    | trust/transport cannot be redirected by a plan (§9.3.2)     |
| arbitrary process environment or files        | never                     | violates the no-ambient-authority goal (G10)                |
| generic clock / random calls during execution | never                     | all visible run values are captured once in immutable `env` |

This taxonomy is the decision rule for future platform values: expose a value only when it changes the *question*
and must be authored against; leave model-driven request correctness to botocore; keep transport, secrets, retry,
and protocol machinery out of the language.

Every referenced `env` value is echoed in `diagnostics.env`, so a reviewer can see what "24 hours ago" meant
and a caller can reproduce it by pinning that value as an input. Nothing is persisted. `accountId`/`arn`/`userId`
are the only exposed values whose lazy resolution makes an additional AWS call, and that call appears in the
call accounting.

## 13. Execution

### 13.1 Scheduling

**Graph construction** (in the normalizer, before any call):

1. Resolve every `{"ref":…}` through the symbol table (§9.2.2). A reference whose symbol provenance is a
   `let` declaration adds an edge to that binding; references to local `source`, `forEach`, or `fold` symbols
   stay within the current node. Because expressions are data, extraction is a traversal rather than language
   analysis — no over-approximation and no missed edges. Nested blocks carry nested DAGs, and a name local to a
   sibling block is rejected during resolution rather than at runtime.
2. Classify effectful `Call` nodes as reads or mutations (§13.4). Add edges from every AWS read call to every
   mutation call that is not already ordered by data, reject any AWS read call transitively dependent on a
   mutation, and leave pure bindings governed solely by their references.
4. Reject cycles (validation error, with the cycle path in the diagnostic). Reject references to
   undefined ids.
5. Compute the topological levels used by `explain` for its wave display.

**Execution.** A single work-stealing pool serves the whole run; there is no per-binding pool. A task is
`(binding, traversal element | none, page cursor)`. A binding becomes *ready* when all predecessors have
bound; ready tasks enter the same queue, so the scheduler naturally overlaps the tail of a wide traversal with
the start of an independent binding instead of idling at wave boundaries. Waves are a
*presentation* concept for review; the executor does not barrier between them unless a mutating step
requires it.

**Governor.** Concurrency is bounded globally (default 8, ceiling 32), then per service, then per
`(service, region)` endpoint. `--max-concurrency` may lower the global bound and may raise it only to
the ceiling. Because independent steps overlap, the global bound is what protects the account from
`Σ(fan-out widths)`; per-service caps stop one noisy service from starving the rest.

**Retries and backpressure.** Retries are the SDK's job: standard/adaptive retry mode with jittered
backoff for throttling and transient 5xx; counts are reported. On sustained throttling the governor
lowers the effective concurrency for the affected service rather than continuing to hammer, which is
also why concurrency is not a plan-level knob — the right value is only knowable at run time.

**Clients.** One per `(service, region, credential-scope)`, created eagerly when the graph is built and
reused across tasks: client *creation* is the thread-unsafe part, client *use* is not.

**Cancellation.** When a step with `onError: "fail"` fails, the run stops admitting new tasks,
in-flight tasks are cancelled where the transport allows, and steps that never started are reported as
`cancelled` rather than silently missing — so a reviewer can tell "did not run" from "ran and returned
nothing". Steps that had already completed keep their bindings, and `result` is evaluated on a
best-effort basis with status `partial` if it succeeds.

### 13.2 Error taxonomy and policy

| Class         | Example                                      | Default handling                                      |
|---------------|----------------------------------------------|-------------------------------------------------------|
| Plan invalid  | unknown operation, bad expression            | Reject before execution; exit 252                     |
| Configuration | no credentials, no region, unknown profile   | Reject before execution; exit 253                     |
| Authorization | `AccessDenied`, SCP denial                   | Per `onError`; typically `collect` in fan-out         |
| Throttling    | `Throttling`, `RequestLimitExceeded`         | Retry with backoff, then per `onError`                |
| Transient     | 5xx, timeouts, endpoint unreachable          | Retry, then per `onError`                             |
| Not found     | `NoSuchBucket`, `InvalidInstanceID.NotFound` | Per `onError`; `skip` is often right                  |
| Expression    | type error, budget exceeded                  | Binding failure per `onError`; never a host exception |
| Budget        | max calls/bytes/time exceeded                | Stop cleanly, mark `partial`, return what exists      |
| Cancelled     | sibling step failed with onError=fail        | Reported as `cancelled`, not as an error              |
| Interrupt     | Ctrl-C                                       | Cancel in-flight, return partial, exit 130            |

`onError` values: `fail` (abort the run; default for Express blocks), `skip` (drop the failed element;
default for Traverse blocks), `collect` (retain the error as data alongside successes). `--on-error
strict` overrides every step to `fail` for use in automation.

### 13.3 Budgets (hard, runtime-owned)

| Budget                   | Default                              | Flag                     |
|--------------------------|--------------------------------------|--------------------------|
| Total API calls          | 500                                  | `--max-calls`            |
| Concurrency              | 8 (ceiling 32)                       | `--max-concurrency`      |
| Items retrieved per step | 100 000                              | `--max-items`            |
| Result envelope bytes    | 256 KB                               | `--max-result-bytes`     |
| Field pruning            | on                                   | `--no-prune` (debugging) |
| Wall clock               | 300 s                                | `--timeout`              |
| Expression evaluations   | node/size/time bounds per evaluation | not user-tunable         |

Exceeding a budget produces a clean `partial` result with a diagnostic, never a truncated stream or a
crash. `explain` estimates calls before running: exact for non-fan-out steps, `width × pages` bounded
for fan-out (width known when it depends only on literals or a preceding step's cardinality; a range
otherwise).

### 13.4 Capability gates

- **Read-only by default.** Every referenced operation is classified from the service model
  (HTTP method, operation name prefix, documented side effects) plus a curated override table. A plan
  containing a non-read operation fails validation unless `--allow-mutations` is passed, and every
  such operation is listed at the top of `explain`.
- **No filesystem, no processes, no sockets.** The interpreter's only effect is invoking the operations
  the plan names. It writes nothing, anywhere — not even a log or a cache — so there is no state to
  leak, corrupt, grow unbounded, or require cleanup. There is no plan-level construct for reading a file,
  writing output to a path, or shelling out. `--cli-input-*`, `--outfile`, `file://` and `fileb://`
  parameter expansion are **not** available inside plans.
- **Credentials.** Normal resolution chain. `profile` overrides per call are rejected unless
  `--allow-profile-override` is passed, because cross-account fan-out is a materially different blast
  radius than the plan text suggests.
- **Region overrides** are allowed freely (they are the point) but every distinct region contacted is
  listed in `explain`.
- **Approval.** Interactively, `run` renders the explanation and asks for confirmation when the plan
  mutates, exceeds a call threshold, or spans multiple credential scopes. `--yes` skips the prompt;
  agent harnesses supply it explicitly, having shown the plan themselves.

### 13.5 Reviewing a plan: `explain`

`explain` is the review surface for both the human and (per §20.5) potentially the harness. It renders
the derived graph as waves, which is the only place the parallelism becomes visible — the plan text
itself never mentions it.

```
$ aws codemode explain --plan inventory.json
VPC and subnet inventory per enabled region
2 bindings   2 waves   read-only   no mutations   result: perRegion

wave 1  (1 binding)
  regions    ec2:DescribeRegions             us-east-1   ~1 call
             pushed:  --filters Name=opt-in-status,Values=opt-in-not-required,opted-in
             fields:  RegionName                    (derived from perRegion.forEach.region.from)

wave 2  (1 binding)
  perRegion  forEach.region from regions.RegionName   ~17 elements
             do (per element):
               wave 1
                 vpcs          source.vpc call ec2:DescribeVpcs
                               fields: VpcId, CidrBlock (derived from dependent query/result)
               wave 2
                 subnetsByVpc  forEach.vpc from vpcs
                   source:     subnet = ec2:DescribeSubnets record
                   logical:    one subnet query per VPC
                   correlation: subnet.VpcId = vpc.VpcId
                   lowering:   server filter vpc-id, batched by modeled limits
                   physical:   1-ceil(VPCs/100) calls per region
               result: { region, vpcs: subnetsByVpc }

inferred symbols:
  regions        many ec2.Region
  region         one  ec2.Region             source record
  perRegion      many Envelope<region, …>
  vpc            one  ec2.Vpc                traversal/source record (nested scopes)
  subnet         one  ec2.Subnet              dependent source record
  subnetsByVpc   many Envelope<vpc, many SubnetSummary>

regions contacted: 17 (derived from regions)
credential scopes: default profile
logical dependent queries: sum(VPCs across regions)
physical estimate: 1 + 17 x (1 DescribeVpcs + batched DescribeSubnets)   budget: 500
estimated result:  small (projected fields only)
```

What a reviewer can answer from this that they cannot answer from a stream of individual commands: does
anything mutate (no), how wide does this go, how many calls it could possibly make, which bindings overlap,
and which binding is the answer. Nesting is rendered by indentation,
so a nested block's waves appear in place rather than requiring a second command. `--output json` emits the
same tree for programmatic review.
`explain` is also the dry run (§8): it resolves the estimate as far as static information allows and
contacts nothing.

## 14. Result envelope

```jsonc
{
  "status": "ok" | "partial" | "failed",
  "runId": "…",              // correlation id for this process only; not a lookup key
  "result": <the plan's result expression>,
  "diagnostics": {
    "calls": 37,
    "retries": 2,
    "durationMs": 4120,
    "waves": [["openSgs", "openVols", "users"], ["mfa"]],
    "maxObservedConcurrency": 8,
    "cancelled": [],
    "regions": ["us-east-1", "…"],
    "pushdown": [ { "binding": "vols", "pushed": ["encrypted=false"], "residual": ["Size > 100"] } ],
    "env": { "now": "2026-08-27T20:59:00Z", "accountId": "…" },
    "truncated": [ { "binding": "stats", "reason": "maxItems", "retrieved": 100000 } ],
    "errors":    [ { "binding": "inst", "element": "ap-east-1", "code": "AuthFailure", "message": "…" } ],
    "warnings":  [ { "binding": "inst", "code": "ClientSideFilter", "message": "…" } ]
  }
}
```

Because nothing is persisted (G13), this envelope *is* the record: the resolved `env` bindings that
made the run reproducible, the call and error accounting, and the truncation flags. A caller that wants
history keeps the envelope alongside the plan it sent; the CLI does not keep either for it.
`diagnostics.env` echoes only the bindings the plan actually referenced.

Result on stdout, progress on stderr. Progress lines mirror the prototype's trace, which proved
legible in practice:

```
[codemode] plan: 2 steps, est. 18–52 calls, read-only
[codemode] step 'regions' -> ec2:DescribeRegions (us-east-1)
[codemode] step 'regions' <- 1 page, 17 items
[codemode] step 'inst' fans out over 17 item(s) (concurrency=8)
[codemode] step 'inst' [eu-west-1] <- 2 pages, 34 items
[codemode] step 'inst' [ap-east-1] !! AuthFailure (collected)
[codemode] done: partial, 21 calls, 3.9s
```

With independent steps, the trace interleaves, and each line is tagged with its step id so the
interleaving stays readable:

```
[codemode] plan: 4 steps, 2 waves, est. 3–13+N calls, read-only
[codemode] wave 1: openSgs, openVols, users (3 steps in parallel, concurrency=8)
[codemode] step 'users'    -> iam:ListUsers
[codemode] step 'openSgs'  -> ec2:DescribeSecurityGroups
[codemode] step 'openVols' -> ec2:DescribeVolumes
[codemode] step 'users'    <- 1 page, 42 items
[codemode] step 'mfa' ready; fans out over 42 item(s)
[codemode] step 'openSgs'  <- 2 pages, 11 items
[codemode] step 'openVols' <- 6 pages, 219 items
[codemode] done: ok, 51 calls, 2.1s
```

Note `mfa` starting while `openVols` is still paginating: readiness is per step, not per wave (§13.1).

Exit codes follow `aws help return-codes`: `0` ok, `1` partial (some steps/items failed or were
truncated), `130` interrupted, `252` invalid plan, `253` invalid configuration/credentials, `254`
service errors caused failure, `255` other.

## 15. Schema service

### 15.1 Constraint: there is no model inside the CLI

The CLI cannot embed, rank semantically, or "understand" a query. Retrieval must be deterministic
lexical matching over the service models already on disk. That is fine for precision and terrible for
recall: an agent asking for `"vms"` or `"list my servers"` gets nothing from a substring match on
`DescribeInstances`.

The failure this creates is exactly the one Code Mode exists to eliminate. A lookup returning zero
results costs a full LLM turn to rephrase, and three bad guesses in a row is worse than the multi-turn
baseline it replaces. So the design goal is not ranking quality, it is **turn elimination**:

- **N1 — Never return zero results** when any interpretation of the query could match. Relax instead
  of failing, and say which relaxation matched (§15.4).
- **N2 — Answer many questions per invocation.** One call, many queries, mixed kinds (§15.2).
- **N3 — Collapse Tier 1 into Tier 2 whenever the top hit is unambiguous.** Do not make the agent ask
  "which operation?" and then "what is its shape?" as two turns (§15.5).
- **N4 — Accept the agent's first guess.** Most of the time the model already knows the operation name
  from pre-training and does not need keyword lookup at all; the command must reward that by correcting
  near-misses in place rather than rejecting them (§15.5).
- **N5 — Tell the agent how to query, in help.** Concise noun phrases, one query per fact needed, all
  in one invocation. A worked multi-query example in `help schema` is worth more than any ranking
  improvement.

### 15.2 One command, two kinds of query

There is a single lookup command. It takes any number of queries, and each query is one of two kinds,
distinguished by **syntax, not by intent**:

| Query looks like                                                       | Treated as                 | Returns                                    |
|------------------------------------------------------------------------|----------------------------|--------------------------------------------|
| `ec2:DescribeInstances`, `ec2:describe-instances`, `DescribeInstances` | an operation **reference** | that operation's signature                 |
| anything else (`unencrypted volumes`)                                  | **keywords**               | matching operations from the keyword index |

```
$ aws codemode schema ec2:DescribeRegions iam:ListMFADevices "unencrypted volumes"
```

There is no `search` subcommand and no `get` subcommand, because there was never a behavioural
difference worth a command name — both are "look up operations by a key and print what is known about
them", and the parser can see which kind of key it was given. Splitting them would only add a decision
the caller can get wrong: `get` on a phrase, or `search` on a name it already knew. One command, no mode
selection, no wasted turn.

#### 15.2.1 It is `apropos`, not semantic search

The keyword path deserves an honest name. There is no model in the CLI, so this is not semantic search,
not embeddings, and not question answering. It is a keyword index over operation names and the first
sentence of their documentation — the same shape of thing as `man -k` / `apropos` — with a curated
synonym table (§15.4) in front of it and fixed ranking rules (§15.3) behind it.

Three consequences that belong in `help` verbatim, because they determine whether an agent's first query
works:

- **Write keywords, not questions.** `"unencrypted volumes"` matches; `"how do I find all of my EBS
  volumes that aren't encrypted?"` matches the same thing only because the stopwords are thrown away.
  Two or three content words is the sweet spot.
- **Stopwords are stripped**, so question-shaped queries degrade gracefully rather than failing: a static
  list (`how, do, i, find, all, my, the, that, are, is`) is removed before matching. Verbs like
  `list`/`describe`/`get` are a special case — they are also operation-name prefixes, so they are dropped
  from scoring but kept as a hint (§15.4, intent verbs).
- **Matching is lexical, so vocabulary matters more than phrasing.** Recall comes from the synonym table,
  not from understanding. If a term is neither in the table nor in the service models, no rewording of the
  sentence will help — which is why the fallback of §15.4 hands back services and `--list` rather than
  nothing.

#### 15.2.2 Batching and output

Batching is the point of allowing many queries: one invocation answers every schema question a plan
needs, instead of one turn per operation.

- Results are labelled per query and independently budgeted, so a broad keyword query cannot crowd out
  the precise ones:

```
# ec2:DescribeRegions  (reference)
<full signature>

# iam:ListMFADevices  (reference)
<full signature>

# "unencrypted volumes"  (keywords: volumes, unencrypted → K1, all terms in name)
ec2:DescribeVolumes            reads  paginated  server-filters: Filters
ec2:DescribeVolumeStatus       reads  paginated  server-filters: Filters
  note: no server-side "unencrypted" filter; use Filters Name=encrypted,Values=false
```

- `--queries file://queries.json` or stdin accepts a JSON array, for harnesses that would rather not
  build a long argv.
- `--service ec2` scopes keyword queries; `--service ec2 --list` returns that service's whole operation
  index, a few thousand tokens, and is often the cheapest possible answer to "what can I do here?".
- Default `--limit 4` per keyword query. `--brief` forces one-line output even for references; `--full`
  forces signatures for every hit.
- An unqualified name (`DescribeInstances`) that exists in exactly one service resolves as a reference; if
  several services define it, the response lists those services rather than guessing.

### 15.3 Resolution order and ranking

A query is resolved in two phases. Phase A is tried first because it is exact; a query that fails it
falls through to phase B rather than erroring, which is what makes "syntax, not intent" safe — a
name-shaped query that names nothing is simply treated as a keyword.

**Phase A — reference resolution.** Exact match after style normalization (`ec2:DescribeInstances` ≡
`ec2:describe-instances` ≡ `ec2 describe-instances`), then unqualified name unique across services, then
unambiguous near-miss corrected in place (§15.5). Fails ⇒ phase B.

**Phase B — keyword ranks.** Fixed, explainable tiers evaluated in order until `--limit` is filled. No
scores to tune, no model, identical output for identical input, so the whole thing is golden-testable:

| Rank | Match                                                                            |
|------|----------------------------------------------------------------------------------|
| K1   | All query terms appear in the operation name                                     |
| K2   | All terms appear in the operation name + the first sentence of its documentation |
| K3   | All terms match after vocabulary bridging (§15.4)                                |
| K4   | Any term, ordered by how many matched, then by service prominence                |
| K5   | Fuzzy: edit distance ≤ 2 per term, or acronym expansion                          |

Ties break on: operation is a read; operation is paginated (list/describe operations are what workflows
need); service name matches a query term; shorter operation name. The index is built at package time from
the shipped service models, so a lookup is a dictionary hit, not a scan.

Every result line reports which phase and rank matched. That is the signal the agent needs in order to
decide whether to trust a hit or narrow the query — and narrowing with *more* information in hand is a
productive turn, unlike re-guessing after an empty result.

### 15.4 Vocabulary bridging: never return nothing

Recall comes from a static, curated synonym and expansion table shipped with the CLI, applied before
matching. No model, no embeddings, just a dictionary that encodes what agents actually type:

| Kind              | Examples                                                                                                                                                                                                         |
|-------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Resource synonyms | vm, server, machine, host → instance · disk, drive → volume · dns → route53 · queue → sqs · topic → sns · function → lambda · container → ecs, eks · database, db → rds, dynamodb · secret → secretsmanager, ssm |
| Acronyms          | sg → security group · asg → auto scaling group · alb, elb, nlb → load balancer · iam, kms, vpc, ebs, efs, sqs, sns → service names · mfa → multi factor                                                          |
| Intent verbs      | list, show, get, find, enumerate → describe/list operation prefixes · count → list + paginate · audit, check, inventory → describe                                                                               |
| Morphology        | plural/singular, `-ing`/`-ed` stripping, case and separator folding (`DescribeInstances` ≡ `describe-instances` ≡ `describe instances`)                                                                          |
| Task hints        | "unattached volumes" → `ec2:DescribeVolumes` + filter hint · "public buckets" → `s3api:GetBucketPolicyStatus`, `s3api:GetPublicAccessBlock` · "who has access" → IAM/Access Analyzer operations                  |

Relaxation is progressive and always terminates in something useful:

1. Try phase A, then keyword ranks K1–K2 on the literal query.
2. Apply bridging, retry (K3).
3. Drop to any-term matching (K4).
4. Fuzzy match (K5).
5. Still nothing ⇒ return the **best available orientation** rather than an empty list: the services
   whose names or documentation matched any term, each with its most prominent list/describe
   operations, plus a pointer to `--service <name> --list`.

Step 5 is the important one. An empty result set is the only outcome that guarantees a wasted turn, so
it is not a permitted outcome. The output states plainly that nothing matched well and offers the next
lookup, so the agent's follow-up is narrowing rather than re-guessing.

Task hints are curated data, and curated data rots. It is explicitly a *supplement* to mechanical
matching, never a dependency: an empty hint table degrades recall for phrasings like "public buckets"
and breaks nothing. Maintenance cost and ownership are open (§20.3).

### 15.5 One turn, not two: promotion and correction

Two mechanisms exist so that discovery is normally one turn:

- **Auto-promotion.** When a keyword query's top hit is unambiguous — K1 or K2, and the runner-up is
  materially weaker — the full signature is emitted instead of a one-line summary. The agent
  asked "what lists instances", and got the answer *and* the shape it needs to write the step.
  `--brief` suppresses this; `--full` forces it for every hit.
- **Correcting a reference in place.** `schema ec2:DescribeInstance` returns the `DescribeInstances`
  signature with a `corrected:` note, not an error with a suggestion. Same for naming-style mismatches
  and unambiguous case/separator differences. The only case that returns candidates instead of content
  is genuine ambiguity.

This is the same principle as Tier 3 validation (§15.7): when the system knows what the agent meant,
it should act on it and say so, because the alternative is a round-trip that teaches the agent nothing
it could not have been told immediately.

### 15.6 Signature output

```
$ aws codemode schema ec2:DescribeInstances
ec2:DescribeInstances  (reads, paginated: NextToken/MaxResults, records: Reservations[].Instances[])
records:
  InstanceId, InstanceType, LaunchTime: timestamp, PrivateIpAddress, PublicIpAddress, VpcId, SubnetId,
  State.Name: enum(pending|running|shutting-down|terminated|stopping|stopped),
  Placement.AvailabilityZone, Tags: [{Key, Value}]  (pseudo-path: tag:<name>), …+38
filterable (server-side):
  tag:<name>        eq, in
  State.Name        eq, in          → instance-state-name
  VpcId, SubnetId   eq, in
  InstanceType      eq, in
  InstanceId        eq, in          → InstanceIds parameter
relationships:
  VpcId    → ec2:DescribeVpcs.VpcId
  SubnetId → ec2:DescribeSubnets.SubnetId
  Tags[?Key=='aws:autoscaling:groupName'].Value → autoscaling:DescribeAutoScalingGroups.AutoScalingGroupName
```

Design points:

- **Records, not responses.** The signature names the record path once, so predicates and projections are
  written against records and nobody writes `Reservations[].Instances[]`.
- **Filterability is stated, not implied.** Each entry names the operators the capability table supports and,
  where the names differ, the request parameter it lowers to. The agent does not need this to write a valid
  plan — the compiler decides — but seeing it lets the agent *choose* a shape that pushes down.
- **Relationships make correlated traversal authorable, checkable, and batchable.** This is the metadata Cypher-style tooling exists to
  provide; exposing it here gives the correlation capability without adopting a graph query language or
  materializing a graph (§20.9).
- Compact signature notation rather than JSON Schema — 5–10× fewer tokens for the same information — with
  depth pruning (`…+38`) and `--depth`/`--fields` to go deeper.
- Sourced from the models already on disk plus the two curated tables (§17.5): offline, free,
  version-matched to the CLI.

### 15.7 Validation as the real schema channel

```
$ aws codemode validate --plan plan.json
error  let.inst.source.instance.call.operation      unknown operation 'ec2:DescribeInstance'
                                          did you mean: DescribeInstances, DescribeInstanceStatus?
error  let.inst.source                      source must be a map with exactly one identifier key
error  let.subnetsByVpc.forEach.vpc.do.filter  correlation must be a conjunction of direct equalities between
                                          do.source.<name> paths and enclosing traversal names
error  let.subnetsByVpc.forEach.vpc.do.filter  subnet.VpcId = vpc.VpcId is not server-pushable for this operation;
                                          v1 forbids repeated residual scans
error  let.stats.fold                   fold must be a map with exactly one aggregate name
error  let.inst.filter.and[0].eq[0].path    'Encrypted' is not a field of ec2:DescribeInstances records
                                          did you mean: EnaSupport, EbsOptimized?
                                          (see: schema ec2:DescribeInstances)
error  let.inst.filter.and[1].greaterThan  'greaterThan' is not a standard function
                                          relations: eq ne startsWith endsWith contains | lt lte gt gte |
                                          present absent | setEq setNe subset
error  let.vols.filter.and[1].gt[1]  gt expects two numbers; got number and string "large"
error  let.stats.forEach.bucket.from                {"ref":"buckets"} is not a defined name (defined: regions, inst)
error  let.report.fold                     "fold" requires "source"; there is no stream to fold
error  let.summary.result.region.path      standalone paths do not exist; name the value explicitly
                                          (did you mean {"ref":"region"}?)
error  let.summary.onError                 "onError" needs "source" or "forEach"; this block assembles
                                          bindings and makes no calls, so it has no failures of its own
error  let.report.source.page.call.args.Prefix              {"ref":"pages"} is not in scope here; it is local to "crawl".
                                          Scope runs inward only: hoist "pages" to this block, or read
                                          "crawl" itself
error  let.x.let.y                         name "vols" shadows a binding in an enclosing scope; names must be
                                          unique along the scope chain
error  let.empty                           a block needs at least one of "source", "let", or "result"
error  let.deep.let.deeper.let             nesting depth 3 exceeds the limit of 2; flatten or restructure
error  (document)                          duplicate key "vpcs" in "let"
error  (document)                          no unique sink (candidates: vpcByInstance, igwVpcs); "result" is required
warn   let.vols.filter                     'Size > 100' cannot be pushed to ec2:DescribeVolumes; all pages
                                          will be fetched and filtered locally
warn   let.wrap                            single-binding "let" whose value is the block's value anyway;
                                          the bindings can be hoisted
warn   let.inst                            no consumer names any field of this binding and it has no
                                          "result"; it will be fetched and discarded
```

Diagnostics are also emitted as JSON (`--output json`) with pointer, code, message, and candidates,
so an agent can repair mechanically. The economics: a repair turn costs a few hundred tokens; an
execution turn costs a full API payload.

### 15.8 Where the metadata comes from: Smithy selectors at build time

Three tables sit behind the schema service, and an earlier draft proposed hand-curating all of them. Most of
their content is already declared upstream, in the Smithy models AWS services are defined by, and Smithy ships
a query language for exactly this: **selectors**, a DSL for matching shapes in a model graph.

| Table                         | Used by                                         | Derived from                                        |
|-------------------------------|-------------------------------------------------|-----------------------------------------------------|
| Paths, records, pagination    | typing, pruning, `paginate`                     | the `paginated` trait; input/output shape traversal |
| Filterable fields + operators | pushdown (§11.2)                                | operation input members plus filter conventions     |
| Relationships                 | correlated `forEach` (§10.1.1), `schema` output | resource identifier and lifecycle relationships     |
| Read-only classification      | mutation gate (§13.4)                           | the `readonly` trait; resource lifecycle bindings   |

The selectors that produce them are ordinary model queries:

```
operation [trait|paginated]                                  # paginated operations
operation -[input]-> structure > member                      # candidate filter inputs
resource -[identifier]->                                     # resource identity members
resource -[list, read]-> operation                           # which operation lists / reads a resource
service ~> operation :not([trait|readonly])                  # mutating operations
```

The last two matter most. Smithy has **first-class `resource` shapes** with `identifier`, `property`, and
`create`/`read`/`update`/`delete`/`list`/`put` lifecycle relationships. That is the resource-graph ontology
correlated traversal needs and the read-only signal §13.4 needs — declared by service teams, not invented by us. It is
also, notably, the ontology that graph-based cloud tooling spends years assembling by hand (§20.9).

How it ships:

- **Selectors run at build time**, not at run time. The generation step consumes Smithy models and emits the
  tables as data files packaged with the CLI. There is no Smithy engine, no model download, and no selector
  evaluation in the hot path — the runtime reads a dict.
- **Hand curation shrinks to overrides.** Where a model is silent (filter names that differ from member
  names, semantics that don't match client-side evaluation), an override file supplies or *removes* an entry.
  Overrides are reviewed; derivations are regenerated.
- **Correctness stays gated.** A derived filterability entry still requires a passing differential test
  (§11.2) before it is used for pushdown. Derivation improves coverage and reduces toil; it does not lower the
  bar for correctness.
- **Both degrade safely.** A missing filterability entry means residual client-side filtering. A missing
  relationship means the agent states the equality paths explicitly. Neither absence can produce a wrong answer.

The layer distinction is worth restating because it is easy to lose: selectors query the *model*; `filter`
(§10.3) queries the *data* the model describes. Selectors cannot express `State.Name == "running"` — there is
no runtime record in their world — which is why they are a build-time tool here rather than the query surface.

## 16. Prototype evidence

A working prototype of this execution model exists in
`~/projects/hello/hello-spring-ai-bedrock` (`synth/Workflow.kt`, `synth/WorkflowInterpreter.kt`,
`synth/SynthScenario.kt`). It plans a workflow against an MCP tool catalog and executes it without a
model in the loop. The prototype used **JSONata** as a general expression language; this spec replaces that
with three closed algebras (§10), for reasons the prototype itself produced — see the dialect finding below.
The execution model carried over unchanged. Findings that shaped this spec:

- **The data-structure-plus-expressions split works.** Tool invocation and fan-out are *structural*
  (fields in a JSON document); only data reshaping is expression-level. The interpreter is ~100 lines
  and has no way to do anything but call tools and evaluate expressions.
- **The prototype's `${ … }` delimiter removed literal-vs-expression ambiguity** — a problem §9.2 deletes
  outright by making references objects and literals scalars.
- **Model output needed defensive parsing.** The prototype's `Workflow.parse` strips markdown fences
  before deserializing, because the planner wrapped its JSON in ```` ```json ```` regularly. §8.1.2
  makes that tolerance an explicit, warned behaviour rather than an undocumented hack.
- **Naming each binding's value and the traversal element was learned from a short grammar plus one example.**
  The prototype used `$<id>` and `$item` because JSONata has variables; §9.2 uses the single lexical
  `{"ref": …}` form for both. The lesson that transferred is that *one* short grammar plus *one* worked
  example suffices — not the specific syntax.
- **Returning `{<as>: element, value|error}` envelopes from fan-out** turned out to be essential: the final
  projection needs the input key (there, the symbol; here, the region) to make sense of each result.
- **Concurrency must not be in the plan.** The prototype fixed `maxConcurrency=6` in the interpreter
  and instructed the planner to never mention concurrency; the resulting plans were stable.
- **Binding-level parallelism was left on the table.** The prototype executed its steps strictly
  sequentially and only parallelized within a fan-out step. In the javadoc trace that cost nothing
  (each step genuinely fed the next), but it is the wrong default: the sequential loop is an accident
  of the implementation, not a property of the plans. §4.2 makes the graph explicit so unrelated reads
  overlap.
- **Prompt shaping mattered more than grammar.** Two instructions did the heavy lifting: *filter
  before you fan out*, and *make the result as small as possible*. Both are promoted to
  first-class mechanisms here (derived pruning and `fold`, plus compiler-decided pushdown), because instruction alone
  is not enforcement.
- **Dialect confusion was the top failure mode, and it is why there is no dialect now.** The planner kept
  reaching for JSONPath and JMESPath syntax; the prototype's system prompt had to say, in capitals, that this
  is JSONata and *not* JSONPath, and to forbid `[?…]` filters. That instruction is the artifact worth keeping:
  the model was guessing among interchangeable syntaxes for the same operations. Replacing the expression
  language with closed vocabularies (§10) removes the guess — there is one spelling of "equals", and it is an
  enum value the schema will reject if misspelled.
- **The expression language was doing three unrelated jobs.** Filtering, aggregation, and dataflow were all
  JSONata, which is why the prompt needed rules like *filter before you fan out* and *make the result small*:
  the language could not distinguish them, so the instructions had to. Splitting them into three algebras
  makes those rules structural — filtering happens in `filter`, which the compiler may push server-side, and
  reduction happens in `fold`, which folds as pages arrive.
- **Nothing in the execution model changed.** The step DAG, `{item, output}` pairing, controlled fan-out
  concurrency, and single-shot planning all carried over unmodified. What changed is the surface the model
  writes, not the machinery underneath.

Prototype trace (12-way fan-out over javadoc symbols, one planning turn, no model in the execution
loop) is reproduced in the prototype's logs and matches the progress format of §14.

### 16.1 External empirical evidence

SPL's 1200-run experiment (10 models × 20 problems × 2 arms × 3 repetitions, arXiv:2607.07727) tests a
structurally similar arrangement — a model emits a structured plan, a deterministic engine executes it — and
three of its findings bear directly on decisions here.

- **Markdown fences were an entire failure class, and stripping them eliminated it.** One model produced
  0% pass because it wrapped plans in ```` ```plaintext ```` fences; adding a fence stripper took
  `plan_format_error` to *zero across all 1200 runs*. §8.1.2's fence tolerance is therefore not a nicety —
  it is the difference between a model being usable and unusable, and our prototype independently needed
  the same thing (§16).
- **The dominant failure mode was semantic, not syntactic.** After fences were handled, every failure was
  the engine rejecting an expression — wrong names, unsupported operations — not malformed output. This is
  the strongest available argument for where to spend effort: §15's schema lookups and §15.7's
  did-you-mean diagnostics target exactly that failure mode, while grammar instruction targets the one that
  turned out not to matter.
- **Format compliance is a capability separable from reasoning.** Models scoring 100% on the unstructured
  arm scored as low as 32% on the structured arm, and a ~2B open model (93%) beat a frontier model (85%).
  Two consequences for us: schema-constrained generation is worth more than prompt quality, and the
  *planning* turn may not need a frontier model at all — plan authoring is format translation, while the
  summarizing turn is where reasoning matters. A deployment could reasonably use a small fast model for
  planning and a larger one for the answer.

A caveat on transfer: their structured target is a terse `expr|op` line format with no schema, whereas ours
is JSON with a published schema and constrained-decoding support. Their format-compliance numbers are
therefore a *lower* bound on what should be achievable here — which is itself an argument for the schema
being load-bearing rather than decorative.

## 17. Implementation notes (aws-cli v2)

New customization package `awscli/customizations/codemode/`, registered as a command group in the style of
`awscli/customizations/wizard/` and `.../agenttoolkit/`.

### 17.1 Make illegal plans unrepresentable

The three algebras are three closed type hierarchies, and parsing is the only way to construct them:

```
Plan        = (Version, Description, Inputs, Block)
Block       = ( Let: Map[Name, Block], Role )
Role        = Traverse(ForEach, OnError)     -- one of three; see the table in §9.1
            | Express(Source, Stages, PureTransform?, OnError)
            | Assemble(PureTransform?)                       -- no stages, no OnError: it makes no calls
Map1[N,A]   = Map[N,A] where size = 1
ForEach     = Map1[Name, (From: Expr, Batch: Int?, Do: Block)]
Source      = Map1[Name, Call(Service, Operation, Args, Region?, Paginate?) | Expr]
Fold        = Map1[Name, Aggregator]
Stages      = ( Filter: Expr[Boolean]?, Dedup: List[Expr]?, Fold? )
                                                        -- fixed composition order; flat on the wire
Expr[A]     = Scope(Name, Path?) | Input(Name, Path?) | Env(Name, Path?)
            | Literal | Apply(Function[Args,A], Args[Expr])
Function    = Casefold | Age | Date | Number | Size | Cidr | CidrSize
            | Eq | Ne | StartsWith | EndsWith | Contains | Lt | Lte | Gt | Gte
            | Present | Absent | SetEq | SetNe | Subset | And | Or | Not
Predicate   = Expr[Boolean]
PureTransform = Expr | Record(Map[Name, PureTransform]) | ExplicitRecord(Map[Name, PureTransform])
Aggregator  = (Prepare: Expr, Monoid, Present)
Monoid      = Sum | Semilattice(Order) | Free | SetUnion | MapOf(Monoid) | Product(Map[Field, Monoid])
Paginate    = (MaxItems: Int?, MaxPages: Int?, PageSize: Int?)  -- absent bound = budget-bounded
OnError     = Fail | Skip | Collect
```

There is no wrapper object or type whose field stores an identifier. `let`, `source`, `forEach`, and `fold`
entries introduce `Symbol`s from their map keys; `Block` remains the single structural workflow type, and
`Plan` is a `Block` with version/description/input declarations. Local `let` bindings and an Express or Traverse
role are orthogonal, which is what allows nested helpers without a special nested-source variant.

`Role` is a **union over the three shapes a block can take** (§9.1), so the combinations a flat ten-key record
would admit — stages without a source, `forEach` beside `source`, `onError` on a block that makes no calls — are
unrepresentable rather than checked. It carries **no discriminator field**: presence of `ForEach` versus `Source`
decides the branch, so the generated schema is a `oneOf` and the document stays exactly as flat as §9.1.2 shows.
`Stages` is likewise a record rather than a list, which makes stage order canonical (§10.1) and "filter after
fold" unwriteable. Both are members of a variant in the type while being flat on the wire — the same
relationship `Plan` has to `Block`, and the reason the parser is where flat keys become structure.

**A note on unrepresentability versus checks.** An earlier draft of this spec collapsed the block forms into one
flat record and moved three rules into prose — stages needing a source, `forEach` excluding `source`, and a block
needing some value-provider. `Role` puts them back into the types at no cost to the document, which is the
outcome worth generalizing: **a flat wire format and a structured type are not in tension, because a JSON Schema
`oneOf` can discriminate on which keys are present rather than on a tag.** When a collapse seems to force rules
out of the types, the question to ask first is whether the union simply needs to be written down without a
discriminator field. A filter is one `Expr[Boolean]`; conjunction is the standard `and` application rather
than a parallel container grammar. There is no projection stage: the pruned field set is *derived* (§11.2)
and lives on `ValidPlan` as something parsing learned, while the authored `PureTransform` is the block's
optional `result`. `PureTransform` is itself algebraic — an `Expr`, a path-named product, or a recursive record
product (§10.4) — so Assemble, Express, and Traverse all use the same pure transformation type over different
subjects.

What the encoding removes rather than adds:

- **Stages compose in a fixed order** (§10.1), so the only typing question they raise is whether
  `Fold` is present — which decides `Stream` versus `One`. Illegal stage orders are not diagnosed because they
  cannot be written.
- **`Function` and `Monoid` are closed**, so the type checker, pushdown classifier, and folder match
  exhaustively: an operation nobody taught them is a compile error, not a silent residual.
- **`Aggregator` is one type with instances**, not a set of names. `count` is `(const 1, Sum, id)`;
  `avg` is `(r ↦ (r.p,1), Product, divide)`. The laws are properties of `Monoid`, so nothing named needs its
  own proof (§10.2).
- **Deref sources are pure and carry no `Paginate`**, while only `Call` owns request policy and API failure.
- **Nesting is `let` inside `let`**, bounded by a parse-depth parameter, so an over-nested plan fails
  construction rather than execution.
- **`NonEmpty`** for bindings and for `and`/`or` argument lists deletes empty branches from the normalizer and
  executor.
- **Domain types over primitives**: `SymbolId`, `ServiceName`, `OperationName`, `RegionName`, `Path`,
  `MaxItems`. The bug class this prevents is a region flowing where a profile is expected, or an unnormalized
  string where a canonical operation name is expected.
- **Parse, don't validate.** `parse : Document -> Either[NonEmpty[Diagnostic], ValidPlan]` runs once; every
  later stage takes `ValidPlan` and cannot receive anything else, so `explain`, the lowerer, and the executor
  contain no revalidation and no defensive branches. A `ValidPlan` also carries what parsing *learned* —
  resolved operation models, typed paths, the dependency graph, the pushdown split — so downstream stages
  consume facts instead of re-deriving them.
- **`Expr` has four source forms plus one application form.** Raw literals and the `ref`/`input`/`env` sources
  have no sentinels, and field navigation is always attached to a named source. `ForEach` groups
  `From`/`As`/`Batch`/`Do` for the opposite reason stage keys are flat: those four are meaningless apart, and a
  traversal missing its name or body is unrepresentable.
- **Name-keyed maps are parsed with a duplicate-key hook.** `json` silently keeps the last of a repeated key,
  so `{"vpcs": …, "vpcs": …}` would otherwise lose a binding without complaint. The hook makes it a
  diagnostic, in `let`, `inputs`, and `args` alike.
- **Unkeyed commutative collections are sorted canonically by the normalizer** — `and`/`or` arguments and
  `dedup` keys. Argument order remains intact for noncommutative function applications. Thus no ordering that
  lacks semantic meaning can affect behaviour or output (§9.1.1).
- Python lacks sealed hierarchies, so the encoding is frozen dataclasses with a `Literal` discriminator and
  `match` statements with exhaustiveness assertions. The discriminator is the JSON key, so the wire format and
  the type have the same shape — and one definition generates both the JSON Schema and `help --output json`,
  so they cannot disagree.

### 17.2 Purity and the one effectful boundary

Everything except invocation is a pure function of `ValidPlan`: normalization, graph construction, lowering,
pure-expression evaluation, folding, and rendering. Those are the parts with interesting logic, and they are
testable without a network, a clock, or credentials.

The effects are confined to one injected interface:

```
class AwsCalls(Protocol):
    def invoke(self, req: Request) -> Response: ...
    def pages(self, req: Request, policy: Paginate) -> Iterator[Response]: ...
```

with a botocore implementation for production and a fake for tests. Impure values are resolved once into an
immutable `Env` (§12) and passed in, so no code below the entry point reads a clock or an RNG. Bindings are
written once into an immutable map as they complete; there is no shared mutable accumulator, which is what
makes §4.2.2's schedule-independence a property of the code rather than a convention. Combiners fold
functionally — `fold(identity, combine)` — so a streaming fold and a batch fold are the same function.

### 17.3 Modules

`plan.py` (parse → `ValidPlan`), `algebra.py` (the three vocabularies and their laws), `graph.py`
(references → DAG, barriers, cycles, waves), `schema.py` (service-model projections, paths, relationships),
`capability.py` (the pushdown table), `lower.py` (CNF, pushdown split, correlated-query validation,
batching/fusion, request construction, repartition plan), `query.py`
(typed standard functions, pure-expression evaluation, Boolean normalization), `explain.py`, `executor.py`
(scheduler, governor, invoker, fold), `envelope.py`. No persistence layer (§13.4).

Invocation goes through botocore clients and the existing paginator configuration; plans carry structured
arguments, so there is no string round-trip through `clidriver`. `awscli/paramfile.py` is reused for
`file://` sources (§8.1).

### 17.4 Test onion

Fastest and most general first, so the loop stays tight:

1. **Types.** Most illegal plans are unrepresentable (§17.1), so they need no tests.
2. **Property tests on the algebras** — the layer that carries the most weight:
   - each `Combiner` satisfies its declared laws (associativity, identity, and commutativity where
     declared), over generated inputs;
   - `fold` in any grouping and any order equals the sequential fold, for commutative combiners — the
     property that licenses out-of-order streaming;
   - CNF normalization preserves truth value over generated `Expr<boolean>` trees;
   - typed function composition is associative, and evaluation of nested unary applications agrees with
     evaluation of the corresponding composed standard function;
   - application arity and type errors are rejected for every standard function signature;
   - lexical-reference extraction returns exactly the symbol names used by generated expression trees, so no
     dependency edge can be missed;
   - batched correlated execution, repartitioned by key, equals the logical result of evaluating each outer
     element independently and preserves traversal input order;
   - indexed bound-source correlation equals the same logical semantics and performs O(n+m) key work rather
     than O(n×m) comparisons.
3. **Unit tests on pure functions**: pagination arithmetic, budget accounting, path typing against fixture
   shapes, lowering decisions, `explain` rendering.
4. **Integration with the fake `AwsCalls`**: concurrency, throttling backpressure, partial failure,
   truncation, cancellation, mutation barriers, correlated-query batching, batched-error fan-out, and
   response repartitioning — deterministic, no network.
5. **Differential pushdown tests** (§11.2): for each capability-table entry, server-side and client-side
   evaluation of the same clause return identical sets over recorded fixtures. This is the gate for adding
   an entry.
6. **End-to-end** against local mock endpoints, with §9.4–9.6 as golden plans.

Assertions are plain boolean expressions over ordinary code, not a matcher DSL.

### 17.5 Metadata: derived, then overridden

Three tables back the schema service and the lowerer. They are **generated at build time from Smithy models
using selectors** (§15.8), with a reviewed override file for what the models do not say:

- **Shape/pagination/record paths** — typing for paths, pruning, and `paginate`.
- **Filterability** — per operation, which paths are server-filterable, by which operators, with which
  semantics. Each entry is gated on a differential test (§11.2).
- **Relationships and lifecycle** — the edges that make correlated traversal authorable and optimizable, and the read-only
  classification that §13.4's mutation gate and §4.2.1's barriers depend on.

Generation is a build step whose output is data, so the runtime has no Smithy dependency. The override file is
the only hand-maintained artifact, and a test asserts every override still resolves against the current
models — so overrides for shapes that no longer exist fail the build rather than rotting silently.

## 18. Instruction surface: `help` is the manual, the skill is the pointer

### 18.1 Why the split

The instructions for authoring a plan are large and must match the executor exactly. The fact that
`aws codemode` exists is tiny and must be present before the agent looks for anything. These are
different artifacts with different lifecycles, so they are shipped separately (§4.5).

An agent's pre-training will not contain this command for a long time after release, and a skill file
copied to disk today can be read by a CLI updated tomorrow. So: **help ships with the executor and is
authoritative; the skill only triggers the lookup.**

### 18.2 `aws codemode help`

Structured into addressable topics so an agent pulls only what it needs, and available as JSON for
machine consumption:

| Topic       | Contents                                                                                                                                                           | Budget       |
|-------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------|
| (default)   | What it is, when to use it vs single commands, the subcommands, the minimum viable plan, and the topic index                                                       | ~600 tokens  |
| `plan`      | One block form; map-key declarations for `let`/`source`/`forEach`/`fold`; inferred symbols/types; stages, `result`, refs, errors — JSON Schema via `--output json` | ~1350 tokens |
| `passing`   | How to hand a plan to the CLI: heredoc, single-quoted inline, `file://`; the shell-quoting hazard of §8.1.1                                                        | ~250 tokens  |
| `query`     | The typed `{function:[arguments]}` algebra, expression sources, pure `result` products, Boolean normalization, and pushdown                                        | ~1000 tokens |
| `combiners` | The monoid vocabulary, what each expects and produces, and product combiners                                                                                       | ~250 tokens  |
| `errors`    | `onError` semantics, error taxonomy, partial results, truncation, exit codes                                                                                       | ~500 tokens  |
| `examples`  | 4 worked plans: single chain, multi-region fan-out, independent-reads DAG, parallel fold                                                                           | ~1200 tokens |
| `schema`    | One lookup call, many queries; keywords not questions; the signature notation; the vocabulary it knows                                                             | ~500 tokens  |
| `limits`    | Budgets, defaults, and which are overridable                                                                                                                       | ~200 tokens  |

Requirements on this output:

- **Self-contained (D1).** An agent that has read `help` and `help plan` can write a valid plan with no
  other document. The default topic alone must be enough for a trivial two-step plan.
- **Exhaustive where it claims to be (D2).** The `expressions` and `combiners` topics are generated
  from the same allowlist tables the validator enforces, so they cannot drift from the implementation.
  A function that is not listed is a validation error, and the diagnostic points at the topic.
- **Machine-readable (D2).** `--output json` yields `{topic, sections[], grammar, functions[],
  combiners[], examples[]}`, so a harness can cache or splice it into a prompt without scraping text.
- **Stable anchors.** Diagnostics reference topics by name (`see: aws codemode help expressions`), which
  is how the repair loop of §15.7 stays a single hop.
- **Teaches batching explicitly.** The `schema` topic leads with a multi-query example and the rule
  "one invocation, one concise query per fact you need", because the expensive mistake is not a bad
  query, it is a sequence of them (§15.1).
- **Versioned (D3).** `help --output json` includes the CLI version and the plan-format version, so a
  harness can cache the manual per binary version and invalidate on upgrade.

The reason for the token budgets: this material is read at author time, in the same turn as the plan.
If the manual costs more than the multi-turn approach it replaces, the feature does not pay for itself.
Full manual under ~5000 tokens; the common path (default + `plan` + `expressions`) under ~2700.

### 18.3 The pointer skill

Everything the external skill needs to contain:

```markdown
---
name: aws-codemode
description: Run multi-step AWS CLI workflows in one shot instead of many separate commands.
---

When a task needs more than about three AWS API calls — especially the same call across many
regions, accounts, or resources, or calls whose inputs come from earlier calls' outputs — do not
issue them one at a time. The AWS CLI can execute a whole declarative workflow in a single
invocation, with parallelism, pagination, and error handling handled for you.

Run `aws codemode help` to learn the plan format, then `aws codemode help examples` for worked
plans. If `aws codemode help` is not recognized, the installed CLI is too old; fall back to
individual commands.
```

Properties that matter more than the wording:

- **No grammar, no function list, no examples.** Anything duplicated here will eventually contradict
  the installed binary. The skill's only content is a trigger condition and a command to run.
- **Small enough to be permanently resident** in every session (D4).
- **Explicit failure mode.** The "not recognized ⇒ CLI too old ⇒ fall back" line prevents the skill
  from breaking older installs, which matters because skills and binaries update independently.
- **Trigger condition is quantitative** ("more than about three calls", "same call across many X"), not
  aspirational. Vague triggers produce either no adoption or plans for single-call tasks.

### 18.4 Other discovery paths (D5)

Not every agent has the skill installed, so the CLI advertises the capability in-band:

- **`aws help` topic** — a `codemode` entry in the existing topic index (`awscli/topics/`), which is
  where an agent exploring the CLI's own documentation will find it.
- **Mentioned in related help** — a one-line "for multi-step or multi-region workflows, see
  `aws codemode help`" in `aws help`, and in the `--query`/pagination topics, since those are what an
  agent reads when it is about to do this the hard way.
- **In-band hint (optional, opt-in).** When a session issues many similar read commands in a short
  window, a one-line stderr hint pointing at `aws codemode help`. This is the most effective path and
  the most intrusive; it belongs behind a config setting, and it must never touch stdout. Treated as an
  open question (§20.8) rather than a commitment.

## 19. Rollout

1. Executor + plan format + validate/explain/run, read-only, no fan-out beyond region overrides.
   DAG scheduling ships here, not later: it is the cheapest parallelism to implement (dependency
   extraction plus a ready-queue) and it is what makes even trivial plans worth writing.
   `help` ships in this phase too — it is the feature's documentation, not a follow-up.
2. `forEach`, derived pruning, `fold`, budgets, partial results.
3. Schema service (Tier 1/2), the pointer skill, and the `aws help` topic; benchmark against
   multi-turn baselines. Adoption is measurable here: how often does an agent with only the pointer
   in context reach `codemode help` on a task that warrants it?
4. `inputs` for parameterized plans, re-run from files the caller manages (still no CLI-side storage);
   mutation support behind `--allow-mutations`; policy hooks so an organization can restrict permitted
   operations, regions, or budgets by configuration.

## 20. Open questions

1. **How large must the relation and value-function vocabularies be?** (§10.3) Cloud Custodian's set is the
   starting point and it grew over a decade. Starting smaller risks inexpressible tasks; starting larger risks
   operators with no pushdown story and no tests. Proposal: ship the enumerated set in §10.3 and require a
   worked failing example before adding to it.
2. **Does `result` need an escape hatch?** Projections are structured, hence fully validatable and
   schema-generatable. If real tasks turn out to need shaping the projection algebra cannot express, the
   options are an operator (`sort`, `limit`, `flatten`) or an expression escape hatch in `result` alone (JMESPath
   being the obvious candidate, since `--query` already uses it). Prefer the operator;
   the escape hatch forfeits the constrained-generation property.
3. **Ownership and derivation of the two curated tables** (§17.5). How much of the capability table can be
   generated from the models — filter parameter names often appear in documentation strings — and how much
   must be hand-written? Same question for relationships, where shape names (`VpcId`) are a strong but
   imperfect signal.
4. **Cross-account fan-out.** Assume-role fan-out over an Organization is the natural next request after
   multi-region, and a large blast-radius increase. v1 gated flag, or later with its own review UX?
5. **Should `explain` be part of the harness contract?** Should showing the rendered plan to the user be
   required before `run --yes`, and can that be enforced?
6. **Plan-level conditionals.** Excluded deliberately, but "skip this step if the previous result is empty"
   recurs. Does the empty-fan-out rule (§9.2) cover enough in practice?
7. **Exit code `1` for partial.** Consistent with the `s3` precedent, but automation may want to distinguish
   "partial but useful" from "failed".
8. **In-band discovery hints.** (§18.4) A stderr nudge after repeated similar commands reaches agents without
   the skill and risks annoying everyone else. Config-gated, default which way?
9. **openCypher as a second front end, if a materialized graph ever exists.** (§10.5) The relationship table
   (§17.5) is the ontology a graph query language needs, and Prowler shows read-only openCypher runs
   unmodified against both Neo4j and Neptune. Against live APIs the cost model is unbounded — every hop is a
   paginated call, and variable-length patterns are unestimatable without statistics — so this stays out of
   v1. It becomes attractive if the CLI gains a Config aggregator, Resource Explorer, or a local inventory
   cache, at which point the algebras here lower to it instead.
10. **Should a `transduce` source ever exist — an LLM call *inside* the plan?** Some questions need per-item
    semantic judgment no API provides: "which of these 200 policies look over-permissive", "cluster these log
    messages". Today such a plan must return everything to the summarizing turn, which blows the result
    budget — the exact failure Code Mode exists to prevent. Agentics (§10.5) shows the shape that would work:
    a typed transduction with schema in and schema out, batched and run in parallel, which is the same
    applicative structure as `forEach` over `call`.

    The costs are real and would have to be paid explicitly: determinism is lost (so §4.2.2 no longer holds
    for that binding), cost accounting becomes tokens rather than calls, nothing about it can be pushed down,
    and the mode boundary must be visible in the plan and in `explain` the way SPL insists on. Guardrails if
    it is ever done: opt-in flag, separate token budget, marked non-reproducible in the envelope, forbidden
    excluded from the plan-is-deterministic claim. Deferred, but this is the most
    plausible future extension, and the algebra already has the right shape for it.
11. **Where does `help` live?** The topic system gives free discovery via `aws help topics`, but the token
    budgets and `--output json` requirement of §18.2 push toward generated, purpose-built output. Possibly
    both: generated content surfaced through the topic index.

## 21. Appendix: grammar conformance fixture

This plan is deliberately **exhaustive rather than exemplary**. No real task needs every construct at once; the
purpose is a single artifact that touches every production, for use as a golden test of the parser, the generated
JSON Schema, the dependency graph, `explain`, and the lowerer. A plan that stops round-tripping this is a
regression.

It is also the only example in this document that mutates, which exercises the read-then-mutate phase split
(§4.2.1). It would require `--allow-mutations` to run; botocore supplies the modeled idempotency token for the
runbook call (§12).

```json
{
  "codemode": "v1",
  "description": "Quarterly account hygiene sweep (grammar conformance fixture)",
  "inputs": {
    "env": {"type": "string", "default": "prod"},
    "staleDays": {"type": "integer", "default": 90},
    "logPrefix": {"type": "string", "default": "/aws/lambda/"},
    "paramNames": {"type": "array", "default": []},
    "metadata": {"type": "document", "default": {"eq": [1, 2]}}
  },
  "let": {
    "regions": {
      "source": {"region": {"call": {"service": "ec2", "operation": "describe-regions"}}},
      "filter": {"eq": [{"ref": "region", "path": "OptInStatus"}, ["opt-in-not-required", "opted-in"]]}
    },
    "insts": {
      "forEach": {
        "region": {
          "from": {"ref": "regions", "path": "RegionName"},
          "do": {
            "source": {
              "instance": {
                "call": {
                  "service": "ec2",
                  "operation": "describe-instances",
                  "region": {"ref": "region"},
                  "paginate": {"maxItems": 5000, "pageSize": 1000}
                }
              }
            },
            "filter": {
              "and": [
                {
                  "eq": [
                    {"casefold": [{"ref": "instance", "path": "tag:Env"}]},
                    {"casefold": [{"input": "env"}]}
                  ]
                },
                {
                  "or": [
                    {"eq": [{"ref": "instance", "path": "State.Name"}, ["running", "stopped"]]},
                    {"startsWith": [{"ref": "instance", "path": "InstanceType"}, "t2."]}
                  ]
                },
                {"not": [{"present": [{"ref": "instance", "path": "tag:Ephemeral"}]}]},
                {
                  "gt": [{"age": [{"ref": "instance", "path": "LaunchTime"}]}, {"input": "staleDays"}]
                },
                {"lt": [{"size": [{"ref": "instance", "path": "Tags"}]}, 10]}
              ]
            },
            "fold": {
              "instsDoAggregate": {"groupBy": [{"ref": "instance", "path": "InstanceType"}, {"count": []}]}
            }
          }
        }
      },
      "onError": "collect"
    },
    "staleByRegion": {
      "source": {"entry": {"ref": "insts"}},
      "filter": {"present": [{"ref": "entry", "path": "value"}]},
      "result": {
        "region": {"ref": "entry", "path": "region"},
        "byType": {"ref": "entry", "path": "value"}
      }
    },
    "instErrors": {
      "source": {"entry": {"ref": "insts"}},
      "filter": {"present": [{"ref": "entry", "path": "error"}]},
      "result": {
        "region": {"ref": "entry", "path": "region"},
        "code": {"ref": "entry", "path": "error.code"},
        "retryable": {"ref": "entry", "path": "error.retryable"}
      }
    },
    "vols": {
      "source": {"volume": {"call": {"service": "ec2", "operation": "describe-volumes", "paginate": {}}}},
      "filter": {
        "and": [
          {"eq": [{"ref": "volume", "path": "Encrypted"}, false]},
          {"gte": [{"ref": "volume", "path": "Size"}, 100]},
          {"lte": [{"ref": "volume", "path": "Size"}, 16384]},
          {"ne": [{"ref": "volume", "path": "State"}, ["deleting", "error"]]},
          {
            "subset": [{"ref": "volume", "path": "Attachments[].State"}, ["attached", "attaching"]]
          },
          {
            "or": [
              {"setNe": [{"ref": "volume", "path": "Tags[].Key"}, ["Owner"]]},
              {
                "and": [
                  {
                    "subset": [{"ref": "volume", "path": "Tags[].Key"}, ["Owner", "Env", "Team"]]
                  },
                  {
                    "not": [
                      {
                        "setEq": [{"ref": "volume", "path": "Tags[].Key"}, ["Owner", "Env", "Team"]]
                      }
                    ]
                  }
                ]
              }
            ]
          },
          {"endsWith": [{"ref": "volume", "path": "SnapshotId"}, "0"]},
          {"contains": [{"ref": "volume", "path": "AvailabilityZone"}, "us-"]}
        ]
      },
      "dedup": [{"ref": "volume", "path": "VolumeId"}]
    },
    "volStats": {
      "source": {"volume": {"ref": "vols"}},
      "fold": {
        "volStatsAggregate": {
          "n": {"count": []},
          "bytes": {"sum": [{"number": [{"ref": "volume", "path": "Size"}]}]},
          "biggest": {"max": [{"ref": "volume", "path": "Size"}]},
          "smallest": {"min": [{"ref": "volume", "path": "Size"}]},
          "meanSize": {"avg": [{"ref": "volume", "path": "Size"}]},
          "zones": {"distinctCount": [{"ref": "volume", "path": "AvailabilityZone"}]},
          "ids": {"collect": [{"ref": "volume", "path": "VolumeId"}]},
          "byZone": {"groupBy": [{"ref": "volume", "path": "AvailabilityZone"}, {"count": []}]}
        }
      }
    },
    "volSnap": {
      "forEach": {
        "volume": {
          "from": {"ref": "vols"},
          "do": {
            "let": {
              "snapshots": {
                "source": {
                  "snapshot": {
                    "call": {
                      "service": "ec2",
                      "operation": "describe-snapshots",
                      "args": {"OwnerIds": ["self"]},
                      "paginate": {"maxItems": 20000}
                    }
                  }
                },
                "filter": {
                  "eq": [
                    {"ref": "snapshot", "path": "VolumeId"},
                    {"ref": "volume", "path": "VolumeId"}
                  ]
                }
              }
            },
            "result": {
              "volume": {"ref": "volume", "path": "VolumeId"},
              "size": {"ref": "volume", "path": "Size"},
              "snapshots": {"ref": "snapshots", "path": "[].SnapshotId"},
              "newest": {"date": [{"ref": "snapshots", "path": "[0].StartTime"}]}
            }
          }
        }
      }
    },
    "volStatus": {
      "forEach": {
        "volIds": {
          "from": {"ref": "vols", "path": "VolumeId"},
          "do": {
            "source": {
              "volumeStatus": {
                "call": {
                  "service": "ec2",
                  "operation": "describe-volume-status",
                  "args": {"VolumeIds": {"ref": "volIds"}}
                }
              }
            },
            "result": {
              "VolumeId": {"ref": "volumeStatus", "path": "VolumeId"},
              "Status": {"ref": "volumeStatus", "path": "VolumeStatus.Status"}
            }
          },
          "batch": 200
        }
      },
      "onError": "skip"
    },
    "logGroups": {
      "source": {
        "logGroup": {
          "call": {
            "service": "logs",
            "operation": "describe-log-groups",
            "args": {"logGroupNamePrefix": {"input": "logPrefix"}},
            "paginate": {"maxItems": 200, "pageSize": 50}
          }
        }
      },
      "filter": {
        "and": [
          {"gt": [{"ref": "logGroup", "path": "storedBytes"}, 0]},
          {"absent": [{"ref": "logGroup", "path": "retentionInDays"}]}
        ]
      },
      "result": {
        "logGroupName": {"ref": "logGroup", "path": "logGroupName"},
        "storedBytes": {"ref": "logGroup", "path": "storedBytes"}
      }
    },
    "params": {
      "forEach": {
        "nameBatch": {
          "from": {"input": "paramNames"},
          "do": {
            "source": {
              "parameter": {
                "call": {
                  "service": "ssm",
                  "operation": "get-parameters",
                  "args": {"Names": {"ref": "nameBatch"}, "WithDecryption": false}
                }
              }
            },
            "result": {
              "Name": {"ref": "parameter", "path": "Name"},
              "Type": {"ref": "parameter", "path": "Type"}
            }
          },
          "batch": 10
        }
      },
      "onError": "skip"
    },
    "perRegionNet": {
      "forEach": {
        "region": {
          "from": {"ref": "regions", "path": "RegionName"},
          "do": {
            "let": {
              "vpcs": {
                "source": {
                  "vpc": {
                    "call": {
                      "service": "ec2",
                      "operation": "describe-vpcs",
                      "region": {"ref": "region"}
                    }
                  }
                },
                "filter": {"gte": [{"cidrSize": [{"cidr": [{"ref": "vpc", "path": "CidrBlock"}]}]}, 16]}
              },
              "subnetsByVpc": {
                "forEach": {
                  "vpc": {
                    "from": {"ref": "vpcs"},
                    "do": {
                      "source": {
                        "subnet": {
                          "call": {
                            "service": "ec2",
                            "operation": "describe-subnets",
                            "region": {"ref": "region"}
                          }
                        }
                      },
                      "filter": {
                        "eq": [{"ref": "subnet", "path": "VpcId"}, {"ref": "vpc", "path": "VpcId"}]
                      },
                      "result": {
                        "vpc": {"ref": "vpc", "path": "VpcId"},
                        "cidr": {"ref": "vpc", "path": "CidrBlock"},
                        "subnet": {"ref": "subnet", "path": "SubnetId"}
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "tagVols": {
      "forEach": {
        "volId": {
          "from": {"ref": "vols", "path": "VolumeId"},
          "do": {
            "source": {
              "tagResult": {
                "call": {
                  "service": "ec2",
                  "operation": "create-tags",
                  "args": {
                    "Resources": [{"ref": "volId"}],
                    "Tags": [{"Key": "HygieneReview", "Value": {"env": "today"}}]
                  }
                }
              }
            }
          }
        }
      },
      "onError": "fail"
    },
    "taggedOk": {
      "source": {"entry": {"ref": "tagVols"}},
      "filter": {"present": [{"ref": "entry", "path": "value"}]},
      "result": {"volId": {"ref": "entry", "path": "volId"}}
    },
    "runbook": {
      "forEach": {
        "taggedVol": {
          "from": {"ref": "taggedOk", "path": "volId"},
          "do": {
            "source": {
              "execution": {
                "call": {
                  "service": "ssm",
                  "operation": "start-automation-execution",
                  "args": {
                    "DocumentName": "AWS-CreateSnapshot",
                    "Parameters": {"VolumeId": [{"ref": "taggedVol"}]}
                  }
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
    "runId": {"env": "runId"},
    "asOf": {"env": "now"},
    "window": {"env": "ago", "path": "d90"},
    "staleInstances": {"ref": "staleByRegion"},
    "regionErrors": {"ref": "instErrors"},
    "volumes": {"ref": "volStats"},
    "volumeSnapshots": {"ref": "volSnap"},
    "volumeStatus": {"ref": "volStatus"},
    "logGroups": {"ref": "logGroups"},
    "parameters": {"ref": "params"},
    "network": {"ref": "perRegionNet"},
    "metadata": {"input": "metadata"}
  }
}
```

### 21.1 Coverage

| Construct                                                                                                                          | Where                                                       |
|------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------|
| plan = block + `codemode`, `description`, `inputs`                                                                                 | top level                                                   |
| typed `inputs` with defaults, including an array                                                                                   | `env`, `staleDays`, `logPrefix`, `paramNames`, `metadata`   |
| `let` as a name-keyed map                                                                                                          | top level and inside `perRegionNet`                         |
| `source` + stages (an expression)                                                                                                  | most bindings                                               |
| `let` + `result` (bindings and a value)                                                                                            | top level; `perRegionNet`                                   |
| `source.<name>: call` plus `args`/`region`/`paginate`                                                                              | `insts`, `logGroups`, `volSnap.do.snapshots`                |
| `source.<name>: Expr` (pure dereference, no calls)                                                                                 | `staleByRegion`, `instErrors`, `volStats`, `taggedOk`       |
| equality-correlated `forEach.do` over call sources                                                                                 | `volSnap`, `perRegionNet.subnetsByVpc`, `vpcByInstance`     |
| `forEach.<name>` with `from`/`do`, read via `{"ref": …}`                                                                           | `insts`, `perRegionNet`, `tagVols`, `runbook`               |
| `forEach` with `batch`                                                                                                             | `params` (SSM `Names` caps at 10); `volStatus` (200 ids)    |
| `forEach.<name>.from` as an `input` expression                                                                                     | `params`                                                    |
| nesting: `let` inside `let` (depth 2)                                                                                              | `perRegionNet`                                              |
| `forEach` with `batch` (no separate chunk stage)                                                                                   | `volStatus` (200 ids), `params` (10 names)                  |
| `paginate` bounds: `maxItems`, `pageSize`                                                                                          | `insts`, `vols`, `logGroups`                                |
| `filter` as one relation application                                                                                               | `regions`, `staleByRegion`, `instErrors`                    |
| nested Boolean applications (`and` / `or` / `not`)                                                                                 | `insts`, `vols`                                             |
| constant-array argument = alternatives                                                                                             | `regions.OptInStatus`, `insts.State.Name`, `vols.State`     |
| relation functions: `eq` `ne` `gt` `gte` `lt` `lte` `startsWith` `endsWith` `contains` `present` `absent` `setEq` `setNe` `subset` | `insts`, `vols`, `logGroups`                                |
| all value functions: `casefold` `age` `date` `number` `size` `cidr` `cidrSize`                                                     | `insts`, `volStats`, `volSnap`, `perRegionNet.subnetsByVpc` |
| symmetric case-insensitivity as `eq(casefold(ref/path), casefold(input))`                                                          | `insts.tag:Env`                                             |
| pseudo-path `tag:<name>`                                                                                                           | `insts`, `tagVols`                                          |
| path with `[]` flattening                                                                                                          | `vols.Attachments[].State`, `volSnap.snapshots[]`           |
| `dedup` with explicit keys                                                                                                         | `vols`                                                      |
| aggregator application `groupBy(expr, aggregate)`                                                                                  | `insts`, `volStats.byZone`                                  |
| `fold.<name>` product; constructors `count` `sum` `min` `max` `avg` `distinctCount` `collect`                                      | `volStats`                                                  |
| explicit field products in `result`                                                                                                | `volStatus`, `logGroups`, `params`, `taggedOk`              |
| recursive pure `result` transform (expressions and record products)                                                                | `staleByRegion`, `volSnap`, `perRegionNet`, top level       |
| `result` omitted, defaulting to the unique sink                                                                                    | `perRegionNet.subnetsByVpc` is the sink of its `let`        |
| `result` at plan level assembling refs                                                                                             | top level                                                   |
| expression sources: raw JSON literals, lexical `ref` with optional `path`, and external `input`/`env`                              | throughout                                                  |
| `onError` `fail` / `skip` / `collect`                                                                                              | `tagVols`, `params`, `insts`                                |
| mutation ordering via a data dependency                                                                                            | `runbook` traverses `taggedOk`, derived from `tagVols`      |
| mutating operations (requires `--allow-mutations`)                                                                                 | `tagVols`, `runbook`                                        |

### 21.2 What the fixture should exercise in the *implementation*

- **Derived pruning** (§11.2): `regions` is consumed only through `{"ref":"regions","path":"RegionName"}`; `vols` is consumed by
  four different bindings whose demands union to a wider set; `params` demands nothing beyond its own `result`.
- **Pushdown classification** (§11.2): `vols` mixes clauses that push (`Encrypted eq false`) with clauses that
  cannot (`Size gte 100`, the `subset` over a projection, the cross-key `or`), so it exercises both branches and
  the warning path.
- **Waves and nesting** (§4.2): `regions` → {`insts`, `perRegionNet`}; `vols` and `snaps` in parallel with all
  of it; `volSnap` issuing dependent snapshot queries after `vols`; inner correlated waves inside `perRegionNet`.
- **Read/mutate effect ordering** (§4.2.1): every AWS read call completes before the first mutation call.
  `taggedOk` is a pure binding, so it legitimately runs after `tagVols` to inspect its envelope records; the
  second mutation (`runbook`) then depends on `taggedOk`. No AWS read call occurs after a mutation.
- **Sink defaulting** (§9.1): the nested block omits `result` and must resolve to `subnetsByVpc`; the top level has
  many sinks and must therefore require an explicit `result`.
- **Model-driven defaults** (§12.2): `runbook` intentionally omits `ClientToken`; the botocore model marks it
  as an idempotency token and the client must populate it once per logical task and reuse it for retries.
