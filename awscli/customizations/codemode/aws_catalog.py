"""AWS catalog for TOWL v3 over the vendored botocore service models (CODE_MODE-SPEC.md §3).

Namespaces are AWS service names; operations are exact API PascalCase names. botocore shapes map to
TOWL types with the profile's member policy: list/map members are defaulted (never null), every
other non-required member is ``T | Null``. Pageable operations expose the merged output shape;
pagination members are runtime-owned and never authorable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from awscli.autocomplete.local.model import ModelIndex
from awscli.botocore import xform_name
from awscli.botocore.session import Session

from . import types as T
from .aws_profile_metadata import effect_for

# TOWL §12.4 error classes
TRANSIENT, VALIDATION, AUTHORIZATION, AVAILABILITY, ABSENCE, STATE, OTHER = (
    "transient", "validation", "authorization", "availability", "absence", "state", "other",
)

_CLASS_TABLE = {
    "throttling": TRANSIENT, "throttlingexception": TRANSIENT, "requestlimitexceeded": TRANSIENT,
    "toomanyrequestsexception": TRANSIENT, "requesttimeout": TRANSIENT, "serviceunavailable": TRANSIENT,
    "internalerror": TRANSIENT, "internalfailure": TRANSIENT, "requestexpired": TRANSIENT,
    "validationexception": VALIDATION, "validationerror": VALIDATION, "invalidparametervalue": VALIDATION,
    "invalidparametercombination": VALIDATION, "malformedpolicydocument": VALIDATION, "invalidfilter": VALIDATION,
    "invalidparameter": VALIDATION, "missingparameter": VALIDATION,
    "accessdenied": AUTHORIZATION, "accessdeniedexception": AUTHORIZATION, "unauthorizedoperation": AUTHORIZATION,
    "authfailure": AUTHORIZATION, "unauthorized": AUTHORIZATION,
    "optinrequired": AVAILABILITY, "unsupportedoperation": AVAILABILITY,
    "incorrectinstancestate": STATE, "dependencyviolation": STATE, "resourceinuseexception": STATE,
    "conditionalcheckfailedexception": STATE, "incorrectstate": STATE, "invalidstate": STATE,
}


def classify_error(code: str) -> str:
    """Deterministic: exact table, then the NotFound/NoSuch substring rule, then other (Code Mode §3.6)."""
    c = (code or "").lower()
    if c in _CLASS_TABLE:
        return _CLASS_TABLE[c]
    if "notfound" in c or "nosuch" in c or "not_found" in c:
        return ABSENCE
    if "throttl" in c or c.startswith("5"):
        return TRANSIENT
    if "accessdenied" in c or "unauthorized" in c:
        return AUTHORIZATION
    if "invalidparam" in c or "validation" in c:
        return VALIDATION
    return OTHER


@dataclass(frozen=True)
class PaginatorSpec:
    input_token: Any
    output_token: Any
    limit_key: Optional[str]
    result_key: Any

    @property
    def input_members(self):
        toks = self.input_token if isinstance(self.input_token, list) else [self.input_token]
        return tuple(t for t in toks + [self.limit_key] if isinstance(t, str))

    @property
    def output_members(self):
        toks = self.output_token if isinstance(self.output_token, list) else [self.output_token]
        return tuple(t.split(".")[0].split("[")[0] for t in toks if isinstance(t, str))


class OperationSpec:
    """One resolved operation: TOWL types plus the botocore models schema rendering still needs."""

    def __init__(self, service, name, description, input_type, output_type, effect, error_codes,
                 paginator, input_shape, output_shape, operation_model, required=()):
        self.required = frozenset(required)  # required input members (presence enforced regardless of type)
        self.namespace = service
        self.name = name  # API PascalCase
        self.description = description
        self.input = input_type  # TRecord or None
        self.output = output_type
        self.effect = effect  # read | mutate | unknown
        self.error_codes = frozenset(error_codes)
        self.open_error_codes = True
        self.paginator = paginator
        self.input_shape = input_shape
        self.output_shape = output_shape
        self.operation_model = operation_model

    @property
    def id(self):
        return f"{self.namespace}.{self.name}"

    @property
    def service(self):
        return self.namespace

    @property
    def operation(self):
        return kebab(self.name)

    @property
    def paged(self):
        return self.paginator is not None

    @property
    def runtime_owned(self) -> Tuple[str, ...]:
        return self.paginator.input_members if self.paginator else ()

    @property
    def effects(self):  # search ranking compatibility
        return frozenset({self.effect})


@dataclass(frozen=True)
class Unknown:
    suggestions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationSummary:
    """Cheap schema-search record sourced from the shipped auto-prompt command index."""

    service: str
    operation: str  # kebab
    service_full_name: str = ""
    description: str = ""

    @property
    def id(self):
        return f"{self.service}:{self.operation}"

    @property
    def effects(self):
        return frozenset({effect_for(self.service, self.operation)})

    paged = False


_CLI_COMMAND_FOR_SERVICE = {"s3": "s3api"}


def kebab(name: str) -> str:
    return xform_name(name, "-")


def _compact(name: str) -> str:
    return re.sub(r"[-_]", "", name).lower()


class AwsCatalog:
    """Immutable-ish view over the botocore model snapshot supplied by the AWS CLI distribution."""

    name = "aws"

    def __init__(self, session=None, services=None, model_index=None):
        self.session = session or Session()
        self._services = tuple(services or self.session.get_available_services())
        self._service_set = frozenset(self._services)
        self._models = {}
        self._paginators = {}
        self._model_index = model_index or ModelIndex()
        self._summary_cache = {}
        self._documentation_cache = {}
        self._ops = {}
        self._types: Dict[Tuple[str, str], T.Type] = {}

    # ── namespaces / operations (TOWL §10) ────────────────────────────────────

    @property
    def namespaces(self):
        return self._service_set

    def services(self):
        return self._services

    def operation(self, namespace, name):
        resolved = self.resolve(namespace, name)
        return resolved if isinstance(resolved, OperationSpec) else None

    def error_class(self, op, code):
        return classify_error(code)

    def shape_type(self, namespace, shape_name):
        if namespace not in self._service_set:
            return None
        model = self._model(namespace)
        try:
            shape = model.shape_for(shape_name)
        except Exception:
            return None
        return self._type_of(namespace, shape)

    def _model(self, service):
        if service not in self._models:
            self._models[service] = self.session.get_service_model(service)
        return self._models[service]

    def _paginator_config(self, service, api_name):
        if service not in self._paginators:
            try:
                model = self.session.get_paginator_model(service)
                self._paginators[service] = model._paginator_config
            except Exception:
                self._paginators[service] = {}
        return self._paginators[service].get(api_name)

    def resolve(self, service, operation):
        if not service or service not in self._service_set:
            return Unknown(tuple(_nearest(service or "", self._services)))
        key = (service, operation)
        if key in self._ops:
            return self._ops[key]
        model = self._model(service)
        api_name = None
        if operation in model.operation_names:
            api_name = operation
        else:
            by_kebab = {kebab(n): n for n in model.operation_names}
            api_name = by_kebab.get(operation.lower())
            if api_name is None:
                compact = _compact(operation)
                hits = [n for n in model.operation_names if _compact(n) == compact]
                api_name = hits[0] if len(hits) == 1 else None
        if api_name is None:
            return Unknown(tuple(_nearest(operation, list(model.operation_names))))
        op = model.operation_model(api_name)
        config = self._paginator_config(service, api_name)
        paginator = None
        if config:
            paginator = PaginatorSpec(config.get("input_token"), config.get("output_token"),
                                      config.get("limit_key"), config.get("result_key"))
        input_type = self._type_of(service, op.input_shape, hide=paginator.input_members if paginator else ()) if op.input_shape is not None else None
        output_type = self._output_type(service, op.output_shape, paginator)
        error_codes = {getattr(s, "error_code", None) or s.name for s in getattr(op, "error_shapes", []) or []}
        required = set(getattr(op.input_shape, "required_members", ()) or ()) - set(paginator.input_members if paginator else ())
        spec = OperationSpec(
            service, api_name, (op.documentation or "").strip(), input_type, output_type,
            effect_for(service, kebab(api_name)), error_codes, paginator, op.input_shape, op.output_shape, op, required,
        )
        self._ops[key] = spec
        self._ops[(service, kebab(api_name))] = spec
        return spec

    # ── botocore shape -> TOWL type (Code Mode §3.3) ──────────────────────────

    def _type_of(self, service, shape, hide=(), _seen=None) -> T.Type:
        if shape is None:
            return T.JSON
        kind = shape.type_name
        if kind == "structure":
            name = f"{service}.{shape.name}" if shape.name and not hide else None
            key = (service, shape.name)
            if name and key in self._types:
                return self._types[key]
            seen = set(_seen or ())
            if shape.name in seen:
                return T.JSON  # recursive shape: opaque beyond the first level
            seen.add(shape.name)
            required = set(getattr(shape, "required_members", ()) or ())

            def build(shape=shape, required=required, seen=seen, hide=hide):
                fields = {}
                for member, ms in shape.members.items():
                    if member in hide:
                        continue
                    mt = self._type_of(service, ms, (), seen)
                    if member in required or isinstance(mt, T.TList):
                        fields[member] = mt
                    else:
                        fields[member] = T.nullable(mt)
                return fields

            rec = T.TRecord(None, name=name, thunk=build)
            if name:
                self._types[key] = rec
            else:
                rec = T.TRecord(build(), name=None)
            return rec
        if kind == "list":
            return T.TList(self._type_of(service, shape.member, (), _seen))
        if kind == "map":
            return T.TList(T.TRecord({"key": self._type_of(service, shape.key, (), _seen), "value": self._type_of(service, shape.value, (), _seen)}))
        return {
            "string": T.STRING, "integer": T.INT, "long": T.INT, "float": T.NUMBER, "double": T.NUMBER,
            "boolean": T.BOOL, "timestamp": T.TIMESTAMP, "blob": T.STRING, "char": T.STRING,
        }.get(kind, T.JSON)

    def _output_type(self, service, shape, paginator) -> T.Type:
        if shape is None:
            return T.TRecord({}, name=None)
        if paginator is None:
            return self._type_of(service, shape)
        # merged output shape: cursor members removed, everything else retained (Code Mode §3.4)
        full = self._type_of(service, shape)
        hidden = set(paginator.output_members)
        fields = {k: v for k, v in full.fields.items() if k not in hidden}
        return T.TRecord(fields, name=None)

    # ── search support (unchanged from the v1 prototype) ─────────────────────

    def operation_summaries(self, service=None):
        cache_key = service or "*"
        if cache_key in self._summary_cache:
            return self._summary_cache[cache_key]
        top = {
            name: (full_name or "")
            for name, full_name in self._model_index.commands_with_full_name(["aws"])
            if name in self._service_set
        }
        services = (service,) if service else self._services
        summaries = []
        for svc in services:
            if svc not in self._service_set:
                continue
            # the CLI index keys S3 API operations under the `s3api` command; `aws s3` holds only
            # high-level custom commands (cp, ls, sync ...) that are not operations
            command = _CLI_COMMAND_FOR_SERVICE.get(svc, svc)
            for operation, _ in self._model_index.commands_with_full_name(["aws", command]):
                summaries.append(OperationSummary(svc, operation, top.get(svc, "") or top.get(command, "")))
        value = tuple(summaries)
        self._summary_cache[cache_key] = value
        return value

    def complete_service_summaries(self, service):
        if service not in self._service_set:
            return ()
        indexed = self.operation_summaries(service)
        full_name = indexed[0].service_full_name if indexed else ""
        model = self._model(service)
        return tuple(OperationSummary(service, kebab(api_name), full_name) for api_name in model.operation_names)

    def enrich_summary(self, summary: OperationSummary) -> OperationSummary:
        if summary.id in self._documentation_cache:
            documentation = self._documentation_cache[summary.id]
        else:
            model = self._model(summary.service)
            by_kebab = {kebab(name): name for name in model.operation_names}
            api_name = by_kebab.get(summary.operation)
            documentation = (model.operation_model(api_name).documentation or "").strip() if api_name else ""
            self._documentation_cache[summary.id] = documentation
        return OperationSummary(summary.service, summary.operation, summary.service_full_name, documentation)

    def operations(self, service=None):
        services = (service,) if service else self._services
        for svc in services:
            if svc not in self._service_set:
                continue
            model = self._model(svc)
            for api in model.operation_names:
                resolved = self.resolve(svc, api)
                if isinstance(resolved, OperationSpec):
                    yield resolved


def _nearest(query, candidates, limit=4):
    compact = _compact(query)
    scored = []
    for c in candidates:
        cc = _compact(c)
        prefix = 0
        for a, b in zip(compact, cc):
            if a != b:
                break
            prefix += 1
        score = prefix * 4 - abs(len(compact) - len(cc))
        if compact and (compact in cc or cc in compact):
            score += 20
        scored.append((score, c))
    return [c for score, c in sorted(scored, key=lambda x: (-x[0], x[1]))[:limit] if score > 0]
