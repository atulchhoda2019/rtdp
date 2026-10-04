#!/usr/bin/env python3
"""G9 — ADR-014 autonomy tiers + human-approval gate.

Runs against a seeded local stack:

    RTDP_INGRESS=http://localhost:8080 \\
    RTDP_REGISTRY=http://localhost:8090 \\
    RTDP_APPROVAL=http://localhost:8095 \\
        python tests/e2e/test_agent_g9.py

Requires `make seed` (demo agents benefits-agent-1 [T1 cap] and
benefits-admin-2 [T2 cap], tiered contribution_actions policy) plus the
approval-service and action-dispatcher consuming rtdp.action.commands.v1.
DB assertions shell out to docker-compose psql (RTDP_PSQL override).
"""

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
import urllib.error

INGRESS = os.environ.get("RTDP_INGRESS", "http://localhost:8080")
REGISTRY = os.environ.get("RTDP_REGISTRY", "http://localhost:8090")
APPROVAL = os.environ.get("RTDP_APPROVAL", "http://localhost:8095")
PSQL = os.environ.get(
    "RTDP_PSQL",
    "docker compose exec -T postgres psql -U rtdp -d rtdp -tAc")
RUN = f"{int(time.time())}"

results = []


def check(uc, name, ok, detail=""):
    results.append((uc, name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {uc} {name}  {detail}")


def _req(method, base, path, body=None, headers=None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def reg(method, path, body=None):
    return _req(method, REGISTRY, path, body)


def appr(method, path, body=None):
    return _req(method, APPROVAL, path, body)


def decide(body):
    body.setdefault("transaction_id", f"g9_{RUN}_{time.time_ns()}")
    body.setdefault("transaction_revision", 1)
    body.setdefault("channel", "PORTAL")
    body.setdefault("region", "us-east-1")
    body.setdefault("provider_id", "prv_g9")
    body.setdefault("currency", "USD")
    body.setdefault("event_time",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return _req("POST", INGRESS, "/v1/decide", body,
                {"X-RTDP-Client-Id": "demo-client-a"})


def mint(principal_id, agent_ids):
    st, r = reg("POST", "/v1/delegations/mint",
                {"principal_kind": "PERSON", "principal_id": principal_id,
                 "tenant_id": "tenant_a", "agent_ids": agent_ids})
    assert st == 200, f"mint failed: {st} {r}"
    return r["delegation_token"]


def intents_by_type(r):
    return {i["action_type"]: i for i in r.get("intents", [])}


def exec_state(decision_id, action_type):
    sql = ("SELECT state FROM action_execution "
           f"WHERE decision_id='{decision_id}' AND action_type='{action_type}'")
    out = subprocess.run(
        shlex.split(PSQL) + [sql], capture_output=True, text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))))
    return out.stdout.strip()


def poll_exec_state(decision_id, action_type, want, timeout_s=20):
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        last = exec_state(decision_id, action_type)
        if last in want:
            return last
        time.sleep(1.0)
    return last


def poll_pending(decision_id, pred, timeout_s=20):
    deadline = time.time() + timeout_s
    rows = []
    while time.time() < deadline:
        st, r = appr("GET", "/v1/approvals/pending?tenant_id=tenant_a")
        if st == 200:
            rows = [p for p in (r.get("pending") or [])
                    if p["decision_id"] == decision_id]
            if rows and all(pred(p) for p in rows):
                return rows
        time.sleep(1.0)
    return rows


def warm():
    """Retry a legacy decide until the pipeline is warm — cold services
    return transient 502 deadline errors after a rebuild."""
    for i in range(12):
        st, r = decide({"event_type": "CONTRIBUTION_CHANGE",
                        "tokenized_claimant": f"tok_g9_warm_{RUN}_{i}",
                        "amount": 500.0})
        if st == 200 and r.get("outcome", "").startswith("DECISION_"):
            return
        time.sleep(3)
    print("warn: pipeline still cold after warm retries")


def held_decide(agent, amount, claimant=None):
    tok = mint("participant-p", [agent])
    body = {"event_type": "CONTRIBUTION_CHANGE",
            "delegation_token": tok, "amount": amount,
            "tokenized_claimant": claimant or f"tok_g9_{RUN}_{agent}"}
    for attempt in range(6):
        body["transaction_id"] = f"g9_{RUN}_{time.time_ns()}"
        st, r = decide(body)
        if st == 200:
            return r
        # transient broker/cold-path deadline — safe to retry: an aborted
        # durable commit never lands, so a fresh txn id can't double-apply
        if "DeadlineExceeded" not in str(r.get("error", "")):
            break
        time.sleep(1.5)
    assert False, f"decide failed after retries: {st} {r}"


def main():
    warm()
    # --- G9a: per-action autonomy + agent max_autonomy cap ----------
    # T2 agent, small amount: APPLY (T1, cond amount<=t1_limit) +
    # NOTIFY (T2) both complete alone -> APPROVE, no holds.
    r = held_decide("benefits-admin-2", 2000.0)
    its = intents_by_type(r)
    check("G9a", "T2 agent + small amount completes alone",
          r.get("outcome") == "DECISION_APPROVE"
          and all(i["status"] == "INTENT_READY" for i in its.values()),
          f"outcome={r.get('outcome')} "
          f"{ {k: v['status'] for k, v in its.items()} }")

    # T2 agent, large amount: APPLY (T1, condition false) is held but
    # NOTIFY (T2) still completes -> per-intent autonomy, not atomic.
    r = held_decide("benefits-admin-2", 4000.0)
    its = intents_by_type(r)
    check("G9a", "T2 agent: held T1 does not block ready T2",
          r.get("outcome") == "DECISION_PENDING_APPROVAL"
          and its.get("APPLY_CONTRIBUTION_CHANGE", {}).get("status")
          == "INTENT_AWAITING_APPROVAL"
          and its.get("NOTIFY_PARTICIPANT", {}).get("status")
          == "INTENT_READY",
          f"outcome={r.get('outcome')} "
          f"{ {k: v['status'] for k, v in its.items()} }")

    # T1 agent, large amount: agent ceiling caps NOTIFY's T2 -> held
    # too. Both intents wait.
    r_t1 = held_decide("benefits-agent-1", 4000.0)
    its = intents_by_type(r_t1)
    check("G9a", "T1 agent ceiling caps T2 action -> held",
          r_t1.get("outcome") == "DECISION_PENDING_APPROVAL"
          and all(i["status"] == "INTENT_AWAITING_APPROVAL"
                  for i in its.values()),
          f"outcome={r_t1.get('outcome')} "
          f"{ {k: v['status'] for k, v in its.items()} }")
    did = r_t1["decision_id"]

    # --- G9b: approval gate — self-approval and non-listed barred ---
    rows = poll_pending(did, lambda p: True, timeout_s=15)
    check("G9b", "held intents projected to approval queue",
          len(rows) >= 2, f"rows={len(rows)}")

    st, _ = appr("POST", f"/v1/approvals/{did}",
                 {"approver_identity": "benefits-agent-1",
                  "verdict": "APPROVE"})
    check("G9b", "requesting agent cannot self-approve",
          st == 403, f"status={st}")
    st, _ = appr("POST", f"/v1/approvals/{did}",
                 {"approver_identity": "participant-p",
                  "verdict": "APPROVE"})
    check("G9b", "requesting principal cannot self-approve",
          st == 403, f"status={st}")
    st, _ = appr("POST", f"/v1/approvals/{did}",
                 {"approver_identity": "random-bob", "verdict": "APPROVE"})
    check("G9b", "non-listed approver rejected",
          st == 403, f"status={st}")

    # --- G9c: listed approver releases -> dispatcher executes -------
    st, r = appr("POST", f"/v1/approvals/{did}",
                 {"approver_identity": "plan_admin",
                  "verdict": "APPROVE", "note": "g9 test"})
    check("G9c", "listed approver releases held intents",
          st == 200 and r.get("verdict") == "RELEASED",
          f"status={st} resp={r}")

    st_notify = poll_exec_state(did, "NOTIFY_PARTICIPANT",
                                ("ACKNOWLEDGED",), timeout_s=25)
    check("G9c", "released intent dispatched to adapter",
          st_notify == "ACKNOWLEDGED", f"state={st_notify}")

    # REJECT path: a second held decision, rejected -> CANCELLED, never
    # dispatched.
    r2 = held_decide("benefits-agent-1", 4000.0,
                     claimant=f"tok_g9_{RUN}_rej")
    did2 = r2["decision_id"]
    poll_pending(did2, lambda p: True, timeout_s=15)
    st, r = appr("POST", f"/v1/approvals/{did2}",
                 {"approver_identity": "plan_admin",
                  "verdict": "REJECT", "note": "g9 reject"})
    st_exec = poll_exec_state(did2, "NOTIFY_PARTICIPANT",
                              ("CANCELLED",), timeout_s=20)
    check("G9c", "rejected intents marked CANCELLED, not dispatched",
          st == 200 and st_exec == "CANCELLED",
          f"approve_status={st} exec={st_exec}")

    # --- G9d: SLA breach -> escalation ------------------------------
    # Fresh claimant + amount>fraud_amount -> REVIEW -> PLAN_AUDIT
    # (T0, sla_seconds=6, ESCALATE) held; sweeper escalates it.
    r3 = held_decide("benefits-admin-2", 6000.0,
                     claimant=f"tok_g9_{RUN}_audit")
    its3 = intents_by_type(r3)
    did3 = r3["decision_id"]
    check("G9d", "REVIEW decision emits T0 held intent",
          r3.get("outcome") == "DECISION_REVIEW"
          and its3.get("PLAN_AUDIT", {}).get("status")
          == "INTENT_AWAITING_APPROVAL",
          f"outcome={r3.get('outcome')} "
          f"{ {k: v['status'] for k, v in its3.items()} }")

    rows = poll_pending(
        did3, lambda p: p["action_type"] != "PLAN_AUDIT" or p["escalated"],
        timeout_s=20)
    esc = [p for p in rows if p["action_type"] == "PLAN_AUDIT"]
    check("G9d", "SLA breach escalates to escalation approvers",
          bool(esc) and esc[0]["escalated"]
          and esc[0]["state"] == "ESCALATED",
          f"rows={rows}")

    # --- G9e: compile gate — ownerless action policy rejected -------
    try:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "services/control-plane"))
        from rtdp_contracts.compiler import _validate_action_policy
        try:
            _validate_action_policy(
                {"allowed_actions": ["X"], "adapters": {"X": "a@1"},
                 "action_rules": {"X": ["APPROVE"]}},
                "ownerless", 1, "platform")
            check("G9e", "ownerless action policy fails compile",
                  False, "no error raised")
        except Exception as e:
            check("G9e", "ownerless action policy fails compile",
                  "MISSING_OWNER" in str(e) or "owner" in str(e),
                  f"{type(e).__name__}: {str(e)[:60]}")
    except ImportError as e:
        check("G9e", "ownerless action policy fails compile",
              False, f"contracts import failed: {e}")

    fails = [x for x in results if not x[2]]
    print(f"\n{len(results) - len(fails)}/{len(results)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
