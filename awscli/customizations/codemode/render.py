"""Typed rendering and validation report (TOWL_SPEC.md §13.1, CODE_MODE-SPEC.md §4.2)."""

from __future__ import annotations

from collections import defaultdict


def effect_text(site) -> str:
    mult = f"×{site.static_width}" if site.static_width is not None else "×dynamic"
    parts = [site.op.id, site.op.effect]
    if site.op.paged:
        parts.append("paged")
    parts.append(mult)
    if site.tolerate:
        parts.append("tolerate " + ",".join(site.tolerate))
    return " ".join(parts)


def typed(checked) -> str:
    """The source with inferred types and effects as trailing comments: the reviewer's artifact."""
    notes = defaultdict(list)
    for inp in checked.program.inputs:
        notes[inp.pos.line].append(f"input {inp.name}: {inp.type}")
    for b in checked.program.bindings:
        notes[b.pos.line].append(f"{b.name}: {checked.binding_types.get(b.name)}")
    for b in _block_bindings(checked):
        notes[b.pos.line].append(f"{b.name}: {checked.type_of(b.expr)}")
    for site in checked.effects:
        notes[site.call.pos.line].append(effect_text(site))
    for node in _for_nodes(checked):
        w = checked.waves[id(node)]
        notes[node.pos.line].append(f"wave ×{w if w is not None else 'dynamic'}")
    notes[checked.program.result.pos.line].append(f"result: {checked.result_type}")
    lines = checked.source.splitlines()
    width = min(max((len(l) for l in lines), default=0), 88) + 2
    out = []
    for i, l in enumerate(lines, 1):
        n = notes.get(i)
        if not n:
            out.append(l)
        else:
            pad = l.ljust(width) if len(l) + 2 <= width else l + "  "
            out.append(pad + "// " + "; ".join(n))
    return "\n".join(out)


def _block_bindings(checked):
    from .syntax import BlockE, ExprArg, LambdaArg, ListE, Member, MethodCall, OpCall, RecordE

    found = []

    def walk(x):
        if isinstance(x, BlockE):
            for b in x.bindings:
                found.append(b)
                walk(b.expr)
            walk(x.result)
        elif isinstance(x, MethodCall):
            walk(x.target)
            for a in x.args:
                if isinstance(a, ExprArg):
                    walk(a.expr)
                elif isinstance(a, LambdaArg):
                    walk(a.body)
        elif isinstance(x, Member):
            walk(x.target)
        elif isinstance(x, RecordE):
            for _, v in x.fields:
                walk(v)
        elif isinstance(x, ListE):
            for v in x.items:
                walk(v)
        elif isinstance(x, OpCall):
            walk(x.params)

    for b in checked.program.bindings:
        walk(b.expr)
    walk(checked.program.result)
    return found


def _for_nodes(checked):
    from .syntax import BlockE, ExprArg, LambdaArg, ListE, Member, MethodCall, OpCall, RecordE

    found = []

    def walk(x):
        if isinstance(x, MethodCall):
            if x.name == "for" and id(x) in checked.waves:
                found.append(x)
            walk(x.target)
            for a in x.args:
                if isinstance(a, ExprArg):
                    walk(a.expr)
                elif isinstance(a, LambdaArg):
                    walk(a.body)
        elif isinstance(x, Member):
            walk(x.target)
        elif isinstance(x, RecordE):
            for _, v in x.fields:
                walk(v)
        elif isinstance(x, ListE):
            for v in x.items:
                walk(v)
        elif isinstance(x, BlockE):
            for b in x.bindings:
                walk(b.expr)
            walk(x.result)
        elif isinstance(x, OpCall):
            walk(x.params)

    for b in checked.program.bindings:
        walk(b.expr)
    walk(checked.program.result)
    return found


def report(checked) -> dict:
    return {
        "status": "valid",
        "result_type": str(checked.result_type),
        "inputs": [{"name": i.name, "type": str(i.type)} for i in checked.program.inputs],
        "bindings": [{"name": b.name, "type": str(checked.binding_types.get(b.name)), "line": b.pos.line} for b in checked.program.bindings],
        "effects": [e.to_dict() for e in checked.effects],
        "mutations": checked.mutations,
        "regions": sorted({_option_literal(e.call, "region") for e in checked.effects} - {None}),
        "warnings": [d.to_dict() for d in checked.warnings],
        "typed": typed(checked),
    }


def _has_option(call, key):
    from .syntax import RecordE

    return isinstance(call.options, RecordE) and any(k == key for k, _ in call.options.fields)


def _option_literal(call, key):
    from .syntax import Lit, RecordE

    if isinstance(call.options, RecordE):
        for k, v in call.options.fields:
            if k == key and isinstance(v, Lit):
                return v.value
    return None
