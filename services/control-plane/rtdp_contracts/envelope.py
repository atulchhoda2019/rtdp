"""Signal envelope validation against a registered semantic contract.

Runtime equivalent lives in the Go signal-resolver; this implementation is
used by the compiler's golden-vector checks and by contract tests.
"""

from datetime import datetime, timezone
from typing import Any

from .registry import ContractError


def _check_value(field: str, value: Any, spec: dict, asset: str):
    t = spec["type"]
    ok = {
        "float64": isinstance(value, (int, float)) and not isinstance(value, bool),
        "int64": isinstance(value, int) and not isinstance(value, bool),
        "string": isinstance(value, str),
        "bool": isinstance(value, bool),
        "bytes": isinstance(value, (bytes, str)),
        "float64_list": isinstance(value, list) and
            all(isinstance(x, (int, float)) for x in value),
        "string_list": isinstance(value, list) and
            all(isinstance(x, str) for x in value),
    }[t]
    if not ok:
        raise ContractError("VALUE_TYPE", f"{field} must be {t}", asset)
    if t == "float64" and "minimum" in spec:
        if not (spec["minimum"] <= float(value) <= spec["maximum"]):
            raise ContractError(
                "VALUE_RANGE",
                f"{field}={value} outside [{spec['minimum']},{spec['maximum']}]",
                asset)


def validate_envelope(env: dict, contract: dict,
                      now: datetime | None = None) -> dict:
    """Validate one decoded signal envelope. Returns the validated values map.

    Enforces: exact signal_name + contract version/digest, allowed status,
    required value fields and types/ranges, non-negative revision, and expiry.
    """
    asset = f"{contract['signal_name']}@{contract['version']}"
    if env.get("signal_name") != contract["signal_name"]:
        raise ContractError("SIGNAL_MISMATCH", "signal_name mismatch", asset)
    if str(env.get("contract_version")) != str(contract["version"]):
        raise ContractError("CONTRACT_VERSION",
                            f"contract {env.get('contract_version')} not in "
                            f"accepted set (contract {contract['version']})", asset)
    status = env.get("status")
    if status not in contract["allowed_statuses"]:
        raise ContractError("STATUS", f"status {status!r} not allowed", asset)
    if int(env.get("transaction_revision", 0)) < 1:
        raise ContractError("REVISION", "transaction_revision must be >= 1", asset)

    values = env.get("values") or {}
    if status == "OK":
        schema = contract["value_schema"]
        for field, spec in schema.items():
            if spec.get("required") and field not in values:
                raise ContractError("VALUE_MISSING",
                                    f"required value {field} absent", asset)
            if field in values:
                _check_value(field, values[field], spec, asset)

    expires = env.get("expires_at")
    if expires:
        exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if (now or datetime.now(timezone.utc)) > exp:
            raise ContractError("EXPIRED", "signal envelope expired", asset)
    return values
