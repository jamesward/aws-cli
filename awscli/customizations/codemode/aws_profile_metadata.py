"""Reviewed AWS Code Mode profile metadata: effect classification (CODE_MODE-SPEC.md §3.5).

botocore models do not carry a reliable read-only trait, so the profile classifies operations from
their verb family plus reviewed overrides. Unknown is unsafe by default: it is treated as mutate and
never silently as read.
"""

from __future__ import annotations

PROFILE_METADATA_VERSION = 3

READ = "read"
MUTATE = "mutate"
UNKNOWN_EFFECT = "unknown"

_EFFECT_OVERRIDES = {
    ("ec2", "create-tags"): MUTATE,
    ("ssm", "start-automation-execution"): MUTATE,
    ("sts", "assume-role"): READ,
    ("sts", "get-session-token"): READ,
}
_READ_PREFIXES = (
    "batch-get-", "check-", "describe-", "get-", "head-", "list-", "lookup-",
    "scan-", "search-", "select-", "query", "test-", "validate-", "estimate-", "preview-",
)
_MUTATING_PREFIXES = (
    "accept-", "add-", "associate-", "attach-", "authorize-", "batch-delete-", "batch-put-", "batch-write-",
    "cancel-", "copy-", "create-", "delete-", "deregister-", "detach-", "disable-", "disassociate-",
    "enable-", "execute-", "import-", "invoke-", "modify-", "publish-", "put-", "reboot-", "register-",
    "reject-", "release-", "remove-", "replace-", "reset-", "restore-", "revoke-", "rotate-", "run-",
    "send-", "set-", "start-", "stop-", "tag-", "terminate-", "untag-", "update-", "upload-", "write-",
)


def effect_for(service: str, operation: str) -> str:
    """``operation`` is the kebab (CLI) spelling."""
    override = _EFFECT_OVERRIDES.get((service, operation))
    if override:
        return override
    if operation.startswith(_READ_PREFIXES):
        return READ
    if operation.startswith(_MUTATING_PREFIXES):
        return MUTATE
    return UNKNOWN_EFFECT
