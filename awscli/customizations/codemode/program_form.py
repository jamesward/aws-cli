"""The structured program form (TOWL_SPEC.md §3.1): the program skeleton as JSON with expression text at
the leaves, rendered to the text form so one parser and checker serve both. Node forms:

  { name, value }                                        any expression (text)
  { name, call, args?, options?, then? }                 an operation call; call is "service.Operation",
                                                          args is real JSON, {"$": "expr"} inside it is a reference
  { name, for: { over, as, bindings, result } }          a fan-out with structured inner bindings
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .syntax import Diagnostic

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CALL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*\.\S+$")  # namespace, then everything after the first "." (TOWL §3.2)


def is_structured(text: str) -> bool:
    return text.lstrip().startswith("{")


def quote_text(s: str) -> str:
    return json.dumps(s)


def literal(v: Any) -> str:
    """JSON params -> TOWL record literal text; {"$": "expr"} becomes the expression text."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return quote_text(v)
    if isinstance(v, list):
        return "[" + ", ".join(literal(x) for x in v) + "]"
    if isinstance(v, dict):
        if len(v) == 1 and "$" in v and isinstance(v["$"], str):
            return v["$"]
        return "{ " + ", ".join(f"{k}: {literal(x)}" for k, x in v.items()) + " }"
    return quote_text(str(v))


class Rendered:
    def __init__(self, source: str, where_by_line: Dict[int, str]):
        self.source, self.where_by_line = source, where_by_line


class _Renderer:
    def __init__(self):
        self.lines: List[str] = []
        self.where: Dict[int, str] = {}

    def add(self, text: str, tag: str):
        for l in text.split("\n"):
            self.lines.append(l)
            self.where[len(self.lines)] = tag


def structural_diagnostics(doc: dict, namespaces) -> List[Diagnostic]:
    out: List[Diagnostic] = []
    known = {"towl", "description", "inputs", "bindings", "result"}
    if doc.get("towl") not in (None, 3, "3"):
        out.append(Diagnostic("error", "syntax", "syntax.version", None, "towl must be 3"))
    if not str(doc.get("result") or "").strip():
        out.append(Diagnostic("error", "syntax", "syntax.noResult", None, "'result' is required: the expression whose value is the answer",
                              "add result, e.g. the name of the last binding or a record { a: x, b: y }"))
    for k in doc:
        if k not in known:
            out.append(Diagnostic("error", "syntax", "syntax.unknownMember", None, f"'{k}' is not a program member",
                                  f"a program has towl, description, inputs, bindings: [{{ name, ... }}], result; put '{k}' inside bindings as {{ name: \"{k}\", value: ... }}"))
    bindings = doc.get("bindings") or []
    if not isinstance(bindings, list):
        out.append(Diagnostic("error", "syntax", "syntax.binding", None, "'bindings' must be a list of { name, ... } objects"))
        bindings = []
    for i, node in enumerate(bindings):
        _node_structural(node, f"binding '{node.get('name', f'#{i}') if isinstance(node, dict) else f'#{i}'}'", namespaces, out)
    return out


def _node_structural(node, tag, namespaces, out):
    if not isinstance(node, dict):
        out.append(Diagnostic("error", "syntax", "syntax.binding", None, f"{tag} must be an object"))
        return
    if not str(node.get("name") or "").strip():
        out.append(Diagnostic("error", "syntax", "syntax.binding", None, f"{tag} has no name"))
    forms = [f for f in ("value", "call", "for") if node.get(f) is not None]
    if len(forms) != 1:
        out.append(Diagnostic("error", "syntax", "syntax.binding", None, f"{tag} must have exactly one of value, call, for; found {forms}"))
    call = node.get("call")
    if call is not None:
        if not isinstance(call, str) or not _CALL.match(call):
            out.append(Diagnostic("error", "syntax", "syntax.binding", None, f"{tag}: call must be service.Operation, got '{call}'"))
        elif namespaces and call.split(".")[0] not in namespaces:
            ns = call.split(".")[0]
            out.append(Diagnostic("error", "catalog", "catalog.unknownNamespace", None, f"{tag}: '{ns}' is not an AWS service in this catalog, so {call} does not exist",
                                  "use service.Operation with an AWS service name (ec2, s3, iam, ...) and an operation from `aws codemode operation search`"))
    if call is None and any(node.get(k) is not None for k in ("args", "options", "then")):
        out.append(Diagnostic("error", "syntax", "syntax.binding", None, f"{tag}: args/options/then belong to a call binding", "add call: \"service.Operation\""))
    if node.get("params") is not None:
        out.append(Diagnostic("error", "syntax", "syntax.binding", None, f"{tag}: 'params' is now 'args'"))
    unknown = [k for k in node if k not in ("name", "value", "call", "args", "params", "options", "then", "for")]
    for old in ("each", "map"):
        if node.get(old) is not None:
            out.append(Diagnostic("error", "syntax", "syntax.binding", None, f"{tag}: '{old}' is now 'for': {{ name, for: {{ over, as, bindings, result }} }}"))
            unknown = [k for k in unknown if k != old]
    if unknown:
        out.append(Diagnostic("error", "syntax", "syntax.binding", None, f"{tag} has unknown members {unknown}",
                              "a binding is { name, value } or { name, call, args?, options?, then? } or { name, for }"))
    m = node.get("for")
    if m is not None:
        if not isinstance(m, dict):
            out.append(Diagnostic("error", "syntax", "syntax.for", None, f"{tag}: for must be an object {{ over, as, bindings, result }}"))
            return
        for k, what in (("over", "the list expression"), ("as", "the element name"), ("result", "the body's value")):
            if not str(m.get(k) or "").strip():
                out.append(Diagnostic("error", "syntax", "syntax.for", None, f"{tag}: for.{k} ({what}) is required"))
        for i, inner in enumerate(m.get("bindings") or []):
            _node_structural(inner, f"binding '{inner.get('name', f'#{i}') if isinstance(inner, dict) else f'#{i}'}' in for '{node.get('name')}'", namespaces, out)


def render(doc: dict) -> Rendered:
    r = _Renderer()
    head = f"towl {doc.get('towl') or 3}"
    if doc.get("description"):
        head += " " + quote_text(str(doc["description"]).replace("\n", " "))
    r.add(head, "header")
    for n, t in (doc.get("inputs") or {}).items():
        r.add(f"input {n}: {t}", f"input '{n}'")
    for i, node in enumerate(doc.get("bindings") or []):
        _render_node(node, r, "", f"binding '{node.get('name', f'#{i}')}'")
    r.add(str(doc.get("result") or ""), "result")
    return Rendered("\n".join(r.lines), r.where)


def _render_node(node: dict, r: _Renderer, indent: str, tag: str):
    head = f"{indent}{node.get('name') or '_'} = "
    if node.get("value") is not None:
        r.add(head + str(node["value"]), tag)
    elif node.get("call") is not None:
        opts = []
        options = node.get("options") or {}
        for k, v in options.items():  # every key is rendered, so the checker reports unknown ones
            if v is not None:
                opts.append(f"{k}: {literal(v)}")
        service, _, operation = str(node["call"]).partition(".")
        args = node.get("args")
        parts = [quote_text(service), quote_text(operation)]
        if args or opts:
            parts.append(literal(args or {}))
        if opts:
            parts.append("{ " + ", ".join(opts) + " }")
        r.add(head + f"call({', '.join(parts)}){node.get('then') or ''}", tag)
    elif node.get("for") is not None:
        e = node["for"]
        r.add(head + f"for {e.get('as')} in {e.get('over')}", tag)
        for i, inner in enumerate(e.get("bindings") or []):
            _render_node(inner, r, indent + "  ", f"binding '{inner.get('name', f'#{i}')}' in for '{node.get('name')}'")
        r.add(f"{indent}  {e.get('result')}", f"result of for '{node.get('name')}'")
    else:
        r.add(head, tag)
