"""Contract registry: loads and validates versioned asset definitions.

Asset kinds:
  signal_contract     contracts/signal_contracts/<name>/<version>.yaml
  feature_definition  contracts/feature_definitions/<name>/<version>.yaml
  ruleset             assets/<scope>/rulesets/<id>/<version>.yaml
  provider_binding    assets/<scope>/bindings/<id>/<version>.yaml
  action_policy       assets/<scope>/action_policies/<id>/<version>.yaml
  product             assets/<scope>/products/<id>/<version>.yaml
  state_flow          assets/<scope>/flows/<id>/<version>.yaml
"""

import re
from pathlib import Path
from typing import Any

import yaml

from .digest import canonical_digest


class ContractError(Exception):
    """Structured validation/compile diagnostic."""

    def __init__(self, code: str, message: str, asset: str | None = None):
        self.code = code
        self.asset = asset
        super().__init__(f"{code}: {message}" + (f" [{asset}]" if asset else ""))


SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
ASSET_ID = re.compile(r"^[a-z][a-z0-9_.\-]*$")

VALUE_TYPES = {"float64", "int64", "string", "bool", "bytes",
               "float64_list", "string_list"}
ALLOWED_SIGNAL_STATUSES = {"OK", "UNAVAILABLE", "INVALID_INPUT", "TIMED_OUT"}


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ContractError("YAML_PARSE", str(e), str(path))
    if not isinstance(doc, dict):
        raise ContractError("YAML_SHAPE", "asset file must be a mapping", str(path))
    return doc


def _semver(v: Any, field: str, asset: str) -> tuple[int, int, int]:
    if not isinstance(v, str) or not SEMVER.match(v):
        raise ContractError("BAD_VERSION", f"{field} must be semver, got {v!r}", asset)
    return tuple(int(x) for x in v.split("."))


def _require(cond: bool, code: str, msg: str, asset: str):
    if not cond:
        raise ContractError(code, msg, asset)


def validate_signal_contract(doc: dict, asset: str) -> dict:
    name = doc.get("signal_name")
    _require(isinstance(name, str) and ASSET_ID.match(name),
             "BAD_NAME", "signal_name must be a lowercase dotted id", asset)
    _semver(doc.get("version"), "version", asset)

    schema = doc.get("value_schema")
    _require(isinstance(schema, dict) and schema,
             "BAD_SCHEMA", "value_schema must be a non-empty map", asset)
    for field, spec in schema.items():
        _require(isinstance(spec, dict), "BAD_SCHEMA",
                 f"value_schema.{field} must be a map", asset)
        _require(spec.get("type") in VALUE_TYPES, "BAD_TYPE",
                 f"value_schema.{field}.type must be one of {sorted(VALUE_TYPES)}",
                 asset)
        if "minimum" in spec or "maximum" in spec:
            _require(isinstance(spec.get("minimum"), (int, float))
                     and isinstance(spec.get("maximum"), (int, float))
                     and spec["minimum"] <= spec["maximum"],
                     "BAD_RANGE", f"value_schema.{field} needs numeric min<=max",
                     asset)

    sem = doc.get("semantics")
    _require(isinstance(sem, dict), "BAD_SEMANTICS",
             "semantics block is required", asset)
    for f in ("meaning", "population", "target_label",
              "label_observation_horizon", "unit", "higher_means"):
        _require(isinstance(sem.get(f), str) and sem[f],
                 "BAD_SEMANTICS", f"semantics.{f} is required", asset)

    statuses = doc.get("allowed_statuses")
    _require(isinstance(statuses, list) and
             set(statuses) <= ALLOWED_SIGNAL_STATUSES and "OK" in statuses,
             "BAD_STATUSES",
             f"allowed_statuses subset of {sorted(ALLOWED_SIGNAL_STATUSES)} incl. OK",
             asset)

    compat = doc.get("compatibility", {})
    _require(compat.get("semantic_change") in (None, "new_major_required"),
             "BAD_COMPAT", "semantic_change must be new_major_required", asset)
    return doc


def validate_feature_definition(doc: dict, asset: str) -> dict:
    _require(isinstance(doc.get("feature_name"), str) and
             ASSET_ID.match(doc["feature_name"]),
             "BAD_NAME", "feature_name must be a lowercase dotted id", asset)
    _require(isinstance(doc.get("version"), int) and doc["version"] > 0,
             "BAD_VERSION", "version must be a positive int", asset)
    _require(doc.get("tier") in ("tier1", "tier2"),
             "BAD_TIER", "tier must be tier1 or tier2", asset)
    _require(doc.get("value_type") in ("int64", "float64"),
             "BAD_TYPE", "value_type must be int64 or float64", asset)
    comp = doc.get("computation")
    _require(isinstance(comp, dict) and "kind" in comp,
             "BAD_COMPUTATION", "computation.kind required", asset)
    _require(isinstance(doc.get("scope"), list) and doc["scope"],
             "BAD_SCOPE", "scope must be a non-empty list", asset)
    return doc


def validate_ruleset(doc: dict, asset: str) -> dict:
    _require(isinstance(doc.get("ruleset_id"), str), "BAD_NAME",
             "ruleset_id required", asset)
    _require(isinstance(doc.get("version"), int) and doc["version"] > 0,
             "BAD_VERSION", "version must be a positive int", asset)
    rules = doc.get("rules")
    _require(isinstance(rules, list) and rules,
             "NO_RULES", "rules must be a non-empty list", asset)
    seen = set()
    for r in rules:
        _require(isinstance(r, dict) and r.get("id"), "BAD_RULE",
                 "each rule needs an id", asset)
        _require(r["id"] not in seen, "DUP_RULE",
                 f"duplicate rule id {r['id']}", asset)
        seen.add(r["id"])
        _require(isinstance(r.get("when"), str) and r["when"],
                 "BAD_RULE", f"rule {r['id']} needs a CEL 'when'", asset)
        out = r.get("outcome")
        _require(isinstance(out, dict) and out.get("decision") in
                 ("APPROVE", "DECLINE", "REVIEW", "NOT_APPLICABLE"),
                 "BAD_OUTCOME", f"rule {r['id']} needs a valid decision", asset)
    _require(doc.get("default_outcome") in
             ("APPROVE", "DECLINE", "REVIEW", "NOT_APPLICABLE"),
             "BAD_DEFAULT", "default_outcome required", asset)
    return doc


VALIDATORS = {
    "signal_contract": validate_signal_contract,
    "feature_definition": validate_feature_definition,
    "ruleset": validate_ruleset,
}

# kind -> the YAML field holding the asset id
ID_FIELDS = {
    "signal_contract": "signal_name",
    "feature_definition": "feature_name",
    "ruleset": "ruleset_id",
    "provider_binding": "binding_id",
    "action_policy": "action_policy_id",
    "product": "product_id",
    "state_flow": "flow_id",
}


class ContractRegistry:
    """Read-only view over contract/asset files with digest resolution."""

    def __init__(self, contracts_dir: Path, assets_dir: Path):
        self.contracts_dir = Path(contracts_dir)
        self.assets_dir = Path(assets_dir)
        self._cache: dict[str, dict] = {}

    def _paths(self, kind: str) -> list[Path]:
        base = {
            "signal_contract": self.contracts_dir / "signal_contracts",
            "feature_definition": self.contracts_dir / "feature_definitions",
        }.get(kind)
        if base is not None:
            return sorted(base.glob("*/*.yaml")) if base.exists() else []
        # tenant/platform assets live under assets/seed/tenants/<t>/ and assets/platform/
        found: list[Path] = []
        subdir = {
            "ruleset": "rulesets", "provider_binding": "bindings",
            "action_policy": "action_policies", "product": "products",
            "state_flow": "flows",
        }.get(kind)
        if subdir is None:
            return []
        for root in sorted(self.assets_dir.rglob(subdir)):
            found.extend(sorted(root.glob("*/*.yaml")))
        return found

    def load_all(self, kind: str) -> list[dict]:
        docs = []
        for p in self._paths(kind):
            doc = load_yaml(p)
            validator = VALIDATORS.get(kind)
            if validator:
                doc = validator(doc, str(p))
            doc["_path"] = str(p)
            doc["_digest"] = canonical_digest(
                {k: v for k, v in doc.items() if not k.startswith("_")})
            docs.append(doc)
        return docs

    def get(self, kind: str, asset_id: str, version: str) -> dict:
        key = f"{kind}:{asset_id}:{version}"
        if key in self._cache:
            return self._cache[key]
        id_field = ID_FIELDS.get(kind)
        for doc in self.load_all(kind):
            if doc.get(id_field) == asset_id and \
                    str(doc.get("version")) == str(version):
                self._cache[key] = doc
                return doc
        raise ContractError("NOT_FOUND", f"{kind} {asset_id}@{version} not found")
