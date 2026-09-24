# TOWL — Tool Orchestration Workflow Language Specification

Status: draft / request for comment  
Language version: `3`

TOWL is a small, total, statically typed expression language for describing a bounded workflow of external tool calls and pure data transformations. An AI agent (or a person) writes a TOWL program from knowledge of an external service's schema; a deterministic processor validates it, shows a typed review, executes it without a model in the loop, and returns either a complete result or a failure report precise enough for the next attempt to be right.

---

## 1. Design

Eleven commitments shape everything below. The first nine are about what a program can mean; the last two are about who has to read and write it.

1. **Effects only at operation calls.** The built-in `call("namespace", "operation", args?, options?)` — naming a catalog namespace such as an AWS service or an MCP server and one of its operations, both as string literals — is the only expression with an effect. Everything else is pure and total. The set of effects in a program is a lexical scan for `call(`.
2. **One binder.** `for x in list` followed by a body is the only construct that binds a name for a computation; it is also the only fan-out. It is not a loop and not a lambda: bodies are independent, there are no function values, and `x` is an alias for the element inside the body. Calls and `for` never appear inside the pure layer — paths, predicates, and call arguments.
3. **Path-based pure layer.** Element-wise transforms take *paths from the implicit element* and closed predicate expressions, never functions. Aggregation is a fixed set of monoids.
4. **No silent partial values.** A pageable call returns its complete result; the processor paginates. A run-wide limit or an error that is not absorbed (item 5) stops the program, and a failed run returns no result, only a report of what completed. The only way an element leaves a result without an authored filter is a loss — an absent value, a read that could not be done, or a read over a per-call limit (item 5, §12.3) — and every such element is reported in the envelope's `losses`.
5. **Errors are not values; absence is not a type.** A read call that fails because its subject does not exist, or cannot be read here (access denied, not enabled in this region), does not stop the program: the processor classifies the failure (§12.4); a subject that does not exist is an absent value, and one that cannot be read drops the element it belonged to, recorded as a loss; every other error stops the program, and a program declares no error handling. Absence — an optional member the provider left out, a read whose subject does not exist, `single()` of nothing — is a runtime fact, not a type: every expression has a plain type, a function of an absent value is absent, and a list never holds an absent value, so an element that would be absent is dropped and recorded as a loss. There is no null value and no default operator; absence can be tested (`present()`, `absent()`) but never replaced, and an absent value that reaches an operation's arguments stops the program (§9.2).
6. **Order is not observable.** No operation depends on list position; result lists are bags whose serialization order is normalized.
7. **Ordering is data.** The only dependency between two calls is a reference to a result. There is no sequencing construct and no ordering option; a call that uses another call's result is dispatched after it, independent work is dispatched together, and a host orders independent mutations by policy (§12.1).
8. **The catalog owns provider facts.** Types, which members are required, defaulted, or optional, pagination, effect class, error classes, context options, and the meaning of a documented absence all come from the catalog (§10). A program cannot restate or override them, and everything a program needs to know about a provider is discoverable from the catalog before writing.
9. **One meaning per token, and the author is told the cause.** Braces are records; `call` is the effect; `for` is the binder; indentation delimits the only two kinds of block (§3.1). Every diagnostic names the authored decision that caused it and carries a fix (§11). The same skeleton is available as a JSON structure for hosts that pass programs as tool arguments (§3.2).
10. **Reviewable by a human as written.** A program reads top to bottom as the workflow it performs: one item per line, the name before the thing it names, the result last, every effect visible as `call(` and every fan-out as `for`. Nothing a program does depends on distant or hidden context — there are no defaults for absent values, no implicit ordering, no truncation, and no meaning attached to list position — so a reviewer who has read the lines has read the behavior. The typed review (§11, §13.1) annotates those same lines with inferred types and effects rather than presenting a translation; the reviewer approves the program text.
11. **Frugal for the author.** The language is small enough to teach in one screen of plain text and to hold in a model's context alongside the task: catalog names in their native spelling, no type annotations except on inputs, no nullability to reason about, no delimiters beyond braces and brackets for data, no boilerplate around calls or bodies, and one form per concept so there is nothing to choose between. A task should cost one capability search, one schema lookup, one program, and one validation; a processor's authoring guide, diagnostics, and catalog rendering are designed toward that count, and the count is measured (Code Mode specification, §9).

### 1.1 Non-goals

User-defined functions, recursion, lambdas of any kind, first-class functions, closures, nullable types, a null value, default values for absent members, string truncation or slicing, a map type, positional list operations (`first`, `take`, `zip`, `sort`), arithmetic outside aggregators and the closed time functions, conditionals outside predicates, ordering directives, streaming or partial results, retries or concurrency authored in the program, registries, hashing, persistence, and any provider-specific syntax.

### 1.2 Roles

Following the institutional model, three roles are architecturally separated: the **author** (typically an LLM) writes programs; the **processor** validates, types, and executes with catalog access the author cannot bypass; the **reviewer** (human or policy) approves the typed review before effects. Constraints are constitutive: what the grammar cannot express, the author cannot do.

### 1.3 Normative terms

MUST, MUST NOT, SHOULD, MAY are normative. A **program** is a parsed TOWL v3 source. A **validated program** is a program that passed every static check in §11. A **catalog** is the provider-supplied description of services, operations, shapes, effects, and pagination (§10).

---

## 2. Lexical structure

```text
IDENT     = [A-Za-z_][A-Za-z0-9_]*
STRING    = JSON string, double or single quoted; a single-quoted string has JSON's escapes plus \'
NUMBER    = JSON number
comments  = "//" to end of line | "#" to end of line
```

Inside brackets (`()`, `[]`, `{}`) whitespace and newlines are insignificant. Outside brackets, **layout is significant** (§3.1): a block's items are lines that share one indentation, a `for` body is the lines indented more than its `for` line, and a line that starts with `.` continues the previous line. Indentation uses spaces; a tab is a lexical error. Trailing commas in records, lists, and argument lists are permitted, and `;` is an ignored separator (so several items may share a line). `true` and `false` are literals. `null` is reserved and is not a value (§9.2). `towl`, `input`, `for`, `in`, `call`, `null`, `Null`, `list`, and the type names in §4 are keywords.

Effect boundary, lexically: the keyword `call` followed by `(` is an operation call. Nothing else has an effect. Because the namespace and operation are string literals, the effect boundary is a regular expression over the token stream and the set of operations a program can reach is a lexical scan, which is what makes grammar-constrained decoding and grammar-level capability removal possible; and because no identifier is a namespace, the program's own names never collide with the catalog.

---

## 3. Grammar

```text
program   = "towl" "3" STRING? input* binding* expr
input     = "input" IDENT ":" type
binding   = IDENT "=" expr
expr      = primary postfix*
postfix   = "." IDENT                                   ; member access (absent when the member is absent, §9.2)
          | "." STDFN "(" args? ")"                     ; stdlib method (§8, §9); argument forms fixed per function
STDFN     = EXPRFN | AGGFN | STRFN | TIMEFN | "present" | "absent"
EXPRFN    = "flat" | "flatten" | "where" | "distinct" | "concat" | "group" | "single"
AGGFN     = "count" | "sum" | "min" | "max" | "avg" | "collect" | "any" | "all" | "top" | "bottom"
STRFN     = "after_last" | "before_first" | "lower" | "upper"
TIMEFN    = "minus_days" | "minus_hours" | "minus_minutes" | "start_of_day" | "start_of_month" | "date"
call      = "call" "(" STRING "," STRING ("," expr ("," record)?)? ")" ; the only effect (§6): namespace, operation, args?, options?
for       = "for" IDENT "in" expr ":"? body                        ; the only binder (§7); ":" is accepted and dropped
body      = expr                                                  ; on the for line: a record or any single expression
          | NEWLINE INDENT binding* expr DEDENT                    ; laid out: bindings then the result (§3.1)
primary   = literal | IDENT | call | for | record | list | "(" expr ")"
literal   = STRING | NUMBER | "true" | "false"
record    = "{" (IDENT ":" expr ("," IDENT ":" expr)* ","?)? "}"
list      = "[" (expr ("," expr)* ","?)? "]"
args      = arg ("," arg)*
arg       = path | pred | ref | literal | expr
          ; flat, group, distinct(path): path.
          ; sum, min, max, avg, collect: path, omitted when the elements are scalars (§9.1).  top, bottom: (literal | ref), path?
          ; where, any, all: pred.  concat: expr.  after_last, before_first, minus_*: ref | literal.  present, absent: none.  others: none.
path      = ("." IDENT)+ ("." (EXPRFN | AGGFN | STRFN | TIMEFN | "present" | "absent") "(" args? ")")*   ; from the implicit element
ref       = IDENT ("." IDENT)* ("." (STRFN | TIMEFN) "(" (literal | ref)? ")")*   ; enclosing binding or for variable
pred      = operand cmp operand | operand "in" (plist | ref) | pred "&&" pred | pred "||" pred | "!" pred | "(" pred ")"
          | operand "." ("present" | "absent" | "empty") "(" ")"                                    ; empty: operand : list[T]
          | operand "." ("contains" | "starts_with" | "ends_with") "(" (STRING | ref) ")"
          | operand "." ("any" | "all") "(" pred ")"                                        ; operand : list[T]; inner element is T
operand   = spath | literal | ref
plist     = "[" ((literal | ref) ("," (literal | ref))* ","?)? "]"                          ; a pure list
spath     = ("." IDENT)+ ("." (STRFN | TIMEFN) "(" (literal | ref)? ")")*            ; a path; only string/time continuations (no transforms or aggregations)
cmp       = "==" | "!=" | "<" | "<=" | ">" | ">="
type      = "string" | "int" | "number" | "bool" | "timestamp"
          | "list" "[" type "]"
          | "{" IDENT ":" type ("," IDENT ":" type)* "}"
          | IDENT "." IDENT                              ; catalog shape: namespace.Shape
          | "json"
```

`{` always opens a record; there is no brace-delimited block. Blocks — bindings followed by a result — exist in exactly two places, the program and a `for` body, and are delimited by layout (§3.1). Namespace names are not reserved and may be used as ordinary binding names.

Operation calls, `for`, and blocks appear only in `expr` positions: top level, bindings, `for` bodies, record and list elements, and `concat` arguments. They never appear inside `path`, `spath`, `ref`, `pred`, call `args`, or `options`; those are the **pure layer**. Predicate operands admit only string and time continuations on paths, plus a nested `any`/`all` over a list-typed operand whose inner predicate sees the inner element; an operand that is absent makes the comparison *undecided* (§8.3), so an optional number is compared directly: `.Size > 50`. `present()` and `absent()` are also value-producing methods of type `bool` (`{ public: i.PublicIpAddress.present() }`); they are the only way to observe absence.

Forms borrowed from other languages are syntax errors whose fix shows the TOWL form: `xs.map(…)`, `xs.each(…)`, and `x => …` in any argument position (fix: `for x in xs` and its body forms); `service.Operation(…)` (fix: `call("service", "Operation", { … })`); `.or(…)` (fix: none is needed — an absent value drops its element and is reported, §9.2); `{ a = … }` (fix: braces enclose records; bindings go on their own lines); `null` as a value (`syntax.notInTowl`; fix: omit an optional parameter or field, test absence with `.x.absent()`).

Null-handling forms that other languages teach, and that TOWL does not need, are **read with a warning** rather than rejected, so that the habit costs no turn: `?.` is read as `.` everywhere, including before a stdlib method (`syntax.nullSafe`); `x == null` and `x != null` in a predicate are read as `x.absent()` and `x.present()` (`syntax.nullCompare`); `T | Null` in an input type is read as `T` (`syntax.nullType`); `.compact()` is the identity, since lists never hold absent values (`syntax.compact`). The canonical rendering drops them.

Precedence: postfix binds tighter than everything; `!` tighter than `&&` tighter than `||`. There are no binary operators outside `pred`.

### 3.1 Layout

A block is `binding* expr` and ends at its result, so a parser can find every block boundary without indentation. Layout is therefore **checked, not parsed**: the processor determines structure from the tokens and then verifies that the indentation agrees, so that every disagreement is reported as a layout mistake at the line where it occurs rather than as a confusing consequence elsewhere.

- The program is the outermost block. Its items (inputs, bindings, result) that begin a line MUST share one indentation, fixed by the first of them.
- A `for` whose body does not begin on the `for` line opens a block: the body's items are the following lines, indented more than the line containing the `for` (canonical: two more spaces); they share one indentation; the block ends at its result. A line indented like the body after the body's result is an error (`syntax.resultNotLast`): the result must be last.
- A body on the `for` line is a single expression — most often a record — and ends where the expression ends; postfix may continue it. A laid-out body ends the `for` expression except for the dedented continuation below; the usual way to post-process a fan-out is to bind it (`per = for r in regions …` then `per.flatten()`).
- Layout is suspended inside brackets: a multi-line record, list, or argument list is free-form, and a `for` inside brackets is not layout-checked.
- A line beginning with `.` continues an expression, chosen by its column:
  - at or beyond the items of the current block, it continues the expression on the previous line — inside a laid-out body, that is the body's last line;
  - after a laid-out `for` body, indented more than the `for` line and less than the body, it applies to the `for` expression as a whole;
  - after a laid-out `for` body, at the column of the `for` line's block, it is an error (`syntax.continuation`): a laid-out body ends its expression. The fix is to indent it between the two columns, or to bind the `for` and continue the name.
  - A line ending in `=` continues onto the next.

  ```text
  per = for r in regions          # the block's items are not indented; the body is indented 4 spaces
      insts = …
      insts.count()
    .sum()                        # 2 spaces, between the two → (for …).sum()
  n = for r in regions [r]
    .flatten()                    # one-line body: continues the body → [r].flatten()
  m = for r in regions
    [r]
  .flatten()                      # at the block's indentation after a laid-out body → syntax.continuation
  ```
- Items may also share a line (`;` or juxtaposition), which is how one-line programs are written; only line-starting items are checked.
- Tabs are an error; a trailing `:` on a `for` line is accepted and dropped by the canonical rendering.

### 3.2 Structured program form

Hosts that receive programs as tool arguments MAY accept the program as a JSON object. It is a pure adapter: the processor renders it to the text form and applies the same grammar and checks, mapping each diagnostic back to the node it came from (`where`). Three node forms exist, so that the two places where structure matters most — operation arguments and fan-out bodies — are real JSON rather than text.

```text
Program  = { "towl": 3, "description": STRING?, "inputs": { IDENT: type-text }?,
             "bindings": [ Node, ... ]?, "result": expr-text }
Node     = { "name": IDENT, "value": expr-text }                                       ; any expression
         | { "name": IDENT, "call": "namespace.operation", "args": Args?,             ; split at the first "."
             "options": { "region": ..., ... }?, "then": postfix-text? }              ; an operation call
         | { "name": IDENT, "for": { "over": expr-text, "as": IDENT,
                                     "bindings": [ Node, ... ]?, "result": expr-text } }      ; a fan-out
Args     = JSON object; every value is literal data except an object of exactly the form {"$": expr-text},
           which is an expression (a reference such as "ver" or "s.link"); JSON null is not a value (omit the member)
```

The `call` string is split at its first `.`: the namespace is the text before it (namespaces contain no `.`), the operation is everything after it (MCP tool names may contain `.`). Rendering: `towl 3 "description"`; one `input name: type` line per input; one `name = value` line per `value` node; `name = call("namespace", "operation"[, <args as a record literal>[, <options as a record literal>]])<then>` per `call` node, where `{"$": e}` renders as `e`; `name = for as in over` followed by the inner nodes and the result on lines indented two spaces, per `for` node; then the result. Because a literal string in `args` is always data, a processor SHOULD warn (`catalog.literalLooksLikeName`) when a string argument equals a name in scope. This form exists because a tool's input schema teaches the skeleton before the model writes anything, and because parameters written as JSON cannot suffer the quote and newline escaping mistakes of a multi-line string.

---

## 4. Types

```text
T ::= string | int | number | bool | timestamp | json
    | list[T]
    | { field: T, ... }                      ; closed record
    | namespace.Shape                        ; catalog structure, closed
```

Rules:

- There are no union types and no null type. Whether a value is present is a runtime fact (§9.2); it never changes a type, so no expression needs a different form because a value might be absent.
- Records and shapes are closed; assignability is field-wise and exact. A record literal is assignable to a shape when every required shape member is present with an assignable type and no unknown member is present.
- `int` is assignable to `number`. Nothing else coerces.
- There is no map type. A provider map member is decoded by the catalog as `list[{ key: K, value: V }]`; lookup is `.where(.key == "Name").single().value`, and `group` already yields key/items records. One collection type keeps the stdlib closed.
- `json` is an opaque value the catalog could not type (a tool with no output schema, a free-form object). It supports no member access, comparison, or aggregation; it may only be passed whole to a parameter typed `json`, placed in a record or list, or returned. Accessing a member of `json` is a validation error `type.opaque` that names the operation whose schema is missing.
- Equality (`==`, `!=`, `distinct`, `group` keys, `in`) is defined on `string int number bool timestamp` and structurally on records and lists of such. Ordering (`< <= > >=`, `min`, `max`) is defined on `int number string timestamp`.
- Catalog members are **required**, **defaulted** (absence normalized to a declared default, e.g. `[]`), or **optional** (§10). All three have the member's type `T`. Optionality is information for the author — schema renderings write an optional member `Name?: T` so the author can foresee where losses may occur — and has no effect on typing.

**Literals.** A `NUMBER` without fraction or exponent is `int`, otherwise `number`. `int` is arbitrary-precision (there is no overflow; `sum` of `int` is exact) and `number` is an IEEE 754 double. `true`/`false` are `bool`; a `STRING` is `string`, except in a position whose expected type is `timestamp` (a call parameter, an input, or a comparison or `in` whose other operand is a timestamp), where it is `timestamp` if it parses as RFC 3339 and a validation error otherwise. A record literal has the closed record type of its fields. A list literal's elements MUST have one type after the `int → number` join; `[]` and `{}` take their type from the expected type of their position (parameter, input, `concat` argument, `in` operand) and are a validation error `type.emptyLiteral` when no expected type exists. There is no null literal.

**Lists are bags.** List equality is multiset equality; `concat` is bag union; `collect` and `distinct` are commutative monoids on bags. Serialization order is canonical (§12.5) and never observable to a program. A list never contains an absent value (§9.2).

---

## 5. Programs, inputs, bindings, blocks

**Header.** `towl 3 "description"`. The description is informative.

**Inputs.** `input name: type` declares a value the host binds before execution. Inputs are how a program receives external data — including the `completed` values of a previous failed run (§13.3) — without embedding literals. Types are checked at preflight; a missing or ill-typed input is a preflight error and no effect occurs. A JSON `null` in a record field of an input value is an absent field (so values a previous envelope reported round-trip); a `null` list element or a `null` input is a preflight error. Two inputs are **predefined** and need no declaration: `now: timestamp` (the run's start instant, UTC) and `today: string` (its `YYYY-MM-DD` date); the processor binds them, so time windows are computed inside the program (§9.4) rather than pasted in as literals. A program MAY declare an input or bind a name `now` or `today`; the predefined input of that name is then not in scope, so the program's definition is the only one and the no-shadowing rule below is kept. Other well-known inputs a host offers (Code Mode's `region`) MUST be declared to be bound. A declared input that the program never references is a warning (`names.unusedInput`).

**Bindings.** `name = expr` binds once. Names are unique across the whole scope chain (no shadowing, no rebinding). References to a binding create dependency edges; the binding graph MUST be acyclic (it is, because a binding can only reference earlier bindings and enclosing `for` variables). Namespace names are not reserved: `s3 = call("s3", "ListBuckets").Buckets` is legal, because the namespace in a call is a string, not a name.

**Blocks.** A block is `binding* expr`: bindings scoped to the block, then its value. The program is a block, and a laid-out `for` body is a block (§3.1); there is no other block form and no brace-delimited block. A `for` body written on the `for` line is a single expression, usually a record.

**Reachability.** Every binding MUST be referenced, transitively, from the program's final expression. This is what makes "all effects flow into the result" true: there are no statements, and a mutation whose response is unwanted is still included in the result. Ordering between operations is data: a call that uses another call's result is dispatched after it, and there is no other ordering construct (§12.1 says how a host orders mutations that share no data).

**Result.** The final expression is the program result. Its type is reported at validation and is the type of the success envelope's value (§13.2).

---

## 6. Operation calls (effects)

```text
call("namespace", "operation")
call("namespace", "operation", args)
call("namespace", "operation", args, options)
```

`call` is a built-in primary, not a method: the namespace (an AWS service such as `"ec2"`, an MCP server such as `"javadocs"`) and the operation name in the catalog's exact spelling (`"DescribeInstances"`, `"get_latest_version"`) are string literals, so the set of operations a program uses is a lexical scan and no name in the program can collide with a namespace. An unknown namespace is a validation error naming the nearest namespaces (`catalog.unknownNamespace`); an unknown operation is one naming the nearest operations. `args` defaults to the empty record; `options` defaults to none.

### 6.1 Args

`args` is an expression whose type MUST be assignable to the operation's input shape (§4). Every value in `args` and `options` MUST be present when the call is dispatched: an absent value anywhere in the parameter structure (`Dimensions[0].Value`, a `region` option) stops the program with class `data`, naming the parameter path and where the absence came from (§12.6 origins). Unlike every other consumer of an absent value, a call does not drop its element: an operation invoked without a value the author supplied acts on a different population than the author meant, and dropping the element would turn that into a plausible answer over the wrong set (§16). To call only for elements where a value exists, filter first: `for b in buckets.where(.BucketRegion.present())`. An optional parameter is supplied by writing it and omitted by not writing it; a parameter is never omitted because its value was absent. An empty list is never a legal argument value: a literal `[]` is a validation error (`catalog.emptyListParameter`; optional parameters are omitted), and a computed empty list anywhere in `args` at dispatch stops the program with class `data` (code `EmptyArgument`), naming the parameter path. Many providers read an empty or omitted list as "no restriction" (`InstanceIds: []` describes every instance), so a list that happened to be empty would silently widen the call; to call only when there is something to pass, filter first or fan out over the list. `args` may reference bindings, enclosing `for` variables, and earlier call results (creating data dependencies). It may not contain operation calls or `for`.

### 6.2 Options

`options` is a record literal of **context options**: settings of the call's environment rather than of the operation, such as the endpoint region or credential profile. The language defines no options of its own; each catalog declares its keys and their types (§10, `optionKeys`), and an unknown key is a validation error. Option values are pure expressions like `args` (§6.1): they may reference bindings and `for` variables but contain no calls or `for`, and an absent value stops the program with class `data`. Hosts MAY gate options by policy (for example, allow a credential-profile option only when the user permits it).

Options never order calls: ordering is data (§1, item 7; §12.1).

### 6.3 Typing

```text
call("ns", "op", args)                    : Out
```

where `Out` is the operation's output type as the catalog declares it (§10): a shape, a record derived from a JSON Schema, `string` for text-only tools, or `json` when no schema exists. For a pageable operation, `Out` is the catalog's **merged output shape** (result-key lists concatenated across pages, pagination members removed). Authors never see pages or tokens; pagination members are not authorable in `args` (validation error with a pointer to this rule).

### 6.4 Effect class

Every operation has a catalog effect class: `read` or `mutate` (unknown is treated as `mutate`). The class is reported per call site with its fan-out multiplicity. Hosts gate `mutate` by policy; the language does not.

---

## 7. Traverse: `for`

```text
for x in list[A] body : list[B]        where x : A ⊢ body : B
```

- `for` is the only binder and the only fan-out. `x` names the element for the body (the structured form calls it `as`); it is not a loop variable and not a lambda parameter: there is no iteration order, no function value, and `x` is visible only inside the body. The body is a single expression on the `for` line or a laid-out block of bindings and a result (§3.1); it may contain operation calls and nested `for`. `for` is an expression: it may be bound (`per = for r in regions …`) and appears wherever an expression may.
- Result length equals source length minus the elements whose body result is absent; each such element is dropped and recorded as a loss (§9.2), so a read that fails for one element (§12.4) removes exactly that element. Element order is not observable (§1, item 6).
- Every body instance is independent: bodies may not reference other elements or each other. Concurrency is derived (§12.1), never authored.
- A body containing calls is a **wave**. Its multiplicity is the source length: **static** when the source has statically known length, **dynamic** otherwise. Static length is defined inductively and is an upper bound: a list literal has its element count; `for` has at most its source's static length; `concat` of two static lists sums them; every other expression (inputs, call results, `where`, `distinct`, `flat`, `flatten`, `group`, `collect`) is dynamic. Dynamic widths are admitted at runtime against the host's width budget before any body call is dispatched (§12.3).
- The processor SHOULD warn when `x` is unused (the traversal then repeats one identical value or call).

---

## 8. Express: paths, predicates, transforms

Express functions operate on `list[A]` with an **implicit element** of type `A`. Their arguments are paths or predicates over that element — never functions.

### 8.1 Paths

A path `.m1.m2…` navigates from the implicit element through record/shape members. `.m` on `T` requires `m` to be a member of `T`; its value is absent when the member is absent or the receiver is absent (§9.2). A path MAY continue with Express/Aggregate/string/time stdlib methods (`.items.count()`), but never with an operation call or `for`. Paths are the only way to refer to the element inside an Express function; there is no element variable there (that is what `for x in …` is for).

### 8.2 Reshaping

There is no projection function. One value per element is `collect(.path)` (§9.1); one record per element is a `for` whose body is a record: `for g in vols.group(.AvailabilityZone) { az: g.key, gib: g.items.sum(.Size) }`. A `for` whose body makes no call is pure — not a wave, no fan-out cost — and every field names the value it reads, which is also how an element is tagged with an enclosing value (`for o in objs { bucket: b.Name, key: o.Key }`). A field whose value is absent is an absent field and the record is kept (§9.2). To continue a chain after a reshaping `for`, bind it or parenthesize it: `(for x in xs { … }).top(10, .size)`.

### 8.3 Predicates

The closed predicate sublanguage (§3 `pred`): comparisons between two operands of the same equality/ordering type, `in` against a list, `present()`/`absent()` on any operand, `empty()` on a list-typed path, string tests (a test compared with a boolean literal, `.L.empty() == false`, is the test or its negation), `any(pred)`/`all(pred)` over a list-typed operand (the inner predicate's paths start from the inner element; enclosing `for` variables remain visible as refs), and `&& || !`.

Predicates are three-valued. A comparison, `in`, `empty()`, or string test whose operand is absent is **undecided**; `present()` and `absent()` are decided, except on an unknown value (§9.2), where they are undecided too. `!` of undecided is undecided; `&&` is false if any term is false, else undecided if any term is undecided; `||` is true if any term is true, else undecided if any term is undecided; `any` and `all` fold their elements the same way. `where` keeps the elements whose predicate is true, drops those that are false, and drops **and records as a loss** those that are undecided (§9.2). Consequently `.Size <= 50` and `!(.Size > 50)` select the same elements, and an element whose `Size` is absent is in neither result and is reported either way. To keep elements whose member is absent, say so: `.Platform.absent() || .Platform != "windows"`.

Predicates contain no calls or binders, so a host MAY push them down to a provider's server-side filters when it has an exact mapping; the result, including its losses, MUST be identical either way. Because a `for` variable is a legal operand, a predicate inside a `for` body can correlate the element with another list — this is how two independent results are joined (§14, example 6).

### 8.4 Transforms

| Function | Signature | Notes |
|---|---|---|
| `flat(path)` | `list[A] → list[T]` where path `: list[T]` | the only element-wise flatten; an element whose list is absent contributes nothing (loss) |
| `flatten()` | `list[list[T]] → list[T]` | one level |
| `where(pred)` | `list[A] → list[A]` | undecided elements are dropped (loss) |
| `distinct()` | `list[A] → list[A]` | `A` equatable |
| `concat(list[A])` | `list[A] → list[A]` | bag union; the argument is a general `expr` (may contain calls or `for`) |
| `group(path)` | `list[A] → list[{ key: K, items: list[A] }]` | path `: K`, `K` an equatable scalar; an element whose key is absent is dropped (loss) |
| `single()` | `list[A] → A` | zero elements → absent; more than one → runtime error `cardinality` |

---

## 9. Aggregate, absence, and results

### 9.1 Aggregators (monoids)

Each aggregator is `(prepare, monoid, present)` with a declared identity and associative combine; `count`, `sum`, `min`, `max`, `distinct`, `collect` are commutative, so the processor MAY fold partitions in any order. Empty input yields the identity.

| Function | Signature | Monoid |
|---|---|---|
| `count()` | `list[A] → int` | sum of ones |
| `sum(path)` | `list[A] → number` (path `: int \| number`) | numeric sum; `int` if the path is `int` |
| `min(path)`, `max(path)` | `list[A] → T` (path `: T` ordered) | semilattice; absent on empty |
| `avg(path)` | `list[A] → number` (path `: int \| number`) | product `(sum, count)`; absent on empty |
| `collect(path)` | `list[A] → list[T]` (path `: T`) | bag union; adds exactly one list level, never flattens |
| `distinct(path)` | `list[A] → list[T]` (`T` equatable) | set union |
| `any(pred)`, `all(pred)` | `list[A] → bool` | three-valued or / and (§8.3); absent when undecided; `all` of empty is `true` |
| `top(n, path)`, `bottom(n, path)` | `list[A] → list[{ rank: int, value: A }]` (path `: K` ordered; `n : int`, literal or ref) | the `n` elements with the largest (smallest) key, `rank` 1..n; a bounded semilattice (merge, keep `n`) |

An element whose aggregator path is absent is skipped by every path aggregator (`sum min max avg collect distinct top bottom`) and the skip is recorded as a loss (§9.2), so `sum(.Size)` over seven volumes of which two lack a size is the sum of five, and the envelope says so.

**Scalar lists.** When the elements are scalars the path names the element itself and MAY be omitted: `sizes.sum()`, `names.max()`, `sizes.top(3)`. Wrapping scalars in records to reach an aggregator is never necessary.

**Selection by rank is not positional.** `top`/`bottom` order by a key the program names, so the result is determined by the data (ties are broken by canonical element order, §12.5) and not by the order a provider happened to return; the `rank` is carried as a field because result lists are bags. There is still no `first`, `take`, or `sort`.

String tests `contains`, `starts_with`, `ends_with` take a receiver of type `string` and one argument of type `string`; an absent receiver or argument makes the test undecided. `in` requires a `list[T]` right operand and a `T` left operand.

### 9.2 Absence

A value is **absent** when it is an optional catalog member the provider left out, the value of a read call that failed with an error of class `absence` (§12.4), `single()` of an empty list, `min`/`max`/`avg` of an empty list, a `null` field of an input record (§5), or any value computed from an absent value. Absence is not a type and not a value a program can write: every expression keeps its one type, and whether it is present is decided at runtime. The rules are three, and they are the whole of what an author needs to know:

1. **Carry.** A function of an absent value is absent: member access (`b.BucketRegion.after_last("-")`), string and time functions, list functions and aggregators applied to an absent list (`objs.where(…)` where `objs` is absent), `for` over an absent list, `concat` with an absent operand. A record field whose value is absent is an absent field; the record exists and the field is reported as `null` in the result.
2. **Drop and record.** A list never holds an absent value. Where a list would receive one — a `for` body whose result is absent, a list literal element that is absent — the element is dropped. An element whose aggregator path or `group` key is absent is skipped (§9.1), and a `where` element whose predicate is undecided is dropped (§8.3). An **unknown** value — a read that failed with class `authorization` or `availability` (§12.4), or exceeded a per-call budget (§12.3) — is carried like an absent one, except that a record containing it is itself unknown and `present()`/`absent()` of it are unknown (a failed read says nothing about existence), so it reaches the nearest list and drops its element even when it was only a field. Each drop is a **loss**: it is recorded with the node that dropped it, the origin of the absence, and a sample of the element (§12.6). Nothing is ever dropped silently.
3. **Stop at an operation.** An absent value anywhere in a call's `args` or `options` stops the program with class `data` (§6.1). An absent program result also stops (class `data`, code `AbsentResult`), since there is no enclosing list to drop it from; to report an absence, return it as a record field.

`present()` and `absent()` are the only observations of absence; on an absent value they are decided and never cause a loss (on an unknown value they are unknown, since a read that could not be done says nothing about existence), so queries whose answer *is* the absence — buckets without a policy, instances without a public address — are written directly: `per.where(.policy.absent())`.

```text
x.m              : U          ; absent if x is absent or its member m is absent
x.present()      : bool       ; decided on an absent x, unknown on an unknown x; x.absent() is its negation
```

**There is no default operator** and no null value. Nothing turns an absent value into a present one, because a default is indistinguishable from data once it is in the result (rationale in §16). When a provider documents that absence *means* a value (S3's `LocationConstraint` is absent for `us-east-1`), that meaning belongs to the catalog's typing of the member (§1, item 8), not to the program.

**What the author does not do.** No expression needs a different form because a value might be absent: there is no `?.`, no nullable type to thread through bindings, no `compact()` before a `flatten()`, and no call declares which errors it expects: a read that may fail is written exactly like one that cannot. The author reads `losses` in the envelope instead of predicting them in the program.

### 9.3 Strings

Value-producing string functions, usable in paths, refs, and record fields. All are total; on an absent receiver or argument they are absent.

```text
s.after_last(sep)  : string   ; text after the last occurrence of sep, or s when absent
s.before_first(sep): string   ; text before the first occurrence of sep, or s when absent
s.lower() / s.upper(): string
```

There is deliberately no truncation or slicing of strings: a program cannot hide part of a value from the result. Bounding large text is a catalog operation's job (for example an LLM `summarize` operation), so that the reduction is a visible effect in the effect table rather than a silent transform.

### 9.4 Timestamps

`timestamp` values are RFC 3339 instants. There is no arithmetic; a closed set of total functions, usable wherever string functions are, covers the time windows that metric, log, and cost operations require. On an absent receiver or argument they are absent.

```text
t.minus_days(n) / t.minus_hours(n) / t.minus_minutes(n) : timestamp   ; n : int, literal or ref
t.start_of_day() / t.start_of_month()                     : timestamp   ; UTC
t.date()                                                  : string      ; "YYYY-MM-DD" (date-typed parameters such as Cost Explorer's)
```

The receiver is normally the predefined input `now` (§5): `StartTime: now.minus_days(4), EndTime: now`. A string literal in a `timestamp` position is a timestamp (§4).


---

## 10. Catalog contract

A processor executes only against an immutable catalog that supplies:

```text
namespaces()                                 -> [namespace]
operation(namespace, name)                   -> { input: Shape, output: type, paged: bool,
                                                  mergedOutput: Shape?, effect: read | mutate | unknown,
                                                  errorCodes: [string], optionKeys: { name: type } }
shape(namespace, Shape)                      -> structure definition
member(Shape, name)                          -> required T | defaulted (T, default) | optional T
errorClass(namespace, name, code)            -> transient | validation | authorization | availability | absence | state | other
```

A member is **defaulted** when the catalog knows what its absence means: a list member whose absence is emptiness (`[]`), or a member whose documentation gives absence a value (S3's `LocationConstraint` is absent for `us-east-1`); the catalog normalizes such a member to that value, so it is never absent and the program never has to supply the meaning of an absence (§9.2). An **optional** member may be absent at runtime; its type is still `T`, and renderings mark it `Name?: T`.

A catalog decides how a provider's schema language maps to TOWL types. For JSON-Schema-described tools (MCP): `object` with `properties` → closed record, properties not in `required` → optional, except that a catalog MAY declare list-typed properties **defaulted** to `[]` (the reference MCP catalog and the AWS profile both do, since absence and emptiness are indistinguishable to callers); `array` → `list[T]`; `string`/`integer`/`number`/`boolean` → the scalar; an object with `additionalProperties` and no `properties` → `list[{ key: string, value: T }]` (`value: json` when untyped); `oneOf`/`anyOf` and untyped values → `json`; a tool with no output schema whose result is text → `string`, otherwise `json`. MCP `readOnlyHint` maps to `read`; tools without annotations are `unknown` (treated as `mutate`). MCP tools are never `paged`.

Operations MAY be model-backed (an LLM `summarize`, a classifier); they are ordinary effects, appear in the effect table, and their cost is metered by the host alongside the author's own model turns. A program that needs to reduce text uses such an operation; the language has no truncation (§9.3). Code classification SHOULD accept the loose spellings that appear in providers and tool descriptions (`NotFoundError`, `ResourceNotFoundException`, `NoSuchKey` all classify as `absence`), because a code the classifier does not recognize is `other` and stops the program.

`errorClass` decides what a failed read does (§12.4), so it MUST classify every code, including codes the provider's model does not list; a code it cannot place is `other` and stops the program. `optionKeys` extends §6.2 with context options such as `region`. Pagination configuration (cursor, limit, result keys) is catalog- and processor-owned and never appears in programs.

---

## 11. Static semantics (validation)

Validation is total and invokes no operation. It runs these phases and reports **all** diagnostics it can find, each with `line:col`, the source excerpt, the inferred type at that point where relevant, a stable code, and one concrete fix:

1. **syntax** — grammar (§3) and layout (§3.1: `syntax.indent`, `syntax.resultNotLast`, `syntax.continuation`, `syntax.tab`), the borrowed forms of §3 with their fixes, the normalizations of §3 (`syntax.nullSafe`, `syntax.nullCompare`, `syntax.nullType`, `syntax.compact`; warnings), and the `pure` restriction on paths, predicates, and args.
2. **names** — undefined identifiers, shadowing, rebinding, unreferenced bindings, unused inputs (warning).
3. **catalog** — unknown namespace/operation (each with nearest names), args not assignable to the input shape, unknown option keys (`catalog.unknownOption`), empty-list arguments, authored pagination members, unknown shape names in inputs.
4. **types** — every expression has exactly one type by the rules of §§4–9; non-equatable comparisons, `flat` on a non-list path, aggregator path types, `single` on a non-list, `for` on a non-list, `in` against a non-list. No type rule concerns absence.
5. **effects** — the effect table: every call site with its operation, class, and multiplicity (static `n`, or `dynamic ≤ budget`), nested wave depth, and the count of `mutate` sites.

The validation report (§13.1) is the reviewer's artifact. A program with any error in phases 1–4 is invalid and MUST NOT execute. Phase 5 never fails validation; hosts apply policy to it.

**Diagnostic quality.** A diagnostic MUST name the nearest *authored decision* that caused it, not the symptom the checker met, because the author repairs what it names. Rules a conforming processor follows:

- `call("x", …)` with an unknown `x` is "`x` is not a namespace in this catalog (did you mean …)", and the method form `x.Op(…)` is "operations are written `call(\"x\", \"Op\", …)`", never "`Op` is not a function": the author was trying to call an operation.
- `.m` on a scalar names the operation or binding that produced the scalar and says to drop `.m`: "`ns.op` returns the string itself, not a record".
- A string parameter equal to a name in scope is warned about (`catalog.literalLooksLikeName`), since a literal where a reference was meant type-checks and runs with the wrong value.
- In the structured form (§3.2), every diagnostic carries `where` — the input, binding, `for` body, or result it belongs to — because the author did not write lines.
- Every error has a `fix` when one exists; a diagnostic without a fix is a defect in the processor.
- An empty list literal as a parameter value is an error (`catalog.emptyListParameter`): providers reject it and it is never what the author meant; the fix says to omit optional parameters. The catalog's schema rendering names the required parameters so the author knows which may be omitted (Code Mode specification, "schema").
- A normalization warning (`syntax.nullSafe` and the others of §3) MUST say that the form is unnecessary and that nothing replaces it: an author who reads it as a demand for a different null-handling form will look for one, and there is none to find.
- A runtime `data` stop for an absent argument MUST name the parameter path, the origin of the absence (the member and its line, or the failed call and its code), and the filter that would call only where the value exists (`.where(.BucketRegion.present())`).

Type inference is syntax-directed with no polymorphism beyond the stdlib signatures above and needs no annotations; `input` types are the only declared types.

---

## 12. Runtime semantics (execution)

### 12.1 Evaluation model

The validated program is an SSA graph: nodes are bindings, calls, `for` traversals, and pure transforms; edges are references. Execution is **opportunistic**: a node evaluates as soon as its inputs are values; a call is dispatched as soon as its `args` and `options` dependencies are values; a `for` unrolls into one body instance per element as soon as its source list is a value. Independent nodes proceed concurrently: two bindings that share no dependency are dispatched together, and the first expression that references both is their join point and waits for both (§14, example 6). Because the only effects are calls determined by their arguments, and every call is dispatched exactly once (§12.2 limits retries so that a mutation is never applied twice), the final value does not depend on scheduling (confluence). The loss log (§12.6) is a bag, a commutative monoid, so it is confluent too: the same external responses produce the same losses in any schedule. Two `mutate` calls with no data dependency MAY execute in any order or concurrently; hosts MAY impose stricter policy (the Code Mode profile runs them one at a time in source order).

### 12.2 Pagination and retries

A pageable call is executed to completion by the processor, producing the merged output. Transient failures (throttling, 5xx, timeouts, connection errors) are retried by the processor with backoff and are invisible to the program; retry exhaustion is an error of class `transient`. A `mutate` call is retried only when the retry cannot apply the mutation twice: the failure proves the request was not applied (throttling, a 4xx before processing), or the request carries an idempotency token the catalog declares and the processor fills in. Otherwise — a timeout or connection error after the request may have been received — the call fails with class `mutation` and is reported as possibly applied.

### 12.3 Admission and limits

Hosts define budgets: maximum wave width, maximum nested wave depth, maximum operation calls, maximum pages per call, maximum result bytes, wall time. Static widths are checked at validation. A dynamic wave is admitted only when its now-known width is within budget; the check happens before any call in the wave is dispatched. Exceeding a budget is an error of class `budget`, with one exception: a **per-call** budget (maximum items or pages of one paged call) exceeded by a read inside a `for` element makes that call's value unknown (§9.2), so the element is dropped and recorded as a loss with reason `budget`, and the loss says which limit to raise to include it. A per-call budget exceeded outside any element, a wave in which every element exceeds it, and every run-wide budget (calls, width, depth, result bytes, wall time) stop the program; so does any budget under a host's strict mode. Run-wide budgets are checked as the run proceeds, so one can be reached after some mutations have succeeded; they are then listed in `mutations` of the failure envelope (§13.3), and a host that wants no partially applied set of mutations bounds them statically (a static wave width, a host rule against dynamic mutating waves). The result-bytes budget is a backstop against a program returning whole documents into the author's context; its message says to return fewer fields or narrow the list, and successful runs report `result_bytes` so the author sees the cost even when under budget.

### 12.4 Errors

Errors are classified (§10) and a failed **read** call is handled by its class; the program declares nothing:

- `absence` — the subject does not exist (`NoSuchBucketPolicy`, `ResourceNotFoundException`). The call's value is **absent** (§9.2): carried into a record field as `null`, dropped at a list.
- `authorization`, `availability` — the subject may exist but cannot be read here (`AccessDenied`, `OptInRequired`). The call's value is **unknown**: it drops the enclosing list element even from inside a record field, because a `null` field would claim the value does not exist. Where there is no enclosing element, it stops.
- every other class, and any failure of a `mutate` call — stop.

Each absorbed failure is recorded (`absorbed` in the envelope, §13.2), and each element it removes is a loss whose origin names the operation, code, and class (§12.6). A wave (§7) whose every element is unknown (including by budget) stops with the first such error (canonical element order) instead of succeeding empty: an `authorization` or `availability` failure that affects everything is a fault in the program or its credentials, not a fact about the data. (Every element being *absent* is a fact — no bucket has a policy — and does not stop.) A host MAY offer a strict mode in which every error stops. In addition, a processor MUST NOT dispatch a `mutate` call while the loss log is non-empty unless the host has explicitly allowed losses; it stops with class `losses` instead. Losses usually shrink the set a mutation acts on, but not always — every element lost from an exclusion list (`all.where(!(.Id in protected))`) is one more element mutated — and a mutation cannot be taken back, so the default is to act only on complete data. On stop:

- no further node is dispatched;
- in-flight `read` calls are awaited for a host-configured grace period (default 5 s); those that complete resolve normally and may make further pure nodes complete; those that do not are discarded;
- in-flight `mutate` calls are always awaited and reported;
- when several calls fail before the stop completes, the **primary** error is the one whose call site has the smallest `(line, col)`, then the smallest canonical element (§12.5); all others appear in `fanout[].failed` or `errors_also`;
- the result is the failure envelope (§13.3), never a partial value.

Error classes and their suggested action:

| Class | Meaning | Action |
|---|---|---|
| `input` | preflight: missing, undeclared, or ill-typed input | `rewrite` |
| `transient` | retries exhausted | `rerun` |
| `validation` | provider rejected parameters | `rewrite` |
| `authorization` | permission denied (as a stop: outside any list element, or every element of a wave; a per-call budget overrun stops the same way with class `budget`) | `narrow` |
| `availability` | opt-in, endpoint, unsupported in region (as `authorization`) | `narrow` |
| `absence` | provider models "does not exist" as an error (as a stop: only under a host's strict mode) | `narrow` |
| `state` | resource state or race | `rewrite` |
| `cardinality` | `single()` found more than one | `rewrite` |
| `data` | an absent value or an empty list reached a call's arguments or options, or the program result is absent | `rewrite` |
| `losses` | a `mutate` call was about to be dispatched while losses existed | `losses` |
| `budget` | a host limit | `budget` |
| `cancelled` | host or user cancellation | `rerun` |
| `mutation` | any failure of a `mutate` call, whatever its provider class, including one that may have been applied (§12.2) | `mutation` |
| `other` | anything else | `rewrite` |

The catalog classifies provider codes (§10); the runtime maps a failed `mutate` call to `mutation` regardless and keeps the provider classification in the envelope's provider detail.

### 12.5 Result normalization

Lists in results are serialized in a canonical order — by the RFC 8785 (JCS) serialization of each element, after the element's own nested lists have been normalized the same way — so that two runs with the same external responses produce byte-identical results and no consumer can depend on provider ordering. An absent record field is serialized as `null`; `null` appears nowhere else in a result.

### 12.6 Losses

A **loss** is an element dropped or skipped because of an absent value (§9.2). The processor records every loss in a log keyed by `(node, origin)`:

```text
{ node, line, col,                 // the function or for that dropped the element: "for", "where", "collect", "top", …
  reason: "absent" | "unknown" | "budget" | "undecided",  // a needed value was absent | a read could not be done | a read exceeded a per-call budget | a where predicate was undecided
  origin: { kind: "member", member, line, col }            // an optional member left out (by the provider, or null in an input)
        | { kind: "error", operation, code, class, line }   // a read call that failed with an absorbed class (§12.4)
        | { kind: "budget", operation, limit, value, line } // a read over a per-call budget (§12.3); limit names the host setting
        | { kind: "empty", function, line }                 // single/min/max/avg of an empty list
  count,                           // elements lost at this node for this origin
  sample: [ element ] }            // at most 3 elements, abbreviated to their scalar members
```

The origin of an absent value is where it first became absent and is carried unchanged through every function of it, so a loss at `top` whose value came from a failed call names the call, the code, and the class. `nodes` (§13.2) counts elements in and out of each node; `losses` explains the difference that absence caused.

---

## 13. Envelopes

### 13.1 Validation report

```text
{ status: "valid" | "invalid",
  result_type: type,                                   // when valid
  inputs:   [ { name, type } ],
  bindings: [ { name, type, line } ],
  effects:  [ { node, line, operation, class, multiplicity: n | { dynamic: true, max: n }, depth } ],
  mutations: n,
  diagnostics: [ { severity: "error" | "warning", phase, code, line, col, excerpt, message, fix, type? } ] }
```

### 13.2 Success

```text
{ status: "ok", type, value,
  losses:    [ Loss ],                                                   // §12.6; always present, [] when nothing was lost
  absorbed:  [ { line, element?, operation, code, class } ],               // every failed read that did not stop the run
  effects:   [ { node, operation, class, calls: n, pages: n } ],
  nodes:     [ { node, line, kind: "call" | "where" | "flat" | "for" | ..., in: n?, out: n } ],    // element counts per list-producing node
  accounting: { calls, pages, waves, wall_ms, result_bytes, absorbed, losses } }
```

A run that absorbed failures or lost elements is `ok`; every absorbed failure and every loss is listed, a `null` field in the value is always a real absence (there is no default operator, §9.2), and `accounting.losses` is the total count of lost elements. A value with losses is complete *for the elements that survived*: a consumer that reports the value MUST report its losses with it, because a count or a top-N over a list that lost elements looks the same as one that did not. `nodes` records how many elements each list-producing node received and produced, so an empty or small result can be explained ("618 symbols, 7 kept by `where`") without re-running.

### 13.3 Failure

```text
{ status: "error",
  error:     { class, code, message, operation?, node, line, element?, action },
  completed: { <binding>: { type, value } },           // top-level bindings fully resolved before the stop
  losses:    [ Loss ],                                 // losses recorded before the stop (§12.6)
  errors_also: [ { class, code, message, operation?, node, line, element? } ],   // non-primary failures
  fanout: [ {                                          // when the failure occurred inside a for: outermost first
    node, line, element_type,
    completed:   [ { element, value } ],
    failed:      [ { element, code, message } ],
    interrupted: [ element ],                          // started, no failure of its own, stopped before its body finished
    not_started: [ element ] } ],
  mutations: [ { node, element?, operation, args, options, response } ],     // every mutate call that succeeded
  accounting: { calls, pages, waves, wall_ms, losses } }
```

Every value in `completed` and `fanout[].completed` is complete (fully paginated, fully evaluated). A value that was mid-evaluation is absent, never partial. For nested waves, `fanout` lists the enclosing waves outermost first; `completed` of an outer wave contains only elements whose whole body finished. An element is `interrupted` when its body had started and the stop prevented a later dispatch in that body; it is neither complete nor failed and belongs in the next program's remaining work together with `not_started`. Hosts bind these sections to `input`s of the next program so that a rewrite can resume without repeating work and without re-issuing completed mutations (§14, example 4). A preflight `input` failure produces this envelope with empty `completed`, `fanout`, and `mutations`.

---

## 14. Examples

**1. One record per region** (group-preserving fan-out; a region that cannot be read is a loss, any other error stops the run)

```text
towl 3 "Running instances per region"

regions = ["us-east-1", "us-west-2", "eu-west-1"]

for r in regions
  insts = call("ec2", "DescribeInstances", { Filters: [{ Name: "instance-state-name", Values: ["running"] }] }, { region: r })
            .Reservations.flat(.Instances)
  { region: r, count: insts.count(), ids: insts.collect(.InstanceId) }
```
Type: `list[{ region: string, count: int, ids: list[string] }]`. Effects: `ec2.DescribeInstances read ×3`.

**2. Error as absence**

```text
towl 3 "Bucket policies"

buckets = call("s3", "ListBuckets").Buckets
for b in buckets { bucket: b.Name, policy: call("s3", "GetBucketPolicy", { Bucket: b.Name }).Policy }
```
Type: `list[{ bucket: string, policy: string }]`; `policy` is `null` for a bucket without a policy (`NoSuchBucketPolicy` is class `absence`, so the call is absent, and so is its `.Policy`), and the record is kept; a bucket whose policy cannot be read (`AccessDenied`) is dropped and listed in `losses`, never reported as having no policy. Effects: `s3.ListBuckets read ×1; s3.GetBucketPolicy read × dynamic`. To list only the buckets without a policy, bind the `for` (`per = for b in buckets …`) and return `per.where(.policy.absent()).collect(.bucket)`.

**3. Grouped aggregation**

```text
towl 3 "Volume storage by availability zone"

for g in call("ec2", "DescribeVolumes").Volumes.group(.AvailabilityZone)
  { az: g.key, volumes: g.items.count(), gib: g.items.sum(.Size) }
```
Type: `list[{ az: string, volumes: int, gib: int }]` (a volume without `AvailabilityZone` would be dropped by `group` and listed in `losses`).

**4. Resume after a failure** (host binds inputs from the previous failure envelope)

```text
towl 3 "Running instances per region — resume"
input done: list[{ region: string, count: int, ids: list[string] }]   // ← fanout[0].completed[].value
input remaining: list[string]                                          // ← fanout[0].failed[].element ∪ interrupted ∪ not_started

more = for r in remaining
  insts = call("ec2", "DescribeInstances", { Filters: [{ Name: "instance-state-name", Values: ["running"] }] }, { region: r })
            .Reservations.flat(.Instances)
  { region: r, count: insts.count(), ids: insts.collect(.InstanceId) }
done.concat(more)
```

**5. Ordered mutations** (the second call takes its arguments from the first's result, which is the only way to order two calls; the reviewer sees the dependency in the typed rendering)

```text
towl 3 "Stop, then tag the instances being stopped"
stopped = call("ec2", "StopInstances", { InstanceIds: ["i-0123"] }).StoppingInstances.collect(.InstanceId)
tagged  = call("ec2", "CreateTags", { Resources: stopped, Tags: [{ Key: "state", Value: "stop-requested" }] })
{ stopped: stopped, tagged: tagged }
```

**6. Two independent operations, joined and aggregated** (`insts` and `vols` share no dependency, so they are dispatched concurrently; the `for` is the join point and waits for both. The join is a predicate that correlates the volume list with the instance element.)

```text
towl 3 "Attached storage per running instance"

insts = call("ec2", "DescribeInstances", { Filters: [{ Name: "instance-state-name", Values: ["running"] }] })
          .Reservations.flat(.Instances)
vols  = call("ec2", "DescribeVolumes").Volumes

for i in insts
  attached = vols.where(.Attachments.any(.InstanceId == i.InstanceId))
  { id: i.InstanceId, volumes: attached.count(), gib: attached.sum(.Size) }
```
Type: `list[{ id: string, volumes: int, gib: int }]`. Effects: `ec2.DescribeInstances read ×1; ec2.DescribeVolumes read ×1` (one wave of two). The `for` body has no calls, so it is pure fan-in. A flat summary instead of a per-instance join is just a record: `{ instances: insts.count(), volumes: vols.count(), gib: vols.sum(.Size) }`.

**7. A non-AWS catalog: MCP tools plus an LLM operation** (namespaces `javadocs` and `llm`; `get_javadoc_symbol` has no output schema and is typed `string`; `llm.summarize` is an ordinary catalog operation the author chose to include, so the reduction of each document is a visible effect)

```text
towl 3 "Classes in the latest jackson-databind that involve polymorphic type validation"

ver  = call("javadocs", "get_latest_version", { groupId: "com.fasterxml.jackson.core", artifactId: "jackson-databind" }).result
syms = call("javadocs", "list_javadoc_symbols", { groupId: "com.fasterxml.jackson.core", artifactId: "jackson-databind", version: ver })
         .result.where(.fqn.contains("Polymorphic"))

for s in syms
  doc = call("javadocs", "get_javadoc_symbol", { groupId: "com.fasterxml.jackson.core", artifactId: "jackson-databind",
                                      version: ver, link: s.link })
  { class: s.fqn.after_last("."), summary: call("llm", "summarize", { text: doc }) }
```
Type: `list[{ class: string, summary: string }]`. Effects: `javadocs.get_latest_version ×1; javadocs.list_javadoc_symbols ×1; javadocs.get_javadoc_symbol ×dynamic; llm.summarize ×dynamic` (the two calls in the body are a dependency chain per element). `nodes` in the envelope reports `list_javadoc_symbols out: 618`, `where out: 7`, `for out: 7`.

**8. A read that fails for some elements of a fan-out, and what did not survive**

```text
towl 3 "Top 10 largest S3 objects"

buckets = call("s3", "ListBuckets").Buckets

per = for b in buckets
  objs = call("s3", "ListObjectsV2", { Bucket: b.Name }, { region: b.BucketRegion }).Contents
  for o in objs { bucket: b.Name, key: o.Key, size: o.Size }

per.flatten().top(10, .size)
```
Type: `list[{ rank: int, value: { bucket: string, key: string, size: int } }]`. A bucket whose listing is denied makes `objs` unknown, so the bucket is dropped by the `for`; an object without `Size` is skipped by `top`. Both appear in the envelope and nothing else about the program changes:

```text
losses: [ { node: "for", line: 5, reason: "unknown", count: 1,
            origin: { kind: "error", operation: "s3.ListObjectsV2", code: "AccessDenied", class: "authorization", line: 6 },
            sample: [ { Name: "logs-archive", BucketRegion: "eu-west-1" } ] } ]
```
A bucket whose `BucketRegion` were absent would instead stop the run (class `data`, parameter `region`), because the absent value reached a call (§6.1); and if every bucket were denied, the run would stop with `AccessDenied` rather than return an empty top 10 (§12.4).

---

## 15. Conformance

A conforming processor:

1. rejects any program whose header is not `towl 3`;
2. parses exactly the grammar of §3 with the tolerances of §2 and nothing else, and checks layout as §3.1 requires;
3. resolves every operation, shape, member, error code, and option key against one immutable catalog;
4. assigns exactly one type to every expression by §§4–9, or reports diagnostics;
5. reports all diagnostics found in phases 1–4 in one pass, each with location and a fix;
6. never executes an invalid program and never invokes an operation during validation or preflight;
7. binds and type-checks every `input`, and binds the predefined inputs `now` and `today` (§5), before the first dispatch;
8. dispatches each call exactly once, when its dependencies are values, and never retries a mutation that may have been applied (§12.2);
9. admits dynamic waves against budget before dispatching any call in the wave;
10. paginates pageable calls to completion and retries transient failures invisibly;
11. stops on every error except a failed read of class `absence`, `authorization`, or `availability` or a per-call budget overrun, inside a list element that is not the whole wave (§12.3, §12.4), and returns the failure envelope with only complete values;
12. reports every absorbed failure, every loss (§12.6), and every succeeded mutation; never drops an element except by an authored filter or a recorded loss;
13. normalizes result list order;
14. produces identical results and identical losses for identical external responses regardless of scheduling;
15. stops an absent value or an empty list at a call's arguments and options (§6.1), and never dispatches a `mutate` call while losses exist unless the host allows losses (§12.4).

Required tests: one program per example in §14, including example 7 against a JSON-Schema (MCP-style) catalog and example 8 with a denied read and an absent member; a fixture per failed-read class (absent carried into a field, unknown dropping its element from a field, stop outside any element, stop when a whole wave fails, stop for a failed mutation) and for a per-call budget overrun inside and outside an element; a fixture per diagnostic code; a fixture per error class exercising the failure envelope; a fixture per absence rule of §9.2 (carry into a record field, drop at `for`/list literal, skip at each path aggregator and `group`, undecided `where` including `!` and De Morgan equivalence, absent argument stop, losses before a mutation); a scheduling-permutation test demonstrating invariant 14, including example 6's two independent calls; a fixture showing a `list[list[T]]` diagnostic for `collect(.Instances)` versus `flat(.Instances)`.

---

## 16. Informative: rationale and related work

- **Why v3 replaced v2.** v2 encoded cardinality in 37 operator names and needed 156 tagged node kinds because JSON carries no inference and a JSON object can be data or an expression. v3 infers types from a textual surface, keeps one binder, and moves streams, coverage, and error unions out of the type system: pagination is the processor's, partiality is the failure envelope's, and errors stop the program.
- **Effects at external calls only; opportunistic, confluent evaluation** follow Quasar (Mell et al., 2026) and λ^O/Opal (Mell et al., OOPSLA 2025). The same work's observation that order must be data, not statement position, is taken all the way: the only ordering between calls is a data dependency.
- **A tiny grammar amenable to constrained decoding and grammar-level capability control**, keyword arguments, and linear left-to-right authoring follow Pel (Mohammadi, 2025).
- **Paths and closed predicates** follow the AWS CLI's JMESPath usage, retyped against catalog shapes so that flatten-vs-group mistakes are type errors.
- **Monoid aggregators with the SQL grouping rule** are carried over from TOWL v1.
- **Architecturally enforced role separation** (author / processor / reviewer) follows Waites, *Artificial Organisations* (2026); the composition-as-morphisms view and product/coproduct joins (`group`/`concat`) echo the same author's *plumbing*.
- **Provider neutrality was proven on TOWL v1** by a Kotlin implementation over MCP tool catalogs (Spring AI + Bedrock; javadoc tools): the agent's whole tool belt was three tools — a capability search that returns the language guide plus matching operations, validate, and run — and every data-fetching call happened inside the interpreter at zero model tokens. v3 keeps that shape: namespaces instead of AWS services, JSON-Schema-to-type mapping in the catalog, `string`/`json` for untyped tool output, per-node element accounting in the envelope, and the string functions (`after_last`) those tasks needed; bounding long documents became an explicit `llm.summarize` catalog operation rather than a hidden truncation.
- **What the first live v3 trials taught** (Kotlin implementation, javadoc MCP tools, one task, several runs). The text form failed twice on habits, not semantics: `;` between block bindings, and a `.result` wrapper generalized from two operations to a third that returns bare text. The structured form (§3.2) with JSON parameters removed the escaping class of error entirely. An authoring guide that named an operation the catalog lacked (`llm.summarize`, disabled for the test) sent the agent into a search loop that only ended when the helper began reporting *unmatched* capabilities and the guide was generated from the catalog. A diagnostic that named the symptom ("`summarize` is not a function") prolonged the same loop; naming the cause ("`llm` is not a namespace") ended it. Hence the rules in §11 and the host requirements in the Code Mode specification.
- **What the first live AWS trial taught** (Python implementation, "summarize the top 5 largest buckets in all regions"). The agent found `cloudwatch.GetMetricStatistics` and wrote the fan-out correctly on the first try, then lost four turns to three gaps that were the language's, not its own: it needed "four days ago" and computed the dates in a shell because there was no way to say it (hence `now` as a well-known input and §9.4); it wrapped numbers in `{ n: x }` to reach `.max(.n)` (hence the scalar-list rule in §9.1); and it ranked the result outside the program because "top 5" was inexpressible (hence `top`/`bottom`, which order by a named key and so keep the bag semantics). A fourth loss came from the parameter relaxation stopping at the top level: `Value: b.Name` inside a `Dimensions` list was a hard type error with no fix. The lesson generalizes: every time the agent has to leave the language to finish the task, the result leaves the effect table with it.
- **A silent wrong answer is worse than a stop** (second live AWS trial, same task). The program validated, ran, and returned a plausible top-5 — with every bucket in `us-east-1`, because `s3.ListBuckets` leaves `BucketRegion` absent unless the request carries a parameter, and the program had written `b.BucketRegion.or("us-east-1")`. Seven defaults fired, the metric queries went to the wrong region, and the agent reported "all buckets are in us-east-1" as a finding. Nothing in the language had been violated; the envelope simply did not show that the values were manufactured. The first response was to account for defaults in the envelope and warn when a tolerated call was defaulted; the next revision removed the default operator altogether (below). The same run lost a turn to `ExtendedStatistics: []` — the author took a non-nullable optional list for a required one — hence required-parameter marks in the schema rendering and the empty-list-parameter error.
- **The guide can teach the bug** (agent evaluation with `claude -p`, eight tasks, no credentials). Replaying the top-5-buckets task showed the two bad defaults, `b.Name.or("")` and `b.BucketRegion.or("us-east-1")`, in the agent's *first draft*, before any diagnostic: the guide had said "a member typed `T | Null` must go through `?.` or `.or(...)` before use", and the agent complied. Rewording it — pass nullable members to parameters as they are, keep them nullable in results, default only when the default is the documented meaning of absence — changed the next run's program to `GetBucketLocation(...).LocationConstraint.or("us-east-1")`, which is exactly that case. Two more findings from the same trial: the unpaginated `ListBuckets` request form omits `BucketRegion` (the runner now forces the paginated form), and an undeclared `now` cost a turn until the `names.undefined` fix said to add `input now: timestamp` — and, since it kept costing a turn with the fix in place, `now` and `today` later became predefined (§5). Authoring guidance is part of the language's conformance surface; it should be tested the way the checker is.
- **Why the binder is `for x in xs`, bodies are laid out, calls are `call("ns", "op", …)`, and `.or` is gone** (third revision of the v3 surface, after the trials above). The `each(x => body)` form borrowed the lambda shape while binding nothing but a name; agents brought lambda habits with it (arrows in other argument positions, function bodies where a path was wanted). A postfix `map@x { … }` fixed that but left braces meaning two things (record or block) and put every binder's name after the thing it named. `for x in xs` puts both names first, has the strongest prior of any shape for "one body per element", and — because a block always ends at its result — lets bodies be delimited by indentation that the processor checks rather than parses, so braces mean records only and the program and every body are the same construct. Method-style calls on a namespace made every service name a reserved word and every misspelled receiver "not a function"; making the namespace and operation string arguments of a built-in `call` turns both into catalog lookups with nearest-name fixes and frees the program's namespace. `.or(d)` was removed rather than accounted for: every one of its uses in the trials was a wrong answer waiting to happen, its correct uses (`LocationConstraint` absent meaning `us-east-1`) are provider facts that belong in the catalog, and once it was gone the two remaining reasons to want it — comparing an optional number and passing an optional value to a required parameter — were better served by making comparisons Null-tolerant and by the argument relaxation that already existed. (Both were later replaced when nullable types were removed; see "Why nullability left the type system".)
- **Why there is no `after`.** Earlier drafts had a call option `after: name` — dispatch only once a binding has resolved, value unused — as the way to order two mutations that share no data. It was removed because an author has no way to know when it is needed (nothing in a task says "and these two must be sequenced"), because every real case of "do B after A" is a data dependency once B's arguments are taken from A's result, which is also the only version a reviewer can check, and because a B that uses nothing of A is asking for an ordering the program cannot express and the host's mutation policy already supplies.
- **What the eval taught about layout** (eight tasks through `claude -p` after the `for`/layout revision). First drafts were in the new syntax with no layout errors. Two habits needed accommodation: a `.flatten()` line indented *between* a `for` line and its body, meant for the `for`'s result, which the continuation rule now honors (§3.1), and `.L.empty() == false`, a test compared with a boolean, which now parses as the negated test. A third retry cause — declaring `now` — was removed by predefining it.
- **Exit-and-rewrite with a precise failure envelope** rather than program-level error handling reflects the judgment that an author cannot correctly decide in advance which runtime failures are acceptable, while the processor can classify a failure after it happens and a reviewer can see the resulting losses or an explicitly narrowed resume plan.
- **Why nullability left the type system** (live AWS trial, "top 10 largest S3 objects"). The agent tolerated `AccessDenied` on `s3.ListObjectsV2` inside a per-bucket fan-out, which made `Contents` a `list[s3.Object] | Null`, and then spent six validate cycles trying to tag each object with its bucket: `objs?.project(…)` was not in the grammar, `for` refused a nullable list, `where(.objs.present())` did not narrow the type, and `flat` refused a nullable path. It finished by removing the `tolerate`, making the program less robust to get it to type-check; the only form that worked, `[resp].compact().flat(.Contents)`, was a default operator in disguise. Adding `?.` before methods would have fixed that task and left the concept in place for the next one. Instead, absence became a runtime fact: a function of an absent value is absent, a list cannot hold one, so the element is dropped, and every drop is logged — a `Witherable` traversal in a `Writer` of a bag of losses, which is order-independent and so keeps evaluation confluent (§12.1). The agent's first draft of that task is now the correct program (§14, example 8). Two lessons of earlier trials are kept as stops rather than drops. An absent argument stops, because dropping the seven buckets whose `BucketRegion` was absent would have produced the same plausible wrong population as defaulting them; and a mutation does not run while losses exist, because a loss from an exclusion list widens a mutation instead of narrowing it. Predicates became three-valued at the same time, which makes `!(.Size > 50)` and `.Size <= 50` agree — under the two-valued `Null`-is-false rule they did not. The price is that a successful result may be complete only for its surviving elements; `losses` is always present in the envelope, and hosts are required to show it with the value (§13.2).
- **Why there is no `tolerate`** (after the absence revision). `tolerate: [codes]` asked the author to predict, per call, which provider error codes a run would meet — a list authors copied from documentation, where spellings vary (`PermanentRedirect` was refused as `other`; `AccessDenied` was accepted only with an "unmodeled code" warning). With absence already a runtime fact reported as losses, the declaration added nothing but a way to get it wrong: the processor classifies the failure after it happens. Two distinctions from `tolerate` were kept. "Does not exist" (`absence`) and "could not be read" (`authorization`, `availability`) are different answers, so the first becomes a `null` field and the second drops its element — a bucket whose policy is denied is never reported as having no policy. And a wave in which *every* element is denied or unavailable stops, because a failure that affects everything means the credentials or the program are wrong, and an `ok` result with zero survivors is the silent failure the language is built to prevent.
- **Why there is no `project`** (after the `tolerate` removal). `project` meant something else to developers, and it had become redundant: once absent values drop their element, `project(.path)` and `collect(.path)` had the same type and the same behavior, and `project({ … })` was a `for` with a record body under another name. Removing it leaves one form per concept, and the `for` form names every value it reads, which is how an element is tagged with an enclosing value — the step the agent could not find in the first top-10-objects trial.
- **Why a per-call budget inside a fan-out is a loss** (third top-10-objects trial). The first program was correct and validated; the run stopped because one CloudTrail bucket had more than 100,000 objects, and the agent excluded the bucket by name, reran, and reported the exclusion — by hand, what a loss does. Making that overrun a loss with reason `budget` returns the same answer in one run, fetches the other buckets once, names the bucket that was not scanned, and says which limit would include it; a run-wide budget, an overrun with no element to drop, and an overrun on every element still stop.
