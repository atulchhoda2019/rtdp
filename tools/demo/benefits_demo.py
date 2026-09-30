#!/usr/bin/env python3
"""Benefits demo runner — BUC-1..BUC-5 (docs/demo-benefits.md).

Drives the seeded benefits products through the live decision API and
prints a run log: outcome, reason codes, pinned bundle digest, manifest
epoch, latency. Same HTTP surface as tests/e2e/test_benefits_use_cases.py —
no service code involved; everything shown here is configuration.

    RTDP_INGRESS=http://localhost:8080 python tools/demo/benefits_demo.py
    RTDP_INGRESS=http://localhost:8080 python tools/demo/benefits_demo.py --only hsa

Scenarios:
  dep     BUC-1 dependent verification fast-track (SLM doc consistency)
  hsa     BUC-2 HSA reimbursement — same $320 receipt, Client A vs Client B
  change  BUC-3 pointer to tools/demo/benefits_live_change.py
  contrib BUC-4 contribution change — approve path + takeover pattern
  shadow  BUC-5 champion vs challenger digests from the activation manifest
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

INGRESS = os.environ.get("RTDP_INGRESS", "http://localhost:8080")
ROOT = Path(__file__).resolve().parents[2]
RUN = f"{int(time.time())}"
CLIENTS = {"a": "demo-client-a", "b": "demo-client-b"}


def decide(client_id, txn, timeout=40):
    req = urllib.request.Request(
        f"{INGRESS}/v1/decide",
        data=json.dumps(txn).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client_id},
        method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
            return r.status, body, time.time() - started
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"error": f"http {e.code} (edge-masked body)"}
        return e.code, body, time.time() - started
    except Exception as e:
        return 0, {"error": str(e)}, time.time() - started


def txn(event_type, tag, claimant=None, amount=2500.0):
    return {
        "transaction_id": f"bdemo_{RUN}_{tag}",
        "transaction_revision": 1,
        "event_type": event_type,
        "channel": "PORTAL",
        "region": "us-east-1",
        "tokenized_claimant": claimant or f"tok_bdemo_{RUN}_{tag}",
        "provider_id": "prv_bdemo",
        "currency": "USD",
        "amount": amount,
        "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def show(label, status, r, latency):
    dig = (r.get("bundle_digest") or "")[:19]
    print(f"  [{status:>3}] {label:<44} "
          f"{r.get('outcome', 'ERR'):<16} "
          f"{str(r.get('reason_codes')):<46} "
          f"bundle={dig}… epoch={r.get('manifest_epoch', '-')} "
          f"{latency * 1000:7.0f}ms")


def scenario_dep():
    print("\n== BUC-1 dependent_verification — SLM doc consistency ==")
    print("   'AI approves, humans handle the rest' — the action policy")
    print("   binds ENROLL_DEPENDENT to APPROVE only; no DECLINE exists.")
    s, r, lat = decide(CLIENTS["a"],
                       txn("DEPENDENT_VERIFICATION", "dep", amount=1200.0))
    show("dependent verification (SLM ~2-12s)", s, r, lat)
    # Replayability: identical retry must return the same decision_id.
    t = txn("DEPENDENT_VERIFICATION", "dep_replay", amount=1200.0)
    s1, r1, _ = decide(CLIENTS["a"], t)
    s2, r2, _ = decide(CLIENTS["a"], t)
    same = r1.get("decision_id") == r2.get("decision_id")
    print(f"  replay: identical request -> "
          f"{'same' if same else 'DIFFERENT'} decision_id "
          f"({r1.get('decision_id', '')[:18]}…)")


def scenario_hsa():
    print("\n== BUC-2 hsa_reimbursement — per-client auto-approve limits ==")
    print("   Same product, same model, same $320 receipt.")
    print("   Client A threshold $500; Client B overlay $250.")
    for cid, name in (("a", "Client A ($500)"), ("b", "Client B ($250)")):
        s, r, lat = decide(CLIENTS[cid],
                           txn("HSA_CLAIM", f"hsa_{cid}",
                               claimant=f"tok_hsa_{cid}_{RUN}",
                               amount=320.0))
        show(f"HSA receipt $320 — {name}", s, r, lat)


def scenario_change():
    print("\n== BUC-3 live plan change — run the dedicated demo ==")
    print("   python tools/demo/benefits_live_change.py")
    print("   Compiles tenant_b's overlay 250 -> 400 under a new epoch,")
    print("   re-submits the $320 scenario before/after, proves image")
    print("   digests + migration version unchanged, then rolls back.")


def scenario_contrib():
    print("\n== BUC-4 contribution_change — decision vs effect ==")
    print("   APPLY_CONTRIBUTION_CHANGE -> local_timeout_simulator: the")
    print("   decision is APPROVE, the provider effect lands UNKNOWN")
    print("   (on_unknown_outcome: RECONCILE — no blind retry).")
    s, r, lat = decide(CLIENTS["a"],
                       txn("CONTRIBUTION_CHANGE", "cc_small",
                           amount=200.0))
    show("small change $200 (first-time participant)", s, r, lat)
    print(f"   decision_id={r.get('decision_id')} — check "
          "action_execution.state=UNKNOWN:")
    print("   docker compose exec -T postgres psql -U rtdp -d rtdp -c "
          f"\"select state, provider_reference from action_execution "
          f"where decision_id='{r.get('decision_id')}'\"")
    s, r, lat = decide(CLIENTS["a"],
                       txn("CONTRIBUTION_CHANGE", "cc_takeover",
                           amount=8000.0))
    show("large change $8000 (first-time participant)", s, r, lat)


def scenario_shadow():
    print("\n== BUC-5 shadow challenger — champion/challenger pinning ==")
    acts_path = ROOT / "build" / "bundles" / "activations.json"
    if not acts_path.exists():
        print("   activations.json not readable — run `make seed` first,")
        print("   or inspect the deployed manifest in the bundles bucket.")
        return
    acts = json.loads(acts_path.read_text())
    for a in acts:
        types = (((a.get("bundle") or {}).get("effective_config") or {})
                 .get("routing") or {}).get("event_types") or []
        if a.get("tenant_id") == "tenant_a" and "HSA_CLAIM" in types:
            sigs = (a.get("bundle") or {}).get("signals") or []
            model = next((s.get("model") for s in sigs
                          if s.get("alias") == "hsa"), "?")
            print(f"   cohort={a.get('cohort'):<9} "
                  f"bundle={a.get('bundle_digest', '')[:23]} "
                  f"model={model}")
    print("   ActivationFor matches tenant+env+event_type and returns the")
    print("   first entry — the champion serves live traffic today; the")
    print("   shadow entry pins the challenger manifest for inspection.")
    print("   Champion vs challenger scores on identical features:")
    print("   RTDP_INFERENCE_ADDR=localhost:50051 "
          "python tests/e2e/test_benefits_use_cases.py")


SCENARIOS = {"dep": scenario_dep, "hsa": scenario_hsa,
             "change": scenario_change, "contrib": scenario_contrib,
             "shadow": scenario_shadow}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", choices=list(SCENARIOS) + ["all"],
                    default="all")
    args = ap.parse_args()
    print(f"benefits demo -> {INGRESS} (run {RUN})")
    names = list(SCENARIOS) if args.only == "all" else [args.only]
    for n in names:
        SCENARIOS[n]()
    print("\ndone — everything shown is configuration: products, rulesets,")
    print("signal contracts, bindings, action policies and tenant overlays.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
