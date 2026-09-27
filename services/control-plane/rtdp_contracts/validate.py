"""rtdp-contracts: validate every contract/asset file under contracts/ + assets/."""

import argparse
import json
import sys
from pathlib import Path

from .registry import ContractError, ContractRegistry


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rtdp-contracts")
    ap.add_argument("--contracts", default="contracts")
    ap.add_argument("--assets", default="assets")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    registry = ContractRegistry(Path(args.contracts), Path(args.assets))
    kinds = ["signal_contract", "feature_definition", "ruleset",
             "provider_binding", "action_policy", "product", "state_flow"]
    results, errors = [], []
    for kind in kinds:
        for path in registry._paths(kind):
            try:
                from .registry import load_yaml, VALIDATORS
                doc = load_yaml(path)
                v = VALIDATORS.get(kind)
                if v:
                    v(doc, str(path))
                results.append({"path": str(path), "kind": kind, "ok": True})
            except ContractError as e:
                errors.append({"path": str(path), "kind": kind,
                               "code": e.code, "detail": str(e)})
    report = {"checked": len(results) + len(errors),
              "ok": len(results), "errors": errors}
    print(json.dumps(report, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
