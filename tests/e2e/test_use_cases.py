#!/usr/bin/env python3
"""Use-case suite — one check per demo use case (docs/demo-use-cases.md).

Runs against any ingress endpoint: RTDP_INGRESS=http://localhost:8080 for
port-forward/tunnel, or the in-cluster service DNS from a pod.

    RTDP_INGRESS=http://localhost:8080 python tests/e2e/test_use_cases.py

Exit 0 = all checks passed. Designed to be re-runnable: every request uses
fresh transaction ids and fresh claimants so tier-1 state never makes the
suite order-dependent.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

INGRESS = os.environ.get("RTDP_INGRESS", "http://localhost:8080")
RUN = f"{int(time.time())}"
CLIENTS = {"a": "demo-client-a", "b": "demo-client-b"}
EVENT_TYPES = ("CLAIM_SUBMISSION", "POLICY_APPLICATION", "QUOTE_REQUEST",
               "CLAIM_DOCUMENT_INTAKE")
VALID_OUTCOMES = ("DECISION_APPROVE", "DECISION_REVIEW", "DECISION_DECLINE")

results = []


def check(uc, name, ok, detail=""):
    results.append((uc, name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {uc} {name}  {detail}")


def decide(client_id, txn, timeout=40):
    req = urllib.request.Request(
        f"{INGRESS}/v1/decide",
        data=json.dumps(txn).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client_id},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def txn(event_type, tag, claimant=None, amount=2500.0):
    return {
        "transaction_id": f"uc_{RUN}_{tag}",
        "transaction_revision": 1,
        "event_type": event_type,
        "channel": "PORTAL",
        "region": "us-east-1",
        "tokenized_claimant": claimant or f"tok_uc_{RUN}",
        "provider_id": "prv_uc",
        "currency": "USD",
        "amount": amount,
        "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def decide_retry(client_id, t, attempts=10, timeout=40):
    """Deadline rejection is a designed outcome (total_deadline_ms bounds
    the path), not a correctness failure — retry; structural errors
    return immediately."""
    for _ in range(attempts):
        status, body = decide(client_id, t, timeout)
        if status == 0 or "DeadlineExceeded" in str(body.get("error", "")):
            time.sleep(0.3)
            continue
        return status, body
    return status, body


def main():
    # UC-08: unknown tenant rejected before any decision work.
    status, body = decide("rogue-client", txn("CLAIM_SUBMISSION", "rogue"))
    check("UC-08", "unknown_tenant", status == 401, f"http={status}")

    # UC-09: missing routing fields rejected.
    bad = txn("CLAIM_SUBMISSION", "nofields")
    del bad["tokenized_claimant"]
    status, body = decide(CLIENTS["a"], bad)
    check("UC-09", "missing_fields", status == 400, f"http={status}")

    # UC-01/02/03: fast-path decisions, each product family.
    for event in EVENT_TYPES[:3]:
        status, r = decide_retry(CLIENTS["a"], txn(event, f"fast_{event}"))
        ok = status == 200 and r.get("outcome") in VALID_OUTCOMES
        check("UC-01/02/03", event, ok,
              f"http={status} outcome={r.get('outcome')} "
              f"reasons={r.get('reason_codes')}")

    # UC-04: SLM document-intake path (slow by design — 30s budget).
    status, r = decide_retry(CLIENTS["a"], txn("CLAIM_DOCUMENT_INTAKE",
                                               "docintake"))
    ok = status == 200 and r.get("outcome") in VALID_OUTCOMES
    check("UC-04", "document_intake_slm", ok,
          f"http={status} outcome={r.get('outcome')} "
          f"reasons={r.get('reason_codes')}")

    # UC-11: every decision carries pinned replay coordinates.
    ok = (bool(r.get("bundle_digest", "").startswith("sha256:"))
          and bool(r.get("manifest_epoch")))
    check("UC-11", "pinned_addressing", ok,
          f"bundle={r.get('bundle_digest', '')[:19]}… "
          f"epoch={r.get('manifest_epoch')}")

    # UC-10: same event type resolves different bundles per tenant.
    status_a, ra = decide_retry(CLIENTS["a"], txn("CLAIM_SUBMISSION",
                                                  "pin_a"))
    status_b, rb = decide_retry(CLIENTS["b"], txn("CLAIM_SUBMISSION",
                                                  "pin_b"))
    ok = (status_a == 200 and status_b == 200
          and ra.get("bundle_digest") != rb.get("bundle_digest")
          and ra.get("bundle_digest", "").startswith("sha256:"))
    check("UC-10", "tenant_bundle_pinning", ok,
          f"a={ra.get('bundle_digest', '')[:19]}… "
          f"b={rb.get('bundle_digest', '')[:19]}…")

    # UC-06: identical retry returns the same decision id.
    t = txn("CLAIM_SUBMISSION", "idem", claimant=f"tok_idem_{RUN}")
    s1, r1 = decide_retry(CLIENTS["a"], t)
    s2, r2 = decide_retry(CLIENTS["a"], t)
    ok = (s1 == 200 and s2 == 200
          and r1.get("decision_id") == r2.get("decision_id"))
    check("UC-06", "idempotent_retry", ok,
          f"decision_id={r1.get('decision_id', '')[:18]}…")

    # UC-07: same transaction id, mutated payload => dedup conflict.
    s3, r3 = decide(CLIENTS["a"], {**t, "amount": 99999.0})
    ok = s3 not in (200, 201) and "CONFLICT" in json.dumps(r3)
    check("UC-07", "payload_conflict", ok, f"http={s3} body={r3}")

    # UC-05: velocity_decline_count=12 — request 13 for a fresh claimant
    # must DECLINE with VELOCITY_LIMIT; 1-12 must not carry the reason.
    claimant = f"tok_vel_{RUN}"
    saw_early = None
    declined = None
    for i in range(1, 14):
        status, r = decide_retry(CLIENTS["a"],
                                 txn("CLAIM_SUBMISSION", f"vel_{i}",
                                     claimant=claimant))
        if status != 200:
            continue
        reasons = r.get("reason_codes") or []
        if i <= 12 and "VELOCITY_LIMIT" in reasons:
            saw_early = i
        if i == 13:
            declined = (r.get("outcome") == "DECISION_DECLINE"
                        and "VELOCITY_LIMIT" in reasons)
    check("UC-05", "velocity_limit", saw_early is None and declined,
          f"early_trigger_at={saw_early} req13_declined={declined}")

    failed = [r for r in results if not r[2]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
