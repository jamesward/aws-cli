"""Deterministic, bounded lexical schema discovery over local botocore models."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

from . import types as T
from .aws_catalog import OperationSpec, Unknown

_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[a-z0-9]+")
# AWS read verbs are interchangeable from a capability standpoint: "list regions" must find DescribeRegions.
_READ_VERBS = {"list", "describe", "get", "search", "enumerate", "show", "find", "read", "fetch", "query"}
_SYNONYMS = {
    **{v: _READ_VERBS - {v} for v in _READ_VERBS},
    "delete": {"remove", "terminate"},
    "instance": {"ec2", "server"},
    "volume": {"disk", "ebs"},
    "identity": {"caller", "account", "sts"},
    "log": {"logs", "cloudwatch"},
}
# services most tasks are about; a small prior so "list regions" ranks ec2.DescribeRegions above sso-admin.ListRegions
_CORE_SERVICES = {"ec2", "s3", "iam", "sts", "lambda", "cloudwatch", "logs", "rds", "dynamodb", "sqs", "sns", "cloudformation",
                  "ecs", "eks", "route53", "kms", "secretsmanager", "ssm", "cloudtrail", "elbv2", "autoscaling", "organizations", "account", "ce"}
# words that carry no capability information in a query
_STOPWORDS = {"aws", "amazon", "all", "my", "the", "a", "an", "of", "in", "for", "with", "and", "each", "every", "any"}

_TASK_HINTS = {
    "ec2:describe-instances": {"ec2", "describe", "running", "instances", "inventory", "servers", "list", "compute"},
    "ec2:describe-volumes": {"volumes", "disks", "ebs", "inventory", "list"},
    "sts:get-caller-identity": {"caller", "identity", "account", "whoami"},
    "logs:describe-log-groups": {"logs", "groups", "cloudwatch", "list"},
}


def text_doc(doc):
    return " ".join(html.unescape(_TAG.sub(" ", doc or "")).split())


def tokens(text):
    return _WORD.findall(text.lower())


def _expand(words):
    out = set(words)
    for word in words:
        out.update(_SYNONYMS.get(word, ()))
    return out


@dataclass(frozen=True)
class Match:
    operation: Any
    tier: int
    score: int
    matched: tuple


class SchemaService:
    def __init__(self, catalog, registry=None):
        self.catalog = catalog
        self.registry = registry

    def search(self, queries, service=None, limit=4, brief=False, full=False, depth=2):
        if not isinstance(queries, (list, tuple)):
            queries = [queries]
        limit = max(1, min(int(limit), 20))
        depth = max(0, min(int(depth), 5))
        summaries = self._summaries(service)
        results = []
        for query in queries:
            query_summaries = summaries
            if service is None and hasattr(self.catalog, "complete_service_summaries"):
                query_terms = set(tokens(query))
                named_services = {
                    candidate for candidate in self.catalog.services()
                    if set(tokens(candidate)) and set(tokens(candidate)) <= query_terms
                }
                if named_services:
                    merged = {summary.id: summary for summary in summaries}
                    for named_service in named_services:
                        for summary in self.catalog.complete_service_summaries(named_service):
                            merged[summary.id] = summary
                    query_summaries = tuple(merged.values())
            exact = self._exact(query)
            if exact is not None:
                matches = [Match(exact, 0, 10000, tuple(tokens(query)))]
            else:
                # Phase 1 ranks the shipped auto-prompt command index: no service/paginator models.
                coarse = self._sorted_matches(
                    m for op in query_summaries for m in [self._rank(query, op)] if m
                )
                pool = coarse[:max(8, limit * 2)]
                # Phase 2 loads documentation only for the bounded pool, still without paginators.
                enriched = [
                    Match(self.catalog.enrich_summary(m.operation), m.tier, m.score, m.matched)
                    for m in pool
                ]
                ranked = self._sorted_matches(
                    m for candidate in enriched
                    for m in [self._rank(query, candidate.operation)] if m
                )
                # Phase 3 fully resolves only final candidates (paginator/cardinality/shapes).
                matches = []
                for candidate in ranked:
                    resolved = self.catalog.resolve(
                        candidate.operation.service, candidate.operation.operation
                    )
                    if isinstance(resolved, OperationSpec):
                        matches.append(Match(resolved, candidate.tier, candidate.score, candidate.matched))
                    if len(matches) >= limit:
                        break
            results.append({
                "query": query,
                "matches": [self.signature(m.operation, depth, summary=brief and not full, match=m)
                            for m in matches],
                "diagnostics": [] if matches else [
                    "No lexical match. Refine the capability terms (service/resource plus verb) "
                    "and retry `aws codemode operation search`; do not request schema until an exact identifier is found."
                ],
            })
        unmatched = [x["query"] for x in results if not x["matches"]]
        response = {
            "queries": results,
            "count": sum(len(x["matches"]) for x in results),
            "mode": "brief" if brief and not full else ("full" if full else "signature"),
            "depth": depth,
            "unmatched": unmatched,
        }
        if unmatched:
            response["note"] = ("No operation matches: " + "; ".join(unmatched) + ". Refine the capability terms (service or resource plus verb) once; "
                                "if it still does not match, the capability does not exist in this catalog — design the program without it.")
        return response

    def exact(self, identifiers, depth=5):
        """Resolve only exact service:operation identifiers; never perform keyword fallback."""
        if not isinstance(identifiers, (list, tuple)):
            identifiers = [identifiers]
        depth = max(0, min(int(depth), 5))
        results = []
        for identifier in identifiers:
            diagnostics, matches = [], []
            if not isinstance(identifier, str) or not any(sep in identifier for sep in ".:"):
                diagnostics.append(
                    f"Exact schema identifier '{identifier}' must be service.Operation (or service:operation); "
                    "discover operations with `aws codemode operation search <queries...>`."
                )
            else:
                sep = "." if "." in identifier else ":"
                service, operation = identifier.split(sep, 1)
                resolved = self.catalog.resolve(service, operation)
                shape = None if isinstance(resolved, OperationSpec) else self._shape(service, operation)
                if isinstance(resolved, OperationSpec):
                    matches.append(self.signature(resolved, depth, summary=False))
                elif shape is not None:
                    matches.append({"shape": f"{service}.{operation}", "fields": T.describe(shape, 1), "shapes": named_shapes([shape], depth)})
                else:
                    suggestions = list(resolved.suggestions) if isinstance(resolved, Unknown) else []
                    message = f"Unknown exact operation or shape '{identifier}'."
                    if suggestions:
                        message += " Candidates: " + ", ".join(suggestions)
                    message += " Use `aws codemode operation search` to discover the exact identifier."
                    diagnostics.append(message)
            results.append({"query": identifier, "matches": matches, "diagnostics": diagnostics})
        return {
            "queries": results,
            "count": sum(len(item["matches"]) for item in results),
            "mode": "exact",
            "depth": depth,
        }

    def _shape(self, service, name):
        """A named record shape such as `cloudwatch.Dimension`, when the catalog can resolve it."""
        fn = getattr(self.catalog, "shape_type", None)
        if fn is None:
            return None
        t = fn(service, name)
        return t if isinstance(t, T.TRecord) else None

    def _summaries(self, service=None):
        if hasattr(self.catalog, "operation_summaries"):
            return self.catalog.operation_summaries(service)
        # Catalog-neutral fallback used by injected/fake catalogs.
        return tuple(self.catalog.operations(service))

    @staticmethod
    def _sorted_matches(matches):
        return sorted(
            matches,
            key=lambda m: (
                m.tier, -m.score, _effect_rank(m.operation),
                len(m.operation.operation), m.operation.id,
            ),
        )

    def list_service(self, service):
        summaries = self._summaries(service)
        return [{
            "service": op.service,
            "operation": op.operation,
            "description": _first_sentence(
                self.catalog.enrich_summary(op).description
                if hasattr(self.catalog, "enrich_summary") else op.description
            ),
        } for op in summaries]

    def _exact(self, query):
        if not any(sep in query for sep in ".:") or " " in query:
            return None
        sep = "." if "." in query else ":"
        service, op = query.split(sep, 1)
        resolved = self.catalog.resolve(service, op)
        return resolved if isinstance(resolved, OperationSpec) else None

    def _rank(self, query, op):
        q = [w for w in tokens(query) if w not in _STOPWORDS] or tokens(query)
        if not q:
            return None
        name = set(tokens(op.operation.replace("-", " ")))
        svc = set(tokens(op.service))
        service = svc | set(tokens(getattr(op, "service_full_name", "")))
        first = set(tokens(_first_sentence(op.description)))
        all_doc = set(tokens(op.description))
        qset = set(q)
        hints = _TASK_HINTS.get(op.id, set()) | _READ_VERBS  # a hinted task matches whatever read verb the query used

        def hits(words, pool):
            """query words satisfied by the pool, exactly or through a synonym (read verbs are one class)"""
            exact = {w for w in words if w in pool}
            loose = {w for w in words - exact if _SYNONYMS.get(w, set()) & pool}
            return exact, loose

        n_exact, n_loose = hits(qset, name | service)
        rest = qset - n_exact - n_loose
        f_exact, f_loose = hits(rest, first)
        rest2 = rest - f_exact - f_loose
        d_exact, d_loose = hits(rest2, all_doc)
        if qset <= hints or not rest:
            tier = 1
        elif not rest2:
            tier = 2
        elif not (rest2 - d_exact - d_loose):
            tier = 3
        elif n_exact | n_loose | f_exact | f_loose | d_exact | d_loose:
            tier = 4
        else:
            # Bounded prefix/acronym fallback.
            op_compact = "".join(tokens(op.operation))
            if not any(len(w) >= 3 and (w in op_compact or op_compact.startswith(w[:4])) for w in q):
                return None
            tier = 5
        # a read verb in the query matched by any read verb in the name counts as exact: the noun carries the meaning
        verb_loose = {w for w in n_loose if w in _READ_VERBS}
        exact_name = len(qset & name) + len(qset & svc) + len(verb_loose)  # naming the service is as strong as naming the verb
        loose_name = len(n_loose - verb_loose)
        coverage = len(qset) - len(rest2 - d_exact - d_loose)
        doc_hits = len(f_exact | f_loose | d_exact | d_loose)
        score = exact_name * 20 + loose_name * 15 + coverage * 5 + doc_hits + (5 if getattr(op, "paged", False) else 0) + (
            60 if qset <= hints else 0
        ) + (10 if op.service in _CORE_SERVICES else 0)
        matched = tuple(sorted(n_exact | n_loose | f_exact | f_loose | d_exact | d_loose))
        return Match(op, tier, score, matched)

    def signature(self, op, depth=2, summary=False, match=None):
        """TOWL v3 view of one operation: exact id, parameter and (merged) result types, effect, paging, codes."""
        out = op.output
        list_members = [k for k, v in out.fields.items() if isinstance(v, T.TList)] if isinstance(out, T.TRecord) else []
        base = {
            "operation": op.id,
            "cli": f"{op.service}:{op.operation}",
            "description": text_doc(op.description),
            "params": T.describe(op.input, 1) if op.input is not None else "{}",
            "returns": returns_text(op.output),
            "effect": op.effect,
            "paged": op.paged,
            "note": (
                "the call's value IS the result (a bare value); there is no wrapper member" if T.is_scalar(out) or out is T.JSON
                else f"access members of the returned record, e.g. .{(list_members or list(out.fields))[0]}" if isinstance(out, T.TRecord) and out.fields
                else None
            ),
        }
        required = getattr(op, "required", None)
        if op.input is not None and required is not None:
            base["required"] = sorted(k for k in op.input.fields if k in required)
        if not summary:
            base["errorCodes"] = sorted(op.error_codes)
            base["runtimeOwned"] = list(op.runtime_owned)
            base["paramsSchema"] = type_schema(op.input, depth) if op.input is not None else {}
            base["returnsSchema"] = type_schema(op.output, depth)
            base["shapes"] = named_shapes([op.input, op.output], depth)
        if match:
            base["match"] = {"tier": match.tier, "score": match.score, "terms": list(match.matched)}
        return base


def returns_text(t):
    if T.is_scalar(t) or t is T.JSON:
        return f"{t}  (a bare value, not a record)"
    return T.describe(t, 1)


def named_shapes(roots, depth=3):
    """Every named record reachable from the roots within depth, as one-level type text (lossless with the params/returns lines)."""
    out = {}

    def walk(t, d):
        if d < 0:
            return
        if isinstance(t, T.TList):
            walk(t.element, d)
        elif isinstance(t, T.TRecord):
            if t.name and t.name in out:
                return
            if t.name:
                out[t.name] = T.describe(t, 1)
            for v in t.fields.values():
                walk(v, d - 1)

    for r in roots:
        if r is not None:
            walk(r, depth)
    return out


def type_schema(t, depth=3, _seen=None):
    """Expanded TOWL type as JSON, bounded by depth; named shapes beyond the depth are referenced by name."""
    seen = set() if _seen is None else _seen
    if isinstance(t, T.TList):
        return {"list": type_schema(t.element, depth, seen)}
    if isinstance(t, T.TRecord):
        if (t.name and t.name in seen) or depth <= 0:
            return {"shape": t.name or "{...}"}
        inner = seen | ({t.name} if t.name else set())
        out = {"shape": t.name} if t.name else {}
        out["fields"] = {k: type_schema(v, depth - 1, inner) for k, v in t.fields.items()}
        if t.optional:
            out["optional"] = sorted(t.optional)  # may be absent at runtime; informational (TOWL §4)
        return out
    return str(t)


def _first_sentence(doc):
    text = text_doc(doc)
    m = re.search(r"(?<=[.!?])\s", text)
    return text[:m.start() + 1] if m else text


def _effect_rank(op):
    effects = set(op.effects)
    if "read" in effects:
        return 0
    if "unknown" in effects:
        return 1
    return 2


def render_schema_text(response):
    """Concise, lossless plain text: one block per operation in TOWL type syntax."""
    lines = []
    for q in response["queries"]:
        lines.append(f"# {q['query']}")
        for m in q["matches"]:
            if "shape" in m:
                lines.append(f"{m['shape']} = {m['fields']}")
                for name, fields in m.get("shapes", {}).items():
                    if name != m["shape"]:
                        lines.append(f"    {name} = {fields}")
                continue
            lines.append(f"{m['operation']}  [{m['effect']}{', paged' if m['paged'] else ''}]  (cli: {m['cli']})")
            desc = m.get("description") or ""
            if desc:
                lines.append("  " + _wrap(_first_sentence(desc) if response.get("mode") == "brief" else _lead(desc), 96, "  "))
            lines.append(f"  params:  {m['params']}")
            if "required" in m and m["params"] != "{}":
                req = ", ".join(m["required"]) if m["required"] else "none"
                lines.append(f"  required: {req}   (omit the others unless needed; never pass [])")
            lines.append(f"  returns: {m['returns']}")
            if "?:" in m["returns"] or any("?:" in f for f in m.get("shapes", {}).values()):
                lines.append("  (Name?: T = the provider may leave the member out; an element that needs it is dropped and reported in 'losses')")
            if m.get("note"):
                lines.append(f"  note:    {m['note']}")
            if m.get("errorCodes"):
                lines.append("  errors:  " + ", ".join(m["errorCodes"]))
            if m.get("runtimeOwned"):
                lines.append("  runtime-owned (never authored): " + ", ".join(m["runtimeOwned"]))
            if m.get("shapes"):
                lines.append("  shapes:")
                for name, fields in m["shapes"].items():
                    lines.append(f"    {name} = {fields}")
        for d in q.get("diagnostics", []):
            lines.append("  ! " + d)
        lines.append("")
    if response.get("unmatched"):
        lines.append("unmatched: " + "; ".join(response["unmatched"]))
        lines.append(response.get("note", ""))
    return "\n".join(lines).rstrip() + "\n"


def _lead(text, sentences=3, cap=700):
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(parts[:sentences])
    return out if len(out) <= cap else out[:cap].rsplit(" ", 1)[0] + " …"


def _wrap(text, width, indent):
    words, out, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            out.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return ("\n" + indent).join(out)


def render_service_list_text(service, operations, registry=None):
    lines = [f"{service}: {len(operations)} operations"]
    for op in operations:
        lines.append(f"  {op['service']}.{op['operation']}  {op['description']}")
    return "\n".join(lines) + "\n"
