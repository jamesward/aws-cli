"""The stateless phase pipeline: source -> (structured form ->) text -> parse -> check -> run."""

from __future__ import annotations

import json

from . import program_form
from .check import Checker
from .syntax import Diagnostic, Parser, TowlError


def strip_fence(src: str) -> str:
    t = src.strip()
    if not t.startswith("```"):
        return src
    lines = t.splitlines()
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines[1:])


class TowlService:
    def __init__(self, catalog, max_width=200, max_depth=2):
        self.catalog = catalog
        self.max_width = max_width
        self.max_depth = max_depth

    def parse(self, text: str):
        return Parser(text, self.catalog.namespaces, shape_lookup=getattr(self.catalog, "shape_type", None)).program()

    def check_text(self, text: str):
        return Checker(self.catalog, self.max_width, self.max_depth).check(self.parse(text), text)

    def validate(self, source: str):
        """Accept the text form or the structured JSON form; return Checked or raise TowlError.

        For the structured form the rendered text is attached to the exception (``exc.source``) and every
        diagnostic carries ``where``.
        """
        text = strip_fence(source)
        if not program_form.is_structured(text):
            return self.check_text(text)
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            raise TowlError([Diagnostic("error", "syntax", "syntax.json", None, f"the structured program is not valid JSON: {e.msg} at line {e.lineno}")])
        if not isinstance(doc, dict):
            raise TowlError([Diagnostic("error", "syntax", "syntax.json", None, "the structured program must be a JSON object")])
        structural = program_form.structural_diagnostics(doc, self.catalog.namespaces)
        if any(d.severity == "error" for d in structural):
            raise TowlError(structural)
        rendered = program_form.render(doc)
        try:
            checked = self.check_text(rendered.source)
        except TowlError as e:
            for d in e.diagnostics:
                d.where = rendered.where_by_line.get(d.pos.line) if d.pos else "program"
            e.source = rendered.source
            raise
        for d in checked.warnings:
            d.where = rendered.where_by_line.get(d.pos.line) if d.pos else "program"
        return checked
