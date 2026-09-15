# TOWL — Tool Orchestration Workflow Language Specification

Status: archived v2 draft; superseded by TOWL v3  
Language version: `v2`

> This document is the frozen TOWL v2 contract. It was never implemented. Current language work is in [`TOWL_SPEC.md`](TOWL_SPEC.md) (v3). V2 documents are never interpreted as v3.

TOWL stands for **Tool Orchestration Workflow Language**. Its core is a provider-neutral, declarative, typed intermediate representation (IR) for a bounded graph of tool invocations and data transformations. An AI agent, human, compiler, or program constructs a plan IR; a user or host reviews a representation of it; a deterministic processor validates, lowers, and executes it without an LLM in the loop.

This specification defines the TOWL v2 abstract IR, static calculus, lowering contract, and logical execution semantics. Strict JSON is one reference serialization of the IR, not the language itself. Catalogs and host profiles define concrete operations, normalized shapes, effects, capabilities, immutable registry versions, policy, and user interfaces.

---

## 1. Scope and conformance

A conforming processor:

1. accepts an in-memory TOWL IR directly or decodes an external representation through a lossless adapter;
2. rejects every IR/serialization version it does not implement;
3. resolves the exact authored language-registry name/version and operation-catalog name/version;
4. resolves operations against that immutable catalog snapshot;
5. constructs a typed immutable validated plan before effects;
6. applies only the explicit cardinality, list, optional, error, and reduction operators defined here;
7. derives dependencies from free references, never object-member order;
8. executes only through injected operation and context interfaces;
9. preserves logical order, coverage, errors, effects, and deterministic budget admission; and
10. returns structured validation and execution results.

TOWL does not define CLI syntax, files, transports, credentials, provider endpoints, persistence, deployment, or one universal provider schema language.

### 1.1 Normative terms

**MUST**, **MUST NOT**, **REQUIRED**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative. A **plan IR** is the abstract syntax defined by this calculus. A **document** is one external serialization of a plan IR. A **validated plan** is the typed immutable IR after registry resolution, parsing, scope/name resolution, catalog resolution, type/effect/coverage checking, capability checking, and policy-independent validation. Only a validated plan reaches execution.

### 1.2 Representation adapters and reference JSON

TOWL does not require processors to round-trip through JSON. A host may construct IR variants directly through typed classes, an MCP/tool input object, a compiler, or another serialization. Every adapter MUST preserve variant identity, names, declared types, ordering where semantic, literal values, and source locations used for diagnostics; it MUST NOT silently discard, inject, rename, or coerce IR fields.

This specification also defines a **reference strict-JSON encoding** in §4. In that encoding, duplicate keys, comments, trailing commas, Markdown fences, YAML, and JSON5 are invalid. Every IR sum variant has a literal `kind` discriminator and a closed object shape. Name-keyed maps are containers. Authored arbitrary JSON occurs only in the encoded TypedLiteral payload. Runtime/provider JSON islands are explicitly identified in §§6, 13, and 14.

Other encodings are conforming only when decoding them produces the same unvalidated IR and validation/lowering semantics as the reference encoding. Serialization convenience never introduces an implicit language operator.

---

## 2. Goals and non-goals

### 2.1 Goals

- one reviewed artifact before effects;
- exact `One`, `Optional`, and `Many` cardinality typing;
- no implicit lift, flatten, group, materialize, expand, default, require, recover, or fold;
- output-to-input references checked against normalized shapes;
- name-keyed DAG bindings with derived concurrency;
- distinct `group_map` and `flat_map` operators;
- closed pure function, key-codec, and aggregator registries;
- coverage-aware streaming and reductions;
- explicit fail, skip, and collect recovery;
- capability-gated pagination, correlation, and request splitting;
- deterministic logical ordering and admission;
- structured validation, deprecation, partiality, and accounting;
- no ambient authority.

### 2.2 Non-goals for v2

- recursion, user-defined functions, or general-purpose code;
- arbitrary branches, control loops, waits, messages, human tasks, or resumable processes;
- filesystem, process, arbitrary-network, reflection, or dynamic loading primitives;
- authored concurrency or retry algorithms;
- implicit joins, scans, list/stream conversion, optional coercion, or null/absence conversion;
- dynamic omission of operation arguments;
- unbounded AST, expression, or fanout depth.

---

## 3. Type, effect, coverage, and error calculus

### 3.1 Data and flow types

```text
Cardinality C ::= One | Optional | Many

DataType T ::= Null | Boolean | Number | String | Timestamp
             | RegistryScalar<ShapeId>
             | Maybe<T>
             | List<T,Q>
             | Map<String,T>
             | Union<Tag,{case:T,...}>
             | Record<{field:Required<T>|Optional<Maybe<T>>,...}>
             | Outcome<T,Error>
             | Covered<T,Q>

AuthoredDimension ::= max_items | max_pages
BudgetDimension ::= max_logical_calls | max_items_per_binding | max_expression_steps
CoverageReason ::= {kind:"authored_bound", nodePath:String, dimension:AuthoredDimension, limit:PositiveInteger}
                 | {kind:"policy_drop", nodePath:String, code:String}
                 | {kind:"recovered_error", nodePath:String, code:String}
UnintendedReason ::= {kind:"budget", nodePath:String, dimension:BudgetDimension}
                   | {kind:"transport", nodePath:String, code:String}
                   | {kind:"cursor_cycle", nodePath:String}
                   | {kind:"cursor_error", nodePath:String, code:String}
                   | {kind:"timeout", nodePath:String}
                   | {kind:"cancellation", nodePath:String}
                   | {kind:"element_error", nodePath:String, code:String, index:NonNegativeInteger}

Coverage Q ::= Coverage {
  authored: OrderedSet<CoverageReason>,
  policy: OrderedSet<CoverageReason>
}

Exact = Coverage{authored:∅, policy:∅}
FlowType ::= One<T> | Optional<T> | Many<T,Q>

Completion<Q> ::= Complete<Q>
                | Incomplete<Q, OrderedSet<UnintendedReason>>
```

`Optional<T>` is outer flow absence. `Maybe<T>` is data-level member presence. Both differ from JSON null. `List<T,Q>` is ordinary one-valued data plus source provenance; it is not `Many<T,Q>`.

In static judgments, Q is a conservative coverage-effect upper bound: it contains every authored or policy reason that may occur. Runtime `Qactual` contains only reasons that did occur and MUST satisfy componentwise set inclusion `Qactual ⊑ Qstatic`. Thus a skip-capable term has a policy reason in its static type even when a particular run skips nothing; that run may still report Exact actual coverage.

Runtime Many is an ordered sequence plus `Completion<Qactual>`. `Incomplete` retains a valid prefix but is never reduction-safe.

### 3.2 Coverage algebra

```text
join(Q1,Q2) = Coverage {
  authored = canonicalUnion(Q1.authored,Q2.authored),
  policy   = canonicalUnion(Q1.policy,Q2.policy)
}
```

`join` is total, associative, commutative, and idempotent. Runtime incompleteness is separate and dominant:

```text
joinCompletion(Complete(Q1), Complete(Q2)) = Complete(join(Q1,Q2))
joinCompletion(Incomplete(Q1,R1), Complete(Q2)) = Incomplete(join(Q1,Q2),R1)
joinCompletion(Complete(Q1), Incomplete(Q2,R2)) = Incomplete(join(Q1,Q2),R2)
joinCompletion(Incomplete(Q1,R1), Incomplete(Q2,R2)) =
  Incomplete(join(Q1,Q2),canonicalUnion(R1,R2))
```

A reduction is legal only on `Complete(Qactual)`, for any explicit actual coverage. It fails with `partial_input` on `Incomplete`. `materialize` preserves both static coverage effect and actual Q in `List<T,Q>`; `elements` restores them.

### 3.3 Judgments

```text
Pure expression: Γ ⊢ e : T ! τ
Flow term:       Γ ; d ⊢ t : FlowType ! ε ! τ
Aggregate:       Γ, x:T ⊢ a : Aggregator<T,P*,A,U>
Scope:           Γ ; d ⊢ s : FlowType ! ε ! τ
```

- `Γ` maps names to exact flow types or one-valued lexical item types.
- `d ∈ {0,1,2}` is active fanout depth.
- `ε` is the union of catalog effect descriptors.
- `τ` is the canonically ordered set of `ErrorDescriptorRef{origin,code}` identities; each resolves to exactly one class/catchability/phase/retryability descriptor. Errors with the same class but different code remain distinct.

There is no cardinality subtyping. Pure functions are never lifted implicitly.

### 3.4 Normalized shapes and assignability

TOWL does not mandate a provider schema syntax, but the selected `LanguageEnvironment` and operation catalog MUST expose one immutable normalized shape interface:

```text
ShapeSystem = {
  resolve(ShapeRef) -> DataType,
  member(RecordType, MemberName) -> Required<T> | Optional<Maybe<T>> | Unknown,
  validateLiteral(DataType, JSON) -> valid | Diagnostic,
  decodeAuthored(ShapeRef, JSON) -> TypedData | Diagnostic,
  decodeProvider(ShapeRef, JSON) -> TypedData | Diagnostic,
  encodeRuntime(TypedData) -> RuntimePayload,
  assignable(actual, expected) -> Boolean,
  stableId(DataType) -> ShapeId
}
```

Canonical built-in shape IDs include `core.null`, `core.boolean`, `core.number`, `core.string`, `core.timestamp`, and `core.json`. `core.json` is opaque and is assignable only where explicitly expected. Registry scalars have exact registry IDs.

Every catalog shape MUST normalize losslessly to scalar, Maybe, list, map, tagged union, or record. An untagged/ambiguous union or provider unknown shape that cannot normalize is unavailable to TOWL v2 and produces `shape.unsupported`; it is never silently projected to `core.json`. `core.json` is used only when the catalog explicitly declares opaque JSON.

Assignability performs no coercion. Scalars require equal IDs. Lists require assignable elements and exactly equal static coverage effects, recursively through nested lists/records; there is no coverage subtyping or implicit join during assignment. A ShapeRef list denotes Exact, so a non-Exact list cannot be inserted into a ShapeRef-typed list/object/default. Coverage joins occur only in the explicit flow rules. Records require exact required/optional modes, field sets, and recursively assignable field types; v2 has no width subtyping or implicit record projection. Named records additionally require equal stable shape IDs. A JSON string is never inferred as Timestamp or another registry scalar.

### 3.5 Paths and optional members

```text
MemberPath = [MemberName, ...]
```

Paths contain member names only: no indexes, wildcards, slices, predicates, `[]`, or functions. Required member navigation yields T. Optional member navigation yields `Maybe<T>`. Further member navigation through `Maybe<T>` is invalid until explicitly unwrapped.

Pure expressions provide:

```text
is_present(Maybe<T>) -> Boolean
is_absent(Maybe<T>) -> Boolean
require_present(Maybe<T>, ErrorTemplate) -> T ! MissingMember
default_present(Maybe<T>, T) -> T
```

No absent member becomes null, Optional, or T implicitly.

### 3.6 Serializable type rules and unique inference

Registry/catalog manifests use this closed serializable data/type-rule grammar:

```text
SerializableDataType =
  {kind:"scalar", id:ShapeId}
| {kind:"maybe", value:SerializableDataType}
| {kind:"list", element:SerializableDataType}
| {kind:"map", value:SerializableDataType}
| {kind:"union", discriminator:MemberName, cases:Map<String,SerializableDataType>}
| {kind:"record", id:ShapeId?, fields:Map<String,RecordField>}
RecordField = {kind:"required_field", value:SerializableDataType}
            | {kind:"optional_field", value:SerializableDataType}

TypeRule = {kind:"exact_type", type:ShapeRef}
         | {kind:"type_variable", name:Name, constraint:TypeConstraint}
         | {kind:"list_of", element:TypeRule}
         | {kind:"map_of", value:TypeRule}
         | {kind:"union_of", discriminator:MemberName, cases:Map<String,TypeRule>}
         | {kind:"maybe_of", value:TypeRule}
         | {kind:"record_of", id:ShapeId?, closed:Boolean,
            fields:Map<String,RecordRuleField>}
RecordRuleField = {kind:"required_rule",value:TypeRule}
                | {kind:"optional_rule",value:TypeRule}
TypeConstraint = any | scalar | equatable | numeric | string | timestamp | record | list
```

Type constraints are exact predicates on normalized DataType: `scalar` accepts only scalar nodes; `numeric`, `string`, and `timestamp` accept their corresponding exact scalar classes; `record`, `list`, and `equatable` accept only those normalized forms, with `equatable` requiring the normalized ShapeSystem type to appear in its versioned `equatableTypes` set. Key codecs are selected only by dedup/correlation/group_fold syntax, not by function overload resolution. Unbound type variables in a result, accumulator, or output are invalid.

A SerializableDataType list has static Exact coverage when resolved from a catalog/input/literal shape. Non-Exact list coverage arises only from explicit TOWL coverage-preserving operators and is carried in the validated TypeDescriptor.

For `record_of`, required/optional modes and field types must match; `closed:true` requires equal field sets, while `closed:false` accepts additional actual fields but never missing required fields. `closed:false` is legal only in parameter/input matching. It is invalid in function results, aggregator accumulators/outputs, or any TypeRule position synthesized without an actual matched record; outputs must be closed or row-preserving through an explicitly bound input type variable. Nominal `id`, when present, must equal the actual stable shape ID. `union_of` requires the same discriminator and case labels before recursively unifying cases.

Overload checking is deterministic first-order unification with no coercion: bind each type variable on first occurrence, require exact assignability on every later occurrence, instantiate the result rule, and reject zero or multiple matching overloads. Variadics declare `minItems`, `maxItems?`, and one repeated rule. Empty list literals remain unambiguous because `elementType` is authored.

Typing is syntax-directed. For every expression/term node, the processor computes exactly one `(type,effects,terminalErrors,coverageEffect)` or diagnostics. Child expression evaluation order is array order; object fields and name-keyed maps use UTF-8 byte lexical key order for deterministic primary errors. `default_present` and `default_optional` evaluate their default lazily only when absent.

Pure-expression errors are part of the judgment. Literal/member/object/list nodes have declared core error sets; function overload manifests declare catchable evaluator errors. `require_present` adds `missing_member`. Expression error sets union in the evaluation order above.

---

## 4. Abstract IR grammar and reference JSON encoding

The productions below define abstract IR variants. `kind` is the discriminator used by the reference JSON encoding; a direct typed implementation may represent the same variants as sealed classes/enums and need not store a string field internally. Name-keyed maps and arrays map naturally to host map/list types.

The reference JSON adapter has a two-stage JSON Schema 2020-12 decoder. A small envelope schema reads `kind`, `towl`, RegistryRequirement, CatalogRequirement, ProfileRequirement, and ContextRequirement. After exact registry, catalog, context, and profile resolution, the full adapter schema uses distinct `oneFlow`, `optionalFlow`, `manyFlow`, `expr`, operation/cardinality, function, aggregator, codec, and context branches. Every encoded IR variant uses `const kind` and `additionalProperties:false`; reference, type, coverage, effect, and catalog relations remain IR semantic-validation rules.

### 4.1 Scalar lexical types

```text
Name             = ASCII /^[A-Za-z_][A-Za-z0-9_]{0,63}$/
MemberName       = nonempty Unicode string without normalization
ShapeId          = nonempty string
FunctionName     = nonempty string
AggregatorName   = nonempty string
KeyCodecName     = nonempty string
MappingId        = nonempty string
NonNegativeInteger = JSON integer in [0, 9007199254740991]
PositiveInteger  = JSON integer in [1, 9007199254740991]
ExactVersion     = 1..128 ASCII graphic characters excluding `* < > = ~ ^` and ASCII whitespace,
                   and not case-insensitive "latest"
Namespace        = reverse-DNS name
DecimalNatural   = "0" or nonzero ASCII digit followed by ASCII digits
BoundValue       = NonNegativeInteger | {kind:"big_bound",decimal:DecimalNatural} | "symbolic"
```

All string ordering in v2 is lexicographic over UTF-8 bytes. MemberPath comparison is lexicographic by member under that order; if one path is a prefix, the shorter path sorts first. Arrays retain authored order. This comparator governs maps, call arguments/options, diagnostics, and primary errors.

### 4.2 Closed common records

```text
ShapeRef = {"kind":"shape_ref", "id":ShapeId}
CoverageReason =
  {"kind":"authored_bound", "nodePath":String,
   "dimension":"max_items"|"max_pages", "limit":PositiveInteger}
| {"kind":"policy_drop", "nodePath":String, "code":String}
| {"kind":"recovered_error", "nodePath":String, "code":String}

UnintendedReason =
  {"kind":"budget", "nodePath":String,
   "dimension":"max_logical_calls"|"max_items_per_binding"|"max_expression_steps"}
| {"kind":"transport", "nodePath":String, "code":String}
| {"kind":"cursor_cycle", "nodePath":String}
| {"kind":"cursor_error", "nodePath":String, "code":String}
| {"kind":"timeout", "nodePath":String}
| {"kind":"cancellation", "nodePath":String}
| {"kind":"element_error", "nodePath":String, "code":String,
   "index":NonNegativeInteger}
```

Reason sets are deduplicated and ordered by `(kind,nodePath,index?,dimension?,code?,limit?)` using UTF-8 byte lexical order for strings. `policy(code,path)` below is shorthand for the corresponding `policy_drop` or `recovered_error` record; it never replaces existing reasons.

### 4.3 Plan, registry, scope, and bindings

```text
Plan = {
  "kind": "plan",
  "towl": "v2",
  "registry": RegistryRequirement,
  "catalog": CatalogRequirement,
  "profile": ProfileRequirement,
  "context": ContextRequirement,
  "description": String,
  "inputs": Map<Name,InputDecl>?,
  "budget": SemanticBudget?,
  "body": ScopeAny
}

RegistryRequirement = {
  "kind": "registry_requirement",
  "name": String,
  "version": ExactVersion
}
CatalogRequirement = {
  "kind": "catalog_requirement",
  "name": String,
  "version": ExactVersion
}
ProfileRequirement = {
  "kind": "profile_requirement",
  "name": String,
  "version": ExactVersion
}
ContextRequirement = {
  "kind": "context_requirement",
  "name": String,
  "version": ExactVersion
}

ScopeAny      = ScopeOne | ScopeOptional | ScopeMany
ScopeOne      = {"kind":"scope_one", "let":Map<Name,Binding>?, "return":OneTerm}
ScopeOptional = {"kind":"scope_optional", "let":Map<Name,Binding>?, "return":OptionalTerm}
ScopeMany     = {"kind":"scope_many", "let":Map<Name,Binding>?, "return":ManyTerm}

Binding = BindingOne | BindingOptional | BindingMany
BindingOne     = {"kind":"one", "value":OneTerm}
BindingOptional= {"kind":"optional", "value":OptionalTerm}
BindingMany    = {"kind":"many", "value":ManyTerm}
```

Scope kinds make return cardinality schema-visible. A binding tag is an authored assertion and MUST equal inferred term cardinality.

### 4.4 Shape references, typed literals, and inputs

```text
TypedLiteral = {"kind":"literal", "type":ShapeRef, "value":JSON}

InputDecl = {
  "kind":"required_input", "type":ShapeRef
} | {
  "kind":"defaulted_input", "type":ShapeRef, "default":TypedLiteral
} | {
  "kind":"optional_input", "type":ShapeRef
}
```

Defaults MUST be assignable to the declared shape. Required/defaulted inputs yield One; optional inputs yield Optional. Unknown supplied inputs are invalid; missing required inputs are invalid; omitted optional inputs are absent. Supplied values are validated without coercion before execution.

### 4.5 Context contract

The host resolves the exact ContextRequirement against an immutable versioned context catalog:

```text
ContextIdentity = {kind:"context_identity",name:String,version:String}
ContextCatalogResolver = {resolve(ContextRequirement)->ContextCatalog|Unknown}
ContextCatalog = {
  identity:ContextIdentity,
  entries:Map<Name,{cardinality:One|Optional,shape:ShapeRef,authority:AuthorityDescriptor}>
}
ProfileIdentity = {kind:"profile_identity",name:String,version:String}
StructuralLimits = {kind:"structural_limits",maxExpressionDepth:PositiveInteger,
  maxAstNodes:PositiveInteger,maxBindingsPerScope:PositiveInteger,maxFanoutDepth:PositiveInteger}
QualificationRules = {kind:"qualification_rules",serviceRequired:Boolean}
AuthorityRules = {kind:"authority_rules",allowedClasses:[String*]}
ProfileManifest = {kind:"profile_manifest",identity:ProfileIdentity,
  structuralLimits:StructuralLimits,qualificationRules:QualificationRules,
  authorityRules:AuthorityRules}
ProfileResolver = {resolve(ProfileRequirement)->ProfileManifest|Unknown}
```

Validation resolves every env descriptor and checks name, cardinality, shape, and declared authority requirement; it does not read run values. One ContextSnapshot is decoded and frozen at preflight. Every context entry referenced by the plan or a selected descriptor must appear in ContextSnapshot: One entries use context_present; Optional entries use exactly context_present or context_absent. Omission of a referenced Optional entry is a preflight error, not implicit absence. Unreferenced catalog entries MAY be omitted. Unavailable runtime authority is also a preflight error. Repeated execution reads use the frozen normalized value. Profile validation is closed: it may reject only when an authored service qualifier violates `serviceRequired`, an AST exceeds one of the lower structural limits, or a resolved EffectDescriptor authority class is absent from `allowedClasses`. A profile cannot add other acceptance predicates or reinterpret operators. Separate host execution policy may reject an otherwise validated plan and reports that in PolicySummary, but does not change core/profile validity. ValidationReport and ExecutionResult include ProfileIdentity and ContextIdentity; changing either changes the validation environment.

### 4.6 Pure expressions

```text
Expr = TypedLiteral
     | {"kind":"ref_one", "name":Name, "path":MemberPath?}
     | {"kind":"input_one", "name":Name, "path":MemberPath?}
     | {"kind":"env_one", "name":Name, "path":MemberPath?}
     | {"kind":"object", "fields":Map<String,Expr>}
     | {"kind":"list", "elementType":ShapeRef, "items":[Expr*]}
     | {"kind":"apply", "function":FunctionName, "arguments":[Expr*]}
     | {"kind":"list_map", "source":Expr, "as":Name, "select":Expr}
     | {"kind":"maybe_absent", "valueType":ShapeRef}
     | {"kind":"maybe_present", "value":Expr}
     | {"kind":"is_present", "value":Expr}
     | {"kind":"is_absent", "value":Expr}
     | {"kind":"require_present", "value":Expr, "error":ErrorTemplate}
     | {"kind":"default_present", "value":Expr, "default":Expr}
```

`ref_one` names only One bindings or lexical item binders. `input_one` and `env_one` require One descriptors. Empty lists are typed by `elementType`; every item must be assignable. Function names are values, never keys.

### 4.7 Flow-term unions

```text
OneTerm ::= Expr | CallOne | MapOne | Materialize | Fold | GroupFold
          | RequireOptional | DefaultOptional | AttemptOne | AttemptOptional | ScopeOne

OptionalTerm ::= RefOptional | InputOptional | EnvOptional | CallOptional
               | MapOptional | RecoverOptional | RecoverOptionalFailure | ScopeOptional

ManyTerm ::= RefMany | CallMany | MapMany | FilterMany | DedupMany
           | Singleton | PresentOptional | Elements | GroupMap | FlatMap
           | CorrelateMany | RecoverEmpty | AttemptMany | ScopeMany
```

Exact source forms:

```text
RefOne      = {"kind":"ref_one", "name":Name, "path":MemberPath?}
RefOptional = {"kind":"ref_optional", "name":Name}
RefMany     = {"kind":"ref_many", "name":Name}
InputOptional={"kind":"input_optional", "name":Name}
EnvOptional = {"kind":"env_optional", "name":Name}
```

Optional/Many paths require explicit map operators; only One expressions navigate members.


A typed literal synthesizes exactly its resolved ShapeRef type. A `list` expression synthesizes `List<T,Exact>` where `T` is `elementType`; each item must be assignable to T. An `object` expression synthesizes one closed anonymous record in UTF-8 byte field-name order. Empty object/list expressions remain fully typed by their fields/elementType.

### 4.8 Calls, arguments, options, and pagination

```text
Argument = {"kind":"argument", "path":MemberPath, "value":Expr}
CallOption= {"kind":"call_option", "name":String, "value":Expr}

CallOne = {
  "kind":"call_one", "service":String?, "operation":String,
  "arguments":[Argument*], "options":[CallOption*]
}
CallOptional = {
  "kind":"call_optional", "service":String?, "operation":String,
  "arguments":[Argument*], "options":[CallOption*]
}
CallMany = {
  "kind":"call_many", "service":String?, "operation":String,
  "arguments":[Argument*], "options":[CallOption*],
  "pagination":Pagination?
}
```

Paginator-owned inputCursor and pageSizePath are forbidden authored argument paths; the runtime injects them. `pageSize` is invalid when the PagingDescriptor has no pageSizePath and otherwise must fall within declared bounds. Authored argument collision is a validation error, never overwrite/merge/ignore.

Call kind MUST equal catalog result cardinality. Arguments/options are pure One expressions. Argument paths MAY be empty only for an operation whose normalized input root is scalar, list, map, or union; record-input arguments use nonempty paths. Duplicate paths, parent-child collisions, missing required paths, unknown paths, and type mismatches are invalid. Option order has no semantics; duplicate option names are invalid.

Input construction is canonical. For scalar/list/map/union/Maybe roots, exactly one argument with empty path supplies the whole normalized input. For a Maybe root, exactly one empty-path expression assignable to Maybe<T> is required; use maybe_absent or maybe_present explicitly—zero arguments is not implicit absence. For record roots, argument paths form a prefix-free set. A path may supply a whole nested record or its descendants, never both. The processor synthesizes records from leaves in MemberPath UTF-8 order. Every required member of each present record must be covered; any supplied descendant of an optional record makes that parent present and then all of that record's required members must be covered. An optional record with no supplied path in its subtree is absent. Union values are supplied only as a whole typed union payload. These rules run before operation invocation and leave one unique normalized input value.

An omitted optional argument path constructs provider absence. Supplying that path requires an expression assignable to the member's underlying T and constructs provider presence; a `Maybe<T>` expression is accepted only when the operation manifest explicitly expects Maybe. Dynamic omission is unavailable in v2.

For a pageable operation, `pagination` is REQUIRED. For a nonpageable operation it is forbidden.

```text
Pagination = {
  "kind":"all", "pageSize":PositiveInteger?
} | {
  "kind":"bounded", "maxItems":PositiveInteger?,
  "maxPages":PositiveInteger?, "pageSize":PositiveInteger?
}
```

`bounded` requires maxItems or maxPages or both (JSON Schema `anyOf`, not `oneOf`). `all` and nonpageable complete calls have static Exact coverage. `bounded` adds authored reasons. Runtime interruption changes completion to Incomplete without changing static Q.

### 4.9 Map, filter, dedup, and conversions

```text
MapOne      = {"kind":"map_one", "source":OneTerm, "as":Name, "select":Expr}
MapOptional = {"kind":"map_optional", "source":OptionalTerm, "as":Name, "select":Expr}
MapMany     = {"kind":"map_many", "source":ManyTerm, "as":Name, "select":Expr}
FilterMany  = {"kind":"filter_many", "source":ManyTerm, "as":Name,
               "where":Expr, "correlation":CatalogCorrelation?}
DedupMany   = {"kind":"dedup_many", "source":ManyTerm, "as":Name,
               "codec":KeyCodecName, "key":Expr}

Singleton       = {"kind":"singleton", "source":OneTerm}
PresentOptional = {"kind":"present_optional", "source":OptionalTerm}
Elements        = {"kind":"elements", "source":OneTerm}
Materialize     = {"kind":"materialize", "source":ManyTerm}
RequireOptional = {"kind":"require_optional", "source":OptionalTerm, "error":ErrorTemplate}
DefaultOptional = {"kind":"default_optional", "source":OptionalTerm, "default":Expr}
```

Every `as` binder is visible only in `select`, `where`, or `key` respectively; never in `source`. `dedup_many.key` is one expression. Codec-equal classes retain the first source item in logical order; later equal-key items are removed. `group_fold` similarly emits the first encountered original K value as each group's key, even when later codec-equal representations differ. Composite keys use an explicitly typed object/list expression and one matching tuple codec.

### 4.10 Fanout and explicit flattening

```text
GroupMap = GroupMapFail | GroupMapSkip | GroupMapCollect
GroupMapFail = {"kind":"group_map","source":ManyTerm,"as":Name,
  "body":ScopeOne,"errors":"fail"}
GroupMapSkip = {"kind":"group_map","source":ManyTerm,"as":Name,
  "body":ScopeOne,"errors":"skip"}
GroupMapCollect = {"kind":"group_map","source":ManyTerm,"as":Name,
  "body":ScopeOne,"errors":"collect","correlationKey":Expr?}

FlatMap = FlatMapFail | FlatMapSkip | FlatMapCollect
FlatMapFail = {"kind":"flat_map","source":ManyTerm,"as":Name,
  "body":ScopeMany,"errors":"fail"}
FlatMapSkip = {"kind":"flat_map","source":ManyTerm,"as":Name,
  "body":ScopeMany,"errors":"skip"}
FlatMapCollect = {"kind":"flat_map","source":ManyTerm,"as":Name,
  "body":ScopeMany,"errors":"collect","correlationKey":Expr?}
```

`as` is visible only in `body` and collect-mode `correlationKey`. `correlationKey` evaluates once before the body; catchable evaluator errors are promoted to uncatchable core.correlation_key while already-uncatchable errors retain identity, and its typed value appears on both success and error outcomes. `group_map` emits exactly one body value per successful outer item. `flat_map` removes exactly one Many layer and concatenates in outer order then body order. Body cardinality mismatches are schema/type errors.

Mode typing:

```text
group_map fail    : Many<A,Q> × (A -> One<B>)  -> Many<B,Q>
group_map skip    : ... -> Many<B,join(Q,policy(fanout_error_drop))>
group_map collect : ... -> Many<Outcome<B,Error>,Q>

flat_map fail    : Many<A,Q> × (A -> Many<B,Qb>) -> Many<B,join(Q,Qb)>
flat_map skip    : ... -> Many<B,join(Q,Qb,policy(fanout_error_drop))>
flat_map collect : ... -> Many<Outcome<B,Error>,join(Q,Qb)>
```

A flat-map body that emits values then completes Incomplete propagates those values and Incomplete completion in every mode; it is not converted to an item error. Fanout terminal body failures are handled by mode. `collect` wraps every success and one failure per failed outer body with stable outer index and optional `errorKey`.

### 4.11 Explicit non-fanout recovery

```text
RecoverOptional = {"kind":"recover_optional", "source":OneTerm}
RecoverOptionalFailure = {"kind":"recover_optional_failure", "source":OptionalTerm}
RecoverEmpty    = {"kind":"recover_empty", "source":ManyTerm}
AttemptOne      = {"kind":"attempt_one", "source":OneTerm}
AttemptOptional = {"kind":"attempt_optional", "source":OptionalTerm}
AttemptMany     = {"kind":"attempt_many", "source":ManyTerm}
```

- `recover_optional` maps catchable terminal One failure to absent Optional.
- `recover_optional_failure` preserves Optional success/absence and maps catchable terminal failure to absent Optional.
- Both recoveries append a `recovered_error` entry to the execution recovery ledger when used; any ledger entry forces partial status even though Optional has no coverage parameter.
- `recover_empty` has static coverage effect `join(Q,policy(recovered_error))`; at runtime it maps catchable failure before any Many value to empty Complete coverage with that actual reason. Failure after emission remains Incomplete and is not erased.
- `attempt_one` returns `One<Outcome<T,Error>>`.
- `attempt_optional` returns `One<Outcome<Maybe<T>,Error>>`.
- `attempt_many` wraps values as outcomes and appends a terminal error outcome when catchable; runtime Incomplete remains Incomplete.

Without an explicit recovery/fanout mode, terminal errors fail the scope.

### 4.12 Correlation

Provider filter lowering is explicit on `filter_many`:

```text
CatalogCorrelation = {
  "kind":"catalog_correlation", "outer":Name, "mapping":MappingId
}
```

It is required when the predicate references an ancestor fanout binder and the source is a provider call. The catalog mapping fixes equivalent argument derivation, missing/case/duplicate semantics, limits, batching, repartitioning, order, and error attribution.

A validator computes each Many term's **source lineage** through refs and elementwise map/filter/dedup nodes. For every `filter_many.where` with ancestor free references, exactly one ancestor binder is permitted and MUST equal `correlation.outer`; zero/multiple/different ancestors are invalid. Conversely, a CatalogCorrelation object is forbidden when the predicate has no qualifying ancestor free reference or the lineage is not eligible. Lineage MUST resolve to exactly one eligible `call_many`, the mapping MUST belong to that operation, and its versioned recognizer/lowerer MUST prove the complete normalized outer-dependent predicate observationally equivalent. Wrapping or rebinding never bypasses this rule. A lineage containing fanout, recovery, materialize/elements, multiple calls, or outer-dependent effects is not catalog-correlatable; use `correlate_many` for an immutable bound source instead.

Bound-stream correlation is:

```text
CorrelateMany = {
  "kind":"correlate_many", "source":ManyTerm, "as":Name,
  "outer":Name, "innerKey":Expr, "outerKey":Expr,
  "codec":KeyCodecName
}
```

`as` is visible only in `innerKey`; `outer` must be an ancestor fanout binder. The executor indexes source once. Unsupported outer-dependent scans are invalid.

### 4.13 Fold and aggregates

```text
Fold = {"kind":"fold", "source":ManyTerm, "as":Name, "aggregate":Aggregate}

Aggregate = {
  "kind":"aggregate_apply", "aggregator":AggregatorName,
  "arguments":[ExpressionArgument*]
} | {
  "kind":"aggregate_product", "fields":Map<String,Aggregate>
}
ExpressionArgument = {"kind":"expression_argument", "value":Expr}
```

`as` is visible in aggregate expression arguments. V2 aggregate application arguments are expressions only; recursive aggregate arguments are removed. Higher-order grouping aggregators are represented by distinct registered constructors whose expression parameters and accumulator/output rules are fully declared, or by product aggregates. Aggregate syntax is legal only under fold.

### 4.14 Grouped fold

```text
GroupFold = {
  "kind":"group_fold", "source":ManyTerm, "as":Name,
  "key":Expr, "codec":KeyCodecName, "aggregate":Aggregate
}
```

`as` is visible in key and aggregate expression arguments. `group_fold` indexes groups in first-key logical order and returns `One<Covered<List<Record{key:K,value:U},Exact>,Q>>`; generic non-string map encoding is deliberately absent. It replaces higher-order nested aggregate arguments; no aggregate node consumes another aggregate node.

### 4.15 Error templates and semantic budgets

```text
ErrorTemplate = {"kind":"error_template", "code":String, "message":String}

SemanticBudget = {
  "kind":"semantic_budget",
  "maxLogicalCalls":PositiveInteger?,
  "maxItemsPerBinding":PositiveInteger?,
  "maxResultBytes":PositiveInteger?,
  "maxExpressionSteps":PositiveInteger?
}
```

At least one semantic budget member is required. Physical requests, concurrency, retries, and wall-clock timeout are host governors, not portable authored semantics.

---

## 5. Scope, free references, and dependency graph

Inputs, let bindings, and operator binders share one lexical namespace. Same-scope duplicates and ancestor shadowing are invalid; disjoint sibling scopes may reuse names. Binding maps are mutually visible, so forward references are legal. Child declarations never escape.

Binder regions are exact:

- map binder: select only;
- filter binder: where only;
- dedup binder: key only;
- list-map binder: select only;
- fold binder: aggregate expression arguments only;
- group/flat binder: body and collect-mode correlationKey only;
- correlate inner binder: innerKey only.

A binder is never visible in its own source.

Every free binding reference anywhere in a binding or return AST induces a dependency edge. References bound by the node's local binder do not. This single rule covers calls, conversions, recovery, correlation, fanout, fold, expressions, and nested scopes. Cycles are invalid. Document map order has no dependency meaning.

Every binding in an entered scope executes when dependencies are ready, even if return does not reference it. Return is not a dead-effect root.

Canonical node paths use RFC 6901 JSON Pointer over the authored tree, with binding-map names escaped per RFC 6901. Canonical comparison is UTF-8 byte lexical order of the pointer, followed by outer/body nonnegative indices numerically. Diagnostic, primary-error, accounting, and semantic-budget order all use this relation.

Only group_map and flat_map consume fanout depth. Their bodies check at `d+1`; fanout is invalid at depth 2. V2 also fixes maximum expression depth at 64, maximum total AST nodes at 10,000, and maximum bindings in one scope at 1,024. AST count is the recursive sum of one for Plan, each requirement/input/budget, every scope, binding, flow term, expression, aggregate, argument, call option, pagination, correlation, ErrorTemplate, and ShapeRef; map/list containers and every JSON value inside TypedLiteral/diagnostic/provider/migration/extension islands count zero. Expression depth counts only Expr variants, root depth 1, following expression-child edges (including list_map source/select) but not enclosing flow terms. Profiles MAY impose lower limits and report them before execution; they MUST NOT accept higher limits as v2.

### 5.1 Scope failure

A scope succeeds only when every eager binding and return succeeds. The sequential reference interpreter stops at the first terminal Error in canonical evaluation order, prevents later admissions, and returns that single semantic terminal Error. Optimized physical work admitted under equivalence may report secondary attempt failures only as diagnostics/accounting; it MUST preserve the same single semantic terminal Error and effect set as the reference interpreter. Unordered effectful bindings must satisfy catalog commutation requirements (§11) or validation rejects the scope.

---
## 6. Shape, registry, context, and catalog contracts

### 6.1 Language environment

```text
LanguageEnvironmentResolver = {
  resolve(name,version) -> LanguageEnvironment | UnknownName | UnknownVersion,
  available() -> [RegistryIdentity]
}

RegistryIdentity = {name:String, version:String}
Deprecation = {since:String, message:String, replacement?:String, removal?:String}

LanguageEnvironment = {
  identity:RegistryIdentity,
  shapeSystem:ShapeSystemManifest,
  functions:Map<FunctionName,[FunctionOverloadManifest,...]>,
  aggregators:Map<AggregatorName,AggregatorConstructorManifest>,
  keyCodecs:Map<KeyCodecName,KeyCodecManifest>,
  deprecation?:Deprecation
}

FunctionOverloadManifest = {
  parameters:[TypeRule,...], variadic?:{minItems:NonNegativeInteger,maxItems?:NonNegativeInteger,rule:TypeRule},
  result:TypeRule, errors:[EvaluatorErrorDescriptor,...], laws:FunctionLaws,
  evaluatorId:String, contextDependencies:[ContextPath,...],
  deprecation?:Deprecation
}

AggregatorConstructorManifest = {
  expressionParameters:[TypeRule,...], input:TypeRule,
  accumulator:TypeRule, output:TypeRule,
  errors:[EvaluatorErrorDescriptor,...], laws:MonoidLaws,
  constructorId:String, deprecation?:Deprecation
}
KeyCodecManifest = {
  input:TypeRule, missingSemantics:MissingSemantics,
  errors:[EvaluatorErrorDescriptor,...],
  canonicalizeId:String, equalityId:String, hashId:String,
  laws:KeyCodecLaws, deprecation?:Deprecation
}
```


In FunctionOverloadManifest, `parameters` are the fixed prefix. Variadic minItems/maxItems count only repeated suffix arguments; total arity is prefix length plus suffix count. A nonvariadic manifest accepts exactly the prefix length.

All manifests are ordinary versioned JSON. Runtime handles never appear in them. Evaluator/constructor/codec IDs select immutable implementations within the installed exact registry version; registry admission requires a fixed conformance corpus to match every manifest.

MissingSemantics is exact: `error` emits the codec's declared catchable error when a Maybe key is absent; `distinct` canonicalizes every absence to one shared missing token; `forbidden` requires static proof that key type contains no Maybe/optional member and is otherwise a validation error. It applies identically to dedup, group_fold, correlation, and commutation key use.

Function evaluators and key codecs are deterministic and pure. Key-codec laws require canonicalization stability, equality equivalence, equal-values-imply-equal-hash, collision-safe equality after hash, and declared missing behavior. Aggregator constructors instantiate `prepare`, `identity`, `combine`, and `present` from typed per-item expression parameters. Product aggregate is the only core aggregate composition.

Context dependencies are resolved exactly from the frozen ContextCatalog, create dependencies in the validated plan, appear in `resolvedContext`, and fail before effects when unavailable.

### 6.2 Exact registry resolution

Registry metadata has this closed wire form:

```text
RegistryIdentity = {"kind":"registry_identity","name":String,"version":String}
ProfileIdentity = {"kind":"profile_identity","name":String,"version":String}
Deprecation = {"kind":"deprecation","since":String,"message":String,
  "replacement":String?,"removal":String?}
ContextPath = [Name, MemberName*]
MissingSemantics = "error" | "distinct" | "forbidden"
FunctionLaws = {"kind":"function_laws","associative":Boolean,
  "commutative":Boolean,"idempotent":Boolean,"relationRole":String?,
  "booleanRole":String?,"complement":String?}
MonoidLaws = {"kind":"monoid_laws","commutative":Boolean,
  "orderedCombine":Boolean}
KeyCodecLaws = {"kind":"key_codec_laws","equivalence":Boolean,
  "stableCanonicalization":Boolean,"equalImpliesEqualHash":Boolean,
  "collisionCheckedByEquality":Boolean}
ShapeSystemManifest = {"kind":"shape_system_manifest","id":String,
  "types":Map<ShapeId,SerializableDataType>,"equatableTypes":[ShapeId,...],
  "schemaProjectionId":String,"assignabilityId":String,
  "decodeAuthoredId":String,"decodeProviderId":String,"encodeRuntimeId":String}
AuthorityDescriptor = {"kind":"authority","class":String,"scope":String}
EvaluatorErrorDescriptor = {"kind":"evaluator_error","class":ErrorClass,
  "code":String,"phase":String,"catchable":Boolean,"retryable":Boolean,
  "messageTemplate":String}
```

The plan's exact ContextRequirement is the sole context catalog used for authored env references and descriptor context dependencies; registry manifests do not embed or override it. A ContextPath is nonempty: its first segment names a ContextCatalog entry and remaining segments use ordinary member typing. Required paths deliver T; optional paths deliver Maybe<T>. Function evaluator signatures include these implicit context values after authored arguments in declared ContextPath order.

The shape system MUST project each SerializableDataType to JSON Schema 2020-12 and implement canonical authored/provider decoding plus runtime encoding. Exact-version registry admission runs shared fixtures proving schema validation, decode normalization, member/assignability behavior, and encode/decode round trips for every scalar, optional record, list, map, and union type.

Registry name/version resolution is exact; ranges, aliases, current-version substitution, and fallback are invalid. Several versions may coexist. Semantic manifests are immutable. Non-semantic deprecation advisories MAY evolve for an installed version; the processor freezes one advisory snapshot for validation/execution and reports it, but it never changes typing or behavior.

Validation emits exactly one deduplicated warning for each deprecated selected registry, function, aggregator, or codec.

### 6.3 Operation catalog

```text
OperationId = nonempty string
CatalogIdentity = {"kind":"catalog_identity","name":String,"version":String}
OperationCatalogManifest = {"kind":"operation_catalog","identity":CatalogIdentity,
  "operations":Map<OperationId,OperationManifest>,
  "commutationRelations":[CrossCommutationRule,...]}
OperationCatalogResolver = {
  resolve(CatalogRequirement) -> OperationCatalog | UnknownName | UnknownVersion,
  available() -> [CatalogIdentity]
}
OperationCatalog = {
  manifest:OperationCatalogManifest,
  resolve(service?,operation) -> ResolvedOperation | Unknown | Ambiguous
}

OperationManifest = {
  "kind":"operation_manifest","id":OperationId,
  "service":String?,"operation":String,
  "input":ShapeRef,"response":ShapeRef,
  "result":{"cardinality":"one"|"optional"|"many",
            "subjectPath":MemberPath,"shape":ShapeRef,
            "optionalPresence":ProjectionRule?},
  "resultExtractionId":String,
  "resultBounds":{"lower":BoundValue,"upper":BoundValue},
  "effects":[EffectDescriptor,...],"errors":[OperationErrorDescriptor,...],
  "paging":NotPaged|Paged,
  "correlations":Map<MappingId,CorrelationDescriptor>,
  "splitRule":InputSplitRule?,"options":CallOptionSchema,
  "retrySafety":RetrySafety,"commutationClass":String,
  "commutationKey":ProjectionRule,"commutationCodec":KeyCodecName,
  "invokerId":String
}
NotPaged = {"kind":"not_paged"}
Paged = {"kind":"paged","descriptor":PagingDescriptor}
ResolvedOperation = {manifest:OperationManifest, invocationHandle:OpaqueInvocationHandle}
```

`OpaqueInvocationHandle` is injected runtime authority and never serialized. Catalog admission verifies it against the manifest `invokerId` and conformance fixtures for the exact catalog version. All normalized input/response/result types are ShapeRefs resolved through the selected ShapeSystem. The invocation handle returns exactly `response`; `resultExtractionId` and conformance fixtures normalize `subjectPath` into the declared logical shape/cardinality. Empty subjectPath selects the response root. Optional results require `optionalPresence`; One forbids it. Provider-native Shape and CatalogPath values never enter the TOWL contract.

Catalog descriptor manifests have these minimum closed forms:

```text
EffectDescriptor = {kind:"effect", class:String, resourceKeyProjection:ProjectionRule,
  authority:AuthorityDescriptor}
OperationErrorDescriptor = {kind:"operation_error", code:String, class:ErrorClass,
  providerCode:String?, phase:String, retryable:Boolean, catchable:Boolean,
  messageTemplate:String}
PagingDescriptor = {kind:"paging", inputCursor:MemberPath, outputCursor:MemberPath,
  cursorType:ShapeRef, cursorCodec:KeyCodecName,
  cursorAbsentMeansEnd:Boolean, cursorNullMeansEnd:Boolean,
  pageSizePath:MemberPath?, pageSizeMin:PositiveInteger?, pageSizeMax:PositiveInteger?,
  itemPath:MemberPath, order:"page_then_item",
  algorithmId:String}
RetrySafety = {kind:"retry_never"}
            | {kind:"retry_idempotent"}
            | {kind:"retry_tokenized", tokenPath:MemberPath}
CrossCommutationRule = {kind:"commutation_rule", leftClass:String,rightClass:String,
  relation:"always"|"different_keys"|"never", algorithmId:String}
CorrelationDescriptor = {kind:"correlation",
  predicateRole:"equality"|"ordering"|"membership",
  recognizerId:String, lowererId:String,
  outerArgumentProjection:ProjectionRule, missing:MissingSemantics,
  caseMode:"exact"|"registry_normalized", duplicates:"preserve"|"deduplicate",
  maxValues:PositiveInteger?, batching:"per_item"|"set"|"tuple",
  repartition:"stable_outer_order", errorAttribution:"outer_item"}
CallOptionSchema = {kind:"call_options", entries:Map<String,{type:TypeRule,owner:String}>}
ProjectionRule = {kind:"projection", manifest:JSON, algorithmId:String}
```

`ProjectionRule.manifest` is an explicitly named catalog-owned JSON island whose algorithm ID is fixed by the exact catalog version and conformance fixtures.

The following are versioned catalog interfaces, not authored wire nodes:

- `PagingDescriptor` defines cursor injection/extraction/type/equality, absent/null terminal states, legal page-size hints, item extraction, page/item order, and an immutable algorithm plus conformance fixtures.
- `CorrelationDescriptor` defines predicate equivalence, outer argument derivation, case/missing/duplicate behavior, max values, batching, repartitioning, and error attribution.
- `CallOptionSchema` is a closed name/type/owner map; duplicate authored options are invalid.
- `RetrySafety` is `never | idempotent | tokenized(tokenPath)`.
- `commutationClass`, each operation's key projection, and the catalog-wide symmetric `CrossCommutationRule` matrix provide a total pairwise decision. Missing class pairs mean `never`. Two unordered logical invocations are legal only when the selected relation proves all normalized key abstractions commute.
- `OperationErrorDescriptor` assigns stable class/code, phase, retryability, and catchability.
- `EffectDescriptor` assigns stable effect class/resource-key projection and authority requirements.

Unknown service/operation resolution and ambiguity are validation errors listing candidates. A supplied qualifier is never ignored. Profiles may require qualification even for unique names. Catalog name/version selects one immutable manifest. Catalog admission resolves inputCursor and pageSizePath against input, resolves subjectPath/itemPath/outputCursor against response, requires both cursor paths assignable to cursorType and cursorCodec input to accept cursorType, and requires pageSizePath (when present) to be integer within declared bounds. It requires One bounds 1..1, Optional bounds 0..1, and `Paged => result.cardinality == many`; paging itemPath and result subjectPath resolve to the same logical item shape/order. One/Optional extraction, missing/null behavior, and paging extraction are fixed by the versioned extraction/paging IDs and fixtures.

List members do not imply Many. Cardinality and subject are catalog metadata. The validated plan stores all resolved handles/descriptors; execution repeats no string lookup.

### 6.4 Catalog request splitting

```text
InputSplitRule = {
  "kind":"stable_list_chunks",
  "path":MemberPath,
  "maxValues":PositiveInteger,
  "resultCombine":"ordered_concatenate",
  "failureMode":"fail_logical_call"
}
```

OperationManifest contains zero or one splitRule; multiple/overlapping rules are unrepresentable. Catalog admission requires its path to resolve against input to a required `List<T>` (not Maybe/optional), and chunk replacement values remain exactly `List<T,Exact>`. The rule activates only when the target list length exceeds maxValues; length zero through maxValues performs one unsplit physical request, preserving provider empty-list semantics. When active it creates adjacent stable nonempty chunks, reconstructs every request by replacing only that path, and performs one logical Many call as several physical requests. Each chunk follows the operation's authored pagination policy; chunk result streams concatenate in chunk order then page/item order. `maxItems` and `maxPages` are global to the one logical call: page responses count across chunks in chunk/page order, and reaching either bound stops later pages and later chunks before admission. One/Optional splitting, overlapping rules, multi-path splitting, custom partitioners, and hole-producing partial continuation are invalid in v2. A failed chunk stops later chunks: failure before any emitted combined value is terminal; failure after emission marks the logical Many Incomplete. Retries, coverage, errors, and logical/physical accounting remain attributed to the one logical call. Authors never specify chunk sizes.

### 6.5 Input/context snapshots and execution preflight

```text
AuthoredLiteralPayload = JSON
InputSnapshotEntry = {kind:"input_present",value:AuthoredLiteralPayload}
ContextSnapshotEntry = {kind:"context_present",value:AuthoredLiteralPayload}
                     | {kind:"context_absent"}
InputSnapshot = {kind:"input_snapshot",values:Map<Name,InputSnapshotEntry>}
ContextSnapshot = {kind:"context_snapshot",identity:ContextIdentity,
  values:Map<Name,ContextSnapshotEntry>}
PreflightOutcome = {kind:"preflight_ready",inputs:Map<Name,TypedData>,
  context:Map<Name,TypedData|MaybeData>}
 | {kind:"preflight_error",errors:[Diagnostic+]}
```

`TypedData` and `MaybeData` above are in-memory values conforming to DataType/Maybe, not new wire variants. Preflight runs after lowering and before execution admission. It rejects unknown inputs, missing required inputs, malformed/default-mismatched values, context snapshot identity mismatch, unknown context names, missing explicitly required referenced context entries, and unavailable authority. Known but unreferenced context entries are ignored without decoding and omitted from resolvedContext. All failures use stable `input.*`/`context.*` diagnostics. It resolves defaults and absence without coercion, freezes normalized snapshots in the executable plan instance, and guarantees no logical invocation is admitted on error. Preflight errors are returned as PreflightOutcome (or the host's configuration-error projection), not as provider ExecutionError; resolvedContext contains only the frozen successful snapshot.

---

## 7. Exhaustive pure-expression calculus

Every pure expression has zero effects. Error sets union in the deterministic child order stated below.

| Expression           | Preconditions/binder                                                  | Result type                            | Added terminal errors                                     | Evaluation order                      |
|----------------------|-----------------------------------------------------------------------|----------------------------------------|-----------------------------------------------------------|---------------------------------------|
| typed literal        | value validates declared ShapeRef                                     | resolved type                          | core.expression on invalid runtime representation         | one node                              |
| ref/input/env one    | source One; member path valid                                         | path result T or Maybe<T>              | unavailable context is pre-execution validation error     | path order                            |
| object               | unique fields                                                         | closed anonymous required-field record | child errors                                              | UTF-8 byte lexical field names        |
| list                 | every item assignable to elementType                                  | List<T,Exact>                          | child errors                                              | array order                           |
| apply                | exactly one overload unifies; no disallowed coverage-bearing argument | instantiated result                    | declared evaluator errors ∪ child errors                  | argument array order                  |
| list_map             | source List<A,Q>; binder A                                            | List<B,Q>                              | source ∪ per-item select errors                           | source order                          |
| maybe_absent         | valueType A                                                           | Maybe<A>                               | none                                                      | one node                              |
| maybe_present        | value A                                                               | Maybe<A>                               | value errors                                              | value first                           |
| is_present/is_absent | operand Maybe<A>                                                      | core.boolean                           | operand errors                                            | operand first                         |
| require_present      | operand Maybe<A>                                                      | A                                      | operand ∪ plan:<template code>[class core.missing_member] | operand, then template only if absent |
| default_present      | operand Maybe<A>; default assignable A                                | A                                      | operand ∪ lazy default errors                             | operand, default only if absent       |

Member-path navigation is left-to-right. Required members continue; an optional member yields Maybe and MUST be the final path step until an explicit Maybe operator unwraps it. Function evaluators receive already-evaluated arguments and may emit only declared evaluator errors.

`list_map` binder is visible only in select. It preserves source order and actual/static list coverage; lists exist only after Complete materialization or as ordinary exact data, so they carry no Incomplete state. It is not Many map and never flattens.

Ordinary registered functions, aggregate expression parameters, call arguments, and call options reject `Covered<T,Q>` and any value recursively containing `List<T,Q>` whose static coverage effect is not Exact. This check is recursive through records, unions, maps, Maybe, Outcome, and nested lists, so fold/group_fold cannot launder inner coverage through a scalar aggregate result. Core `list_map` is the only pure coverage-preserving list transform; coverage-sensitive summaries use `elements` followed by fold, which joins that coverage into the fold result. Covered values are review/result values in v2 and have no eliminator into ordinary Expr, aggregate parameters, or call arguments.

AuthoredLiteralPayload is raw JSON in the reference adapter. TypedLiteral values and InputSnapshot entries use `ShapeSystem.decodeAuthored`; operation response payloads and ContextSnapshot present entries use `decodeProvider` against their resolved ShapeRefs. The two decoders may accept different external representations but MUST normalize to the same TypedData for semantically equal values under conformance fixtures. RuntimePayload in §13 is only the normalized result/report encoding; authored IR never contains its wrappers. Adapter conformance tests require decode-then-normalize equivalence across direct IR and JSON construction. No expression changes One/Optional/Many cardinality.

---

## 8. Exhaustive flow, effect, error, and coverage rules

Let `type(t)`, `effects(t)`, `errors(t)`, and `coverage(t)` be syntax-directed. Every grammar branch has exactly one rule below; rules not listed are invalid.

### 8.1 Sources, maps, and conversions

| Term               | Required source                                                                 | Result                                              | Static coverage                      | Terminal errors                                               | Effects         |
|--------------------|---------------------------------------------------------------------------------|-----------------------------------------------------|--------------------------------------|---------------------------------------------------------------|-----------------|
| any Expr `e`       | `Γ ⊢ e:T ! τe`                                                                  | `One<T>`                                            | n/a                                  | τe                                                            | none            |
| `ref_one`          | binding `One<A>`                                                                | `One<A>`                                            | n/a                                  | none                                                          | none            |
| `ref_optional`     | binding `Optional<A>`                                                           | `Optional<A>`                                       | n/a                                  | none                                                          | none            |
| `ref_many`         | binding `Many<A,Q>`                                                             | `Many<A,Q>`                                         | Q                                    | none                                                          | none            |
| input/env refs     | matching frozen descriptor                                                      | matching cardinality                                | declared                             | unavailable-authority before execution                        | none            |
| `call_*`           | catalog kind equals tag                                                         | catalog flow type                                   | nonpaged/all Exact; bounded authored | argument/option errors ∪ catalog errors ∪ paging codec errors | catalog effects |
| `map_*`            | matching cardinality A                                                          | same cardinality B                                  | preserve Q for Many                  | source errors ∪ select-expression errors                      | source effects  |
| `filter_many`      | `Many<A,Q>` and where exactly `core.boolean`                                    | `Many<A,Q>`                                         | Q                                    | source ∪ predicate errors                                     | source effects  |
| `dedup_many`       | `Many<A,Q>` and codec input unifies with key K                                  | `Many<A,Q>`                                         | Q                                    | source ∪ key/codec errors                                     | source effects  |
| `singleton`        | `One<A>`                                                                        | `Many<A,Exact>`                                     | Exact                                | source errors                                                 | source effects  |
| `present_optional` | `Optional<A>`                                                                   | `Many<A,Exact>`                                     | Exact                                | source errors                                                 | source effects  |
| `elements`         | `One<List<A,Q>>`                                                                | `Many<A,Q>`                                         | Q                                    | source errors                                                 | source effects  |
| `materialize`      | `Many<A,Q>` Complete                                                            | `One<List<A,Q>>`                                    | carried in list                      | source ∪ partial_input                                        | source effects  |
| `require_optional` | `Optional<A>`                                                                   | `One<A>`                                            | n/a                                  | source ∪ missing_required                                     | source effects  |
| `default_optional` | `Optional<A>` and default assignable A                                          | `One<A>`                                            | n/a                                  | source ∪ lazy-default errors                                  | source effects  |
| `fold`             | `Many<A,Q>` Complete                                                            | `One<Covered<U,Q>>`                                 | Q in Covered                         | source ∪ aggregate ∪ partial_input                            | source effects  |
| `group_fold`       | `Many<A,Q>` Complete; codec input unifies with key K; aggregate input unifies A | `One<Covered<List<Record{key:K,value:U},Exact>,Q>>` | Q in Covered                         | source ∪ key/codec/aggregate ∪ partial_input                  | source effects  |

Expression error sets union left-to-right for arrays and UTF-8-byte order for objects. Flow effects union source effects and any nested scope/call effects.

### 8.2 Fanout

| Term/mode     | Result                          | Static coverage | Remaining terminal errors                          |
|---------------|---------------------------------|-----------------|----------------------------------------------------|
| group fail    | `Many<B,Q>`                     | Q               | outer ∪ uncatchable body ∪ catchable body promoted |
| group skip    | `Many<B,join(Q,policyDrop)>`    | joined          | outer ∪ uncatchable body                           |
| group collect | `Many<Outcome<B>,Q>`            | Q               | outer ∪ uncatchable body                           |
| flat fail     | `Many<B,join(Q,Qb)>`            | joined          | outer ∪ uncatchable body ∪ catchable body promoted |
| flat skip     | `Many<B,join(Q,Qb,policyDrop)>` | joined          | outer ∪ uncatchable body                           |
| flat collect  | `Many<Outcome<B>,join(Q,Qb)>`   | joined          | outer ∪ uncatchable body                           |

`policyDrop` is exactly `{kind:"policy_drop",nodePath:<fanout path>,code:"fanout_error_drop"}` in the static effect; runtime actual Coverage includes it only if a body error is dropped. Collected errors do not omit positions but enter the recovery ledger and force partial status. A terminally failed fanout body contributes no body Qactual because it emits no B values; skip contributes only policyDrop and collect emits one error outcome. Any eager Many binding in that failed body that already became Incomplete still propagates its Unintended reasons to fanout completion and partial causes, even though its static/actual authored coverage is not joined as body output. Outer/source Incomplete and flat-body Incomplete always propagate through `joinCompletion`; fanout mode cannot catch them.

Fanout effects are outer effects union the body effects for each admitted logical outer item. Static review reports a symbolic multiplicity. Every body call receives a stable logical identity `(fanoutNodePath,outerIndex,bodyNodePath)`.

### 8.3 Recovery and attempt

Catchability belongs to ErrorDescriptorRef identity, not class alone. Core-origin descriptors have the fixed classification in §9.1. Every noncore registry/operation/profile/plan descriptor uses its admitted `catchable` field; a familiar core class name does not override that field, except admission forbids catchable=true for core-uncatchable classes. Runtime Incomplete is never a descriptor and is uncatchable. Every descriptor ref in τ is therefore classified exactly once.

| Term                     | Result                                             | Caught errors                  | Status/provenance                                                                                                                                                                                |
|--------------------------|----------------------------------------------------|--------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| recover_optional         | `Optional<A>`                                      | catchable from One source      | absent + recovery-ledger entry                                                                                                                                                                   |
| recover_optional_failure | `Optional<A>`                                      | catchable from Optional source | absent + recovery-ledger entry                                                                                                                                                                   |
| recover_empty            | `Many<A,join(Q,policyRecovered)>`                  | catchable pre-emission failure | empty + actual policy reason; after-emission Incomplete propagates                                                                                                                               |
| attempt_one              | `One<Outcome<A>>`                                  | catchable                      | error outcome + ledger                                                                                                                                                                           |
| attempt_optional         | `One<Outcome<Maybe<A>>>`                           | catchable                      | absent/present Maybe in ok outcome; error outcome + ledger                                                                                                                                       |
| attempt_many             | `Many<Outcome<A>,join(Q,policy(attempt_stopped))>` | catchable terminal failure     | prior values wrapped ok, one error outcome, later items stop; actual policy reason is always recorded for the caught cutoff; ledger records caught error; provider/runtime Incomplete propagates |

For `recover_empty`, static coverage adds one `{kind:"recovered_error",nodePath:<recover path>,code:<origin>:<descriptor code>}` for every catchable ErrorDescriptorRef in the finite source error set; runtime actual coverage contains only the code that occurred. `attempt_many` uses `{kind:"policy_drop",nodePath:<attempt path>,code:"attempt_stopped"}` whenever it catches an ElementFailure, including failure on the final known item; this avoids an extra source pull and makes actual coverage deterministic.

Any recovery-ledger entry forces overall partial status. Uncatchable errors remain in the term's terminal error set. Recovery does not rewrite static source semantics or coverage history.

### 8.4 Correlation and scopes

`correlate_many` requires source `Many<A,Q>`, `innerKey:A->K`, `outerKey:B->K`, one codec accepting K, and exactly one ancestor fanout binder B. Its source MUST be a `ref_many` to an outer-scope immutable binding. Index lifetime is once per runtime instance of the scope that declares that source binding, identified by `(sourceBindingPath,declaringScopeLogicalIndices,correlateNodePath,codec,innerKey)`, never once per consuming outer item. The index is prepared as a dependency before fanout bodies that use it; every inner-key/missing/codec failure during index construction is promoted to one uncatchable `core.correlation_index` at synthetic node path `<correlateNodePath>/index` with declaring-scope indices; declared catchable codec errors are replaced by that descriptor. It occurs before fanout admission and is not duplicated per outer item. Each consuming outerKey is evaluated once; any expression/missing/codec failure is promoted to uncatchable `core.correlation_key` at the correlate node and fails that logical body, bypassing recovery/fanout mode. Successful lookup emits exactly the stable source subsequence whose keys are codec-equal, preserving source order and completion Q. Missing/codec/key-evaluation behavior follows the codec manifest and adds declared errors. It never evaluates an effectful source repeatedly.

Scope type is its cardinality-specific return type. Scope effects/errors are unions of every eager binding plus return. A Many return's Completion comes only from the returned term. An unreturned eager Many binding that is Incomplete does not rewrite that return completion; it adds an incomplete PartialCause for the binding and forces overall partial status. Runtime has at most one semantic terminal Error, selected by §5.1 reference order. Recovery/fanout handles that one error if allowed; otherwise it becomes ExecutionError.error.

## 9. Runtime errors, outcomes, and evaluation order

### 9.1 Error classes

```text
ErrorClass = core.missing_member | core.missing_required | core.expression
           | core.codec | core.aggregate | core.correlation_key | core.correlation_index | core.partial_input
           | core.semantic_budget | core.cancellation | core.integrity | core.policy
           | plan:<name> | registry:<name> | operation:<name> | profile:<name>
```

```text
ErrorDescriptorRef = {origin:"core"|"plan"|"registry"|"operation"|"profile", code:String}
CoreErrorDescriptor = {code:ErrorClass, class:ErrorClass,
  phase:String, catchable:Boolean, retryable:false}
```

Core descriptors use code equal to the listed core ErrorClass, phase `evaluation` except partial_input=`reduction`, semantic_budget=`budget`, cancellation=`cancellation`, integrity=`integrity`, and policy=`policy`; all are retryable false and use the code as the fixed default message. `missing_member`, `missing_required`, `expression`, `codec`, and `aggregate` are catchable. `correlation_key`, `correlation_index`, `partial_input`, `semantic_budget`, `cancellation`, `integrity`, and `policy` are uncatchable. Registry/operation/profile descriptors require unique `(origin,code)` within their exact version. Descriptor admission requires `catchable:false` for every class classified core-uncatchable; every other descriptor's catchability field is authoritative, and `catchable:false` places it in Uncatchable. No descriptor belongs to both sets. The static τ set contains descriptor refs, never lossy classes.

Child evaluation is fail-fast: evaluate in the specified deterministic order, stop at the first terminal child error, and do not evaluate later children. For calls, all arguments evaluate in canonical MemberPath order before any options; options then evaluate in UTF-8 name order. The resulting first error is the term error and later potential errors do not occur.

Descriptors declare stable class, catchability, retryability, phase, and provider-code mapping. Pure-expression judgment includes errors. Function arguments evaluate array order; list items array order; object fields UTF-8 byte lexical key order; call arguments sort by canonical MemberPath; options sort by UTF-8 byte lexical name. Defaults are lazy. Collect-mode `correlationKey` evaluates before the body; its error is uncatchable and no body is admitted for that outer item. Runtime Error.code is `<origin>:<code>` for the resolved descriptor ref. Error.class/phase/retryable/message come from that descriptor; provider text is retained only in cause/details. Error.operation is the resolved immutable OperationId for operation-origin descriptors and is absent otherwise. ErrorTemplate is data and cannot itself fail. In `require_present` it replaces the generic missing-member error with a catchable plan-origin descriptor `{code:<authored>,class:core.missing_member}`; in `require_optional` it similarly uses class `core.missing_required`. Duplicate plan-origin codes must have identical message/class. A catchable evaluator error from collect-mode correlationKey is promoted to uncatchable `core.correlation_key` with the original descriptor as cause; it bypasses fanout mode and every enclosing recovery. An already-uncatchable error such as `core.semantic_budget`, cancellation, or integrity propagates unchanged and is never relabeled.

### 9.2 Maybe, correlation, and outcome values

MaybeValue, CorrelationValue, OutcomeValue, and their recursive typed payload relation are defined once in §13.1. Non-fanout attempts use `uncorrelated`; fanout collect uses `outer_correlation`. `attempt_optional` represents successful absence with `maybe_absent`, never null. `attempt_many` wraps each successful item and a catchable terminal error in logical order; runtime Incomplete is represented only by the Many completion and is not converted to OutcomeError.

### 9.3 Partial streams

A provider/page failure after values were emitted is Incomplete, not a catchable item error. A catchable per-element expression/key/codec error in map_many, filter_many, or dedup_many at logical index i first raises an internal `ElementFailure(prefix,index,error)` signal before completion is fixed. “Directly encloses” means the recovery node's `source` field is the failing map/filter/dedup AST node itself. Every intervening ScopeMany, ref_many, map/filter/dedup, fanout, or other ManyTerm is a boundary that converts ElementFailure to Incomplete before an outer recovery sees it. The innermost immediate recovery boundary has priority. If `attempt_many` directly encloses that operator, it catches the signal, wraps prior outputs as OutcomeOk, appends one OutcomeError, and stops later items. If `recover_empty` directly encloses it and the prefix is empty, it catches the signal and returns empty Complete with recovered_error; with a nonempty prefix it cannot catch. At every other boundary (including nonempty recover_empty) the signal becomes Incomplete with `element_error`; later items are not evaluated. Provider/runtime Incomplete never raises ElementFailure and cannot be caught by attempt_many. A call failure before any Many value is a terminal operation error and may be handled by recover/attempt/fanout. Cursor cycles, host timeout, cancellation, semantic-budget denial, and governor exhaustion are uncatchable and create Incomplete or terminal errors as specified by the host report. For `element_error` and `transport`, code is the namespaced runtime Error.code (`<origin>:<descriptor code>`). nodePath is the operator/call where Incomplete is first created; propagation preserves both unchanged and only unions new reasons.

---

## 10. Aggregator and monoid algebra

```text
Aggregator<T,P*,A,U(Q)> = {
  prepare:(P1,...,Pn)->A,
  identity:A,
  combine:A×A->A,
  present:(A,Q)->U(Q)
}
```

At fold validation, type variables are scoped to one constructor application. Unification proceeds in manifest parameter order: source item against `input`, each expression against its expression parameter, then accumulator/output instantiation; arity must match exactly and no variable may remain unbound. Constructor/evaluator/codec errors use their declared descriptors.

Expression parameters are evaluated per source item under the fold binder. `combine` is associative and identity-lawful. Reordering requires declared/tested commutativity; otherwise canonical logical partition order applies. Product aggregates combine componentwise.

Reference aggregation is a strict left fold. Initialize one accumulator with identity; for each source item in logical order evaluate expression parameters, invoke prepare exactly once, then invoke `combine(acc,prepared)` exactly once (including the first item); after completion invoke present exactly once. Product aggregate components execute in UTF-8 field-name order with one independent accumulator each. group_fold performs this same schedule per group in first-key order. Optimizers may use another tree only when values, errors, and the fixed logical step accounting remain identical.

Every fold result is `Covered<U,Qactual>`, including scalar outputs, so source coverage cannot disappear. A list-producing aggregator is explicit fold behavior, not implicit flattening; its U is an ordinary `List<V,Exact>` constructed by the aggregator, while the enclosing Covered carries source Qactual. `materialize` differs: it preserves original values/order and Q on the list itself as the designated flow/list bridge; aggregators may transform/select values according to their descriptor.

Registry admission MUST verify a fixed conformance corpus plus law obligations. Property-based testing is recommended but does not replace semantic obligations.

---

## 11. Validation, lowering, effects, and deterministic admission

### 11.1 Lowering contract

Validation produces a `ValidatedPlan` whose every reference, ShapeRef, overload, operation, cardinality, codec, aggregator, effect, error, pagination, correlation, split rule, and authority is resolved to a typed immutable descriptor. Lowering is a pure phase and invokes no tool.

```text
validate : UnvalidatedPlanIR × Environment -> ValidationOutcome
lower    : ValidatedPlan -> Lowered{plan:LoweredPlan, report:LoweringReport}
preflight: LoweredPlan × InputSnapshot × ContextSnapshot -> PreflightOutcome
execute  : ReadyPlan -> ExecutionResult

ValidationOutcome = Invalid{report:ValidationReport}
                  | Valid{report:ValidationReport, plan:ValidatedPlan}
ReadyPlan = LoweredPlan + frozen normalized input/context snapshots
```

`LoweredPlan` is a procedural logical-task graph derived from the data-oriented IR:

- binding references become dependency edges;
- One/Optional/Many operators become typed logical dataflow nodes;
- call terms become logical invocation templates with normalized one-valued arguments;
- group_map/flat_map become runtime fanout templates with explicit grouping/flattening and error disposition;
- fold/group_fold become streaming accumulator nodes;
- pagination, catalog splitting, and mapped correlation become physical-request templates;
- semantic budget coordinates, effect/commutation checks, and accounting node IDs are fixed.

A lowerer MAY normalize/fuse pure operators, push mapped filters/projections, batch correlation, split requests, or schedule concurrency only when it proves observational equivalence to the reference interpreter in validated output type, values, order, actual coverage, completion, handled/terminal errors, semantic-budget survivors, admitted effects, and logical accounting. Lowering never invents authority and never changes authored error/cardinality operators. ValidationReport exposes resolved types, dependencies, effects, and capabilities. LoweringReport exposes logical/physical classifications, fusions, pushdowns, batching, splitting, and request templates. A host's single static-review response MAY merge both reports but never returns a ValidatedPlan/LoweredPlan as author-editable JSON.

### 11.2 Reference logical execution

The normative **reference interpreter** determines admission and failure semantics independently of scheduling. In each scope it repeatedly selects the UTF-8-lexically smallest ready binding path, evaluates that binding to logical completion (including deterministic fanout/body admission) under semantic budgets, then selects the next; return evaluates after all eager bindings. A fanout source is enumerated to Complete or Incomplete before body admission. If Incomplete, retained prefix items are eligible for bodies in outer-index order and the fanout output propagates source reasons; no body exists beyond the retained prefix. Group-map admits every eligible prefix body. Flat-map admits eligible bodies only through the first Incomplete body (inclusive), then stops later retained-prefix bodies and therefore emits a true outer/body-order prefix. A failure stops new later admissions but every already admitted effect runs to a terminal attempt and is accounted; processors do not internally cancel admitted independent effects. External host cancellation remains an explicitly nondeterministic governor outcome.

Implementations MAY overlap work only when they prove observational equivalence to this reference interpreter in values, output order, actual coverage, handled/terminal errors, semantic-budget survivors, admitted/performed effects, and accounting. This permits parallel reads and proven commuting effects when failures cannot change the reference admitted set.

TOWL adds no hidden effect-order edge. Independent effectful bindings are valid only when the catalog's versioned argument-key commutation relation proves every possible logical invocation pair commutes; otherwise validation rejects the plan. A host profile MAY impose additional barriers on an already valid plan for safety, but MUST NOT use a barrier to make an invalid unordered noncommuting plan valid or change TOWL dependency semantics.


Every valid core plan has a total baseline lowering that mirrors the reference interpreter without optional optimizations. Failure to produce that baseline is an implementation integrity failure, not a plan-level LoweringOutcome. Optional optimizations may be declined and reported residual; they never make lowering fail.
Logical invocation identity is the call AST node's canonical RFC 6901 nodePath plus the full enclosing fanout/body logical index vector. Effect/error primary order uses canonical nodePath then logical indices, never JSON map order or completion time. The validator compares every unordered static or symbolic dynamic invocation pair. If argument-dependent commutation cannot be proved for all possible normalized keys, the pair is noncommuting and the plan is invalid. Fanout multiplicity never weakens this requirement.

The invocation-level happens-before relation is the transitive closure of binding dependency edges, every eager scope binding before that scope's return, lexical containment that requires a source before its operator/body, fanout source completion before body calls, and page/chunk attempts within one logical call. Lexical binding-name order and distinct outer indices do not themselves create happens-before. Two possible logical invocations are **unordered** exactly when neither happens-before the other; validation requires catalog commutation proof for every such effectful pair. The reference interpreter's lexical sequence is a deterministic baseline, not an authored dependency edge. For an unordered pair, relation `always` proves commutation and `never` rejects. Catalog admission canonicalizes every CrossCommutationRule pair as `(minClass,maxClass)` under UTF-8 order and rejects duplicate/conflicting pairs. `different_keys` is admissible only when both operation manifests name the same commutationCodec. It proves commutation only when both key projections reduce during validation to Complete, One-valued typed literals and that codec proves them unequal; dynamic/Optional/Many/unknown projections or codec mismatch are rejected. No implementation may add a stronger proof rule under v2.

### 11.3 Semantic budgets and host governors

Authored semantic budgets use the exact event sequence produced by the causality-respecting reference interpreter above—not an independent absolute path sort. Coordinates identify events and break ties only within the current reference step; dependency readiness and completed earlier lexical bindings determine sequence. The processor reserves budgets in that sequence before any optimized concurrent dispatch. `maxItemsPerBinding` counts value and outcome events accepted after operator semantics; reaching the limit retains the admitted prefix and marks completion Incomplete. `maxExpressionSteps` uses logical costs independent of implementation internals: one per expression AST-node evaluation; one per list_map/filter/dedup element; one prepare plus one combine per aggregate component/source item and one present per component; one key-codec step per dedup/group-fold/correlation inner key and per correlation outer lookup; and one cursor-codec step per received page cursor. Internal canonicalize/hash/equality calls, hash collisions, data structures, physical combine trees, and validation-time commutation proofs do not add semantic steps. Product components count separately in UTF-8 field order. `maxResultBytes` is checked after constructing the result envelope but before publication; excess returns an error result with no value and a deterministic size diagnostic—already performed effects remain accounted. Exhaustion never selects survivors by completion order.

Every named binding instance and each scope return has a coordinate `(nodePath,outerIndices)`. `maxItemsPerBinding` applies independently to each such coordinate and denies event N+1. `maxLogicalCalls` denies logical call N+1 in reference-interpreter sequence. Every expression visit has coordinate `(nodePath,outerIndices,sourceElementIndices,localVisitOrdinal)`, where local ordinal is the syntax-directed child order of §7; `maxExpressionSteps` denies visit N+1 in the same reference sequence. Budget denial belongs to the innermost flow node whose event/call/expression was denied. If that node is One or Optional, denial is terminal `core.semantic_budget` even when an enclosing fanout previously emitted outer values; because semantic_budget is uncatchable, the enclosing computation errors. If that node is Many and has emitted its own prefix, denial marks that Many node Incomplete; before its first event it is terminal. Enclosing Many operators propagate that exact completion through their stated rules. Dependents observe the resulting error/completion through ordinary rules.

For `maxResultBytes`, the processor constructs the closed SemanticResultProjection of §13.6 from the candidate result (Many completion is inside ManyValue), orders its arrays canonically, and measures its RFC 8785 UTF-8 bytes. Messages, diagnostic details, resolvedContext, accounting, physical attempts, extensions, and provider causes are excluded because they are host/implementation-variable. If over limit, the processor discards the candidate value and emits a fixed ExecutionError whose Error has code `core:core.semantic_budget`, class `core.semantic_budget`, nodePath `/$result`, logicalIndices `[]`, phase `budget`, retryable false, message `maxResultBytes exceeded`, and cause/details `{dimension:"max_result_bytes",max:<limit>,actual:<measured>}`, plus accounting of performed effects. The fixed error envelope is exempt from the same limit, preventing recursion.

Physical requests, retries, concurrency, decoded bytes, and wall time are host governors because optimization and environment affect them. Host governor exhaustion is explicitly nonportable execution outcome, reported as Incomplete/error; scheduling-independence claims exclude wall-clock timeout, external cancellation, and changing provider outcomes.

Authored and runtime JSON is restricted to the RFC 8785/I-JSON interoperable domain: finite IEEE-754 numbers, valid Unicode scalar strings (no lone surrogates), and integers exactly representable in that domain. Processors reject other values at intake. This makes result-byte accounting portable.

Retries are physical attempts of one logical invocation and require catalog retry safety/idempotency. External cancellation is best effort and reports every known logical invocation as not_started, in_flight, succeeded, failed, or unknown in InvocationAccounting; it is outside scheduling-independence guarantees but never hidden.

---

## 12. Pagination, completeness, and determinism

Paged calls require explicit `all` or `bounded`. Nonpaged calls forbid pagination. Page order and item order are catalog semantics. Page size does not bound logical results.

A bounded call evaluates `maxItems` before accepting item N+1 and `maxPages` before requesting page P+1. An actual authored coverage reason is recorded when a known next provider cursor, an unprocessed split chunk, or an unaccepted item proves the bound prevented examination of part of the logical domain; natural completion exactly at a limit records no reason. If both bounds independently prevent the next request/item, both canonically ordered reasons are recorded. Every authored_bound uses the enclosing call node path and canonical dimension `max_items` or `max_pages`. Page-size hints are validated against declared bounds; an out-of-range hint is invalid and is never clamped. Valid hints never affect logical coverage. Cursor equality uses the declared codec; a repeated nonterminal cursor produces `cursor_cycle` Incomplete. A paging codec canonicalize/equality failure before any item is emitted is the codec's terminal ErrorDescriptorRef and may be recovered according to its catchability; after emission it stops paging and produces Incomplete `cursor_error` with that namespaced Error.code. During validation-time `different_keys` proof, literal codec failure is a validation diagnostic and the plan is rejected, never a runtime error.

Provider/page interruption, cursor cycle, host budget exhaustion, timeout, or cancellation changes runtime completion to Incomplete and retains a valid prefix. Reductions reject that prefix.

Given identical input/context/catalog/registry snapshots and identical logical operation outcomes, values, ordering, errors, coverage, effects, and semantic-budget admission are scheduling-independent. External provider changes and host timing are not made deterministic by TOWL and are included in diagnostics/accounting.

---

## 13. Abstract validation/execution results and reference encoding

Validation and execution return typed abstract result records. The closed records below define their semantic fields; the shown object forms are the reference JSON encoding. A direct API may return equivalent typed objects. Runtime values under `TypedRuntimeValue` are validated against the accompanying TypeDescriptor; its JSON `value` is a named runtime payload island, not authored IR syntax. `details`, provider `cause`, `resolvedContext` values, and namespaced `extensions` are the only other result-side arbitrary-JSON islands.

### 13.1 Closed type, Maybe, coverage, and outcome descriptors

```text
TypeDescriptor =
  {"kind":"scalar_type","shape":ShapeId}
| {"kind":"list_type","element":TypeDescriptor,"coverageEffect":CoverageValue}
| {"kind":"map_type","value":TypeDescriptor}
| {"kind":"union_type","discriminator":MemberName,"cases":Map<String,TypeDescriptor>}
| {"kind":"record_type","shape":ShapeId?,"fields":Map<String,RecordFieldDescriptor>}
| {"kind":"maybe_type","value":TypeDescriptor}
| {"kind":"outcome_type","value":TypeDescriptor}
| {"kind":"covered_type","value":TypeDescriptor,"coverageEffect":CoverageValue}
RecordFieldDescriptor = {"kind":"required_field","value":TypeDescriptor}
                      | {"kind":"optional_field","value":TypeDescriptor}
FlowTypeDescriptor = {"kind":"one_type","value":TypeDescriptor}
                   | {"kind":"optional_type","value":TypeDescriptor}
                   | {"kind":"many_type","item":TypeDescriptor,
                      "coverageEffect":CoverageValue}

TypedRuntimeValue = {"kind":"typed_value","type":TypeDescriptor,"value":RuntimePayload}
RuntimePayload(TypeDescriptor) =
  scalar_type  -> JSON validated by its ShapeId
  list_type    -> ListValue
  map_type     -> Map<String,TypedRuntimeValue>, every child type exactly equals value
  union_type   -> UnionValue
  record_type  -> Map<String,TypedRuntimeValue|MaybeValue>, exactly declared fields
  maybe_type   -> MaybeValue
  outcome_type -> OutcomeValue
  covered_type -> CoveredValue

ListValue = {"kind":"list_value","coverage":CoverageValue,
             "items":[TypedRuntimeValue*]}
UnionValue = {"kind":"union_value","case":String,"value":TypedRuntimeValue}

MaybeValue = {"kind":"maybe_absent"}
           | {"kind":"maybe_present","value":TypedRuntimeValue}
CoveredValue = {"kind":"covered_value","coverage":CoverageValue,
                "value":TypedRuntimeValue}

CoverageValue = {
  "kind":"coverage","authored":[CoverageReason*],"policy":[CoverageReason*]
}
CompletionValue = {"kind":"complete","coverage":CoverageValue}
                | {"kind":"incomplete","coverage":CoverageValue,
                   "reasons":[UnintendedReason+]}

CorrelationValue = {"kind":"uncorrelated"}
                 | {"kind":"outer_correlation","outerIndex":NonNegativeInteger,
                    "key":TypedRuntimeValue?}
OutcomeValue = {"kind":"outcome_ok","correlation":CorrelationValue,
                "value":TypedRuntimeValue}
             | {"kind":"outcome_error","correlation":CorrelationValue,
                "handledErrorId":String}
```

For a record payload, required fields contain TypedRuntimeValue whose type equals the field descriptor; optional fields are always present and contain MaybeValue whose present value type equals the optional descriptor. List/map child types exactly equal their descriptors. UnionValue.case must be one declared case and its value type exactly equals that case descriptor; the provider discriminator is normalized to/from `case` by the ShapeSystem. ListValue coverage is actual and MUST be a componentwise subset of list_type coverageEffect. A CoveredValue coverage is actual and MUST be a componentwise subset of covered_type coverageEffect. An Outcome<Maybe<T>> is one TypedRuntimeValue with maybe_type whose payload is MaybeValue—bare MaybeValue is never an Outcome payload. Provider-native omissions normalize at decode. Every RuntimePayload branch is selected solely by TypeDescriptor; no alternative representation is accepted.

### 13.2 Diagnostics, errors, summaries, and extensions

```text
Diagnostic = {
  "kind":"diagnostic","code":String,"severity":"warning"|"error",
  "path":String,"message":String,"details":JSON?
}
Error = {
  "kind":"error","code":String,"class":ErrorClass,"message":String,
  "nodePath":String,"logicalIndices":[NonNegativeInteger*],
  "correlation":CorrelationValue?,"operation":OperationId?,"phase":String,
  "retryable":Boolean,"cause":JSON?
}

ResolvedOperationSummary = {
  "kind":"operation_summary","nodePath":String,"operationId":String,
  "cardinality":"one"|"optional"|"many","effects":[EffectSummary*],
  "paging":"not_paged"|"all"|"bounded"
}
BindingSummary = {
  "kind":"binding_summary","name":Name,"nodePath":String,
  "type":FlowTypeDescriptor,"effects":[EffectSummary*]
}
EffectSummary = {"kind":"effect_summary","class":String,
  "authorityClass":String,"resourceKey":"static"|"dynamic"}
WaveSummary = {"kind":"wave","scopePath":String,"index":NonNegativeInteger,
  "bindings":[String*]}
FanoutSummary = {"kind":"fanout_summary","nodePath":String,
  "mode":"group"|"flat","errors":"fail"|"skip"|"collect",
  "lower":BoundValue,"upper":BoundValue}
PolicySummary = {"kind":"policy_summary","allowed":Boolean,
  "reasons":[String*],"extensions":Map<Namespace,JSON>}
LogicalBounds = {"kind":"logical_bounds","lower":BoundValue,
  "upper":BoundValue,"extensions":Map<Namespace,JSON>}
LoweredNodeSummary = {"kind":"lowered_node","nodePath":String,"operator":String,
  "dependencies":[String*],"effects":[EffectSummary*]}
PhysicalTemplateSummary = {"kind":"physical_template","nodePath":String,
  "operationId":OperationId,"strategy":"direct"|"paged"|"split"|"correlated_batch"}
OptimizationSummary = {"kind":"optimization","nodePath":String,
  "class":"fused"|"pushed_filter"|"pushed_projection"|"batched"|"residual",
  "message":String}
LoweringReport = {"kind":"lowering_report","logicalNodes":[LoweredNodeSummary*],
  "physicalTemplates":[PhysicalTemplateSummary*],"optimizations":[OptimizationSummary*],
  "diagnostics":[Diagnostic*],"extensions":Map<Namespace,JSON>}
```

Array order is canonical node-path order. Extension keys are reverse-DNS namespaces; core processors preserve but do not interpret unknown extension payloads.

### 13.3 Validation report

```text
ValidationReport = {
  "kind":"validation_report","valid":Boolean,
  "errors":[Diagnostic*],"diagnostics":[Diagnostic*],
  "registryIdentity":RegistryIdentity?,"catalogIdentity":CatalogIdentity?,
  "profileIdentity":ProfileIdentity?,"contextIdentity":ContextIdentity?,
  "operations":[ResolvedOperationSummary*],"bindings":[BindingSummary*],
  "dependencyWaves":[WaveSummary*],"fanouts":[FanoutSummary*],
  "bounds":LogicalBounds?,"policy":PolicySummary?,
  "extensions":Map<Namespace,JSON>
}
PolicyRejection = {"kind":"policy_rejection","report":ValidationReport,
  "diagnostics":[Diagnostic+]}
```

`valid` is true iff `errors` is empty. `errors` contains every severity=error Diagnostic; `diagnostics` contains only warnings and never duplicates an error. Invalid reports retain every safely resolved field. Validation invokes no operation. Host execution policy evaluates the valid report/lowering report before preflight. If PolicySummary.allowed is false, lowering MAY still be reported but preflight/execute are not called; the host returns PolicyRejection with zero admitted invocations/effects. Policy rejection is not ExecutionError because execution never began. Operations, bindings, fanouts, effects, and diagnostics sort by canonical node path then logical indices. In each scope DAG, dependency-free bindings are wave 0 and every other binding is one plus its maximum dependency wave; WaveSummary includes scope path and UTF-8-sorted bindings. Fanout lower/upper derive from source bounds and body cardinality, with unknown widths `symbolic`. Bound arithmetic uses mathematical nonnegative integers; values above 2^53−1 encode as big_bound decimal rather than rejecting or saturating. Any operation involving symbolic yields symbolic. LogicalBounds sums/products use these rules. EffectSummary projects each EffectDescriptor to class, authority class, and whether its resource-key expression is statically known; duplicates are removed and UTF-8 sorted. Bound transfer ignores possible runtime errors but is otherwise syntax-directed: One=1..1; Optional=0..1; call_many uses catalog resultBounds; ref/map preserve; filter/dedup=0..source.upper; singleton=1..1; present_optional=0..1; elements uses a statically literal list length else 0..symbolic; materialize/fold/group_fold=1..1; group_map uses source bounds; flat_map multiplies source/body lower and upper; recover/attempt preserve the corresponding source upper with lower 0 when recovery may omit; scope uses return bounds. Bound sums/products use arbitrary precision then BoundValue encoding, and symbolic propagates. EffectSummary.resourceKey is `static` only when the projection's entire argument expression is recursively TypedLiteral/object/list of typed literals; refs, inputs, env, calls, and binders are dynamic (no cross-binding constant propagation).

### 13.4 Cardinality envelopes

```text
OneValue = {"kind":"one_value","value":TypedRuntimeValue}
OptionalValue = {"kind":"optional_absent"}
              | {"kind":"optional_present","value":TypedRuntimeValue}
ManyValue = {"kind":"many_value","items":[TypedRuntimeValue*],
             "completion":CompletionValue}
```

### 13.5 Accounting and truncation

```text
NodeAccounting = {"kind":"node_accounting","nodePath":String,
  "logicalIndices":[NonNegativeInteger*],
  "produced":NonNegativeInteger,"filtered":NonNegativeInteger,
  "deduplicated":NonNegativeInteger,"fanoutStarted":NonNegativeInteger,
  "succeeded":NonNegativeInteger,"failed":NonNegativeInteger,"skipped":NonNegativeInteger,
  "incomplete":NonNegativeInteger}
InvocationAccounting = {"kind":"invocation_accounting","logicalId":String,
  "nodePath":String,"logicalIndices":[NonNegativeInteger*],"operationId":OperationId,
  "effects":[EffectSummary*],"state":"not_started"|"in_flight"|"succeeded"|"partial"|"failed"|"unknown",
  "physicalAttempts":NonNegativeInteger}
HandledError = {"kind":"handled_error","id":String,"nodePath":String,
  "logicalIndices":[NonNegativeInteger*],"correlation":CorrelationValue?,
  "disposition":"skip"|"collect"|"recover_optional"|"recover_optional_failure"|"recover_empty"|"attempt",
  "error":Error}
PartialCause = {"kind":"handled_error_cause","handledErrorId":String}
             | {"kind":"incomplete_cause","reason":UnintendedReason}
             | {"kind":"truncation_cause","nodePath":String,"class":String}
Accounting = {"kind":"accounting","logicalCalls":NonNegativeInteger,
  "nodes":[NodeAccounting*],"invocations":[InvocationAccounting*],
  "handledErrors":[HandledError*],"extensions":Map<Namespace,JSON>}
Truncation = {"kind":"truncation","nodePath":String,"class":String,
  "message":String,"details":JSON?}
```

Accounting has one NodeAccounting row for every executed flow-operator AST node and scope return instance `(nodePath,logicalIndices)`, ordered canonically. `produced` is that node's output value/outcome event count; `filtered` is predicate-false input count only on filter_many; `deduplicated` is removed equal-key input count only on dedup_many; `fanoutStarted` is admitted outer bodies. `succeeded` counts bodies reaching Complete success; `failed` counts terminal body failures; `skipped` is a subset of failed omitted by skip mode (so both increment); collect failures increment failed but not skipped; `incomplete` counts bodies ending Incomplete and increments neither succeeded nor failed. Irrelevant counters are zero. Rows are never parent-summed. LogicalInvocationId serializes as RFC 6901 nodePath + `@` + comma-separated base-10 logical indices (empty suffix for none). HandledError IDs append `#` plus zero-based ordinal among handled errors at that exact coordinate. InvocationAccounting has one row for every statically/dynamically known logical call coordinate: admitted complete calls are succeeded, calls yielding Incomplete Many are partial, terminal calls are failed, externally cancelled in-flight calls may be unknown, and semantic-budget-denied calls are not_started with zero physicalAttempts. Accounting.logicalCalls counts admitted calls only. Rows sort by logicalId.

ContextResultValue = {"kind":"context_value","type":FlowTypeDescriptor,
  "value":OneValue|OptionalValue}
SemanticErrorRef = {"kind":"semantic_error_ref","code":String,"nodePath":String,
  "logicalIndices":[NonNegativeInteger*]}
SemanticResultProjection = SemanticProjectionOk | SemanticProjectionPartial | SemanticProjectionError
SemanticProjectionOk = {"kind":"semantic_result_projection","status":"ok",
  "type":FlowTypeDescriptor,"value":OneValue|OptionalValue|ManyValue,
  "errors":[],"partialCauses":[]}
SemanticProjectionPartial = {"kind":"semantic_result_projection","status":"partial",
  "type":FlowTypeDescriptor,"value":OneValue|OptionalValue|ManyValue,
  "errors":[SemanticErrorRef*],"partialCauses":[PartialCause+]}
SemanticProjectionError = {"kind":"semantic_result_projection","status":"error",
  "type":FlowTypeDescriptor,"error":SemanticErrorRef,"partialCauses":[]}

### 13.6 Execution-result status union

```text
ExecutionResult = ExecutionOk | ExecutionPartial | ExecutionError
ExecutionOk = {
  "kind":"execution_result","status":"ok","type":FlowTypeDescriptor,
  "value":OneValue|OptionalValue|ManyValue,
  "errors":[],"truncations":[],"diagnostics":[Diagnostic*],
  "accounting":Accounting,"resolvedContext":Map<Name,ContextResultValue>,
  "catalogIdentity":CatalogIdentity,"registryIdentity":RegistryIdentity,
  "profileIdentity":ProfileIdentity,"contextIdentity":ContextIdentity,"extensions":Map<Namespace,JSON>
}
ExecutionPartial = {
  "kind":"execution_result","status":"partial","type":FlowTypeDescriptor,
  "value":OneValue|OptionalValue|ManyValue,
  "errors":[Error*],"truncations":[Truncation*],"diagnostics":[Diagnostic*],
  "partialCauses":[PartialCause+],"accounting":Accounting,"resolvedContext":Map<Name,ContextResultValue>,
  "catalogIdentity":CatalogIdentity,"registryIdentity":RegistryIdentity,
  "profileIdentity":ProfileIdentity,"contextIdentity":ContextIdentity,"extensions":Map<Namespace,JSON>
}
ExecutionError = {
  "kind":"execution_result","status":"error","type":FlowTypeDescriptor,
  "error":Error,"truncations":[Truncation*],"diagnostics":[Diagnostic*],
  "accounting":Accounting,"resolvedContext":Map<Name,ContextResultValue>,
  "catalogIdentity":CatalogIdentity,"registryIdentity":RegistryIdentity,
  "profileIdentity":ProfileIdentity,"contextIdentity":ContextIdentity,"extensions":Map<Namespace,JSON>
}
```

resolvedContext contains exactly the union of authored env references and descriptor-declared context dependencies that were actually selected by validation, ordered by UTF-8 name; no unrelated snapshot entry is emitted. ContextResultValue carries exact One/Optional flow type and matching envelope; Many context entries are forbidden.

Envelope consistency is exact: one_type requires OneValue whose inner type equals value; optional_type requires OptionalValue whose present inner type equals value; many_type requires ManyValue whose every item type equals item and whose actual completion coverage is a subset of coverageEffect. The execution flow descriptor equals the validated root flow type.

Handled errors appear exactly once as full Error payloads in `Accounting.handledErrors`; OutcomeError and handled-error PartialCause reference their stable handledErrorId. For every root-visible Incomplete reason created from an ErrorDescriptor (`transport`, `cursor_error`, or `element_error`), `ExecutionPartial.errors` MUST contain exactly one matching full Error with the same namespaced code, origin nodePath, and logical indices. Budget, cursor_cycle, timeout, cancellation, and other non-descriptor reasons add no Error unless their rule explicitly creates one. Propagation of one origin reason through multiple bindings preserves its identity and does not duplicate the Error. A semantic terminal unhandled Error instead produces ExecutionError.error. Every Incomplete reason and truncation appears once in partialCauses. No full Error payload is duplicated across handled and unhandled locations.

Status precedence is deterministic:

1. the reference interpreter's unhandled terminal Error -> error;
2. otherwise any Incomplete completion, handled-error entry, skipped/collected error, or host truncation -> partial; every such trigger appears exactly once in `partialCauses`;
3. otherwise -> ok.

Error results have no `value`. Partial results require a typed valid value/prefix. Bare result JSON is nonconforming. Diagnostics sort by `(nodePath,severity,code,occurrenceOrdinal)`; terminal Errors use the total key in §5.1; truncations and accounting use `(nodePath,logicalIndices,kind,ordinal)`. These keys break all same-coordinate ties and define handled-error ordinals.

---

## 14. Normative invariants and conformance tests

A conforming implementation enforces:

1. every IR sum value has exactly one known variant; the reference JSON encoding has its matching kind and no unknown members;
2. every adapter preserves the IR losslessly; arbitrary JSON occurs only in typed literal values and explicitly named diagnostic/provider/extension payload islands;
3. registry and catalog requirements resolve exactly and their manifests are frozen;
4. all ShapeRefs resolve and literals validate without coercion;
5. binding/scope/call cardinalities exactly match inferred/catalog cardinalities;
6. pure expressions are One-valued and reference only One names/items;
7. optional member absence remains Maybe until explicitly handled;
8. no implicit One/Optional/Many/List conversion exists;
9. map preserves cardinality and never flattens;
10. group-map body is One; flat-map body is Many and removes one layer;
11. materialize/elements are the explicit flow/list bridge and preserve coverage;
12. aggregate syntax appears only under fold;
13. reductions reject Incomplete input;
14. fanout/recovery error behavior is explicit and type-reflected;
15. paths are rooted member arrays; argument paths are closed entry arrays;
16. binders have exact lexical regions, no shadowing, and every free reference creates an edge;
17. maps have no execution order; cycles are invalid; entered bindings are eager;
18. pagination is explicit for pageable calls;
19. correlation, splitting, codecs, retries, and effects are capability-gated;
20. unordered noncommuting effects are invalid;
21. static coverage join is total and runtime Incomplete cannot be laundered;
22. semantic budget admission and diagnostic order are canonical;
23. active fanout depth is at most two;
24. execution consumes only a validated immutable plan and returns a typed envelope.

Required tests cover every wrong cardinality pair; every forbidden implicit conversion; typed literal ambiguity; Maybe handling; path rules; exact scope/cardinality schema branches; group/flat mode typing/order/partial-body behavior; recovery; coverage joins and anti-laundering; materialize/elements; aggregate placement/constructor typing/laws/list coverage; input/context rules; operation arguments and explicit pagination; correlation/indexing; catalog splitting; commutation/retry safety; deterministic scope failures/budget admission; result/status envelopes; registry/deprecation/key-codec version and conformance; optimized/canonical equivalence; strict JSON and map-order invariance.

### 14.1 Provider-neutral worked derivation (informative)

This informative derivation uses compact semantic notation rather than a serialized registry/catalog fixture. Conformance implementations supply their own closed manifests and fixtures under §6; this example demonstrates only authored v2 syntax and calculus composition.

```text
registry example-tools:2
  shapes:
    core.string = scalar string
    example.Location = closed record {code:core.string required, name:core.string required}
    example.Asset = closed record {id:core.string required, location:core.string required,
                                   state:core.string required, size:core.number required}
  context: empty
  function equals(core.string,core.string)->core.boolean, errors=[], pure
  key codecs: canonical_string(core.string)
  deprecations: empty

catalog example-catalog:1
  list_locations input=closed record {}, result=Many<example.Location>, NotPaged,
                 effects=[read], errors=[provider_error catchable], retry=idempotent
  list_assets input=closed record {location:core.string required}, result=Many<example.Asset>,
              Paged(items in logical order), effects=[read],
              errors=[provider_error catchable], retry=idempotent
  all unordered calls commute (read-only fixture)
```

Under those assumptions, the generated registry+catalog schema validates the example below.

```json
{
  "kind":"plan",
  "towl":"v2",
  "registry":{"kind":"registry_requirement","name":"example-tools","version":"2"},
  "catalog":{"kind":"catalog_requirement","name":"example-catalog","version":"1"},
  "profile":{"kind":"profile_requirement","name":"example-default","version":"1"},
  "context":{"kind":"context_requirement","name":"example-context","version":"1"},
  "description":"Active assets grouped by location",
  "body":{
    "kind":"scope_many",
    "let":{
      "locations":{
        "kind":"many",
        "value":{"kind":"call_many","operation":"list_locations","arguments":[],"options":[]}
      },
      "byLocation":{
        "kind":"many",
        "value":{
          "kind":"group_map",
          "source":{"kind":"ref_many","name":"locations"},
          "as":"location",
          "errors":"collect",
          "body":{
            "kind":"scope_one",
            "let":{
              "activeAssets":{
                "kind":"many",
                "value":{
                  "kind":"filter_many",
                  "source":{
                    "kind":"call_many",
                    "operation":"list_assets",
                    "arguments":[
                      {"kind":"argument","path":["location"],"value":{"kind":"ref_one","name":"location","path":["code"]}}
                    ],
                    "options":[],
                    "pagination":{"kind":"bounded","maxItems":1000}
                  },
                  "as":"asset",
                  "where":{
                    "kind":"apply","function":"equals","arguments":[
                      {"kind":"ref_one","name":"asset","path":["state"]},
                      {"kind":"literal","type":{"kind":"shape_ref","id":"core.string"},"value":"active"}
                    ]
                  }
                }
              },
              "assetList":{
                "kind":"one",
                "value":{"kind":"materialize","source":{"kind":"ref_many","name":"activeAssets"}}
              }
            },
            "return":{
              "kind":"object","fields":{
                "location":{"kind":"ref_one","name":"location","path":["code"]},
                "assets":{"kind":"ref_one","name":"assetList"}
              }
            }
          }
        }
      }
    },
    "return":{"kind":"ref_many","name":"byLocation"}
  }
}
```

Derivation:

```text
call_many list_locations                                  : Many<Location,Exact>
Q1000 = Coverage{authored:{max_items(nodePath=list_assets,limit=1000)},policy:∅}
call_many list_assets(location.code,bounded)              : Many<Asset,Q1000>
filter_many equals(asset.state,"active")                  : Many<Asset,Q1000>
materialize(activeAssets)                                 : One<List<Asset,Q1000>>
object{location,assets}                                   : One<Record>
group_map collect(locations, body One<Record>)            : Many<Outcome<Record,Error>,Exact>
```

Every transition is named; none is inferred from object placement or list shape.

Expected behavior under the illustrative fixture:

- validation root type is `Many<Outcome<Record{location:String,assets:List<Asset,Q1000>},Error>,Exact>` and fanout mode is collect;
- if all calls succeed and no bound stops data, ExecutionOk contains only outcome_ok items, each asset list has actual Exact coverage, handledErrors is empty, and status is ok;
- if the second location call fails before emission with catchable provider_error, ExecutionPartial preserves outer order with one outcome_error at outerIndex 1, contains exactly one HandledError plus OutcomeError/PartialCause references to its ID, has no duplicate top-level Error, and keeps root completion Complete Exact;
- if a location body becomes Incomplete after emission, group materialize fails with uncatchable partial_input; collect does not convert it to an outcome and the fanout fails according to its remaining terminal error rule.

### 14.2 V1 coexistence and migration requirements

The normative v1 specification remains archived as [`TOWL_V1_SPEC.md`](TOWL_V1_SPEC.md). V1 and v2 are different languages. A migration tool is a separate, versioned processor; a v2 parser never interprets a v1 document directly.

A migrator is selected by a closed manifest and returns a closed result:

```text
MigrationManifestResolver = {
  resolve(manifestId:String) -> {manifest:MigrationManifest,implementation:MigrationImplementation}|Unknown
}
MigrationManifest = {"kind":"migration_manifest","id":String,
  "source":{"towl":"v1","registry":{"name":String,"version":String},
            "catalog":{"name":String,"version":String},
            "profile":{"name":String,"version":String},
            "context":{"name":String,"version":String}},
  "target":{"towl":"v2","registry":{"name":String,"version":String},
            "catalog":{"name":String,"version":String},
            "profile":{"name":String,"version":String},
            "context":{"name":String,"version":String}},
  "implementationId":String}
MigrationDiagnostic = {"kind":"migration_diagnostic","code":MigrationCode,
  "severity":"warning"|"error"|"confirmation_required","sourcePath":String,
  "message":String,"details":JSON?}
MigrationRequest = {"kind":"migration_request","manifestId":String,
  "sourcePlan":JSON,"decisions":[MigrationDecision*]}
MigrationDecision = {"kind":"migration_decision","requestId":String,
  "sourcePath":String,"sourceValue":JSON,"choice":String}
ConfirmationPoint = {"kind":"confirmation_point","requestId":String,
  "sourcePath":String,"sourceValue":JSON,"code":MigrationCode,
  "alternatives":Map<String,String>}
MigrationResult = {"kind":"migration_success","plan":Plan,
                   "diagnostics":[MigrationDiagnostic*]}
                | {"kind":"migration_confirmation_required",
                   "confirmations":[ConfirmationPoint+],
                   "diagnostics":[MigrationDiagnostic+]}
                | {"kind":"migration_refused","diagnostics":[MigrationDiagnostic+]}
```

A confirmation-required result contains no plan and returns every outstanding point sorted by UTF-8 sourcePath, MigrationCode, then ordinal; alternative keys are UTF-8-lexically ordered. requestId is `manifestId#sourcePath#code#zeroBasedOrdinal`. A subsequent MigrationRequest repeats the same manifestId and sourcePlan after RFC 8785 canonicalization and supplies decisions echoing requestId, sourcePath, and exact sourceValue; any mismatch, unknown choice, or source change is refused. The subsequent request MUST supply exactly one decision for every confirmation returned by the immediately prior result, with no duplicates or omissions; otherwise it is refused. `migration_success` is invalid while any point remains unresolved. Migration success diagnostics may contain warnings only; confirmation-required diagnostics contain at least one confirmation_required and no error; refused diagnostics contain at least one error. No diagnostic is duplicated across severities. MigrationManifestResolver selects the manifest and immutable implementationId and runs its golden conformance corpus.

```text
MigrationCode = unavailable_environment | ambiguous_sink | cardinality_intent_required
              | literal_type_required | codec_required | recovery_boundary_unsupported
              | aggregate_constructor_unavailable | path_unsupported | semantic_difference
```

Matrix disposition keywords are normative: `refuse unavailable` -> migration_refused/error/unavailable_environment; `ambiguous sink` -> refused/error/ambiguous_sink; `manual choice` or `ALWAYS require confirmation` -> confirmation_required/cardinality_intent_required with alternatives named in the translation cell; `typed literal` ambiguity -> refused/error/literal_type_required; missing codec -> refused/error/codec_required; unsupported recovery -> refused/error/recovery_boundary_unsupported; unavailable aggregate constructor -> refused/error/aggregate_constructor_unavailable; unsupported path -> refused/error/path_unsupported; every `warn` -> migration_success/warning/semantic_difference. Conditions not matching an automatic row are refused with semantic_difference rather than guessed.

A migrator MUST parse and validate the v1 plan against the exact source registry/catalog/profile/context versions, infer every binding cardinality/shape, and emit a v2 plan only on `migration_success`; refusal emits no partial plan. The emitted plan is validated against exact target registry/catalog/profile/context versions. It applies this matrix:

| V1 construct                                               | V2 translation                                                                                                                                                                    | Required refusal/manual decision                                                                                                                                               |
|------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Plan/registry/catalog/profile                              | add `kind:plan`; exact registry, catalog, and profile requirements                                                                                                                | refuse unavailable environment component                                                                                                                                       |
| V1 aggregate output consumed by a downstream function/call | no automatic translation: v2 fold yields Covered                                                                                                                                  | migration_refused/error/semantic_difference; requires operation redesign                                                                                                       |
| Assemble `let/result`                                      | cardinality-specific scope and bindings                                                                                                                                           | when v1 omitted result, migrate only if validated v1 has exactly one unconsumed sink; otherwise `ambiguous_sink` refusal                                                       |
| Express One source + pure result                           | call/ref + `map_one` or direct expression                                                                                                                                         | none after exact type inference                                                                                                                                                |
| Express Optional source + pure result                      | call/ref + `map_optional`                                                                                                                                                         | automatic when fail; when v1 skip was used, confirmation `cardinality_intent_required` with choices `preserve_absence` or `recover_failure_to_absent`                          |
| V1 required/defaulted/optional input                       | required/defaulted/optional input with exact ShapeRef                                                                                                                             | refuse profile/unknown/document types that cannot normalize                                                                                                                    |
| V1 cardinality-polymorphic function                        | explicit map_one/map_optional/map_many around pure v2 function                                                                                                                    | refuse signatures with no exact v2 overload                                                                                                                                    |
| Express Many filter/dedup/pure result                      | `filter_many`, `dedup_many`, `map_many`                                                                                                                                           | require a key codec for dedup                                                                                                                                                  |
| V1 aggregate result                                        | explicit `fold` or `group_fold`                                                                                                                                                   | refuse mixed pure/aggregate leaves that cannot be represented without a covered result                                                                                         |
| V1 `forEach` body One                                      | `group_map`                                                                                                                                                                       | v1 explicit policy maps identically; omitted Traverse policy maps to `skip`                                                                                                    |
| V1 `forEach` body Optional                                 | `flat_map` whose scope_many applies `present_optional`                                                                                                                            | explicit policy maps identically; omitted Traverse policy maps to `skip`                                                                                                       |
| V1 `forEach` body Many                                     | choice `flatten` -> flat_map; choice `group_materialized` -> group_map after body materialize                                                                                     | confirmation_required/cardinality_intent_required; never automatic                                                                                                             |
| V1 collect of list-valued elements                         | choice `preserve_nested` -> list-producing fold; choice `expected_flat` -> refusal                                                                                                | ALWAYS confirmation_required/cardinality_intent_required; never infer author flatten intent                                                                                    |
| Implicit source/bare paths                                 | introduce explicit binders and rooted `ref_one` paths                                                                                                                             | refuse unresolved/optional path ambiguity                                                                                                                                      |
| Path `[]` list projection                                  | pure `list_map` with binder                                                                                                                                                       | refuse index/slice constructs unsupported in both versions                                                                                                                     |
| Raw literal                                                | typed literal using inferred exact shape                                                                                                                                          | refuse ambiguous/heterogeneous/opaque literals without a declared shape                                                                                                        |
| One<List> used as Many                                     | explicit `elements`                                                                                                                                                               | none when provenance is known                                                                                                                                                  |
| Many used as list argument                                 | if static coverage is Exact, named `materialize` then `ref_one`                                                                                                                   | refuse/redesign every non-Exact or unknown-coverage source because v2 call arguments prohibit it; runtime Incomplete still raises partial_input                                |
| Pagination omitted on pageable call                        | explicit `all`                                                                                                                                                                    | migration_success warning/semantic_difference: pagination made explicit                                                                                                        |
| V1 bounded pagination                                      | `bounded` with the same maxItems/maxPages and `pageSize = clamp(authoredPageSize, sourceCapability.minPageSize, sourceCapability.maxPageSize)` when authored; omit it when absent | automatic only after resolving the exact source catalog capability; record normalization in the report; refuse if the source capability or bounds are unavailable/inconsistent |
| V1 fail/skip/collect                                       | fanout mode or explicit recovery/attempt operator                                                                                                                                 | refuse when v1 policy could catch partial_input, semantic budget, cancellation, or another v2-uncatchable class                                                                |
| Recursive/higher-order aggregator                          | product/fold/group_fold or target constructor                                                                                                                                     | require target manifest to declare the same normalized prepare/monoid/present observation corpus; otherwise `aggregate_constructor_unavailable`                                |
| V1 output value                                            | v2 typed cardinality envelope                                                                                                                                                     | compare normalized values, ordering, errors, coverage, and status                                                                                                              |

Canonical v1-to-v2 observation mapping is:

- successful v1 One -> v2 one_value with the normalized target type;
- successful v1 Optional absent/present -> v2 optional_absent/optional_present;
- successful unbounded v1 Many with no truncation/error -> v2 many_value Complete Exact;
- v1 authored pagination bounds -> Complete coverage with corresponding authored_bound reasons when the bound actually truncated;
- v1 truncation/cancellation/page failure -> Incomplete with the corresponding unintended reason;
- v1 skipped/collected errors -> v2 handled-error/Outcome records and partial status;
- v1 terminal error -> v2 ExecutionError;
- values/ordering are compared after provider-shape normalization and this envelope transformation.

Golden migration tests MUST cover every row. For automatic rows, canonical v1 execution and migrated v2 execution must be observationally equivalent after the documented envelope transformation. Refusal rows MUST produce stable diagnostic codes and no partial output plan. Code Mode or another profile migrates only after its profile version, registry, help/schema, fixtures, and implementation pass these tests; rollback keeps the v1 processor and archived spec available.

---

## 15. Informative rationale, migration, and related languages

### 15.1 Why v2 is a new language version

V2 is not a reinterpretation of TOWL v1. V1 selected block role structurally, used implicit source elements, inferred mapping versus folding from result contents, and permitted valid-but-surprising nested collection shapes. V2 changes the root, expressions, calls, bindings, cardinality conversions, fanout, aggregation, errors, and result envelope. A v1 processor MUST reject `"towl":"v2"` before parsing nodes; a v2 processor MUST reject v1. Migration is performed only by a separate tool conforming to §14.2. Saved v1 documents never silently acquire v2 semantics.

### 15.2 Design rationale

- Cardinality tags are authored assertions checked against term and catalog types.
- Operator/function/aggregator names are values under literal kinds, never dynamic object keys.
- Typed literals eliminate timestamp/registry-scalar/list ambiguity.
- Maybe distinguishes data-member absence from flow Optional and JSON null.
- Group-map and flat-map have incompatible body types, making group versus flatten explicit.
- Materialize/elements are the sole generic stream/list bridge.
- Fold is the sole aggregate context.
- Coverage and runtime incompleteness are separate, preventing provenance laundering.
- Effect commutation and retry safety are catalog capabilities, not spelling guesses.
- Task-scoped schemas or surface syntaxes may help authors, but this calculus remains the reviewed execution IR.

### 15.3 Related languages

- Common Workflow Language provides typed DAG/scatter precedent but targets command/file/container workflows.
- OpenAPI Arazzo composes API operations but lacks this explicit flow/cardinality algebra.
- Amazon States Language provides state transitions and mature error handling; TOWL is dataflow rather than a state machine.
- BPMN covers business processes, events, and human work; TOWL is a bounded operation IR.
- Relational/query IRs motivate explicit map/filter/flat-map/fold and pushdown, but not provider effects and correlated item failures.

### 15.4 Informative AWS profile notes

An AWS profile may require service qualification, reserve a lowercase endpoint-region argument, generate exact call/cardinality branches, provide shape IDs and key codecs, classify effects/retry safety/commutation, and expose correlation/splitting metadata. Those are profile contracts, not TOWL core syntax.

See [`CODE_MODE-SPEC.md`](CODE_MODE-SPEC.md) for the AWS profile. Code Mode must migrate atomically to TOWL v2 before claiming this language dependency.

---

End of TOWL v2 draft.
