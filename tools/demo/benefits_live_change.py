#!/usr/bin/env python3
"""BUC-3 live config change — tenant_b HSA auto-approve limit 250 -> 400.

Runs on the seeded local stack (same pattern as change_demo.py):

  1. baseline: the $320 scenario REVIEWs under tenant_b's $250 overlay
  2. compile a new tenant_b bundle with thresholds.auto_approve_limit=400
     and activate it under a new epoch — the only write the decision
     path sees is the activation manifest
  3. the same scenario now APPROVEs; bundle_digest and manifest_epoch
     changed, service image digests and the migration set did not
  4. rollback: re-activate the previous bundle

Evidence: docs/validation/phase3-config-only.json
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                    / "services" / "control-plane"))

import yaml  # noqa: E402
from rtdp_contracts.compiler import compile_bundle, write_bundle  # noqa: E402
from rtdp_contracts.registry import ContractRegistry  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
INGRESS = os.environ.get("RTDP_INGRESS", "http://localhost:8080")
PRODUCT_YAML = (ROOT / "assets/seed/platform/products"
                / "hsa_reimbursement/1.yaml")
SUB_YAML = ROOT / "assets/seed/tenants/tenant_b/subscription.yaml"
EVIDENCE = ROOT / "docs" / "validation" / "phase3-config-only.json"
RUN = f"{int(time.time())}"

ev = {"run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
      "change": "tenant_b hsa_reimbursement "
                "thresholds.auto_approve_limit 250 -> 400 -> 250",
      "scope": "configuration only: tenant overlay -> compiled bundle -> "
               "activation epoch"}


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True,
                          cwd=ROOT, **kw)


def decide(client, txn_id, claimant, amount=320.0):
    req = urllib.request.Request(
        f"{INGRESS}/v1/decide",
        data=json.dumps({
            "transaction_id": txn_id, "transaction_revision": 1,
            "event_type": "HSA_CLAIM", "channel": "PORTAL",
            "region": "us-east-1", "tokenized_claimant": claimant,
            "provider_id": "prv_buc3", "currency": "USD", "amount": amount,
            "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()),
        }).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client}, method="POST")
    for _ in range(10):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.4)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def image_digests():
    """Container image ids for every rtdp-* service — unchanged proves the
    change required no rebuild."""
    if os.environ.get("RTDP_AWS") == "1":
        out = sh(["kubectl", "-n", "rtdp", "get", "deploy",
                  "-o", "jsonpath="
                  "{.items[*].spec.template.spec.containers[*].image}"])
        return sorted(set(out.stdout.split()))
    out = sh(["docker", "compose", "images", "--format", "json"]).stdout
    return sorted(f"{i['Repository']}:{i['ID']}" for i in json.loads(out)
                  if i["Repository"].startswith("rtdp-"))


def migration_set():
    """The applied migration surface is the file set under
    services/control-plane/migrations — digest each file."""
    mdir = ROOT / "services" / "control-plane" / "migrations"
    return {p.name: "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(mdir.iterdir())}


def load_acts():
    return json.loads((BUILD / "bundles" / "activations.json").read_text())


def activate(bundle):
    """Pin a freshly compiled bundle for tenant_b under a new epoch —
    the only write the decision path sees."""
    acts_path = BUILD / "bundles" / "activations.json"
    acts = json.loads(acts_path.read_text())
    for a in acts:
        if (a["tenant_id"] == bundle["tenant_id"]
                and a["bundle"]["product_id"] == bundle["product_id"]
                and a.get("cohort") != "shadow"):
            a["bundle_digest"] = bundle["digest"]
            a["bundle"] = bundle
            a["epoch"] = int(time.time())
    acts_path.write_text(json.dumps(acts, indent=2))


def compile_hsa_bundle(overlay):
    registry = ContractRegistry(ROOT / "contracts", ROOT / "assets")
    product = yaml.safe_load(PRODUCT_YAML.read_text())
    bundle = compile_bundle(registry, product, overlay, "tenant_b")
    bundle["subscription_revision"] = 1
    write_bundle(bundle, BUILD / "bundles")
    return bundle


def tenant_b_overlay(limit):
    return {"overlay_id": "ovl_b_hsa_limit", "version": 1,
            "product": "hsa_reimbursement@1",
            "overrides": {"thresholds.auto_approve_limit": limit}}


def main():
    product = yaml.safe_load(PRODUCT_YAML.read_text())
    if product["thresholds"]["auto_approve_limit"] != 500.0:
        raise SystemExit("expected product default auto_approve_limit=500")
    # claimant_amount_sum_24h is cumulative across the window — each phase
    # uses a fresh participant so the same $320 receipt scenario is judged
    # on identical feature values.

    print("== baseline: tenant_b $320 receipt under $250 overlay ==")
    images0 = image_digests()
    migrations0 = migration_set()
    base = decide("demo-client-b", f"buc3_{RUN}_base",
                  f"tok_buc3_base_{RUN}")
    print(f"  {base['outcome']} reasons={base.get('reason_codes')} "
          f"bundle={base['bundle_digest'][:23]} "
          f"epoch={base['manifest_epoch']}")
    if base["outcome"] != "DECISION_REVIEW":
        raise SystemExit("baseline must REVIEW under the $250 overlay — "
                         "check seeded tenant_b subscription")
    ev["baseline"] = {"outcome": base["outcome"],
                      "reason_codes": base.get("reason_codes"),
                      "bundle_digest": base["bundle_digest"],
                      "manifest_epoch": base["manifest_epoch"]}

    print("== change: compile overlay 250 -> 400, activate new epoch ==")
    bundle_400 = compile_hsa_bundle(tenant_b_overlay(400.0))
    activate(bundle_400)
    print(f"  new bundle {bundle_400['digest'][:23]} activated")
    after = decide("demo-client-b", f"buc3_{RUN}_after",
                   f"tok_buc3_after_{RUN}")
    print(f"  same scenario -> {after['outcome']} "
          f"reasons={after.get('reason_codes')} "
          f"bundle={after['bundle_digest'][:23]} "
          f"epoch={after['manifest_epoch']}")
    assert after["outcome"] == "DECISION_APPROVE"
    assert after["bundle_digest"] == bundle_400["digest"]
    assert after["manifest_epoch"] != base["manifest_epoch"]
    ev["after_change"] = {"outcome": after["outcome"],
                          "reason_codes": after.get("reason_codes"),
                          "bundle_digest": after["bundle_digest"],
                          "manifest_epoch": after["manifest_epoch"]}

    images1 = image_digests()
    migrations1 = migration_set()
    ev["no_rebuild_proof"] = {
        "image_digests_identical": images0 == images1,
        "image_digests": images1,
        "migration_set_identical": migrations0 == migrations1,
        "migration_set": migrations1}
    assert images0 == images1, "no service image may change"
    assert migrations0 == migrations1, "no migration may be added"
    print(f"  image digests unchanged: {len(images1)} services; "
          f"migration set unchanged: {list(migrations1)}")

    print("== rollback: re-activate the $250 bundle ==")
    bundle_250 = compile_hsa_bundle(tenant_b_overlay(250.0))
    activate(bundle_250)
    back = decide("demo-client-b", f"buc3_{RUN}_rollback",
                  f"tok_buc3_back_{RUN}")
    print(f"  {back['outcome']} reasons={back.get('reason_codes')} "
          f"bundle={back['bundle_digest'][:23]} "
          f"epoch={back['manifest_epoch']}")
    assert back["outcome"] == "DECISION_REVIEW"
    ev["rollback"] = {"outcome": back["outcome"],
                      "bundle_digest": back["bundle_digest"],
                      "manifest_epoch": back["manifest_epoch"]}

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(ev, indent=2))
    print(f"\nevidence -> {EVIDENCE}")


if __name__ == "__main__":
    main()
