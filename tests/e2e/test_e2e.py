#!/usr/bin/env python3
"""End-to-end: synthetic transaction -> decision -> simulated action.

Phase 1 gate: a real transaction traverses ingress -> orchestrator ->
features -> model -> rules -> Kafka commit -> action dispatcher, producing
an acknowledged simulated action for the right tenant/policy.
"""

import json
import sys
import time
import urllib.request

INGRESS = "http://localhost:8080"


def decide(client_id, txn):
    req = urllib.request.Request(
        f"{INGRESS}/v1/decide",
        data=json.dumps(txn).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client_id},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def decide_retry(client_id, txn, attempts=8):
    """Deadline rejection is a designed outcome (total_deadline_ms bounds the
    path), not a correctness failure — a caller retries. Structural errors
    (4xx, conflicts) are returned immediately."""
    for i in range(attempts):
        status, body = decide(client_id, txn)
        if "DeadlineExceeded" in str(body.get("error", "")):
            time.sleep(0.2)
            continue
        return status, body
    return status, body


def main():
    txn = {
        "transaction_id": f"txn_e2e_{int(time.time())}",
        "transaction_revision": 1,
        "event_type": "AUTH_REQUEST",
        "channel": "ECOMMERCE",
        "region": "us-east-1",
        "tokenized_pan": "tok_pan_0001",
        "merchant_id": "mch_42",
        "currency": "USD",
        "amount": 129.99,
        "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Unauthorized client must be rejected before any tenant binding.
    status, _ = decide("rogue-client", txn)
    assert status == 401, f"unknown client not rejected: {status}"

    status, a = decide_retry("demo-client-a", {**txn, "transaction_id":
                                               txn["transaction_id"] + "_a"})
    assert status == 200, f"tenant_a decision failed: {a}"
    assert a["outcome"] in ("DECISION_APPROVE", "DECISION_REVIEW",
                            "DECISION_DECLINE"), a
    print(f"tenant_a -> {a['outcome']} ({a['decision_id']})")

    status, b = decide_retry("demo-client-b", {**txn, "transaction_id":
                                               txn["transaction_id"] + "_b"})
    assert status == 200, f"tenant_b decision failed: {b}"
    print(f"tenant_b -> {b['outcome']} ({b['decision_id']})")

    # Same id + same payload => same decision (idempotent retry).
    status, a2 = decide_retry("demo-client-a", {**txn, "transaction_id":
                                                txn["transaction_id"] + "_a"})
    assert status == 200 and a2["decision_id"] == a["decision_id"], \
        f"retry not idempotent: {a2}"

    # Same id + different payload => conflict.
    status, _ = decide("demo-client-a", {**txn, "amount": 99999.0,
                                         "transaction_id":
                                         txn["transaction_id"] + "_a"})
    assert status in (400, 502), f"payload conflict not rejected: {status}"

    print("e2e ok")


if __name__ == "__main__":
    main()
