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
- **G4 — Output → input binding.** A step can consume any previous step's output, with a small
  expression language for reshaping (select, filter, project, join, string/number/array ops).
- **G5 — Derivable parallelism, two axes.** All concurrency is *derived from the plan's data
  dependencies*, never stated by the plan.
  - **G5a — Fan-out (horizontal).** A step declared as "run once per element of this collection" runs
    its tasks concurrently. Multi-region queries are the canonical case.
  - **G5b — DAG (vertical).** Two steps that do not reference each other's outputs are independent and
    run concurrently. The plan is a DAG, not a list: declaration order is *not* execution order, and
    a plan that answers a question from three unrelated `describe` calls should cost one round-trip,
    not three. Independence is inferred from the absence of output → input binding, subject to the
    safety rules in §4.2.
- **G6 — Parallel fold.** A fan-out step can declare a per-item projection and an associative
  combiner, so results are reduced as they arrive rather than materialized. This makes
  "count/sum/group across N regions or M pages" cheap in memory and small in output.
- **G7 — Pagination is a first-class, bounded concern.** Paginated operations are handled by the
  runtime with explicit limits, and truncation is always reported, never silent.
- **G8 — Filters pushed down.** The system actively steers the agent toward server-side filtering and
  reserves client-side expressions for what the service cannot do.
- **G9 — Partial success is a real outcome.** Per-step and per-item error policies, with a result
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
- Conditional branching beyond expression-level conditionals and "skip this step if the collection is
  empty". No `goto`, no dynamic step construction.
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
did-you-mean candidates; missing required parameter; expression references undefined step; type
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

### 4.1 The step pipeline

Every step is the same five-stage pipeline; features fall out of which stages are present.

```
expand ──▶ invoke ──▶ select ──▶ reduce ──▶ bind
```

- **expand** — optional. An expression yields a collection; the step becomes one *task* per element.
  Absent ⇒ exactly one task. This is the only source of fan-out, and it is derived from data, so the
  runtime knows the fan-out width before the first call.
- **invoke** — run the operation for each task. Pagination turns one task into a *sequence of pages*;
  pages are just another fan-in, handled by the same downstream stages.
- **select** — optional per-item/per-page projection. Applied *before* retention, so a plan can pull
  four fields out of a 40 MB page and never hold the page.
- **reduce** — optional associative combiner over items/pages. Because the combiner comes from a
  fixed allowlist with declared algebraic properties, the runtime may fold partial results in any
  order, as they arrive, in bounded memory.
- **bind** — the step's value is bound to its id and becomes visible to later expressions.

Fan-out width × pages is where a naive agent-written workflow explodes; making both stages the
runtime's responsibility (with `select`/`reduce` available to collapse them) is the core of the
design.

### 4.2 Dependency and concurrency model

A plan is a **DAG, not a script.** `steps` is an unordered set that happens to be written in an array;
execution order is derived, and two steps with no path between them may run at the same time.

**Edges are derived, not declared.** An edge `a → b` exists when any expression in `b` references
`$a`. Every expression position participates: `args`, `region`, `forEach`, `select`, and `after`
(§4.2.3). Nothing else creates an edge — in particular, adjacency in the `steps` array does not.

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
itself fan out. Total in-flight work is `Σ(fan-out width of each running step)`, which is why the
concurrency governor is global rather than per-step (§13.1).

#### 4.2.1 When is independence safe?

Absence of data dependency is *sufficient* for reads and *insufficient* for writes.

- **Read ∥ read — always safe.** Two operations with no observable effect cannot interfere. Order is
  unobservable, so any schedule is correct.
- **Read ∥ write, write ∥ write — not inferable.** Data flow cannot see effect-based dependencies. If
  one step creates a resource and another lists resources of that kind, there is a real dependency
  that no expression reference reveals; running them concurrently makes the result depend on timing.
  API-level eventual consistency makes this worse, not better: even a correctly ordered
  create-then-describe may not observe its own write.

Therefore: **mutating steps are ordering barriers.** A mutating step is scheduled alone — after every
step declared before it and before every step declared after it, in `steps` array order. Within the
read-only regions between barriers, full DAG parallelism applies. This makes the safe case fast and
the dangerous case boring, and it is the reason the read-only classification of §13.4 is load-bearing
for more than just the mutation gate.

#### 4.2.2 Determinism

The *result* must not depend on the schedule. Three rules enforce it:

1. `reduce` combiners are associative (and commutative where completion order is nondeterministic).
2. Collecting fan-out results into a list preserves `forEach` input order, not completion order.
3. Independent steps cannot observe each other by construction (no shared mutable state; each step's
   binding is written once, after that step completes).

Consequence: re-running an unchanged plan against an unchanged environment yields byte-identical
`result`, regardless of how the scheduler interleaved the calls. Only `diagnostics` (durations, retry
counts, error ordering) may vary.

#### 4.2.3 Escape hatch: explicit ordering

Some dependencies are real but invisible to data flow — sequencing a write before a read, or spacing
calls against a rate-limited API. A step may declare `after: ["<id>", …]`, which adds edges without
binding any data. It can only add constraints, never remove them, so it cannot be used to make an
unsafe schedule; a cycle is a validation error.

`after` exists so the agent has a correct way to express ordering it knows about. `help plan` presents it
as the answer to "these must not overlap", and the validator suggests it when a plan's declaration
order implies an intent that the DAG does not enforce.

### 4.3 Expression layer requirements

The binding/reshaping language must be:

- **Pure and total-ish.** No I/O, no host access, no reflection, no `eval`. Errors are values or
  typed failures, never host exceptions escaping the sandbox.
- **Data-shaped.** Filtering, projection, grouping, joining, string/number/date formatting — the
  operations that otherwise force an extra LLM turn just to reshape JSON.
- **Small enough to enumerate exhaustively** in `help` output. If the CLI
  cannot list every supported function on one screen, the surface is too large.
- **Explicitly delimited.** It must be impossible to confuse a literal string argument with an
  expression; interpolation is opt-in per value.

Impure needs that legitimately arise from real command arguments — "start time = 24h ago", "client
request token", "my account id", "the current region" — are satisfied by a runtime-provided binding
resolved once per run and echoed back in the result, keeping the plan reproducible and reviewable
without the runtime having to remember anything.

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

| ID   | Requirement                                                                                                                     |
|------|---------------------------------------------------------------------------------------------------------------------------------|
| F1   | Plan is a single, self-contained, declarative document; JSON or equivalent.                                                     |
| F2   | Steps are identified, and any step's output is addressable by later steps.                                                      |
| F3   | Argument values are literals unless explicitly marked as expressions.                                                           |
| F4   | Fan-out declared as "run per element of this collection"; no explicit concurrency.                                              |
| F4b  | Steps form a DAG from output -> input references; independent read-only steps run concurrently.                                 |
| F4c  | Mutating steps are ordering barriers; `after: [ids]` for ordering data flow cannot see; cycles rejected.                        |
| F4d  | `explain` renders the DAG as execution waves, showing what runs in parallel.                                                    |
| F5   | Per-step region/profile override, so fan-out over regions/accounts is expressible.                                              |
| F6   | Per-item projection (`select`) applied before retention.                                                                        |
| F7   | Associative `reduce` combiners from a fixed allowlist; streaming fold; order-independent.                                       |
| F8   | Pagination policy per step: all / first page / bounded items, with page size, and mandatory truncation reporting.               |
| F9   | Server-side filter parameters surfaced in schema output; validator warns when a client-side filter could have been pushed down. |
| F10  | Per-step error policy: fail the run / skip the item / collect the error and continue.                                           |
| F11  | Result envelope with status (ok / partial / failed), result, and structured diagnostics.                                        |
| F12  | Static validation with actionable, machine-readable diagnostics and suggestions.                                                |
| F12b | Schema lookup accepts multiple queries per invocation, results labelled and budgeted per query.                                 |
| F12c | Schema lookup never returns an empty result; it relaxes and reports which relaxation matched.                                   |
| F12d | Unambiguous name near-misses are corrected in place and answered, not rejected with a suggestion.                               |
| F13  | `explain`: human-readable render, execution waves, and call estimate without any API call.                                      |
| F14  | Read-only classification of every operation; mutations gated behind an explicit flag.                                           |
| F15  | Runtime budgets: max calls, max concurrency, max result bytes, max wall clock, max items.                                       |
| F16  | `help` is self-contained and authoritative: an agent that has read only the help output can author a valid plan.                |
| F16b | Exhaustive, self-documenting list of supported expression functions and combiners in `help`.                                    |
| F16c | `help` addressable by topic and emittable as JSON, for machine consumption.                                                     |
| F17  | Progress reporting on stderr; result on stdout; the two never mix.                                                              |
| F18  | Stateless: nothing persisted between invocations; the envelope and progress stream are the audit trail.                         |
| F18b | `validate`, `explain`, and `run` each require an explicit plan source; `run` always re-validates.                               |
| F19  | Plans accept named inputs so a reviewed plan can be re-run with different parameters.                                           |
| F20  | Idempotent re-planning: same task + same schema ⇒ stable plan shape (no hidden nondeterminism in the format).                   |
| F21  | Discoverable by models that predate the feature: minimal skill pointer, `aws help` topic, in-band hints.                        |
| F22  | Plan supplied inline, from stdin, or from a file; no filesystem write access required to run one.                               |
| F23  | Parser tolerates mechanically unambiguous model-output artifacts (code fences, comments) with a warning.                        |

## 6. Risks

| Risk                                                  | Mitigation                                                                                                         |
|-------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| Model predates the feature and never tries it         | Discovery is a separate, minimal artifact (§18.3) plus in-band paths (§18.4); measured in phase 3.                 |
| Instructions drift from the installed executor        | Help ships with the binary and is generated from the enforced allowlists; skill carries no grammar.                |
| Agent hallucinates operations/parameters              | Tier 1/2 schema lookups + strict validation with did-you-mean; bounded repair loop.                                |
| Agent burns turns guessing keyword terms              | No empty results ever (§15.4); many queries per call; auto-promotion to full signature (§15.5).                    |
| Shell expands `${…}` and mangles the plan             | Heredoc/single-quote forms lead the docs; mangled-expression diagnostic names the cause (§8.1.1).                  |
| Lexical-only retrieval has poor recall                | Curated synonym/acronym/intent table applied before matching; progressive relaxation; `--service --list`.          |
| Agent writes expressions in the wrong dialect         | One dialect only; exhaustive function list in `help`; validator reports unknown functions explicitly.              |
| Runaway fan-out (regions × accounts × pages)          | Estimate before running; hard budgets; require approval above thresholds.                                          |
| Concurrent steps interfere via effects                | Reads never interfere; mutating steps are ordering barriers (§4.2.1); `after` for the rest.                        |
| Result depends on scheduling (nondeterminism)         | Associative combiners, input-order collection, write-once bindings; property tests on fold order.                  |
| Overlapping steps multiply throttling                 | Global concurrency bound plus per-service and per-endpoint caps; adaptive backoff lowers it.                       |
| Silent truncation produces a confidently wrong answer | Truncation is a first-class field in the envelope, and the summarizing agent is instructed to surface it.          |
| Unreviewable expression complexity                    | `explain` renders expressions in prose-ish form; encourage server-side filters; keep the function surface small.   |
| Plan performs mutations the user did not expect       | Read-only default; mutation list shown at the top of `explain`; per-run opt-in.                                    |
| Expression language is a sandbox escape vector        | Data-only evaluator with no host bridge; no dynamic evaluation; fuzz + property tests; no plan-controlled I/O.     |
| Result too large to help the agent                    | `select`/`reduce` encouraged; envelope byte cap with explicit truncation; `explain` warns on likely-large results. |

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

The expression language is **JSONata**, restricted to a pure, enumerated subset. This choice is
carried over from a working prototype (see §16) where an LLM planned JSONata workflows against MCP
tools and a ~100-line interpreter executed them with controlled fan-out.

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

#### 8.1.1 Shell quoting is the real hazard

Plans contain `${ … }`. In `bash`/`zsh` double quotes and in PowerShell double quotes, that is
substitution syntax:

```bash
aws codemode run --plan "{"steps":[…"${ $ver.result }"…]}"   # BROKEN: bad substitution
aws codemode run --plan '{"steps":[…"${ $ver.result }"…]}'       # correct: single quotes
```

Since most agent harnesses build a shell command string, this is the single most likely way a valid plan
becomes a failed invocation. Two mitigations, both required:

1. **`help` leads with the safe forms**, in this order: a stdin heredoc with a *quoted* delimiter (no
   expansion at all, no escaping, no length limit), then single-quoted inline, then `file://`.

   ```bash
   aws codemode run --plan - <<'PLAN'
   { "codemode": "v1", … }
   PLAN
   ```

2. **The diagnostic recognizes the damage.** A plan whose expressions arrive empty or mangled
   (`"$var"` → `""`, or a literal `bad substitution` in the input) is reported as a probable shell
   expansion problem with the heredoc form shown, rather than as a generic schema error.

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

### 9.1 Shape

```jsonc
{
  "codemode": "v1",                  // format version; required
  "description": "…",                // one line, for the human review render; required
  "inputs": {                        // optional; named, typed parameters (F19)
    "tagKey": { "type": "string", "default": "Env" }
  },
  "steps": [
    {
      "id": "regions",               // identifier; unique; becomes $regions
      "service": "ec2",
      "operation": "describe-regions",
      "args": { … },                 // literals, or "${ <jsonata> }" for expressions
      "region": "us-east-1",         // optional override (literal or expression)
      "profile": "…",                // optional override; gated (§13.4)
      "paginate": { … },             // optional; see §11
      "forEach": "${ <collection> }",// optional; fan-out (F4)
      "after": ["<id>"],             // optional; ordering edge without data binding (§4.2.3)
      "select":  "${ <projection> }",// optional; per item/page (F6)
      "reduce":  "concat",           // optional; associative combiner (F7)
      "onError": "fail"              // fail | skip | collect
    }
  ],
  "result": "${ <jsonata> }"         // final projection; required, and required to be small
}
```

### 9.2 Semantics

- **Literals vs expressions.** A string is a literal unless it is exactly `${ … }`, in which case the
  interior is a JSONata expression evaluated against the current scope. Nested `${…}` inside larger
  strings is *not* supported; use `&` concatenation inside a single `${…}` instead. This keeps the
  literal/expression boundary unambiguous for both the parser and the model.
- **Scope.** Each completed step binds `$<id>`. Inside a fan-out step, `$item` is the current element
  and `$index` its position. Inside `select` on a paginated step, `$page` is the current page.
  `$inputs` holds resolved plan inputs. `$env` holds runtime-provided impure values (§12).
- **Ordering.** `$<id>` for a fan-out step without `reduce` is a list in the *input order* of
  `forEach`, regardless of completion order. Each element is
  `{ "item": <element>, "output": <value> }` — matching the prototype — so results stay correlated
  with the input that produced them (essential for the multi-region case, where the region is the
  key).
- **Errors as data.** With `onError: "collect"`, a failed task contributes
  `{ "item": …, "error": { "code": …, "message": …, "retryable": … } }` instead of `output`. Global
  `$errors` maps step id → list of errors regardless of policy.
- **Empty collections.** A fan-out over an empty collection is a success producing an empty list; it
  is not an error. This prevents whole-plan failure on "no resources in this region".
- **Order.** `steps` is a set. Execution order comes from expression references and `after`, so moving
  two independent steps around in the array changes nothing. Declaration order matters in exactly one
  place: it defines the barrier position of mutating steps (§4.2.1).

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

### 9.4 Worked example: multi-region fan-out (the parallelism case)

Task: _"running instances tagged `Env=prod` across all enabled regions, grouped by region and
instance type."_

```json
{
  "codemode": "v1",
  "description": "Running Env=prod instances per enabled region, counted by instance type",
  "steps": [
    {
      "id": "regions",
      "service": "ec2",
      "operation": "describe-regions",
      "args": {
        "Filters": [
          { "Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"] }
        ]
      }
    },
    {
      "id": "inst",
      "forEach": "${ $regions.Regions.RegionName }",
      "region": "${ $item }",
      "service": "ec2",
      "operation": "describe-instances",
      "args": {
        "Filters": [
          { "Name": "tag:Env", "Values": ["prod"] },
          { "Name": "instance-state-name", "Values": ["running"] }
        ]
      },
      "paginate": { "mode": "all", "maxItems": 5000 },
      "select": "${ $page.Reservations.Instances.{ 'type': InstanceType, 'id': InstanceId } }",
      "reduce": "concat",
      "onError": "collect"
    }
  ],
  "result": "${ $inst.{ 'region': item, 'total': $count(output), 'byType': output{ type: $count($) }, 'error': error.code } }"
}
```

What the runtime derives, and the plan never states:

- fan-out width = number of enabled regions (~17–34), each against a different regional endpoint;
- concurrency = min(global ceiling, per-service ceiling), with throttle backpressure;
- pages per region fetched serially within a region (pagination is inherently sequential), regions in
  parallel;
- `select` discards the ~50 fields per instance the plan does not need, per page;
- `reduce: concat` collapses pages as they arrive, so peak memory is O(selected items), not
  O(response bytes);
- a region that fails (SCP denial, endpoint outage) yields an `error` entry and does not fail the run.

The two filters are **server-side** — the correct default. The client-side work is only the
projection and grouping, which no `describe-instances` parameter can do.

### 9.5 Worked example: DAG parallelism (unrelated reads)

Task: _"give me a security overview of this account: publicly-open security groups, unencrypted
volumes, and IAM users without MFA."_

Three unrelated reads. No step references another, so all three are one wave.

```json
{
  "codemode": "v1",
  "description": "Account security overview: open SGs, unencrypted volumes, users without MFA",
  "steps": [
    { "id": "openSgs",
      "service": "ec2", "operation": "describe-security-groups",
      "args": { "Filters": [ { "Name": "ip-permission.cidr", "Values": ["0.0.0.0/0"] } ] },
      "paginate": { "mode": "all" },
      "select": "${ $page.SecurityGroups.{ 'id': GroupId, 'name': GroupName, 'vpc': VpcId } }",
      "reduce": "concat" },

    { "id": "openVols",
      "service": "ec2", "operation": "describe-volumes",
      "args": { "Filters": [ { "Name": "encrypted", "Values": ["false"] } ] },
      "paginate": { "mode": "all" },
      "select": "${ $page.Volumes.{ 'id': VolumeId, 'size': Size, 'state': State } }",
      "reduce": "concat" },

    { "id": "users",
      "service": "iam", "operation": "list-users",
      "paginate": { "mode": "all" },
      "select": "${ $page.Users.UserName }",
      "reduce": "concat" },

    { "id": "mfa",
      "forEach": "${ $users }",
      "service": "iam", "operation": "list-mfa-devices",
      "args": { "UserName": "${ $item }" },
      "select": "${ $count($page.MFADevices) }",
      "reduce": "sum",
      "onError": "collect" }
  ],
  "result": "${ { 'openSecurityGroups': $openSgs, 'unencryptedVolumes': $openVols, 'usersWithoutMfa': $mfa[output = 0].item } }"
}
```

Derived schedule:

```
wave 1:  openSgs ∥ openVols ∥ users        (three independent reads, no bindings between them)
wave 2:  mfa                               (depends on $users; itself fans out over every user)
```

`openSgs` and `openVols` hit the same service and region and still run concurrently — same-service is
not a dependency, and the governor's per-service cap is what keeps that polite. `mfa` waits only on
`users`, not on the EC2 steps, so a slow `describe-volumes` does not delay the user fan-out. Written
as separate `aws` invocations this is four round-trips plus N for the users; here it is two waves.

Note what makes this safe to infer: every operation is a read. Had the plan included, say,
`ec2:authorize-security-group-ingress`, that step would become a barrier and the reads around it would
be split into two waves (§4.2.1).

### 9.6 Worked example: parallel fold with no retained data

Task: _"how many objects and total bytes in each of these 12 buckets?"_

```json
{
  "codemode": "v1",
  "description": "Object count and total size per bucket",
  "steps": [
    { "id": "buckets", "service": "s3api", "operation": "list-buckets",
      "select": "${ $page.Buckets.Name }" },
    { "id": "stats",
      "forEach": "${ $buckets }",
      "service": "s3api", "operation": "list-objects-v2",
      "args": { "Bucket": "${ $item }" },
      "paginate": { "mode": "all", "maxItems": 1000000, "pageSize": 1000 },
      "select": "${ { 'n': $count($page.Contents), 'bytes': $sum($page.Contents.Size) } }",
      "reduce": "sumFields",
      "onError": "collect" }
  ],
  "result": "${ $stats.{ 'bucket': item, 'objects': output.n, 'bytes': output.bytes, 'error': error.code } }"
}
```

Millions of keys can flow through this plan while retained state stays at two integers per bucket.
This is the concrete payoff of making `select` and `reduce` part of the step pipeline rather than
leaving reduction to the final `result` expression.

## 10. Expression subset

### 10.1 Rules

- Dialect: JSONata. One dialect, no alternatives, to avoid the JMESPath/JSONata/JSONPath confusion
  that dominates LLM syntax errors. (JMESPath remains the language of `--query` for single commands;
  `help expressions` states this boundary explicitly.)
- Allowlisted functions only. An unknown or non-allowlisted function is a **validation error** with
  the allowed list in the diagnostic, not a runtime surprise.
- Lambdas are permitted inside allowlisted higher-order functions (`$map`, `$filter`, `$reduce`,
  `$sift`, `$sort`, `$single`). Recursive user functions are rejected (no unbounded computation).
- Expression evaluation is bounded: node budget, output size budget, and a wall-clock timeout per
  evaluation. Exceeding a bound is a typed step failure, subject to `onError`.
- Regex literals are permitted (`/Polymorphic|Validator/`) but compiled with a size/complexity limit
  to avoid catastrophic backtracking.

### 10.2 Allowed (enumerated in `aws codemode help expressions`)

| Category    | Functions                                                                                                                                                         |
|-------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| String      | `$string $length $substring $substringBefore $substringAfter $uppercase $lowercase $trim $pad $contains $split $join $match $replace $base64encode $base64decode` |
| Number      | `$number $abs $floor $ceil $round $power $sqrt $formatNumber $formatInteger $parseInteger`                                                                        |
| Aggregation | `$sum $max $min $average $count`                                                                                                                                  |
| Array       | `$append $reverse $sort $shuffle*` (see below) `$zip $distinct $filter $map $reduce $single`                                                                      |
| Object      | `$keys $lookup $merge $spread $sift $each $type $exists`                                                                                                          |
| Boolean     | `$boolean $not`                                                                                                                                                   |
| Date (pure) | `$fromMillis $toMillis` — formatting/parsing only                                                                                                                 |
| Conditional | `? :`, `and or`, comparison and `in` operators                                                                                                                    |

`$shuffle` and any other order-nondeterministic function are **excluded** — determinism outranks
completeness.

### 10.3 Rejected outright

`$now`, `$millis`, `$random`, `$eval`, `$assert`, `$error`, `$clone` of host objects, and any
function that reads the clock, the entropy pool, or the host. The first three are the ones an agent
will reach for; the diagnostic for each names the `$env` replacement (§12) so the correction is
mechanical.

## 11. Pagination

### 11.1 Policy

```jsonc
"paginate": {
  "mode": "all" | "first" | "limit",   // default: "all" for paginated ops
  "maxItems": 5000,                    // required when mode = "limit"; capped by budget
  "pageSize": 1000                     // hint; clamped to the operation's legal range
}
```

- Whether an operation paginates, and what its tokens/limit keys are, comes from the shipped service
  models (the CLI's existing paginator configuration) — the plan does not describe pagination
  mechanics, only policy.
- `mode: "all"` is bounded by the run budget for total items and bytes (§13.3). Hitting the bound
  stops that step and records a truncation, it does not error.
- `select` runs per page, before retention. Combined with `reduce`, `mode: "all"` over a huge listing
  is memory-safe.
- Pagination is sequential per task (token chaining) and parallel across fan-out tasks.
- **Truncation is loud.** Any truncated step appears in `diagnostics.truncated[]` with the step id,
  the reason (`maxItems` / `bytes` / `time`), and the count retrieved. Status becomes `partial`.
  `help errors` instructs the agent to state incompleteness in its answer rather than presenting a
  truncated count as a total.

### 11.2 Server-side vs client-side filtering

Ranked preference, documented in `help plan` and enforced by validator warnings:

1. **Server-side filter parameters** — `ec2 describe-instances --filters`, `resourcegroupstaggingapi
   get-resources --tag-filters`, `s3api list-objects-v2 --prefix`, `logs filter-log-events
   --filter-pattern`, `cloudwatch get-metric-data` expressions, `dynamodb query` key conditions. Cost
   is paid by the service; fewer pages; lower latency.
2. **Server-side projections** — `--attribute-names`, `--projection-expression`, `--include`/
   `--exclude` style parameters where available.
3. **`select` in the step** — reduces what the runtime retains, but the bytes were already
   transferred.
4. **Client-side filtering in `result`** — last resort; the full page set has already been paid for.

Tier 2 schema output labels each parameter with `filter: server` where the service model or curated
metadata says so, so the agent can see the pushdown opportunity while planning. The validator emits a
warning (not an error) when a step paginates `mode: "all"` and the only filtering happens
client-side, and `explain` shows that warning to the human. Rationale for warn-not-error: the
mapping from an arbitrary client predicate to a service filter is not always sound, and false
rejections would push agents back to multi-turn.

## 12. Impure inputs

The interpreter has no clock and no entropy. What real commands need instead is a single binding,
resolved once at run start, before any call, and reported back in the result:

| Binding                                     | Value                                                                    |
|---------------------------------------------|--------------------------------------------------------------------------|
| `$env.now`                                  | ISO-8601 timestamp, run start                                            |
| `$env.nowMillis`                            | epoch millis, run start                                                  |
| `$env.today`                                | `YYYY-MM-DD`, run start, UTC                                             |
| `$env.runId`                                | UUIDv4 for this run                                                      |
| `$env.uuid`                                 | list of pre-generated UUIDs for idempotency tokens (`$env.uuid[$index]`) |
| `$env.region`                               | effective default region                                                 |
| `$env.accountId`, `$env.arn`, `$env.userId` | caller identity, resolved lazily via STS only if referenced              |
| `$env.partition`                            | `aws`, `aws-cn`, `aws-us-gov`                                            |

Every referenced binding and its resolved value is echoed in the envelope's `diagnostics.env`, so a
reviewer can see exactly what "24 hours ago" meant and can reproduce the run by pinning those values as
plan `inputs`. Nothing is stored to make this work — the values travel with the result. Relative times are computed
with pure arithmetic on `$env.nowMillis` plus `$fromMillis`:

```
"StartTime": "${ $fromMillis($env.nowMillis - 24 * 60 * 60 * 1000) }"
```

`$env.accountId` requiring an STS call is the one impure resolution that touches the network; it is
an ordinary operation invocation, appears in the call log, and is skipped when unreferenced.

## 13. Execution

### 13.1 Scheduling

**Graph construction** (in the normalizer, before any call):

1. Parse every expression in every step; collect referenced `$<id>` bindings. Those become the step's
   data dependencies. `$env` and `$inputs` are not steps and create no edges.
2. Add the edges declared by `after`.
3. Add barrier edges around every mutating step: for a mutating step at declaration index `i`, add an
   edge from every step with index `< i` and to every step with index `> i` (§4.2.1). Read-only plans —
   the default and the common case — get none of these.
4. Reject cycles (validation error, with the cycle path in the diagnostic). Reject references to
   undefined ids.
5. Compute the topological levels used by `explain` for its wave display.

**Execution.** A single work-stealing pool serves the whole run; there is no per-step pool. A task is
`(step, fan-out element | none, page cursor)`. A step becomes *ready* when all its predecessors have
bound; ready steps' tasks all enter the same queue, so the scheduler naturally overlaps "the tail of a
wide fan-out" with "the start of an independent step" instead of idling at wave boundaries. Waves are a
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

| Class         | Example                                      | Default handling                                   |
|---------------|----------------------------------------------|----------------------------------------------------|
| Plan invalid  | unknown operation, bad expression            | Reject before execution; exit 252                  |
| Configuration | no credentials, no region, unknown profile   | Reject before execution; exit 253                  |
| Authorization | `AccessDenied`, SCP denial                   | Per `onError`; typically `collect` in fan-out      |
| Throttling    | `Throttling`, `RequestLimitExceeded`         | Retry with backoff, then per `onError`             |
| Transient     | 5xx, timeouts, endpoint unreachable          | Retry, then per `onError`                          |
| Not found     | `NoSuchBucket`, `InvalidInstanceID.NotFound` | Per `onError`; `skip` is often right               |
| Expression    | type error, budget exceeded                  | Step failure per `onError`; never a host exception |
| Budget        | max calls/bytes/time exceeded                | Stop cleanly, mark `partial`, return what exists   |
| Cancelled     | sibling step failed with onError=fail        | Reported as `cancelled`, not as an error           |
| Interrupt     | Ctrl-C                                       | Cancel in-flight, return partial, exit 130         |

`onError` values: `fail` (abort the run; default for non-fan-out steps), `skip` (drop the failed item;
default for fan-out steps), `collect` (retain the error as data alongside successes). `--on-error
strict` overrides every step to `fail` for use in automation.

### 13.3 Budgets (hard, runtime-owned)

| Budget                   | Default                              | Flag                 |
|--------------------------|--------------------------------------|----------------------|
| Total API calls          | 500                                  | `--max-calls`        |
| Concurrency              | 8 (ceiling 32)                       | `--max-concurrency`  |
| Items retrieved per step | 100 000                              | `--max-items`        |
| Result envelope bytes    | 256 KB                               | `--max-result-bytes` |
| Wall clock               | 300 s                                | `--timeout`          |
| Expression evaluations   | node/size/time bounds per evaluation | not user-tunable     |

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
- **Credentials.** Normal resolution chain. `profile` overrides per step are rejected unless
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
$ aws codemode explain --plan overview.json
Account security overview: open SGs, unencrypted volumes, users without MFA
4 steps   2 waves   read-only   no mutations

wave 1  (3 steps in parallel)
  openSgs   ec2:DescribeSecurityGroups   us-east-1   paginate all      ~1-3 calls
            server-filter: ip-permission.cidr=0.0.0.0/0
  openVols  ec2:DescribeVolumes          us-east-1   paginate all      ~1-8 calls
            server-filter: encrypted=false
  users     iam:ListUsers                us-east-1   paginate all      ~1-2 calls

wave 2  (1 step)
  mfa       iam:ListMFADevices           us-east-1   fan-out over $users   ~N calls
            depends on: users            on error: collect

regions contacted: us-east-1
credential scopes: default profile
estimated calls:   3 - 13 + N(users)      budget: 500
estimated result:  small (projected fields only)
```

What a reviewer can answer from this that they cannot answer from a stream of individual commands:
does anything mutate (no), how wide does this go (one region, one profile), how many calls could it
possibly make, and which steps overlap. `--output json` emits the same graph for programmatic review.
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
    "env": { "now": "2026-08-27T20:59:00Z", "accountId": "…" },
    "truncated": [ { "step": "stats", "reason": "maxItems", "retrieved": 100000 } ],
    "errors":    [ { "step": "inst", "item": "ap-east-1", "code": "AuthFailure", "message": "…" } ],
    "warnings":  [ { "step": "inst", "code": "ClientSideFilter", "message": "…" } ]
  }
}
```

Because nothing is persisted (G13), this envelope *is* the record: the resolved `$env` bindings that
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
ec2:DescribeInstances  (reads, paginated: NextToken/MaxResults, items: Reservations)
in:
  Filters: [ {Name: string, Values: [string]} ]     filter: server
  InstanceIds: [string]                             filter: server
  MaxResults: int (5..1000)
out:
  Reservations: [ { OwnerId, ReservationId,
      Instances: [ { InstanceId, InstanceType, State: {Name: enum(pending|running|
        shutting-down|terminated|stopping|stopped)}, PrivateIpAddress, PublicIpAddress,
        LaunchTime: timestamp, Placement: {AvailabilityZone}, Tags: [{Key, Value}], …+38 } ] } ]
  NextToken: string
notes:
  Filters supports tag:<key>, instance-state-name, vpc-id, availability-zone, …
```

Design points:
- Compact signature notation, not JSON Schema — roughly 5–10× fewer tokens for the same information.
- Depth-pruned with an explicit `…+38` marker so the agent knows fields exist and can ask for more
  (`--depth 3`, `--fields Instances.NetworkInterfaces`).
- Pagination and item-path facts included, because they determine `paginate` and `select`.
- `filter: server` markers implement the pushdown steering of §11.2.
- Sourced from the service models already on disk: offline, free, always version-matched to the CLI.
- Batchable and mixable with free-text queries in one invocation (§15.2), so schema discovery for a
  whole plan is one turn, not k turns.

### 15.7 Validation as the real schema channel

```
$ aws codemode validate --plan plan.json
error  steps[1].operation   unknown operation 'ec2:DescribeInstance'
       did you mean: DescribeInstances, DescribeInstanceStatus, DescribeInstanceTypes?
error  steps[1].args.Filter unknown parameter 'Filter' for ec2:DescribeInstances
       did you mean: Filters?
error  steps[2].result      $intances is not a defined step (defined: regions, inst)
error  steps[3].after       cycle: mfa -> users -> mfa
warn   steps[1]             paginate.mode="all" with client-side filtering in result;
                            ec2:DescribeInstances supports server-side Filters
warn   steps[2]             no dependency on steps[1]; these will run in parallel.
                            if ordering is required, add "after": ["cleanup"]
```

Diagnostics are also emitted as JSON (`--output json`) with pointer, code, message, and candidates,
so an agent can repair mechanically. The economics: a repair turn costs a few hundred tokens; an
execution turn costs a full API payload.

## 16. Prototype evidence

A working prototype of this execution model exists in
`~/projects/hello/hello-spring-ai-bedrock` (`synth/Workflow.kt`, `synth/WorkflowInterpreter.kt`,
`synth/SynthScenario.kt`). It plans a JSONata workflow against an MCP tool catalog and executes it
without a model in the loop. Findings that shaped this spec:

- **The data-structure-plus-expressions split works.** Tool invocation and fan-out are *structural*
  (fields in a JSON document); only data reshaping is expression-level. The interpreter is ~100 lines
  and has no way to do anything but call tools and evaluate JSONata.
- **`${ … }`-delimited expressions** removed literal/expression ambiguity for the model.
- **Model output needed defensive parsing.** The prototype's `Workflow.parse` strips markdown fences
  before deserializing, because the planner wrapped its JSON in ```` ```json ```` regularly. §8.1.2
  makes that tolerance an explicit, warned behaviour rather than an undocumented hack.
- **Binding each step's output as `$<id>`, and the fan-out element as `$item`,** was learned by the
  model from a short grammar plus one example.
- **Returning `{item, output}` pairs from fan-out** turned out to be essential: the final projection
  needs the input key (there, the symbol; here, the region) to make sense of each result.
- **Concurrency must not be in the plan.** The prototype fixed `maxConcurrency=6` in the interpreter
  and instructed the planner to never mention concurrency; the resulting plans were stable.
- **Step-level parallelism was left on the table.** The prototype executed `steps` strictly
  sequentially and only parallelized within a fan-out step. In the javadoc trace that cost nothing
  (each step genuinely fed the next), but it is the wrong default: the sequential loop is an accident
  of the implementation, not a property of the plans. §4.2 makes the graph explicit so unrelated reads
  overlap.
- **Prompt shaping mattered more than grammar.** Two instructions did the heavy lifting: *filter
  before you fan out*, and *make the result as small as possible*. Both are promoted to
  first-class mechanisms here (`select`/`reduce`, plus validator warnings), because instruction alone
  is not enforcement.
- **JSONata-vs-JSONPath confusion was the top failure mode**, which is why §10 mandates a single
  dialect, an exhaustive function list in `help`, and explicit unknown-function diagnostics.

Prototype trace (12-way fan-out over javadoc symbols, one planning turn, no model in the execution
loop) is reproduced in the prototype's logs and matches the progress format of §14.

## 17. Implementation notes (aws-cli v2)

- New customization package `awscli/customizations/codemode/` registered as a command group, in the
  established style of `awscli/customizations/wizard/` and `.../agenttoolkit/`.
- Modules: `plan.py` (parse/normalize; immutable dataclasses — parsing yields a `ValidPlan` type that
  cannot be constructed from an invalid document), `graph.py` (dependency extraction from expression
  ASTs, barrier insertion, cycle detection, topological levels), `schema.py` (Tier 1/2 projections over
  the shipped service models), `validate.py`, `explain.py`, `expr.py` (JSONata wrapper + allowlist +
  budgets), `executor.py` (scheduler, invoker, reducer, governor), `envelope.py`. There is no persistence layer.
- `schema.py` is backed by an index built at package time from the shipped service models: term →
   operations, name trigrams for fuzzy matching, and the curated vocabulary table (§15.4). Shipping the
   index rather than scanning models at run time keeps a batched multi-query lookup to a few
   milliseconds, which matters because the agent may issue one on every task. Lookup is pure and
   deterministic, so the whole ranking is golden-testable with no AWS calls and no model.
- Dependency extraction reads the compiled expression AST rather than regex-scanning for `$name`, so a
  reference inside a string literal or a lambda parameter shadowing a step id cannot produce a phantom
  or missing edge. This is the correctness-critical part of DAG parallelism: a *missed* edge is a race,
  so extraction must over-approximate if it is ever unsure, and `expr.py` must expose the binding set
  as a first-class result of compilation rather than as a side channel.
- Plan source resolution lives in `plan.py` and reuses `awscli/paramfile.py` for `file://`/`fileb://`
  rather than reimplementing path handling; the inline/stdin/TTY rules of §8.1 are a thin wrapper over it.
- Invocation goes through `botocore` clients and the existing paginator configuration rather than
  re-entering `clidriver` argument parsing: plans carry structured arguments already, so there is no
  string round-trip. Parameter-name normalization reuses the CLI's existing arg-name mapping so
  `--filters`/`Filters` both resolve.
- The reducer combiner allowlist (`concat`, `sum`, `sumFields`, `count`, `min`, `max`, `merge`,
  `mergeDeep`, `groupCount`) is closed and each entry declares associativity/commutativity, which is
  what licenses out-of-order folding.
- **Dependency decision required:** a JSONata implementation for Python. Options: vendor an existing
  pure-Python implementation (license and maintenance review needed), or implement the enumerated
  subset directly (more work, but the subset is small, the semantics are then exactly the documented
  ones, and there is no third-party sandbox surface). Given G10, the second option is the safer
  default and the first is the faster path; this is the main open build/buy call. Either way,
  expressions are compiled once and evaluated per item, and a fresh evaluation context per task keeps
  fan-out thread-safe (as in the prototype).
- No persistence layer, no state directory, nothing under `~/.aws/`: the executor holds the plan and
  intermediate bindings in memory for the life of the process and exits. Redaction still applies, but to
  the *streams* rather than to a stored record — response members marked sensitive in the service model
  are redacted from progress output on stderr, and the envelope contains only what the plan's `select`
  and `result` expressions asked for, which is the caller's choice to make.
- **Testing (test-onion):** types first (a normalized plan makes illegal states unrepresentable —
  e.g. `reduce` without `select` is not constructible where it is meaningless); pure unit tests for
  expressions, pagination planning, budget arithmetic, and combiner algebra (property tests:
  fold order does not change the result); `botocore` stubber integration tests for the executor
  including throttling, partial failure, and truncation paths; end-to-end tests against local mock
  endpoints; the multi-region example of §9.4 as a golden test.
- **Help content** lives beside the implementation and is partly *generated*: the `expressions` and
  `combiners` topics are rendered from the same allowlist tables `expr.py` and the reducer enforce, so
  the manual cannot drift from the validator (§18.2). A test asserts that every allowlisted function
  appears in help and vice versa.
- **Agent skill** delivered through the existing `aws configure agent-toolkit` mechanism is a pointer
  only (§18.3): trigger condition, the `aws codemode help` command, and a fallback line for older
  installs. Target a few hundred tokens. It deliberately contains no grammar, so it cannot go stale
  against the binary.

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

| Topic         | Contents                                                                                                                                                      | Budget       |
|---------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------|
| (default)     | What it is, when to use it vs single commands, the subcommands, the minimum viable plan, and the topic index                                                  | ~600 tokens  |
| `plan`        | Full plan grammar: every field, literal-vs-`${…}` rules, scope (`$<id>`, `$item`, `$index`, `$page`, `$inputs`, `$env`), fan-out, `select`, `reduce`, `after` | ~1200 tokens |
| `passing`     | How to hand a plan to the CLI: heredoc, single-quoted inline, `file://`; the shell-quoting hazard of §8.1.1                                                   | ~250 tokens  |
| `expressions` | The exhaustive allowlist of §10.2, the rejected list of §10.3 with their `$env` replacements, and 10 idiom one-liners (filter, group, project, join)          | ~900 tokens  |
| `combiners`   | The closed `reduce` allowlist with the shape each expects and produces                                                                                        | ~200 tokens  |
| `errors`      | `onError` semantics, error taxonomy, partial results, truncation, exit codes                                                                                  | ~500 tokens  |
| `examples`    | 4 worked plans: single chain, multi-region fan-out, independent-reads DAG, parallel fold                                                                      | ~1200 tokens |
| `schema`      | One lookup call, many queries; keywords not questions; the signature notation; the vocabulary it knows                                                        | ~500 tokens  |
| `limits`      | Budgets, defaults, and which are overridable                                                                                                                  | ~200 tokens  |

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
2. Fan-out, `select`, `reduce`, budgets, partial results.
3. Schema service (Tier 1/2), the pointer skill, and the `aws help` topic; benchmark against
   multi-turn baselines. Adoption is measurable here: how often does an agent with only the pointer
   in context reach `codemode help` on a task that warrants it?
4. `inputs` for parameterized plans, re-run from files the caller manages (still no CLI-side storage);
   mutation support behind `--allow-mutations`; policy hooks so an organization can restrict permitted
   operations, regions, or budgets by configuration.

## 20. Open questions

1. **Expression engine: vendor vs implement the subset?** (§17) The determining factor should be
   whether a vendored implementation can be audited to have no host bridge.
2. **JMESPath compatibility.** Users know `--query`. Do we accept a `jmespath:` prefix for simple
   projections, or hold the line on one dialect? Current recommendation: hold the line in v1;
   revisit if telemetry shows dialect errors dominating.
3. **How much curated metadata** is needed on top of the service models for read-only classification,
   server-side-filter labelling, and the vocabulary table of §15.4 — and where does it live (CLI data
   files vs generated)? The synonym/intent table is the piece most likely to rot: it needs an owner, a
   test that every entry resolves to an operation that still exists, and a decision on whether recall
   gaps get fixed by adding entries or by accepting the `--service --list` fallback.
4. **Cross-account fan-out.** Assume-role fan-out over an Organization is the natural next request
   after multi-region. It is a large blast-radius increase; does it belong in v1 gated behind a flag,
   or later with its own review UX?
5. **Should `explain` be the agent's contract too?** i.e. should the harness be required to show the
   rendered explanation to the user before calling `run --yes`, and should that be enforceable?
6. **Plan-level conditionals.** Deliberately excluded, but "skip this step if the previous result is
   empty" recurs. Does the empty-fan-out rule of §9.2 cover enough cases in practice?
7. **Exit code `1` for partial.** Consistent with the `s3` precedent, but agents and CI may want to
   distinguish "partial but useful" from "failed"; is a dedicated code warranted?
8. **In-band discovery hints.** (§18.4) A stderr nudge after repeated similar commands is the most
   effective way to reach agents without the skill, and the most likely to annoy everyone else.
   Config-gated, default off or default on? Does it risk being parsed as part of a command's output by
   naive harnesses that merge streams?
9. **Are mutation barriers too conservative?** (§4.2.1) Serializing *every* write against *every*
   other step is safe and easy to explain, but a plan that creates 50 unrelated resources gets no
   parallelism at all. A per-resource-scope relaxation is possible but requires effect metadata the
   service models do not carry. Defer until there is demand for write-heavy plans.
10. **Does `help` belong in the CLI's standard help system or as bespoke output?** The topic system
    (`awscli/topics/`) gives free discovery via `aws help topics`, but the token budgets and
    `--output json` requirement of §18.2 push toward generated, purpose-built output. Possibly both:
    generated content surfaced through the topic index.
