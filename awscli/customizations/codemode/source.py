"""Program source resolution (CODE_MODE-SPEC.md §2): text form or structured form, from inline text,
file://, or stdin. The only host repair is stripping one Markdown fence (TowlService does it)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from .syntax import Diagnostic, TowlError


@dataclass(frozen=True)
class PlanSource:
    text: str
    origin: str


def load_plan_source(value, stdin=None) -> PlanSource:
    if value is None:
        raise TowlError([Diagnostic("error", "input", "source.missing", None, "--plan is required: inline program text, inline JSON, file://path, or -")])
    stdin = stdin or sys.stdin
    stripped = value.lstrip()
    if stripped.startswith(("towl", "{", "```")):
        return PlanSource(value, "inline")
    if value == "-":
        return PlanSource(stdin.read(), "stdin")
    if value.startswith(("file://", "fileb://")):
        prefix = "fileb://" if value.startswith("fileb://") else "file://"
        path = os.path.expanduser(value[len(prefix):])
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            raise TowlError([Diagnostic("error", "input", "source.unreadable", None, f"cannot read {path}: {e}")])
        return PlanSource(data.decode("utf-8"), path)
    raise TowlError([Diagnostic("error", "input", "source.unrecognized", None,
                                "unrecognized --plan source; use program text starting with 'towl 3', a structured JSON object, file://path, or - for stdin")])


def load_inputs(values) -> dict:
    """``--input name=value`` bindings: JSON literal, @file.json, or @file.json:<jmespath>."""
    import json

    out = {}
    # argparse `append` + `nargs` yields a list of lists when the flag is repeated
    flat = [x for v in (values or ()) for x in (v if isinstance(v, list) else [v])]
    for item in flat:
        if "=" not in item:
            raise TowlError([Diagnostic("error", "input", "input.syntax", None, f"--input must be name=value, got '{item}'")])
        name, raw = item.split("=", 1)
        if raw.startswith("@"):
            spec = raw[1:]
            path, _, expr = spec.partition(":")
            try:
                with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                raise TowlError([Diagnostic("error", "input", "input.unreadable", None, f"cannot read input '{name}' from {path}: {e}")])
            if expr:
                import jmespath
                doc = jmespath.search(expr, doc)
            out[name] = doc
        else:
            try:
                out[name] = json.loads(raw)
            except json.JSONDecodeError:
                out[name] = raw  # a bare string literal
    return out
