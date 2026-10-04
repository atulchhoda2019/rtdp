#!/usr/bin/env python3
"""G8 — ADR-013 agent identity + delegation gate.

Runs against any ingress + agent-registry pair:

    RTDP_INGRESS=http://localhost:8080 \\
    RTDP_REGISTRY=http://localhost:8090 \\
        python tests/e2e/test_agent_g8.py

Requires the stack seeded with `make seed` (demo agents + grants from
tools/seed/seed.py). Every check is live HTTP — no optional probes.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error

INGRESS = os.environ.get("RTDP_INGRESS", "http://localhost:8080")
REGISTRY = os.environ.get("RTDP_REGISTRY", "http://localhost:8090")
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
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def reg(method, path, body=None):
    return _req(method, REGISTRY, path, body)


def decide(body):
    body.setdefault("transaction_id", f"g8_{RUN}_{time.time_ns()}")
    body.setdefault("transaction_revision", 1)
    body.setdefault("channel", "PORTAL")
    body.setdefault("region", "us-east-1")
    body.setdefault("tokenized_claimant", f"tok_g8_{RUN}")
    body.setdefault("provider_id", "prv_g8")
    body.setdefault("currency", "USD")
    body.setdefault("amount", 1200.0)
    body.setdefault("event_time",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return _req("POST", INGRESS, "/v1/decide", body,
                {"X-RTDP-Client-Id": "demo-client-a"})


def mint(principal_kind, principal_id, agent_ids, ttl_seconds=None):
    body = {"principal_kind": principal_kind, "principal_id": principal_id,
            "tenant_id": "tenant_a", "agent_ids": agent_ids}
    if ttl_seconds:
        body["ttl_seconds"] = ttl_seconds
    st, r = reg("POST", "/v1/delegations/mint", body)
    assert st == 200, f"mint failed: {st} {r}"
    return r["delegation_token"]


def grant_for(principal_id, agent_id):
    st, r = reg("GET", "/v1/grants"
                f"?principal_id={principal_id}&agent_id={agent_id}")
    assert st == 200, f"grants list failed: {st} {r}"
    live = [g for g in r["grants"] if not g["revoked"]]
    assert live, f"no live grant {principal_id}->{agent_id}"
    return live[0]["grant_id"]


def warm():
    """Cold services return transient 502 deadline errors after a
    rebuild — retry both decision paths until warm."""
    for i in range(15):
        st, r = decide({"event_type": "CLAIM_SUBMISSION"})
        if st == 200:
            st2, r2 = decide({"event_type": "CONTRIBUTION_CHANGE"})
            if st2 == 200:
                return
        time.sleep(2)
    print("warn: pipeline still cold after warm retries")


def main():
    warm()
    # --- G8a: valid chain traverses, delegation echoed on the fact ---
    tok = mint("PERSON", "guest-g", ["guest-agent-g"])
    for attempt in range(5):
        st, r = decide({"event_type": "CLAIM_SUBMISSION",
                        "delegation_token": tok})
        if st == 200:
            break
        time.sleep(1.5)
    ok = (st == 200 and r.get("outcome") in
          ("DECISION_APPROVE", "DECISION_REVIEW", "DECISION_DECLINE"))
    d = r.get("delegation") or {}
    ok = ok and d.get("principal", {}).get("id") == "guest-g" \
        and (d.get("links") or [{}])[-1].get("agent_id") == "guest-agent-g"
    check("G8a", "valid chain decides, delegation on result", ok,
          f"outcome={r.get('outcome')} links={[l.get('agent_id') for l in d.get('links', [])]}")

    # legacy caller, no chain — must keep working (spec: legacy path)
    st, r = decide({"event_type": "CLAIM_SUBMISSION"})
    check("G8a", "legacy caller unchanged",
          st == 200 and r.get("outcome", "").startswith("DECISION_")
          and r.get("outcome") != "DECISION_DECLINE_UNAUTHORIZED",
          f"outcome={r.get('outcome')}")

    # tampered signature -> rejected at edge
    bad = tok[:-8] + ("AAAAAAAA" if not tok.endswith("AAAAAAAA")
                      else "BBBBBBBB")
    st, r = decide({"event_type": "CLAIM_SUBMISSION",
                    "delegation_token": bad})
    check("G8a", "invalid signature rejected",
          st == 401 and r.get("outcome") == "DECISION_DECLINE_UNAUTHORIZED",
          f"status={st} reason={r.get('reason', '')[:60]}")

    # expired chain -> rejected at edge
    tok = mint("PERSON", "guest-g", ["guest-agent-g"], ttl_seconds=1)
    time.sleep(1.5)
    st, r = decide({"event_type": "CLAIM_SUBMISSION",
                    "delegation_token": tok})
    check("G8a", "expired chain rejected",
          st == 401 and r.get("outcome") == "DECISION_DECLINE_UNAUTHORIZED",
          f"status={st} reason={r.get('reason', '')[:60]}")

    # --- G8b: grant revoke -> next request denied, propagation <= 5s ---
    pid = f"guest-rev-{RUN}"
    st, r = reg("POST", "/v1/grants",
                {"principal_id": pid, "agent_id": "guest-agent-g",
                 "scopes": ["decide", "reservations"],
                 "purpose": "g8 test", "ttl_hours": 1})
    assert st == 201, f"grant create failed: {st} {r}"
    gid = r["grant_id"]
    tok = mint("PERSON", pid, ["guest-agent-g"])
    t0 = time.time()
    st, r = reg("POST", "/v1/revocations",
                {"target": gid, "reason": "g8 test"})
    assert st == 200, f"revoke failed: {st} {r}"
    st, r = decide({"event_type": "CLAIM_SUBMISSION",
                    "delegation_token": tok})
    el = time.time() - t0
    check("G8b", "revoked grant denied within 5s",
          st == 401 and r.get("outcome") == "DECISION_DECLINE_UNAUTHORIZED"
          and el <= 5.0,
          f"status={st} propagation={el:.2f}s")

    # --- G8c: chain deeper than tenant max rejected ---
    # Mint refuses over-depth chains too; to exercise the *edge* check we
    # raise the ceiling, mint, then restore — the token then violates the
    # live policy and must be denied at ingress.
    st, _ = reg("PUT", "/v1/tenants/tenant_a/config",
                {"max_chain_depth": 3})
    assert st == 200
    tok = mint("PERSON", "guest-g",
               ["guest-agent-g", "sub-agent-a", "sub-agent-b"])
    reg("PUT", "/v1/tenants/tenant_a/config", {"max_chain_depth": 2})
    st, r = decide({"event_type": "CLAIM_SUBMISSION",
                    "delegation_token": tok})
    check("G8c", "chain depth > tenant max rejected",
          st == 401 and r.get("outcome") == "DECISION_DECLINE_UNAUTHORIZED",
          f"status={st} reason={r.get('reason', '')[:60]}")

    # depth-2 chain is still legal (proves the ceiling isn't off-by-one)
    tok2 = mint("PERSON", "guest-g", ["guest-agent-g", "sub-agent-a"])
    st, r = decide({"event_type": "CLAIM_SUBMISSION",
                    "delegation_token": tok2})
    check("G8c", "depth-2 chain accepted",
          st == 200 and r.get("outcome") != "DECISION_DECLINE_UNAUTHORIZED",
          f"status={st} outcome={r.get('outcome')}")

    # --- G8d: required_scope enforced against the selected product ---
    # contribution_change@1 declares required_scope: benefits.
    # guest-agent-g holds [decide, reservations] -> unauthorized.
    tok = mint("PERSON", "guest-g", ["guest-agent-g"])
    st, r = decide({"event_type": "CONTRIBUTION_CHANGE",
                    "delegation_token": tok})
    check("G8d", "missing product scope -> DECLINE_UNAUTHORIZED",
          st == 200 and r.get("outcome") == "DECISION_DECLINE_UNAUTHORIZED",
          f"outcome={r.get('outcome')} "
          f"reasons={r.get('reason_codes', [])}")

    # benefits-agent-1 holds `benefits` -> normal evaluation proceeds.
    # PENDING_APPROVAL is a normal outcome here: the agent is T1-capped
    # (ADR-014) and T2 actions wait for a human — scope still passed.
    tok = mint("PERSON", "participant-p", ["benefits-agent-1"])
    st, r = decide({"event_type": "CONTRIBUTION_CHANGE",
                    "delegation_token": tok})
    check("G8d", "scoped chain evaluates normally",
          st == 200 and r.get("outcome") in
          ("DECISION_APPROVE", "DECISION_REVIEW", "DECISION_DECLINE",
           "DECISION_PENDING_APPROVAL"),
          f"outcome={r.get('outcome')}")

    # legacy (no chain) on the scoped product still works — scope rules
    # bind delegated callers only, per ADR-013 compatibility contract.
    st, r = decide({"event_type": "CONTRIBUTION_CHANGE"})
    check("G8d", "legacy caller on scoped product unaffected",
          st == 200 and r.get("outcome") != "DECISION_DECLINE_UNAUTHORIZED",
          f"outcome={r.get('outcome')}")

    fails = [x for x in results if not x[2]]
    print(f"\n{len(results) - len(fails)}/{len(results)} checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
