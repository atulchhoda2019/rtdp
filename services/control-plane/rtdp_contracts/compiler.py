"""RuntimeBundle compiler.

Resolves a product + tenant overlay into a fully materialized effective
configuration and an immutable, content-addressed bundle manifest with exact
digests of every dependent asset. Compilation is where dependency resolution,
type/semantic checks, and budget checks run — never on the request path.
"""

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from .digest import canonical_digest
from .registry import ContractError, ContractRegistry

# Hard ceiling from the design budget table (design.md "Proposed engineering
# budgets"); bundles exceeding it are rejected at compile time.
MAX_TOTAL_DEADLINE_MS = 100


def _ref(spec: str) -> tuple[str, int]:
    """Parse 'asset@version' -> (asset, int version)."""
    asset, _, ver = spec.rpartition("@")
    if not asset or not ver:
        raise ContractError("BAD_REF", f"expected id@version, got {spec!r}")
    return asset, int(ver)


def apply_overlay(product: dict, overlay: dict | None) -> dict:
    """Apply an allowlisted tenant overlay to a product's effective config.

    Allowed paths (design.md): thresholds.*, routing attrs, signal timeouts
    within bounds. Anything else is an engineering change, not an overlay.
    """
    eff = copy.deepcopy(product)
    allowed_prefixes = ("thresholds.", "routing.", "rollout.")
    if overlay:
        for path, value in overlay.get("overrides", {}).items():
            if not any(path.startswith(p) for p in allowed_prefixes):
                raise ContractError(
                    "OVERLAY_PATH",
                    f"override path {path!r} is not allowlisted")
            parts = path.split(".")
            node = eff
            for p in parts[:-1]:
                node = node.setdefault(p, {})
                if not isinstance(node, dict):
                    raise ContractError("OVERLAY_PATH",
                                        f"cannot set {path} into scalar")
            node[parts[-1]] = value
    return eff


def compile_bundle(registry: ContractRegistry, product: dict,
                   overlay: dict | None, tenant_id: str) -> dict:
    """Compile an effective product into a RuntimeBundle manifest."""
    eff = apply_overlay(product, overlay)
    pid = product["product_id"]

    execution = eff.get("execution", {})
    deadline = int(execution.get("total_deadline_ms", MAX_TOTAL_DEADLINE_MS))
    if deadline > MAX_TOTAL_DEADLINE_MS:
        raise ContractError("BUDGET", f"total_deadline_ms {deadline} exceeds "
                            f"{MAX_TOTAL_DEADLINE_MS}", pid)

    # --- features ---
    features = []
    for ref in execution.get("required_features", []):
        fid, ver = _ref(ref)
        fd = registry.get("feature_definition", fid, str(ver))
        features.append({"name": fid, "version": ver, "tier": fd["tier"],
                         "digest": fd["_digest"], "definition": fd})

    # --- signals: ruleset declares requirements; bindings provide them ---
    ruleset_ref = execution.get("ruleset")
    if not ruleset_ref:
        raise ContractError("NO_RULESET", "execution.ruleset required", pid)
    rid, rver = _ref(ruleset_ref)
    ruleset = registry.get("ruleset", rid, str(rver))

    signals = []
    for name, spec in (execution.get("signals") or {}).items():
        contract_name = spec.get("contract")
        accepted = [str(v) for v in spec.get("accepted_contracts", [])]
        if not contract_name or not accepted:
            raise ContractError("BAD_SIGNAL", f"signal {name} needs contract "
                                "and accepted_contracts", pid)
        timeout = int(spec.get("timeout_ms", 0))
        if timeout <= 0 or timeout >= deadline:
            raise ContractError("BUDGET", f"signal {name} timeout_ms {timeout} "
                                f"must be in (0, {deadline})", pid)
        contract = registry.get("signal_contract", contract_name, accepted[0])
        bind_ref = spec.get("binding")
        if not bind_ref:
            if spec.get("required"):
                raise ContractError("NO_BINDING",
                                    f"required signal {name} has no binding", pid)
            continue
        bid, bver = _ref(bind_ref)
        binding = registry.get("provider_binding", bid, str(bver))
        out_ref = binding.get("output_contract", "")
        oc_name, _, oc_ver = out_ref.rpartition("@")
        if oc_name != contract_name or oc_ver not in accepted:
            raise ContractError(
                "SEMANTIC_INCOMPATIBLE",
                f"binding {bind_ref} emits {out_ref}, ruleset accepts "
                f"{contract_name}@{accepted}", pid)
        signals.append({"alias": name, "contract": contract_name,
                        "accepted_contracts": accepted,
                        "contract_digest": contract["_digest"],
                        "binding": bind_ref, "binding_digest": binding["_digest"],
                        "binding_def": binding,
                        "timeout_ms": timeout,
                        "required": bool(spec.get("required"))})

    # --- ruleset signal dependencies must be satisfied ---
    bound_aliases = {s["alias"] for s in signals}
    for req in ruleset.get("requires", []):
        alias = req.get("alias")
        if not req.get("optional") and alias not in bound_aliases:
            raise ContractError(
                "UNRESOLVED_SIGNAL",
                f"ruleset requires signal alias {alias!r} "
                f"({req.get('signal')}) not bound in execution.signals", pid)

    # --- action policy ---
    ap_ref = eff.get("action_policy")
    action_policy = None
    if ap_ref:
        aid, aver = _ref(ap_ref)
        action_policy = registry.get("action_policy", aid, str(aver))

    bundle = {
        "kind": "runtime_bundle",
        "product_id": pid,
        "product_version": product["version"],
        "tenant_id": tenant_id,
        "subscription_revision": eff.get("subscription_revision"),
        "effective_config": eff,
        "features": [{"name": f["name"], "version": f["version"],
                      "tier": f["tier"], "digest": f["digest"],
                      "computation": f["definition"]["computation"],
                      "scope": f["definition"]["scope"],
                      "value_type": f["definition"]["value_type"]}
                     for f in features],
        "signals": [{k: v for k, v in s.items() if k != "binding_def"} | 
                    {"provider": s["binding_def"].get("provider"),
                     "endpoint_ref": s["binding_def"].get("endpoint_ref"),
                     "model": s["binding_def"].get("model"),
                     "model_digest": s["binding_def"].get("model_digest"),
                     "input_schema_digest":
                         s["binding_def"].get("input_schema_digest"),
                     "preprocessing_digest":
                         s["binding_def"].get("preprocessing_digest"),
                     "maximum_age_ms": s["binding_def"].get("maximum_age_ms"),
                     "input_features":
                         s["binding_def"].get("input_features")}
                    for s in signals],
        "ruleset": {"id": rid, "version": rver, "digest": ruleset["_digest"],
                    "spec": {k: v for k, v in ruleset.items()
                             if not k.startswith("_")}},
        "action_policy": ({"id": aid, "version": aver,
                           "digest": action_policy["_digest"],
                           "spec": {k: v for k, v in action_policy.items()
                                    if not k.startswith("_")}}
                          if action_policy else None),
        "aggregation": execution.get("aggregation"),
        "missing_required_signal": execution.get("missing_required_signal",
                                                 "REVIEW"),
        "compiler_version": "rtdp-compile/0.1.0",
    }
    bundle["digest"] = canonical_digest(
        {k: v for k, v in bundle.items() if k != "digest"})
    return bundle


def write_bundle(bundle: dict, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{bundle['digest'].replace(':', '_')}.json"
    path.write_text(json.dumps(bundle, indent=2, sort_keys=True))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rtdp-compile")
    ap.add_argument("--contracts", default="contracts")
    ap.add_argument("--assets", default="assets")
    ap.add_argument("--product", required=True,
                    help="path to product yaml")
    ap.add_argument("--overlay", help="path to tenant overlay yaml")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--out", default="build/bundles")
    args = ap.parse_args(argv)

    registry = ContractRegistry(Path(args.contracts), Path(args.assets))
    try:
        product = yaml.safe_load(Path(args.product).read_text())
        overlay = (yaml.safe_load(Path(args.overlay).read_text())
                   if args.overlay else None)
        bundle = compile_bundle(registry, product, overlay, args.tenant)
        path = write_bundle(bundle, args.out)
        print(json.dumps({"bundle": str(path), "digest": bundle["digest"]}))
        return 0
    except ContractError as e:
        print(json.dumps({"error": e.code, "detail": str(e)}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
