"""TOWL v3 surface syntax (TOWL_SPEC.md §§2–3): lexer, AST, recursive-descent parser.

``call("ns", "Operation", args?, options?)`` is the only effectful form and ``for x in list`` the only
binder; both are recognized at parse time. Blocks (bindings then a result) exist in exactly two places, the
program and a ``for`` body, and are delimited by layout: a body is the lines indented more than its ``for``
line. Because a block ends at its result expression, the parser finds structure without indentation and then
*checks* that the indentation agrees, so every layout mistake is reported as such. Everything else about legality (pure layer, argument kinds,
types) is the checker's job so that one pass reports every problem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from . import types as T


@dataclass(frozen=True)
class Pos:
    line: int
    col: int

    def __str__(self):
        return f"{self.line}:{self.col}"


@dataclass
class Diagnostic:
    severity: str  # error | warning
    phase: str  # syntax | names | catalog | types | effects | input | policy
    code: str
    pos: Optional[Pos]
    message: str
    fix: Optional[str] = None
    type: Optional[str] = None
    where: Optional[str] = None

    def to_dict(self):
        return {
            "severity": self.severity, "phase": self.phase, "code": self.code,
            "line": self.pos.line if self.pos else None, "col": self.pos.col if self.pos else None,
            "message": self.message, "fix": self.fix, "type": self.type, "where": self.where,
        }

    def __str__(self):
        return f"{self.severity}[{self.code}] {self.pos or '-'}: {self.message}" + (f" (fix: {self.fix})" if self.fix else "")


class TowlError(Exception):
    def __init__(self, diagnostics):
        self.diagnostics = list(diagnostics)
        super().__init__("; ".join(str(d) for d in self.diagnostics))


# ── AST ───────────────────────────────────────────────────────────────────────


class Expr:
    __slots__ = ("pos",)


class Lit(Expr):
    __slots__ = ("value",)

    def __init__(self, value, pos):
        self.value, self.pos = value, pos


class Ref(Expr):
    __slots__ = ("name",)

    def __init__(self, name, pos):
        self.name, self.pos = name, pos


class Implicit(Expr):
    """The implicit element of an Express function; root of every ``.a.b`` path."""

    __slots__ = ()

    def __init__(self, pos):
        self.pos = pos


class RecordE(Expr):
    __slots__ = ("fields",)

    def __init__(self, fields, pos):
        self.fields, self.pos = fields, pos  # list of (name, expr)


class ListE(Expr):
    __slots__ = ("items",)

    def __init__(self, items, pos):
        self.items, self.pos = items, pos


class BlockE(Expr):
    __slots__ = ("bindings", "result")

    def __init__(self, bindings, result, pos):
        self.bindings, self.result, self.pos = bindings, result, pos


class Member(Expr):
    __slots__ = ("target", "name", "null_safe")

    def __init__(self, target, name, null_safe, pos):
        self.target, self.name, self.null_safe, self.pos = target, name, null_safe, pos


class MethodCall(Expr):
    __slots__ = ("target", "name", "args", "_multiline", "_layout")

    def __init__(self, target, name, args, pos):
        self.target, self.name, self.args, self.pos = target, name, args, pos


class OpCall(Expr):
    """``call("ns", "Op", args?, options?)``; ``params`` is the args record (an empty record when omitted)."""

    __slots__ = ("namespace", "operation", "params", "options")

    def __init__(self, namespace, operation, params, options, pos):
        self.namespace, self.operation, self.params, self.options, self.pos = namespace, operation, params, options, pos


class Arg:
    __slots__ = ("pos",)


class ExprArg(Arg):
    __slots__ = ("expr",)

    def __init__(self, expr, pos):
        self.expr, self.pos = expr, pos


class PredArg(Arg):
    __slots__ = ("pred",)

    def __init__(self, pred, pos):
        self.pred, self.pos = pred, pos


class LambdaArg(Arg):
    """The ``for x in list`` binder: ``x`` names the element inside ``body`` (a BlockE or a single expression)."""

    __slots__ = ("param", "body")

    def __init__(self, param, body, pos):
        self.param, self.body, self.pos = param, body, pos


class Pred:
    __slots__ = ("pos",)


class Cmp(Pred):
    __slots__ = ("op", "left", "right")

    def __init__(self, op, left, right, pos):
        self.op, self.left, self.right, self.pos = op, left, right, pos


class InP(Pred):
    __slots__ = ("left", "right")

    def __init__(self, left, right, pos):
        self.left, self.right, self.pos = left, right, pos


class AndP(Pred):
    __slots__ = ("terms",)

    def __init__(self, terms, pos):
        self.terms, self.pos = terms, pos


class OrP(Pred):
    __slots__ = ("terms",)

    def __init__(self, terms, pos):
        self.terms, self.pos = terms, pos


class NotP(Pred):
    __slots__ = ("term",)

    def __init__(self, term, pos):
        self.term, self.pos = term, pos


class TestP(Pred):
    """present() absent() contains(s) starts_with(s) ends_with(s)"""

    __slots__ = ("operand", "fn", "arg")

    def __init__(self, operand, fn, arg, pos):
        self.operand, self.fn, self.arg, self.pos = operand, fn, arg, pos


class QuantP(Pred):
    """operand.any(pred) / operand.all(pred) over a list-typed operand."""

    __slots__ = ("operand", "all", "inner")

    def __init__(self, operand, all_, inner, pos):
        self.operand, self.all, self.inner, self.pos = operand, all_, inner, pos


@dataclass
class Binding:
    name: str
    expr: Expr
    pos: Pos


@dataclass
class InputDecl:
    name: str
    type: T.Type
    pos: Pos


@dataclass
class Program:
    description: Optional[str]
    inputs: List[InputDecl]
    bindings: List[Binding]
    result: Expr
    warnings: List[Diagnostic] = field(default_factory=list)  # parser normalizations (TOWL §3)


EXPR_FNS = {"flat", "flatten", "where", "distinct", "concat", "group", "single"}
AGG_FNS = {"count", "sum", "min", "max", "avg", "collect", "any", "all", "top", "bottom"}
STR_FNS = {"after_last", "before_first", "lower", "upper"}
TIME_FNS = {"minus_days", "minus_hours", "minus_minutes", "start_of_day", "start_of_month", "date"}
TEST_FNS = {"present", "absent", "empty", "contains", "starts_with", "ends_with"}
BOOL_FNS = {"present", "absent"}  # also value-producing: the only observations of absence (TOWL §9.2)
PRED_ARG_FNS = {"where", "any", "all"}
ALL_FNS = EXPR_FNS | AGG_FNS | STR_FNS | TIME_FNS
FOR_FORM = "the binder is written: for x in xs  then the body: a record on the same line, or bindings and a result on indented lines"
ABSENCE_RULE = ("values may be absent at runtime; no null handling is written: an element that needs an absent value is dropped "
                "and reported under 'losses', and .x.present() / .x.absent() test for absence")
BORROWED_FNS = {"or": "TOWL has no '.or' and needs none: " + ABSENCE_RULE,
               "each": "TOWL has no 'each'; " + FOR_FORM, "map": "TOWL has no 'map'; " + FOR_FORM}
# forms of the earlier nullable revision, read as their TOWL meaning with one warning per kind (TOWL §3)
NORMALIZATIONS = {
    "syntax.nullSafe": "'?.' is read as '.'",
    "syntax.nullCompare": "'== null' / '!= null' are read as .absent() / .present()",
    "syntax.nullType": "'| Null' in a type is dropped",
    "syntax.compact": "'.compact()' does nothing (lists never hold absent values) and is dropped",
}
TYPE_KEYWORDS = {"string", "int", "number", "bool", "timestamp", "Null", "json", "list"}
KEYWORDS = {"towl", "input", "in", "for", "call", "true", "false", "null"} | TYPE_KEYWORDS
CALL_FORM = 'operation calls are written call("service", "Operation", { Param: value }, { region: "..." }); args and options may be omitted'

# ── lexer ─────────────────────────────────────────────────────────────────────

_TOKEN = re.compile(
    r"""(?P<ws>[ \t\r\n;]+)|(?P<comment>//[^\n]*|\#[^\n]*)|(?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
    r"""|(?P<num>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)|(?P<op>\?\.|=>|==|!=|<=|>=|&&|\|\||[()\[\]{}.,:=<>!|@])"""
    r"""|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"""
)


@dataclass(frozen=True)
class Token:
    kind: str  # ident str num op eof
    text: str
    pos: Pos


def lex(src: str) -> List[Token]:
    out, i, line, col = [], 0, 1, 1
    while i < len(src):
        m = _TOKEN.match(src, i)
        if not m:
            fix = None
            if src[i] in "+-*/%":
                fix = "TOWL has no arithmetic operators; use aggregates (.sum(.N), .count(), .avg(.N)) or return the values and compute afterwards"
            raise TowlError([Diagnostic("error", "syntax", "syntax.badChar", Pos(line, col), f"unexpected character {src[i]!r}", fix)])
        text = m.group(0)
        kind = m.lastgroup
        pos = Pos(line, col)
        if kind == "ws" and "\t" in text:
            raise TowlError([Diagnostic("error", "syntax", "syntax.tab", pos, "tab characters are not allowed; indentation is significant and uses spaces",
                                        "indent with 2 spaces per level")])
        if kind not in ("ws", "comment"):
            if kind == "str":
                text = _unescape(text[1:-1])
            out.append(Token(kind, text, pos))
        nl = text.count("\n")
        if nl:
            line += nl
            col = len(text) - text.rfind("\n")
        else:
            col += len(text)
        i = m.end()
    out.append(Token("eof", "", Pos(line, col)))
    return out


def _unescape(s: str) -> str:
    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1).startswith("u") else {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), s)


# ── parser ────────────────────────────────────────────────────────────────────

_CMP = {"==", "!=", "<", "<=", ">", ">="}


class Parser:
    def __init__(self, src: str, namespaces, shape_lookup=None):
        self.t = lex(src)
        self.p = 0
        self.ns = set(namespaces)  # only for `service.Shape` type names in input declarations
        self.shape_lookup = shape_lookup  # (namespace, shape) -> Type | None
        self.depth = _bracket_depths(self.t)  # bracket depth at each token: layout is suspended inside brackets
        self.blocks: List[Optional[int]] = []  # indentation (column) of each open block; None until its first line-starting item
        self.normalized: dict = {}  # normalization code -> positions (TOWL §3); reported once per code

    def normalize(self, code, pos):
        self.normalized.setdefault(code, []).append(pos)

    def normalization_warnings(self) -> List[Diagnostic]:
        out = []
        for code, positions in self.normalized.items():
            more = f" (here and {len(positions) - 1} more place(s))" if len(positions) > 1 else ""
            out.append(Diagnostic("warning", "syntax", code, positions[0], NORMALIZATIONS[code] + more,
                                  "remove it; " + ABSENCE_RULE))
        return out

    # layout
    def starts_line(self, i=None) -> bool:
        i = self.p if i is None else i
        return i == 0 or self.t[i].pos.line > self.t[i - 1].pos.line

    def item_start(self):
        """Called at the first token of a block item: check it against the block's indentation."""
        tok = self.peek()
        if tok.kind == "eof" or not self.starts_line() or self.depth[self.p] > 0 or not self.blocks:
            return
        indent = self.blocks[-1]
        if indent is None:
            self.blocks[-1] = tok.pos.col
        elif tok.pos.col != indent:
            self.err("syntax.indent", f"'{tok.text}' is indented to column {tok.pos.col} but the items of this block start at column {indent}",
                     tok.pos, "items of one block (bindings and the result) share one indentation; a continuation line starts with '.'")

    # helpers
    def peek(self, k=0) -> Token:
        return self.t[min(self.p + k, len(self.t) - 1)]

    def at(self, text) -> bool:
        tok = self.peek()
        return tok.kind == "op" and tok.text == text

    def at_ident(self, text=None) -> bool:
        tok = self.peek()
        return tok.kind == "ident" and (text is None or tok.text == text)

    def err(self, code, msg, pos=None, fix=None):
        raise TowlError([Diagnostic("error", "syntax", code, pos or self.peek().pos, msg, fix)])

    def expect(self, text) -> Token:
        if self.at(text):
            tok = self.t[self.p]
            self.p += 1
            return tok
        found = self.peek().text or "end of input"
        self.err("syntax.expected", f"expected '{text}' but found '{found}'")

    def ident(self) -> Token:
        tok = self.peek()
        if tok.kind != "ident":
            self.err("syntax.expected", f"expected a name but found '{tok.text or 'end of input'}'")
        self.p += 1
        return tok

    # program
    def program(self) -> Program:
        head = self.ident()
        if head.text != "towl":
            self.err("syntax.header", "a program starts with 'towl 3'", head.pos)
        ver = self.peek()
        if ver.kind != "num" or ver.text != "3":
            self.err("syntax.version", f"unsupported TOWL version '{ver.text}'; this processor implements version 3", ver.pos)
        self.p += 1
        description = None
        if self.peek().kind == "str":
            description = self.t[self.p].text
            self.p += 1
        self.blocks.append(None)
        inputs = []
        while self.at_ident("input"):
            self.item_start()
            pos = self.t[self.p].pos
            self.p += 1
            name = self.ident().text
            self.expect(":")
            inputs.append(InputDecl(name, self.type(), pos))
        bindings, result = self.block_items("program")
        if self.peek().kind != "eof":
            if self.peek().kind == "ident" and self.peek(1).text == "=":
                self.err("syntax.trailing", f"binding '{self.peek().text}' comes after the result expression",
                         fix="the result is the last line of the program; move this binding above it")
            self.err("syntax.trailing", f"unexpected '{self.peek().text}' after the result expression",
                     fix="a program is: towl 3, inputs, bindings (name = expr), then exactly one result expression")
        return Program(description, inputs, bindings, result, self.normalization_warnings())

    def block_items(self, what):
        """``binding* expr`` at the current block's indentation; the block ends at its result."""
        bindings = []
        while self.peek().kind == "ident" and self.peek(1).kind == "op" and self.peek(1).text == "=":
            self.item_start()
            name = self.t[self.p]
            self.p += 1
            self.expect("=")
            bindings.append(Binding(name.text, self.expr(), name.pos))
        if self.peek().kind == "eof":
            self.err("syntax.noResult", f"a {what} ends with a result expression after its bindings")
        self.item_start()
        return bindings, self.expr()

    def type(self) -> T.Type:
        tok = self.peek()
        if tok.kind == "ident" and tok.text == "Null":
            self.err("syntax.notInTowl", "'Null' is not a type: absence is not part of any type", tok.pos, "declare the type itself; " + ABSENCE_RULE)
        if tok.kind == "ident" and tok.text in ("string", "int", "number", "bool", "timestamp", "json"):
            self.p += 1
            base = {"string": T.STRING, "int": T.INT, "number": T.NUMBER, "bool": T.BOOL, "timestamp": T.TIMESTAMP, "json": T.JSON}[tok.text]
        elif tok.kind == "ident" and tok.text == "list":
            self.p += 1
            self.expect("[")
            base = T.TList(self.type())
            self.expect("]")
        elif self.at("{"):
            self.p += 1
            fields = {}
            while not self.at("}"):
                n = self.ident().text
                self.expect(":")
                fields[n] = self.type()
                if self.at(","):
                    self.p += 1
                else:
                    break
            self.expect("}")
            base = T.TRecord(fields)
        elif tok.kind == "ident" and tok.text in self.ns and self.peek(1).text == ".":
            self.p += 2
            shape = self.ident().text
            resolved = self.shape_lookup(tok.text, shape) if self.shape_lookup else None
            if resolved is None:
                self.err("syntax.shapeType", f"unknown catalog shape {tok.text}.{shape}", tok.pos)
            base = resolved
        else:
            self.err("syntax.type", f"expected a type (string, int, number, bool, timestamp, json, list[T], {{ field: T }}) but found '{tok.text}'")
        if self.at("|"):
            bar = self.t[self.p]
            self.p += 1
            n = self.ident()
            if n.text != "Null":
                self.err("syntax.type", "types have no unions", n.pos)
            self.normalize("syntax.nullType", bar.pos)
        return base

    # expressions
    def expr(self) -> Expr:
        e = self.primary()
        if isinstance(e, MethodCall) and e.name == "for" and getattr(e, "_multiline", False):
            # a laid-out body ends the expression, except for continuation lines indented between the
            # `for` line and its body: those apply to the for expression itself (`  .flatten()`)
            header, body = e._layout
            while (self.at(".") or self.at("?.")) and self.starts_line() and self.depth[self.p] == 0 \
                    and header is not None and body is not None and header < self.peek().pos.col < body:
                e = self.postfix(e)
            return e
        while (self.at(".") or self.at("?.")) and not self.dedented_continuation():
            e = self.postfix(e)
        return e

    def dedented_continuation(self) -> bool:
        """A `.` line indented less than the current block's items belongs to an enclosing expression."""
        tok = self.peek()
        return (self.starts_line() and self.depth[self.p] == 0 and bool(self.blocks) and self.blocks[-1] is not None
                and tok.pos.col < self.blocks[-1])

    def postfix(self, target: Expr) -> Expr:
        dot = self.t[self.p]
        self.p += 1
        if dot.text == "?.":
            self.normalize("syntax.nullSafe", dot.pos)
        name = self.ident()
        if name.text in BORROWED_FNS and (self.at("(") or self.at("@")):
            self.err("syntax.notInTowl", BORROWED_FNS[name.text], name.pos)
        if name.text == "compact" and self.at("("):
            self.p += 1
            if not self.at(")"):
                self.err("syntax.arity", "'compact()' takes no argument", name.pos)
            self.p += 1
            self.normalize("syntax.compact", name.pos)
            return target
        if self.at("(") and (name.text in ALL_FNS or name.text in TEST_FNS):
            self.p += 1
            args = [] if self.at(")") else self.args(name.text)
            self.expect(")")
            return MethodCall(target, name.text, args, name.pos)
        if self.at("("):
            if name.text[:1].isupper():
                # the retired `service.Operation(...)` form
                ns = target.name if isinstance(target, Ref) else "service"
                self.err("syntax.callForm", f"'{ns}.{name.text}(...)' is not how operations are called", name.pos,
                         f'write call("{ns}", "{name.text}", {{ ... }}); ' + CALL_FORM)
            self.err("syntax.unknownFunction", f"'{name.text}' is not a TOWL function", name.pos,
                     "functions: " + " ".join(sorted(ALL_FNS | TEST_FNS)) + '; an operation is call("service", "Operation", { ... })')
        return Member(target, name.text, False, name.pos)

    def for_expr(self) -> Expr:
        """``for x in source`` then a body: a single expression on the same line, or an indented block."""
        kw = self.t[self.p]
        self.p += 1
        if self.at("("):
            self.err("syntax.forForm", "'for' takes no parentheses", self.peek().pos, FOR_FORM)
        param = self.ident()
        if not self.at_ident("in"):
            self.err("syntax.forForm", f"expected 'in' after 'for {param.text}'", self.peek().pos, FOR_FORM)
        self.p += 1
        source = self.expr()
        if self.at(":") and not self.starts_line():
            self.p += 1  # the Python reflex; accepted and dropped by the canonical rendering
        nxt = self.peek()
        if nxt.kind == "eof":
            self.err("syntax.forBody", f"'for {param.text} in ...' has no body", kw.pos, FOR_FORM)
        multiline = self.starts_line()
        if not multiline:
            if self.at("{") and self.peek(1).kind == "ident" and self.peek(2).text == "=":
                self.err("syntax.forBody", "a for body with bindings is written on indented lines, not in braces", nxt.pos, FOR_FORM)
            body = self.expr()
            e = MethodCall(source, "for", [LambdaArg(param.text, body, param.pos)], kw.pos)
            e._multiline = False
            e._layout = (None, None)
            return e
        header_indent = self.blocks[-1] if self.blocks else None
        checked = self.depth[self.p] == 0
        if checked and header_indent is not None and nxt.pos.col <= header_indent:
            self.err("syntax.indent", f"the body of 'for {param.text}' must be indented more than the line that starts it (column {header_indent})", nxt.pos, FOR_FORM)
        self.blocks.append(nxt.pos.col if checked else None)
        bindings, result = self.block_items(f"for {param.text} body")
        body_indent = self.blocks.pop()
        after = self.peek()
        if checked and after.kind != "eof" and self.starts_line() and self.depth[self.p] == 0 and header_indent is not None \
                and after.pos.col > header_indent and not (after.text in (".", "?.") and after.pos.col < body_indent):
            self.err("syntax.resultNotLast", f"this line is indented as part of the 'for {param.text}' body, but that body already ended with its result on line {result.pos.line}",
                     after.pos, "the result of a body is its last line: move the result below this line, or bind this value before the result")
        body = BlockE(bindings, result, nxt.pos) if bindings else result
        e = MethodCall(source, "for", [LambdaArg(param.text, body, param.pos)], kw.pos)
        e._multiline = True
        e._layout = (header_indent, body_indent)
        return e

    def args(self, fn: str) -> List[Arg]:
        out = []
        while True:
            pos = self.peek().pos
            if self.peek().kind == "ident" and self.peek(1).text == "=>":
                self.err("syntax.lambda", "TOWL has no lambdas", pos, FOR_FORM)
            if fn in PRED_ARG_FNS:
                out.append(PredArg(self.pred(), pos))
            else:
                out.append(ExprArg(self.expr(), pos))
            if self.at(","):
                self.p += 1
            else:
                return out

    def call(self) -> Expr:
        """``call("ns", "Operation", args?, options?)``."""
        kw = self.ident()
        self.expect("(")
        ns = self.peek()
        if ns.kind != "str":
            self.err("syntax.callForm", "the first argument of call is the service name as a string", ns.pos, CALL_FORM)
        self.p += 1
        self.expect(",")
        op = self.peek()
        if op.kind != "str":
            self.err("syntax.callForm", "the second argument of call is the operation name as a string", op.pos, CALL_FORM)
        self.p += 1
        params, options = None, None
        if self.at(","):
            self.p += 1
            params = self.expr()
            if self.at(","):
                self.p += 1
                options = self.expr()
        if self.at(","):
            self.err("syntax.callForm", "call takes at most four arguments", self.peek().pos, CALL_FORM)
        self.expect(")")
        if params is None:
            params = RecordE([], kw.pos)
        return OpCall(ns.text, op.text, params, options, kw.pos)

    def primary(self) -> Expr:
        tok = self.peek()
        if tok.kind == "str":
            self.p += 1
            return Lit(tok.text, tok.pos)
        if tok.kind == "num":
            self.p += 1
            return Lit(_number(tok.text), tok.pos)
        if tok.kind == "ident" and tok.text in ("true", "false"):
            self.p += 1
            return Lit(tok.text == "true", tok.pos)
        if tok.kind == "ident" and tok.text == "null":
            self.err("syntax.notInTowl", "'null' is not a value in TOWL", tok.pos,
                     "omit an optional parameter or field instead of passing null; " + ABSENCE_RULE)
        if self.at(".") or self.at("?."):
            return self.postfix(Implicit(tok.pos))
        if self.at("("):
            self.p += 1
            e = self.expr()
            self.expect(")")
            return e
        if self.at("["):
            self.p += 1
            items = []
            while not self.at("]"):
                items.append(self.expr())
                if self.at(","):
                    self.p += 1
                else:
                    break
            self.expect("]")
            return ListE(items, tok.pos)
        if self.at("{"):
            return self.braces()
        if tok.kind == "ident" and tok.text == "call" and self.peek(1).text == "(":
            return self.call()
        if tok.kind == "ident" and tok.text == "for":
            return self.for_expr()
        if tok.kind == "ident" and tok.text in KEYWORDS:
            self.err("syntax.keyword", f"'{tok.text}' is a keyword", tok.pos)
        if tok.kind == "ident":
            self.p += 1
            return Ref(tok.text, tok.pos)
        self.err("syntax.unexpected", f"unexpected '{tok.text or 'end of input'}'")

    def braces(self) -> Expr:
        """``{`` always opens a record; blocks are laid out by indentation (program, for body)."""
        open_ = self.expect("{")
        if self.at("}"):
            self.p += 1
            return RecordE([], open_.pos)
        if self.peek().kind == "ident" and self.peek(1).text == "=":
            self.err("syntax.brace", "braces enclose a record { name: value }; bindings are not allowed inside them",
                     self.peek().pos, "put bindings on their own lines: at the top level, or indented under a 'for x in xs' line, with the result last")
        if self.peek().kind != "ident" or self.peek(1).text != ":":
            self.err("syntax.brace", f"braces enclose a record { name: value }; found '{self.peek().text}'",
                     self.peek().pos, "to group an expression use parentheses; a for body is laid out by indentation")
        fields = []
        while not self.at("}"):
            n = self.ident()
            self.expect(":")
            fields.append((n.text, self.expr()))
            if self.at(","):
                self.p += 1
            elif not self.at("}"):
                self.err("syntax.expected", "expected ',' or '}' in record")
        self.expect("}")
        return RecordE(fields, open_.pos)

    # predicates
    def pred(self) -> Pred:
        first = self.pred_and()
        if not self.at("||"):
            return first
        terms = [first]
        while self.at("||"):
            self.p += 1
            terms.append(self.pred_and())
        return OrP(terms, first.pos)

    def pred_and(self) -> Pred:
        first = self.pred_not()
        if not self.at("&&"):
            return first
        terms = [first]
        while self.at("&&"):
            self.p += 1
            terms.append(self.pred_not())
        return AndP(terms, first.pos)

    def pred_not(self) -> Pred:
        if self.at("!"):
            pos = self.t[self.p].pos
            self.p += 1
            return NotP(self.pred_not(), pos)
        if self.at("("):
            save = self.p
            try:
                self.p += 1
                inner = self.pred()
                self.expect(")")
                return inner
            except TowlError:
                self.p = save
        return self.pred_atom()

    def pred_atom(self) -> Pred:
        pos = self.peek().pos
        operand = self.expr()
        if self.peek().kind == "op" and self.peek().text in _CMP:
            if self.peek().text in ("==", "!=") and self.peek(1).kind == "ident" and self.peek(1).text == "null":
                # `x == null` from the nullable revision: the absence test
                self.normalize("syntax.nullCompare", self.peek().pos)
                fn = "absent" if self.peek().text == "==" else "present"
                self.p += 2
                return TestP(operand, fn, None, pos)
            if isinstance(operand, MethodCall) and operand.name in TEST_FNS and self.peek().text in ("==", "!=") \
                    and self.peek(1).kind == "ident" and self.peek(1).text in ("true", "false"):
                # `.L.empty() == false`: a test compared with a boolean is the test or its negation
                negate = (self.peek().text == "==") != (self.peek(1).text == "true")
                self.p += 2
                test = self.pred_atom_test(operand)
                return NotP(test, pos) if negate else test
            op = self.t[self.p].text
            self.p += 1
            return Cmp(op, operand, self.expr(), pos)
        if self.at_ident("in"):
            self.p += 1
            return InP(operand, self.expr(), pos)
        if isinstance(operand, MethodCall):
            if operand.name in TEST_FNS:
                return self.pred_atom_test(operand)
            if operand.name in ("any", "all"):
                if len(operand.args) != 1 or not isinstance(operand.args[0], PredArg):
                    self.err("syntax.predicate", f"{operand.name}(...) takes one predicate", operand.pos)
                return QuantP(operand.target, operand.name == "all", operand.args[0].pred, operand.pos)
        self.err("syntax.predicate", 'expected a predicate: <operand> == <operand>, <operand> in [...], .field.present(), .list.empty(), .field.contains("x"), .list.any(<pred>), joined with && || !', pos)

    def pred_atom_test(self, operand: MethodCall) -> Pred:
        arg = operand.args[0].expr if operand.args and isinstance(operand.args[0], ExprArg) else None
        if operand.name in ("present", "absent", "empty") and operand.args:
            self.err("syntax.predicate", f"{operand.name}() takes no argument", operand.pos)
        if operand.name not in ("present", "absent", "empty") and arg is None:
            self.err("syntax.predicate", f"{operand.name}(s) takes one string argument", operand.pos)
        return TestP(operand.target, operand.name, arg, operand.pos)


def _bracket_depths(tokens) -> List[int]:
    out, d = [], 0
    for tok in tokens:
        if tok.kind == "op" and tok.text in ")]}":
            d = max(0, d - 1)
        out.append(d)
        if tok.kind == "op" and tok.text in "([{":
            d += 1
    return out


def _number(text: str) -> Any:
    if any(c in text for c in ".eE"):
        return float(text)
    return int(text)


def free_refs(e: Expr) -> set:
    """Names referenced by an expression that it does not itself bind."""
    out = set()

    def pred_exprs(p):
        if isinstance(p, (Cmp, InP)):
            return [p.left, p.right]
        if isinstance(p, (AndP, OrP)):
            return [x for t in p.terms for x in pred_exprs(t)]
        if isinstance(p, NotP):
            return pred_exprs(p.term)
        if isinstance(p, TestP):
            return [p.operand] + ([p.arg] if p.arg is not None else [])
        return [p.operand] + pred_exprs(p.inner)

    def walk(x, bound):
        if isinstance(x, Ref):
            if x.name not in bound:
                out.add(x.name)
        elif isinstance(x, Member):
            walk(x.target, bound)
        elif isinstance(x, RecordE):
            for _, v in x.fields:
                walk(v, bound)
        elif isinstance(x, ListE):
            for v in x.items:
                walk(v, bound)
        elif isinstance(x, BlockE):
            b = set(bound)
            for bd in x.bindings:
                walk(bd.expr, b)
                b.add(bd.name)
            walk(x.result, b)
        elif isinstance(x, OpCall):
            walk(x.params, bound)
            if x.options is not None:
                walk(x.options, bound)
        elif isinstance(x, MethodCall):
            walk(x.target, bound)
            for a in x.args:
                if isinstance(a, ExprArg):
                    walk(a.expr, bound)
                elif isinstance(a, LambdaArg):
                    walk(a.body, bound | {a.param})
                else:
                    for pe in pred_exprs(a.pred):
                        walk(pe, bound)

    walk(e, set())
    return out
