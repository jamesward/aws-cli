# TOWL — Tool Orchestration Workflow Language Specification

Status: draft / request for comment  
Language version: `v1`

TOWL stands for **Tool Orchestration Workflow Language**. It is a provider-neutral, declarative JSON language for describing a bounded graph of typed tool invocations and pure data transformations. A producer—an AI agent, a human, or another program—authors one document; a user or host may review it; a deterministic processor validates and executes it without requiring another model turn.

This document owns the TOWL wire format and language semantics. Provider catalogs and host products define concrete operations, schemas, capabilities, authority, policy, and user interfaces.

---

## 1. Scope and conformance

A conforming TOWL processor:

1. accepts strict JSON after any host-owned preprocessing;
2. parses only documents whose `towl` version it supports;
3. resolves every operation against an immutable catalog snapshot;
4. constructs a typed validated plan before invoking any operation;
5. derives dependencies from references rather than document position;
6. executes only through injected operation and context interfaces;
7. preserves the logical semantics and ordering rules in this specification; and
8. reports invalid documents, unsupported catalog capabilities, policy rejection, execution errors, and partial results distinctly.

TOWL itself does not define command-line syntax, files, network transports, credentials, provider targets, provider-specific pagination tokens, or deployment. Those belong to catalogs, profiles, and hosts.

### 1.1 Normative terms

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative.

A **document** is authored JSON. A **validated plan** is the typed immutable representation produced after parsing, name resolution, catalog resolution, type checking, and capability checking. Only a validated plan may reach execution.

### 1.2 Strict JSON boundary

Canonical TOWL is strict JSON. Duplicate object keys are invalid. Comments, trailing commas, Markdown fences, YAML, and JSON5 are not TOWL syntax. A host may preprocess mechanically unambiguous input, but it MUST report that transformation and pass strict JSON to the TOWL parser.

A host MAY instead deliver a document as an already-parsed JSON value — for example, structured tool-call arguments validated against a published input schema — rather than as text. Object delivery moves the textual concerns (duplicate keys, encoding) to whichever layer first parsed text; everything after that point is unchanged. Any intake layer, typed or untyped, MUST be lossless: it MUST NOT drop, inject, or coerce members, and unknown members MUST reach the TOWL parser so that closed-shape and placement diagnostics still apply. A published input schema MAY type the closed structural vocabulary while leaving registry-dependent positions — expressions, aggregators, results, and name-keyed binder maps — open.

---

## 2. Goals and non-goals

### 2.1 Goals

- **One reviewed artifact.** The complete logical computation is available before effects begin.
- **Typed operation composition.** Operation outputs may feed later inputs through explicit references checked against catalog schemas.
- **Derived DAG parallelism.** Independent bindings are eligible for concurrent execution without authored ordering.
- **Data-driven traversal.** A fixed block may execute once per element of a collection; concurrency remains processor-owned.
- **Bounded streams.** Stream-producing operations may expose capability-gated pagination bounds and truncation diagnostics.
- **Closed pure computation.** Filtering, shaping, and aggregation use enumerable typed algebras rather than arbitrary code.
- **Partial outcomes.** Errors may fail, skip, or become data according to explicit policy.
- **Provider neutrality.** The core does not depend on any particular API protocol, cloud, SDK, schema source, or tool transport.
- **Static review.** A processor can describe dependencies, logical invocations, effects, capabilities, and bounds without execution.
- **No ambient authority.** Documents can invoke only catalog operations and immutable context values made available by the host.

### 2.2 Non-goals for v1

- general-purpose programming;
- recursion or user-defined functions;
- arbitrary branching or control loops;
- timers, waits, resumable process instances, messages, or human tasks;
- filesystem, process, or arbitrary-network primitives;
- authored concurrency values;
- implicit joins or unbounded nested loops;
- a universal API schema or capability discovery protocol.

---

## 3. Catalog and host contracts

TOWL is parameterized by an **operation catalog**. The catalog is frozen for one validation/execution lifecycle.

```text
CatalogIdentity = { name: String, version?: String, fingerprint: ContentHash }
RegistryIdentity = { name: String, version?: String, fingerprint: ContentHash }

OperationCatalog = {
  identity: CatalogIdentity,
  resolve(service?, operation) -> ResolvedOperation | Unknown | Ambiguous
}

ResolvedOperation = {
  id: OperationId,
  input: Shape,
  result: OperationResult,
  effects: EffectSet,
  paging: NotPaged | Paged(PagingDescriptor),
  correlation: CorrelationCapabilities,
  sourceMappings: [SourceMapping],
  inputLimits: [{argumentPath: CatalogPath, maxValues: PositiveInteger}],
  options: CallOptionSchema,
  invoker: InvocationHandle
}

OperationResult = {
  cardinality: One | Optional | Many,
  subjectPath: CatalogPath?,
  shape: Shape
}

PagingDescriptor = {
  inputCursorPath: CatalogPath,
  outputCursorPath: CatalogPath,
  pageSizePath?: CatalogPath,
  legalPageSize?: IntegerRange,
  itemPath: CatalogPath
}

CorrelationCapabilities = {
  mappings: [CorrelationMapping],
  maxBatchSize?: PositiveInteger,
  tupleBatching: unsupported | exact | residualVerified
}

CorrelationMapping = {
  resultPath: CatalogPath,
  argumentPath: CatalogPath,
  missingSemantics: MissingSemantics,
  caseSemantics: CaseSemantics
}

SourceMapping = {
  match: {function: FunctionName} | {relationRole: equality | ordering | membership | other},
  sourceArgumentIndex: NonNegativeInteger,
  resultPath: CatalogPath,
  argumentPath: CatalogPath,
  valueEncoding: scalar | alternatives | providerDefined,
  maxValues?: PositiveInteger,
  missingSemantics: MissingSemantics,
  caseSemantics: CaseSemantics,
  mergeSemantics: forbidden | conjunction | providerDefined
}
```

The catalog may derive this information from JSON Schema, API description languages, protocol descriptors, generated code, or curated metadata. TOWL requires only the contract.

### 3.1 Runtime language environment

A processor supplies an immutable **language environment** for each validation/execution lifecycle:

```text
LanguageEnvironment = {
  functions: FunctionRegistry,
  aggregators: AggregatorRegistry,
  contextSchema: Shape,
  identity: RegistryIdentity
}
```

TOWL defines application syntax and algebraic contracts, but it defines **no mandatory function or aggregator names**. Each runtime chooses its vocabulary. The complete registry must be available during schema generation, validation, explanation, and execution. `RegistryIdentity.fingerprint` MUST be a deterministic content hash of every function/aggregator descriptor and law that can affect validation or execution, and MUST be recorded in every execution result.

A document is portable only to runtimes whose registries provide every function and aggregator it references with compatible signatures. Unknown names fail validation; they never become dynamic calls.

### 3.2 Operation identity

`call.operation` is required. `call.service` is an optional catalog qualifier.

Resolution rules:

1. With `service`, the exact qualified operation MUST resolve. A supplied qualifier is never ignored.
2. Without `service`, exactly one visible operation with the normalized name MUST resolve.
3. Zero matches is an unknown-operation error.
4. Multiple matches is an ambiguity error listing available qualifiers.
5. A catalog MAY require qualification even for a currently unique operation.

The validated plan stores `OperationId` and `InvocationHandle`; execution does not repeat string lookup. Explanation output SHOULD show the resolved origin even when `service` was omitted.

### 3.3 Input and call-option schemas

`call.args` is validated against the resolved operation's effective input schema. Catalogs or profiles may define a closed set of additional call options, but every option MUST:

- have a schema and type;
- participate in dependency extraction and canonicalization;
- be visible in validation and explanation;
- be consumed by the owning catalog/profile; and
- never be silently ignored.

Unknown call members are invalid.

### 3.4 Result cardinality

Every operation result is `One<T>`, `Optional<T>`, or `Many<T>`.

- `One<T>` produces exactly one value.
- `Optional<T>` produces zero or one value.
- `Many<T>` produces an ordered stream of values.

A list-shaped member inside a singular output remains `One<List<T>>` unless catalog metadata explicitly designates its elements as the operation subject. Cardinality is never guessed merely because a shape contains a list.

### 3.5 Effects and authority

The catalog declares operation effects. TOWL does not prescribe provider-specific effect names, but processors MUST support at least an unknown effect and MUST expose effects to host policy before execution.

Catalog effect metadata is descriptive; host policy decides whether execution is permitted. A document cannot acquire operations, credentials, transports, context values, or other authority not injected by the host.

---

## 4. Document model and grammar

A plan is a versioned block with optional declared inputs.

```text
Plan = { towl, description, inputs? } & Block
```

The version key is `"towl"`:

```json
{
  "towl": "v1",
  "description": "example",
  "result": "ok"
}
```

### 4.1 Core grammar

```text
Plan = {
  "towl": "v1",
  "description": String,
  "inputs": Inputs?,
  ...Block
}

Block = Traverse | Express | Assemble

Traverse = {
  "forEach": Map1<Name, ForEach>,
  "onError": OnError?
}

Express = {
  "source": Producer,
  "filter": Expr<Boolean>?,
  "dedup": [Expr, ...]?,
  "result": Result?,
  "onError": OnError?
}

Assemble = {
  "let": Map<Name, Block>?,
  "result": Result?
}

Map1<Name,A> = Map<Name,A> where size = 1

ForEach = {"from": Expr} & Block

Producer = Call | Expr

Call = {"call": Invocation}

Invocation = {
  "operation": Name,
  "service": Name?,
  "args": Map<Name, InputValue>?,
  "paginate": Paginate?
} & CatalogCallOptions

Paginate = {
  "maxItems": PositiveInteger?,
  "maxPages": PositiveInteger?,
  "pageSize": PositiveInteger?
}

InputValue = Expr | [InputValue, ...] | Map<Name, InputValue>

Expr = Literal
     | {"ref": Name, "path": Path?}
     | {"path": Path}
     | {"input": Name, "path": Path?}
     | {"env": Name, "path": Path?}
     | {FunctionName: [Expr, ...]}

Result = Expr
       | Aggregator
       | Map<Name, Result>

Aggregator = {AggregatorName: [Expr | Aggregator, ...]}
           | Map<Name, Aggregator>

OnError = "fail" | "skip" | "collect"
```


### 4.2 Name-keyed declarations

Every authored declaration uses its object key as its identifier, and the language distinguishes exactly two kinds of names:

- `let: {regions: Block}` binds the **value** `regions`. `let` is the only value binder in the language.
- `forEach: {region: {...}}` binds the **element variable** `region` inside its body — the block keys beside `from`.

`source` binds nothing: within its block's `filter`, `dedup`, and `result`, the produced element is implicit and a bare `{"path": P}` navigates from it. Aggregation binds nothing: an aggregate `result` leaf folds the stream in place (§11). There is no `id` or `as` declaration field. The two declaration sites share one lexical namespace. Shadowing along a scope chain is invalid.

### 4.3 Arrays have one meaning

Arrays are used only where values are genuinely ordered or n-ary: operation inputs, function arguments, `dedup` keys, and literal data. Arrays never encode positional declarations, steps, or field-selection shorthand.

### 4.4 Raw JSON literals

Raw JSON is the only constant syntax. There is no `const` wrapper and no `record` wrapper. Object shape is resolved by one precedence rule in every expression, transform, and aggregator position:

1. `ref`, `input`, `env`, `call`, and `path` are **reserved keys**. Registries MUST NOT register them as function or aggregator names.
2. An object with two or more members is a named product, never an application.
3. A single-member object whose key is `ref`, `input`, `env`, or `path`, or resolves in the relevant registry, is that expression/aggregator node. `call` is a structural key and never an expression.
4. Any other single-member object is a single-member product.
5. A single-member product whose sole key collides with a reserved key or a registered name is invalid; rename the member or supply the value through a typed input.

An exact expression-shaped object intended as data must be supplied through a typed input or another catalog-typed literal context.

---

## 5. Blocks, roles, and values

A block is one of three **closed** shapes, discriminated by its distinctive required key: `forEach` selects Traverse, otherwise `source` selects Express, otherwise the block is Assemble. Each variant admits only its own members. There is no separate role tag: a tag beside a distinctive payload would be double discrimination, and a mismatched tag would itself be a new representable invalid state. The summary table is informative; the union above is normative.

| Property          | Traverse                      | Express                                          | Assemble                         |
|-------------------|-------------------------------|--------------------------------------------------|----------------------------------|
| `forEach`         | required                      | absent                                           | absent                           |
| `source`          | absent                        | required                                         | absent                           |
| `filter`, `dedup` | absent; place in the body     | allowed only for `Many`                          | absent                           |
| `let`             | absent; hoist or use the body | absent; hoist to an enclosing block              | allowed                          |
| `result`          | absent; use the body `result` | maps elements, or folds when any leaf aggregates | allowed; defaults to unique sink |
| `onError`         | allowed; applies per element  | allowed; applies to the binding                  | absent                           |

An Assemble block must contain at least one member. Because every variant is closed, a member from another role does not merely fail a check — the document matches no block shape and cannot parse. The union is directly expressible as a JSON Schema `oneOf` whose variants forbid additional properties, so schema-level tooling rejects mixtures before the parser runs. An element variable is never in scope for a sibling `let` binding, so any value formerly declared beside a `source` or `forEach` hoists to an enclosing Assemble block with identical semantics.

`source` takes the producer directly and binds no name. The produced element is implicit within the same block's `filter`, `dedup`, and `result` — including aggregator arguments — where `{"path": P}` navigates from it and `{"path": ""}` denotes the element itself. A bare path is invalid anywhere else, including the block's own call arguments, which are evaluated before the stream exists. Exactly one implicit element can ever be in scope, because expressions never contain blocks; enclosing traversal elements remain available by name. A processor SHOULD warn about an unused `forEach` element variable.

### 5.1 Block value

The block value is determined in this order:

1. explicit `result`;
2. the staged producer output when `source` exists;
3. the unique unconsumed sink of `let`.

If several `let` bindings are sinks and no `result` is present, validation fails.

### 5.2 Source lifting

A producer has `F<T>` where `F` is `One`, `Optional`, or `Many`. Inside an Express block, stages and `result` see one implicit element `T`:

- `One` evaluates `result` once;
- `Optional` evaluates it only when present;
- `Many` evaluates it for each stream element in order.

`result` preserves `F` when every leaf is pure. `filter` and `dedup` require `Many`. A `result` containing an aggregate leaf also requires `Many` and folds it to `One<U>` under the grouping rule (§11).

### 5.3 Scope

A block sees:

- all names from enclosing scopes;
- all names in its own `let` map;
- the implicit `source` element, through bare paths, inside stages and `result`;
- its `forEach` element variable inside the traversal body;
- plan inputs through `input`; and
- host context through `env`.

A binding's internal names are not visible outside that binding. Names cannot shadow outer names.

V1 permits at most two simultaneously active traversal bodies beneath the plan root. Ordinary `let`/Express/Assemble block boundaries do not consume this limit. A third nested traversal is invalid and should be flattened or decomposed. This is a language bound, not a runtime recursion limit.

---

## 6. Types, paths, and references

Only plan `inputs` contain authored type declarations. Other types are inferred from catalog schemas and algebraic composition.

```text
ValueType = Cardinality<Shape>
Cardinality = One | Optional | Many
Shape = CatalogShape | StructuralRecord | Scalar | List | Map | Document | Union | Unknown
```

`Many<T>` is ordered. `dedup` requires a non-empty list of key expressions, adds a uniqueness property while preserving first-occurrence order, and does not produce a set. There is no implicit "derived key" form: deduplication is an explicit reduction of duplicates over named keys.

### 6.1 References

```jsonc
{"ref": "regions"}
{"ref": "region", "path": "name"}
{"path": "name"}
{"input": "environment"}
{"env": "runId"}
```

A path is navigation attached to a named value or, bare, to the implicit element of the nearest enclosing Express block. The bare form is legal only inside that block's stages and `result`.

### 6.2 Path semantics

A catalog defines member names and types. TOWL paths support:

- member navigation: `a.b`; and
- collection flattening: `items[].name`.

There is no positional index or slice syntax: selecting "the first" or "the newest" element is a reduction and must be written as an explicit registered aggregator. Paths contain no functions, predicates, or comparisons. The validator checks every path against the source shape. Navigation through an optional member yields an optional value. Navigation lifted over `Many` preserves order.

### 6.3 Inputs

```text
Inputs = Map<Name, {
  type: "string" | "number" | "integer" | "boolean" | "array" | "document",
  default?: Literal
}>
```

A host may expose richer schema-backed input declarations through a versioned profile, but undeclared external values are unavailable.

### 6.4 List boundaries

An expression of type `Many<T>` supplied where the catalog expects `List<T>` is materialized in stream order, subject to host limits, and this materialization is a reduction for completeness purposes (§12.2). A catalog may declare a maximum element count for a list-valued input path; the processor then splits one logical invocation into several physical invocations along that input, concatenating the result streams in input order while preserving values, errors, and truncation semantics (§8.3). Splitting limits are catalog metadata, never authored.

---

## 7. Workflow and dataflow algebra

```text
F        = One | Optional | Many
source   : Producer -> F<T>
call     : ResolvedOperation × Args -> F<T>
result   : Result<T,U> -> F<T> -> F<U>       -- every leaf pure
         : Result<T,U> -> Many<T> -> One<U>  -- any aggregate leaf
filter   : Expr<Boolean> -> Many<T> -> Many<T>
dedup    : List<Expr> -> Many<T> -> Many<T>
forEach  : Map1<Name, {from} & Block> -> Many<U>                    -- onError fail | skip
         | Map1<Name, {from} & Block> -> Many<{"ok":U}|{"error":E}> -- onError collect
```

### 7.1 Dependency graph

A dependency edge `a → b` exists when any expression in `b` references `a`. Every expression-bearing position participates: call arguments and options, `forEach.from`, filters, dedup keys, aggregators, and results.

Every binding declared in an entered block is part of the computation and executes once when its dependencies are ready, even if the block's `result` does not reference it. `result` shapes the returned value; it is not a reachability root and does not perform dead-effect elimination. A processor may warn about an unconsumed pure/read value, but it may not omit a declared effect.

Document order creates no edge. `let` is a name-keyed map, not a sequence. Two plans differing only in object-member order have the same meaning.

A nested block forms a nested DAG. References may point inward to outer scopes; outer scopes cannot name internal bindings.

### 7.2 Scheduling

A binding becomes ready when all referenced values are available and host policy permits its effects. Independent ready bindings may execute concurrently. The processor owns concurrency, backpressure, retries, and resource limits; the plan has no concurrency field.

### 7.3 Determinism

For an unchanged catalog, context, inputs, and operation results, logical output must not depend on scheduling:

1. values are single-assignment;
2. independent bindings share no mutable state;
3. traversal outputs preserve input order, not completion order;
4. commutative aggregation is required where completion order may vary; and
5. document position has no semantics.

Diagnostics such as duration and retry count may vary.

### 7.4 No control-only ordering edge

TOWL has no `after` field. If an invocation needs another result, it references that result. Rate limiting and backpressure are processor responsibilities. A host may impose effect barriers as policy, but such barriers are not TOWL dataflow edges.

---

## 8. Traversal and correlation

### 8.1 Traversal

`forEach` evaluates its body — the block keys of the element object beside `from` — once for every value produced by `from`. `from` must be `Many<T>` or a list value. An empty collection succeeds with an empty result.

Traversal output depends only on the traversal's error policy:

- Under `fail` and `skip`, the output is `Many<U>` of the body values in input order, with no wrapper. An element whose body value is dynamically absent contributes nothing.
- Under `collect`, each element is a single-key tagged value:

```jsonc
{"ok": {"...": "do result"}}
{"error": {"item": {"...": "input element"}, "code": "...", "message": "..."}}
```

`ok` and `error` are member names inside collect elements only; they are not reserved traversal keys, and the input element is not echoed automatically outside `error.item`. A body `result` that needs its input includes it explicitly, since the traversal name is in scope. The `error` object MUST contain `item`, `code`, and `message`; hosts may add diagnostic members.

### 8.2 Correlation

V1 correlation is constrained to direct equality between a bare path on the nested source element and a path on an enclosing traversal element. A conjunction of such equalities may be combined with predicates independent of the outer value.

The resolved operation's `CorrelationCapabilities` declares result-path to argument-path mappings, equality semantics, batch limits, and tuple-batching behavior. A processor may implement:

- one logical invocation per outer element;
- a provider-side batched semijoin followed by repartitioning; or
- an indexed in-memory lookup for a bound collection source.

It must not degrade to an unbounded repeated full scan. Unsupported correlation is reported before invocation.

### 8.3 Logical versus physical work

Logical tasks determine result cardinality, ordering, and per-element error semantics. Optimizers may fuse or split tasks into different physical requests — batched correlation, list-input chunking (§6.4), pagination — only when they preserve the same logical elements, values, ordering, errors, and truncation behavior.

---

## 9. Runtime-provided pure-expression algebra

TOWL defines one application node but no built-in function vocabulary:

```jsonc
{"functionName": [arg1, arg2]}
```

The sole object key is resolved in the runtime's immutable `FunctionRegistry`. Function names are data in the registry, not productions in the TOWL language specification.

```text
FunctionDescriptor = {
  name: FunctionName,
  parameters: ParameterSignature,
  result: ResultTypeRule,
  cardinality: CardinalityRule,
  evaluator: PureEvaluator,
  contextDependencies: Set<ContextPath>,
  laws: FunctionLaws
}

FunctionLaws = {
  associative?: Boolean,
  commutative?: Boolean,
  idempotent?: Boolean,
  complement?: FunctionName,
  relationRole?: equality | ordering | membership | other,
  booleanRole?: conjunction | disjunction | negation
}
```

A conforming registry may be empty. A runtime may provide equality, Boolean composition, string operations, temporal functions, domain-specific predicates, or none of them. TOWL requires only that registered functions satisfy the following contract.

### 9.1 Function registry requirements

Every registered function MUST declare:

- a unique name;
- fixed or variadic arity, including minimum arity;
- argument type constraints;
- a result-type rule;
- cardinality behavior;
- a deterministic, pure evaluator;
- an explicit set of immutable host-context dependencies, normally empty; and
- every algebraic law or semantic role used by normalization, correlation, or optimization.

The evaluator MUST NOT read ambient state, perform I/O, invoke tools, mutate values, or observe scheduling. A function SHOULD receive host context through explicit `env` arguments. If a runtime registers an implicit immutable context dependency, the descriptor MUST declare it; dependency extraction, explanation, result context, and registry identity MUST include it.

Registries MUST NOT register the reserved keys `ref`, `input`, `env`, `call`, or `path` as function or aggregator names. Unknown functions, wrong arity, incompatible argument types, or unavailable registry versions are validation errors.

### 9.2 Typed application

Applications type-check bottom-up. Paths and literals obtain types before the enclosing function is resolved. The result type is computed from the registered signature and stored in the validated plan; execution does not repeat overload or name resolution.

A registry may support overloaded signatures only when static argument types select exactly one overload. Ambiguity is invalid.

### 9.3 Boolean filters

`filter` accepts any expression whose resolved result type is Boolean. TOWL does not prescribe names for conjunction, disjunction, negation, equality, presence, or comparison. A runtime that wants those operations registers them.

V1 correlation recognizes equality atoms only through `relationRole: equality` and conjunction only through `booleanRole: conjunction`; no particular wire names are required. Registered disjunction and negation roles allow generic normalization but are not legal around outer-dependent v1 correlation atoms.

### 9.4 Normalization

A processor may normalize applications only from declared laws. Examples include:

- flattening a function declared associative;
- sorting arguments of a function declared commutative;
- removing duplicate arguments of a function declared idempotent;
- applying a registered complement under `booleanRole: negation`; and
- bounded normal-form conversion using registered conjunction/disjunction/negation roles.

No law may be inferred from a function's spelling. Normalization must preserve the registered evaluator's result.

### 9.5 Source-side and residual evaluation

TOWL defines the partitioning contract, not provider mappings. Operation-specific `SourceMapping` entries have one owner: the resolved operation catalog/profile. Each entry matches exactly one concrete function name or one semantic relation role through its exclusive `match` union. Function descriptors provide pure semantics and roles; the catalog maps that selector plus result path and argument position to provider input, encoding, limits, missing/case behavior, and merge semantics. Duplicate or conflicting mappings are invalid. A processor evaluates all remaining applications locally.

Moving an application to a source is legal only when the runtime has evidence that the source mapping is observationally equivalent to the pure evaluator for the relevant types and operation. A function wrapped around a source-dependent argument is residual unless such a mapping exists.

### 9.6 Registry introspection

A runtime MUST make its function registry machine-readable enough to generate:

- the accepted expression schema;
- names, signatures, and descriptions for authors;
- normalization laws used by the processor;
- provider/source mappings; and
- conformance tests over the registered entries.

TOWL itself does not prescribe a help command or transport for this information.

---

## 10. Runtime-provided aggregator algebra

TOWL defines the aggregator protocol and product composition, but no mandatory constructor names.

```text
Aggregator<Record,Acc,Out> = (
  prepare: Record -> Acc,
  monoid: Monoid<Acc>,
  present: Acc -> Out
)
```

A runtime's `AggregatorRegistry` maps authored keys to typed constructors:

```text
AggregatorDescriptor = {
  name: AggregatorName,
  parameters: ParameterSignature,
  inputRule: StreamElementTypeRule,
  accumulator: Shape,
  output: Shape,
  prepare: PureFunction,
  monoid: MonoidDescriptor,
  present: PureFunction
}
```

Object shape follows the precedence rule of §4.4: a single-key object whose key resolves in the aggregator registry is an application; an object with two or more members is a product of named aggregators; a single-member product whose sole name collides with a registered aggregator is invalid and must be renamed. Aggregator applications appear only as Express `result` leaves and inside aggregator constructor arguments; an Assemble block has no stream to fold. Function and aggregator registries share one application syntax and their names MUST be disjoint. Runtime registries may provide count, sum, average, grouping, collection, domain-specific sketches, or no aggregators.

### 10.1 Monoid requirements

A `MonoidDescriptor` supplies:

- an accumulator identity;
- an associative combine operation; and
- whether combine is commutative.

Commutativity is required when the processor may combine partial accumulators in nondeterministic completion order. `prepare`, `combine`, and `present` must be deterministic and pure.

### 10.2 Constructor and product typing

Aggregator constructor arguments may contain expressions and nested aggregators according to the registered signature. Expression arguments resolve through the runtime's `FunctionRegistry`.

A product aggregator is a map product: preparation, combination, and presentation are componentwise. Field names are preserved in the output structural record.

### 10.3 Streaming law

For every registered aggregator and every legal partitioning of a stream:

```text
present(fold(combine, identity, map(prepare, records)))
```

must equal presenting the combination of independently folded partitions. Implementations MUST property-test identity and associativity for base monoids and commutativity where declared.

### 10.4 Registry introspection

A runtime MUST expose aggregator names, signatures, input/output types, and law metadata for schema generation, authoring guidance, validation, explanation, and conformance testing. TOWL defines no standard aggregator list.

---

## 11. Pure transforms and results

A `Result` is one expression, one aggregator application, or a recursive named product of results.

```json
{
  "name": {"ref": "item", "path": "name"},
  "size": {"ref": "item", "path": "size"},
  "kind": "asset"
}
```

A raw array remains a literal array; there is no field-list shorthand and no wrapper node. Object shape follows the single precedence rule of §4.4, so a product with two or more members can never be mistaken for an expression, and the one ambiguous case — a single-member product named like an expression node — is a rename-this-field validation error rather than extra grammar.

When every leaf is pure, evaluation is pointwise per element and preserves the producer cardinality. When any leaf is an aggregator application, the block folds the staged stream: aggregate leaves consume the stream through their monoids, pure leaves must be independent of the element variable (enclosing values and outer element variables remain legal), and element references are legal only inside aggregator arguments. An element-dependent pure leaf beside an aggregate leaf is a validation error — the rule SQL applies to non-grouped columns. An aggregate leaf over an empty stream presents its monoid identity, so traversal bodies preserve empty groups.

Algebraically, a record is the product of its member transforms.

---

## 12. Streams and pagination capability

`paginate` is optional. Omission is always valid.

Presence requires:

1. the resolved operation result is `Many`; and
2. the catalog advertises `Paged` capability.

`One`, `Optional`, and non-paging `Many` operations reject `paginate`.

For a paged operation:

- omitted `paginate` means all pages subject to host limits;
- `{}` explicitly states the same policy;
- `maxItems` bounds logical records;
- `maxPages` bounds physical pages;
- `pageSize` is a hint clamped to the capability's legal range; and
- truncation is always reported.

Provider cursor names and request mechanics are catalog metadata and never appear in TOWL expressions.

### 12.1 Completeness and explicit reduction

Every stream value carries a completeness property.

- A stream bounded by authored `paginate` limits is **complete over its authored domain**: the author explicitly specified the reduction of the source data.
- A stream shortened by a host limit, or interrupted mid-stream, is **unintentionally partial**.

Reductions — aggregate `result` leaves, list-boundary materialization (§6.4), and any consumer that summarizes a stream into a smaller value — MUST NOT silently consume an unintentionally partial stream. When a reduction's input becomes unintentionally partial, that binding fails with a partial-input error, which then flows through ordinary error policy. An unintentionally partial stream may still be returned or projected element-wise, because its elements remain individually correct, but it MUST be reported as truncation and force `partial` status.

All data reduction is therefore explicit: authored stream bounds, authored aggregators, authored error policy. Nothing in the language discards or summarizes data implicitly.

### 12.2 Optimization and demanded fields

The processor derives demanded paths by walking references in call arguments/options, filters, dedup keys, correlation atoms, aggregators, and results. A provider may use this set for projection pushdown; every processor may use it to discard unused data after decoding.

Optimizations must preserve values, ordering guarantees, error policy, truncation, completeness, and logical elements. Disabling optimization may change performance and physical accounting, not logical results.

---

## 13. Runtime context, execution, and errors

### 13.1 Host context

`env` references immutable values exposed by the host for one run. The host publishes a closed schema for available names and paths. A document cannot read process environment variables, files, credentials, clocks, random generators, transports, or other host state unless explicitly represented by a host-provided context value or catalog operation.

Repeated reads of one `env` value during a run return the same value.

### 13.2 Limits

A processor enforces host-defined limits such as logical invocations, physical requests, items, bytes, expression work, concurrency, and wall time. Limits are not authored concurrency controls. Hitting a limit produces a structured failure or partial/truncated outcome; it never silently returns a complete-looking value.

### 13.3 Error policy

- `fail`: fail the containing logical computation and cancel dependents as host policy requires.
- `skip`: replace the failed logical value with an explicitly authored empty stream or absent value.
- `collect`: retain a structured error alongside successful values, as §8.1's tagged elements. `collect` is valid only on Traverse.

When omitted, `onError` defaults to `skip` for Traverse and `fail` for Express. Traversal applies policy per input element. Express applies policy to the binding as a whole: a producer that fails mid-stream never yields a silently shortened stream (§12.1); under `skip` the binding's value is an explicitly empty/absent value rather than partial data. Operation transport/protocol errors, provider errors, expression errors, limit failures, and cancellation remain distinguishable diagnostic classes.

### 13.4 Result model

A processor returns an abstract result containing at least:

```text
ExecutionResult = {
  status: ok | partial | error,
  result?: Value,
  errors: [Error],
  truncations: [Truncation],
  diagnostics: [Diagnostic],
  accounting: Accounting,
  resolvedContext: Map<Name,Value>,
  catalogIdentity: CatalogIdentity,
  registryIdentity: RegistryIdentity
}
```

`resolvedContext` contains every explicit or descriptor-declared host-context value referenced by the validated plan, even when an empty/optional path prevents dynamic evaluation. Hosts define concrete serialization, progress reporting, and exit codes. A `partial` result is not equivalent to `ok`.

`Accounting` MUST include per-binding logical counts sufficient to reconstruct where records appeared and disappeared: elements produced by each source, kept and dropped by each `filter` and `dedup`, traversed by each `forEach`, succeeded/failed/skipped per error policy, and invocations per resolved operation. A consumer — human or model — can then explain an unexpected value (an empty result, a partial stream) from the returned result alone, without re-running the plan.

### 13.5 Explanation model

Without invoking operations, a processor can expose:

- resolved operation identities and effects;
- inferred symbols, cardinalities, and shapes;
- dependency waves;
- traversal and correlation structure;
- paging and other capabilities;
- logical work estimates or symbolic bounds;
- optimization classifications; and
- host policy decisions.

The language does not prescribe a command or rendering.

---

## 14. Normative invariants and conformance tests

A conforming implementation MUST enforce these invariants before execution:

1. value and element-variable declarations are map keys and `Map1` sites contain exactly one key;
2. one lexical namespace exists along each scope chain;
3. duplicate JSON keys are rejected by whichever layer first parses text;
4. operation resolution is exact, unique, and frozen in the validated plan;
5. types and paths are resolved before execution;
6. blocks parse only as the closed Traverse/Express/Assemble union, so foreign-role members are unrepresentable, and bare paths appear only inside their block's stages and `result`;
7. stream stages and aggregate results receive only `Many`;
8. stage order is `filter → dedup`, evaluated before `result`;
9. traversal bodies are explicit and traversal nesting depth does not exceed two;
10. all declared bindings execute when their block is entered, regardless of result reachability;
11. references are rooted at a name or the implicit element and create all data dependencies;
12. arrays never declare named bindings;
13. pure applications use `{functionName:[arguments]}`, every name resolves in the runtime registry, and `ref`/`input`/`env` are never registered names;
14. aggregators resolve in the runtime registry, satisfy their declared monoid laws, never collide with function names, and single-member products never collide with registered or reserved names;
15. `result` leaves are pure expressions or aggregator applications under the grouping rule;
16. pagination is optional and capability-gated;
17. reductions never consume unintentionally partial streams (§12.1);
18. document order creates no execution order;
19. no ambient operation, context, function, aggregator, or platform authority is available; and
20. execution always consumes a validated immutable plan.

### 14.1 Required property tests

- parse/serialize/parse stability;
- map-order invariance;
- duplicate-key rejection;
- lossless object intake: unknown members survive any typed intake shim and reach closed-shape diagnostics;
- scope, no-shadowing, nesting-depth, and eager declared-binding execution rules;
- closed-variant parsing (foreign-role members match no block shape), hoisting equivalence, grouped-result typing, function/aggregator name disjointness, and bare-path scoping;
- qualified, unqualified, unknown, and ambiguous operation resolution;
- `One`/`Optional`/`Many` preservation through result mapping;
- rejection of stream stages on singular values;
- reserved-name registration rejection and single-member product collision diagnostics;
- list-boundary materialization and catalog-limit splitting equivalence;
- traversal output shape under each error policy, including tagged collect elements;
- reduction failure on unintentionally partial streams versus authored bounds;
- pagination descriptor and capability checks;
- catalog and registry fingerprint stability;
- reference extraction equals graph edges;
- deterministic traversal ordering;
- normalization equivalence for every law declared by the runtime function registry;
- function-registry name, arity, overload, type, context-dependency, Boolean/relation-role, purity, and result-rule coverage;
- aggregator-registry constructor typing plus monoid associativity/identity and required commutativity;
- batched/indexed correlation equals logical per-element evaluation; and
- optimized execution equals unoptimized logical execution.

### 14.2 Provider-neutral example

Assume a catalog with:

```text
list_locations() -> Many<Location{code,name}>
list_assets(location:string) -> Many<Asset{id,location,state,size}> [Paged]
inspect_asset(id:string) -> One<Inspection{id,summary}>

FunctionRegistry:
  equals(T,T) -> Boolean  [relationRole: equality]
```

```json
{
  "towl": "v1",
  "description": "Inspect active assets in every location",
  "let": {
    "locations": {
      "source": {
        "call": {
          "operation": "list_locations"
        }
      }
    },
    "assetsByLocation": {
      "forEach": {
        "location": {
          "from": {
            "ref": "locations"
          },
          "let": {
            "assets": {
              "source": {
                "call": {
                  "operation": "list_assets",
                  "args": {
                    "location": {
                      "ref": "location",
                      "path": "code"
                    }
                  },
                  "paginate": {
                    "maxItems": 1000
                  }
                }
              },
              "filter": {
                "equals": [
                  {
                    "path": "state"
                  },
                  "active"
                ]
              }
            },
            "inspections": {
              "forEach": {
                "asset": {
                  "from": {
                    "ref": "assets"
                  },
                  "source": {
                    "call": {
                      "operation": "inspect_asset",
                      "args": {
                        "id": {
                          "ref": "asset",
                          "path": "id"
                        }
                      }
                    }
                  },
                  "result": {
                    "assetId": {
                      "ref": "asset",
                      "path": "id"
                    },
                    "summary": {
                      "path": "summary"
                    }
                  }
                }
              },
              "onError": "collect"
            }
          },
          "result": {
            "location": {
              "ref": "location",
              "path": "code"
            },
            "inspections": {
              "ref": "inspections"
            }
          }
        }
      }
    }
  },
  "result": {
    "ref": "assetsByLocation"
  }
}
```

---

## 15. Informative rationale and related languages

This section is informative.

### 15.1 Closest existing languages

- **OpenAPI Arazzo** is the closest API-composition standard: schema-linked operations, named outputs, and output references. Its ordered heterogeneous API interaction and fixed selector-language boundary differ from TOWL's typed runtime registry and stream/fold contracts.
- **Common Workflow Language** is the closest typed dataflow precedent: data links form a DAG and scatter/gather is explicit. Its command/file/container analysis domain and optional JavaScript differ from TOWL's catalog-operation sandbox.
- **Open Workflow DSL** offers broad calls, forks, loops, events, waits, scripts, and containers. TOWL deliberately excludes most of that control and authority surface.
- **Amazon States Language** provides mature state transitions, Map/Parallel, and error handling. TOWL derives scheduling from data references and has no authored state machine.
- **BPMN 2.0.2** excels at graphical business processes, human work, events, and interchange. TOWL is a compact typed dataflow IR rather than a business-process notation.
- **Relational and query-plan IRs** motivate projection/filter pushdown and aggregation laws, but do not alone describe effectful tool invocation and partial tool errors.

### 15.2 Decisions and learnings

- One block replaced sibling call/fan-out/join step kinds so effects, traversal, and pure combination compose.
- Names became map keys to eliminate positional declarations and alias synchronization.
- Explicit traversal bodies distinguish values computed once from values computed per element.
- References became structured nodes rather than string templates, making graph extraction mechanical.
- The expression surface converged on one typed application grammar after expression-dialect confusion in prototypes; concrete functions then moved into runtime registries so TOWL does not standardize one domain's vocabulary.
- Aggregation became `(prepare, monoid, present)` and named constructors moved into runtime registries so streaming laws remain explicit without fixing a universal list.
- Effectful calls became cardinality-polymorphic because an effect may produce a singular value, an optional value, or a record stream.
- `service` became optional because some catalogs expose globally unique operations and protocols such as MCP do not carry a service identifier. A supplied qualifier remains meaningful and mandatory on ambiguity.
- `paginate` became an optional capability rather than a universal call feature.
- Control-only ordering was rejected; references are the only authored dependency edges.
- Traversal envelopes were replaced: `fail`/`skip` traversals yield unwrapped per-element results, and only `collect` produces single-key `ok`/`error` elements, so the common case needs no unwrapping and no reserved traversal names.
- `batch` was removed; catalog-declared list-input limits drive processor-side chunking exactly as paginator metadata drives paging.
- The `record` wrapper was removed in favor of reserved expression keys plus a single precedence rule over object shapes.
- Positional index and slice paths were removed because selecting "the first" element is an implicit order-dependent reduction; explicit aggregators state the intent.
- Completeness became a stream property: every reduction is authored, and unintentionally partial data fails rather than aggregating silently.
- Documents may arrive as parsed values, not only text: publishing the wire shape as a host input schema (structural keys typed, open-vocabulary positions untyped) eliminated a whole class of authoring errors in a tool-transport prototype — models balancing braces inside an escaped JSON string — while mandatory lossless capture of unknown members kept corrective diagnostics intact. A fully typed intake that silently drops foreign members is worse than a string: it erases exactly the mistakes the validator explains best.
- `let` became the sole value binder: aggregation binds nothing, and `let` was confined to Assemble blocks — element variables are never in scope for sibling bindings, so hoisting is always semantics-preserving.
- `forEach` keys remained named element variables rather than moving into `let` or becoming implicit, because nested correlation must name the enclosing element while the inner element is implicit.
- The `do` wrapper was merged away: a traversal element is `{from}` plus the body block itself.
- The source binder was removed entirely: the produced element is implicit within its block's stages and `result` as a structured bare-path node, which eliminated binder-name synchronization errors; the short-lived Invoke role became redundant because a stage-free Express block spells the same thing.
- The block grammar became a closed sum of products discriminated by distinctive required keys. Flat optional keys made mixtures such as source-plus-let representable and pushed rejection into validation with ambiguous attribution; closed variants make them unparseable. An explicit role tag was rejected as double discrimination that introduces tag/payload mismatch as a new invalid state.
- Bare paths were once removed as string-dialect expressions with ambiguous referents; they returned as structured nodes only after the grammar guaranteed a unique implicit element (stages exist solely on Express blocks and expressions never contain blocks).
- `fold` merged into `result` under the SQL grouping rule: aggregate leaves fold the staged stream, pure leaves must be element-independent, and monoid identities preserve empty groups — removing the fold keyword, the fold/result exclusion, and most traversal-body `let` wrappers.

### 15.3 Informative AWS example

Assume the example runtime registers `equals(T,T) -> Boolean` with equality relation semantics.

AWS catalogs are a valid TOWL application. In an AWS profile, a service qualifier may be required, `args` may include a catalog-declared endpoint-region selector, and Smithy-derived metadata may identify record subjects and paginator capability. Those rules are not part of TOWL core; see [`CODE_MODE-SPEC.md`](CODE_MODE-SPEC.md).

```json
{
  "towl": "v1",
  "description": "Running instances in one AWS region",
  "source": {
    "call": {
      "service": "ec2",
      "operation": "describe-instances",
      "args": {
        "region": "us-east-1"
      }
    }
  },
  "filter": {
    "equals": [
      {
        "path": "State.Name"
      },
      "running"
    ]
  },
  "result": {
    "InstanceId": {
      "path": "InstanceId"
    },
    "InstanceType": {
      "path": "InstanceType"
    }
  }
}
```

---

End of TOWL v1 draft.
