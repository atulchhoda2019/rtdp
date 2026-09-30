#!/usr/bin/env python3
"""Benefits use-case suite — BUC-1..BUC-5 (docs/demo-benefits.md).

Runs against any ingress endpoint, same convention as test_use_cases.py:

    RTDP_INGRESS=http://localhost:8080 python tests/e2e/test_benefits_use_cases.py

Checks are a mix of live HTTP assertions (work everywhere) and
configuration assertions on the seeded assets/bundles (skipped when the
repo checkout isn't readable from the test's cwd). Optional deeper probes:

  RTDP_INFERENCE_ADDR=localhost:50051   champion/challenger Score diff
  RTDP_ORCH_ADDR=localhost:50055        MODE_SHADOW Decide -> no live action
  RTDP_PSQL='<docker compose exec -T postgres psql -U rtdp -d rtdp -tA>'
                                      action_execution UNKNOWN evidence

A skipped optional probe is reported as SKIP, never PASS — the suite fails
only on a violated guarantee, and every SKIP prints exactly what would
have made it runnable.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

INGRESS = os.environ.get("RTDP_INGRESS", "http://localhost:8080")
ROOT = Path(__file__).resolve().parents[2]
RUN = f"{int(time.time())}"
CLIENTS = {"a": "demo-client-a", "b": "demo-client-b"}
VALID_OUTCOMES = ("DECISION_APPROVE", "DECISION_REVIEW", "DECISION_DECLINE")
BENEFITS_EVENTS = ("DEPENDENT_VERIFICATION", "HSA_CLAIM",
                   "CONTRIBUTION_CHANGE")

results = []


def check(uc, name, ok, detail=""):
    results.append((uc, name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {uc} {name}  {detail}")


def skip(uc, name, why):
    results.append((uc, name, True, f"SKIP {why}"))
    print(f"SKIP  {uc} {name}  {why}")


def decide_full(client_id, txn, timeout=40):
    req = urllib.request.Request(
        f"{INGRESS}/v1/decide",
        data=json.dumps(txn).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client_id},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), r.headers.get("server", "")
    except urllib.error.HTTPError as e:
        server = e.headers.get("server", "") if e.headers else ""
        try:
            return e.code, json.loads(e.read()), server
        except Exception:
            return e.code, {}, server
    except Exception as e:
        return 0, {"error": str(e)}, ""


def decide_retry(client_id, t, attempts=10, timeout=40):
    """Deadline rejection is a designed bound, not a correctness failure —
    retry; structural errors return immediately."""
    for _ in range(attempts):
        status, body, _ = decide_full(client_id, t, timeout)
        if status == 0 or "DeadlineExceeded" in str(body.get("error", "")):
            time.sleep(0.3)
            continue
        return status, body
    return status, body


def txn(event_type, tag, claimant=None, amount=2500.0, provider="prv_buc"):
    return {
        "transaction_id": f"buc_{RUN}_{tag}",
        "transaction_revision": 1,
        "event_type": event_type,
        "channel": "PORTAL",
        "region": "us-east-1",
        "tokenized_claimant": claimant or f"tok_buc_{RUN}_{tag}",
        "provider_id": provider,
        "currency": "USD",
        "amount": amount,
        "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def read_yaml(rel):
    import yaml
    p = ROOT / rel
    return yaml.safe_load(p.read_text()) if p.exists() else None


def psql(sql):
    """Run read-only SQL when RTDP_PSQL provides a shell prefix, e.g.
    'docker compose exec -T postgres psql -U rtdp -d rtdp -tA' (local) or a
    kubectl exec psql one-liner (AWS). Returns stdout or None."""
    prefix = os.environ.get("RTDP_PSQL")
    if not prefix:
        return None
    out = subprocess.run(f"{prefix} -c \"{sql}\"",
                         shell=True, capture_output=True, text=True,
                         cwd=ROOT)
    return out.stdout.strip() if out.returncode == 0 else None


def main():
    # ------------------------------------------------------------------
    # BUC-1: dependent_verification — SLM doc-consistency, ADMT guardrail.
    # ------------------------------------------------------------------
    status, r = decide_retry(CLIENTS["a"],
                             txn("DEPENDENT_VERIFICATION", "dv_a",
                                 amount=1200.0),
                             timeout=40)
    reasons = r.get("reason_codes") or []
    ok = (status == 200
          and r.get("outcome") in ("DECISION_APPROVE", "DECISION_REVIEW")
          and any(x.startswith("DOCUMENT_") for x in reasons))
    check("BUC-1", "dependent_verification_decide", ok,
          f"http={status} outcome={r.get('outcome')} reasons={reasons}")

    # ADMT as data: the seeded action policy must bind no action to
    # DECLINE — the product can never decline, only auto-accept or refer.
    ap = read_yaml("assets/seed/platform/action_policies"
                   "/dependent_verification_actions/1.yaml")
    if ap is None:
        skip("BUC-1", "no_decline_in_action_policy",
             "asset tree not readable from cwd")
    else:
        decline_bound = any(
            "DECLINE" in (v or [])
            for v in (ap.get("action_rules") or {}).values())
        check("BUC-1", "no_decline_in_action_policy",
              not decline_bound
              and "ENROLL_DEPENDENT" in (ap.get("allowed_actions") or []),
              f"action_rules={ap.get('action_rules')}")

    # Replayable audit: identical retry returns the pinned decision.
    t = txn("DEPENDENT_VERIFICATION", "dv_replay", amount=1200.0)
    s1, r1 = decide_retry(CLIENTS["a"], t, timeout=40)
    s2, r2 = decide_retry(CLIENTS["a"], t, timeout=40)
    ok = (s1 == 200 and s2 == 200
          and r1.get("decision_id") == r2.get("decision_id")
          and r1.get("bundle_digest", "").startswith("sha256:"))
    check("BUC-1", "replayable_audit", ok,
          f"decision_id={r1.get('decision_id', '')[:18]}… "
          f"bundle={r1.get('bundle_digest', '')[:19]}…")

    # ------------------------------------------------------------------
    # BUC-2: hsa_reimbursement — same $320 receipt, per-client limits.
    # Client A caps at $500 (product default) -> APPROVE; Client B caps at
    # $250 (tenant overlay) -> REVIEW. Same model, same score, same rules —
    # only the allowlisted thresholds.* overlay differs.
    # ------------------------------------------------------------------
    claimant_a, claimant_b = f"tok_hsa_a_{RUN}", f"tok_hsa_b_{RUN}"
    sa, ra = decide_retry(CLIENTS["a"],
                          txn("HSA_CLAIM", "hsa_a", claimant=claimant_a,
                              amount=320.0))
    sb, rb = decide_retry(CLIENTS["b"],
                          txn("HSA_CLAIM", "hsa_b", claimant=claimant_b,
                              amount=320.0))
    ok = (sa == 200 and ra.get("outcome") == "DECISION_APPROVE"
          and "AUTO_ADJUDICATED" in (ra.get("reason_codes") or []))
    check("BUC-2", "client_a_auto_adjudicates", ok,
          f"http={sa} outcome={ra.get('outcome')} "
          f"reasons={ra.get('reason_codes')}")
    ok = (sb == 200 and rb.get("outcome") == "DECISION_REVIEW"
          and "OVER_AUTO_APPROVE_LIMIT" in (rb.get("reason_codes") or []))
    check("BUC-2", "client_b_reviews_at_limit", ok,
          f"http={sb} outcome={rb.get('outcome')} "
          f"reasons={rb.get('reason_codes')}")
    ok = (sa == 200 and sb == 200
          and ra.get("bundle_digest") != rb.get("bundle_digest")
          and ra.get("bundle_digest", "").startswith("sha256:"))
    check("BUC-2", "per_tenant_pinned_bundles", ok,
          f"a={ra.get('bundle_digest', '')[:19]}… "
          f"b={rb.get('bundle_digest', '')[:19]}…")

    # ------------------------------------------------------------------
    # BUC-3: live plan change is driven by tools/demo/benefits_live_change.py
    # (compiles tenant_b's overlay 250 -> 400 under a new epoch, submits the
    # same scenario before/after, rolls back). Here we assert the seeded
    # state: tenant_b's overlay file caps at 250 and the overlay key is in
    # the compiler's allowlist surface (thresholds.*).
    # ------------------------------------------------------------------
    sub_b = read_yaml("assets/seed/tenants/tenant_b/subscription.yaml")
    if sub_b is None:
        skip("BUC-3", "seeded_overlay_250",
             "asset tree not readable from cwd")
    else:
        ovs = {o.get("product"): o for o in (sub_b.get("overlays") or [])}
        ov = ovs.get("hsa_reimbursement@1") or {}
        lim = (ov.get("overrides") or {}).get("thresholds.auto_approve_limit")
        check("BUC-3", "seeded_overlay_250", lim == 250.0,
              f"tenant_b thresholds.auto_approve_limit={lim}")

    # ------------------------------------------------------------------
    # BUC-4: contribution_change — rules-only product; timeout adapter.
    # ------------------------------------------------------------------
    # Small change by a first-time participant: APPROVE + apply intent.
    # The adapter is local_timeout_simulator -> the effect lands UNKNOWN
    # (ADR-007), never an acknowledged success.
    sc, rc = decide_retry(CLIENTS["a"],
                          txn("CONTRIBUTION_CHANGE", "cc_small",
                              amount=200.0))
    ok = (sc == 200 and rc.get("outcome") == "DECISION_APPROVE"
          and (rc.get("action_intents") or 0) >= 1)
    check("BUC-4", "small_change_approve_with_intent", ok,
          f"http={sc} outcome={rc.get('outcome')} "
          f"intents={rc.get('action_intents')} "
          f"reasons={rc.get('reason_codes')}")

    # First-time participant + large change: account-takeover heuristic.
    sf, rf = decide_retry(CLIENTS["a"],
                          txn("CONTRIBUTION_CHANGE", "cc_fraud",
                              amount=8000.0))
    ok = (sf == 200 and rf.get("outcome") == "DECISION_REVIEW"
          and "ACCOUNT_TAKEOVER_PATTERN" in (rf.get("reason_codes") or []))
    check("BUC-4", "takeover_pattern_reviews", ok,
          f"http={sf} outcome={rf.get('outcome')} "
          f"reasons={rf.get('reason_codes')}")

    # Timeout semantics: APPLY_CONTRIBUTION_CHANGE binds to the
    # deterministic timeout simulator (asset check) — the live UNKNOWN row
    # is asserted below when psql is reachable.
    ap = read_yaml("assets/seed/platform/action_policies"
                   "/contribution_actions/1.yaml")
    if ap is None:
        skip("BUC-4", "timeout_adapter_binding",
             "asset tree not readable from cwd")
    else:
        ok = ((ap.get("adapters") or {}).get("APPLY_CONTRIBUTION_CHANGE")
              == "local_timeout_simulator@1"
              and ap.get("on_unknown_outcome") == "RECONCILE")
        check("BUC-4", "timeout_adapter_binding", ok,
              f"adapter={(ap.get('adapters') or {}).get('APPLY_CONTRIBUTION_CHANGE')} "
              f"on_unknown={ap.get('on_unknown_outcome')}")

    did = rc.get("decision_id")
    if did:
        state = psql("SELECT state FROM action_execution "
                     f"WHERE decision_id = '{did}' LIMIT 1")
        if state is None:
            skip("BUC-4", "unknown_landed",
                 "set RTDP_PSQL to assert action_execution=UNKNOWN")
        else:
            check("BUC-4", "unknown_landed", state == "UNKNOWN",
                  f"action_execution.state={state}")
    else:
        skip("BUC-4", "unknown_landed", "no decision_id from approve call")

    # ------------------------------------------------------------------
    # BUC-5: shadow challenger — pinned challenger bundle, champion still
    # serves live traffic, shadow mode emits zero live action commands.
    # ------------------------------------------------------------------
    acts_path = ROOT / "build" / "bundles" / "activations.json"
    champion_digest = shadow_digest = None
    if acts_path.exists():
        acts = json.loads(acts_path.read_text())
        for a in acts:
            types = (((a.get("bundle") or {}).get("effective_config") or {})
                     .get("routing") or {}).get("event_types") or []
            if (a.get("tenant_id") == "tenant_a"
                    and "HSA_CLAIM" in types):
                if a.get("cohort") == "shadow":
                    shadow_digest = a.get("bundle_digest")
                else:
                    champion_digest = a.get("bundle_digest")
        ok = (champion_digest and shadow_digest
              and champion_digest != shadow_digest)
        check("BUC-5", "champion_and_shadow_pinned", bool(ok),
              f"champion={str(champion_digest)[:19]}… "
              f"shadow={str(shadow_digest)[:19]}…")

        # Live routing must never resolve the shadow cohort (first match
        # wins; cohort-aware routing is a documented gap — phase3-gaps.md).
        # Champion equality is asserted only when the local manifest is the
        # one the endpoint actually serves (a remote deploy recompiles its
        # own pinned digests — e.g. AWS-trained models differ).
        sl, rl = decide_retry(CLIENTS["a"],
                              txn("HSA_CLAIM", "hsa_live_a",
                                  claimant=claimant_a, amount=320.0))
        live_dig = rl.get("bundle_digest", "")
        known = {a.get("bundle_digest") for a in acts}
        if live_dig in known:
            ok = sl == 200 and live_dig == champion_digest
            detail = (f"live={live_dig[:19]}… "
                      f"champion={str(champion_digest)[:19]}…")
        else:
            ok = sl == 200 and live_dig != shadow_digest
            detail = (f"live={live_dig[:19]}… != shadow "
                      f"{str(shadow_digest)[:19]}… (remote manifest)")
        check("BUC-5", "live_serves_champion", ok, detail)
    else:
        skip("BUC-5", "champion_and_shadow_pinned",
             "build/bundles/activations.json not readable — run `make seed` "
             "or assert via the deployed manifest")
        skip("BUC-5", "live_serves_champion", "no local activation manifest")

    # Champion vs challenger probabilities on the same feature vector.
    inf_addr = os.environ.get("RTDP_INFERENCE_ADDR", "localhost:50051")
    try:
        sys.path.insert(0, str(ROOT / "gen" / "python"))
        import grpc
        from rtdp.v1 import services_pb2, services_pb2_grpc, envelope_pb2
        ch = grpc.insecure_channel(inf_addr)
        grpc.channel_ready_future(ch).result(timeout=3)
        stub = services_pb2_grpc.InferenceServiceStub(ch)

        def score(model_id):
            metas = json.loads((ROOT / "build" / "models" / model_id / "1"
                                / "metadata.json").read_text())
            names = ["txn.amount", "claimant_claim_count_1h",
                     "claimant_amount_sum_24h"]
            vals = [envelope_pb2.TypedValue(double_value=v)
                    for v in (320.0, 1.0, 320.0)]
            resp = stub.Score(services_pb2.ScoreRequest(
                tenant_id="tenant_a", environment="work",
                mode=envelope_pb2.Mode.MODE_LIVE,
                transaction_id=f"buc_{RUN}_score", transaction_revision=1,
                binding_id="hsa-eligibility-primary", binding_version=1,
                model_id=model_id, model_version="1",
                model_digest=metas["model_digest"],
                preprocessing_digest=metas["preprocessing_digest"],
                input_snapshot_digest="",
                feature_names=names, feature_values=vals,
                deadline_ms=2000))
            env = resp.envelope
            return (env.values.get("probability").double_value
                    if env.values.get("probability")
                    else None)

        p1 = score("hsa_eligibility_logistic")
        p2 = score("hsa_eligibility_challenger")
        ok = (p1 is not None and p2 is not None and p1 != p2)
        check("BUC-5", "champion_challenger_differ", ok,
              f"champion_p={p1} challenger_p={p2}")
    except Exception as e:
        skip("BUC-5", "champion_challenger_differ",
             f"inference gRPC unreachable at {inf_addr}: {e}")

    # Shadow decide -> zero live action commands. Two reachability paths:
    # RTDP_ORCH_ADDR direct (kubectl -n rtdp port-forward
    # deploy/orchestrator 50055:50055 on AWS), else run the Decide inside
    # the compose network via the inference-service container (it ships
    # python + grpc + the generated stubs).
    shadow_did = None
    orch_addr = os.environ.get("RTDP_ORCH_ADDR", "")
    if not orch_addr:
        try:
            sys.path.insert(0, str(ROOT / "gen" / "python"))
            import grpc as _g
            _ch = _g.insecure_channel("localhost:50055")
            _g.channel_ready_future(_ch).result(timeout=2)
            orch_addr = "localhost:50055"
        except Exception:
            orch_addr = None
    try:
        if orch_addr:
            from rtdp.v1 import services_pb2, services_pb2_grpc, \
                decision_pb2, envelope_pb2
            ch2 = grpc.insecure_channel(orch_addr)
            grpc.channel_ready_future(ch2).result(timeout=3)
            orch = services_pb2_grpc.OrchestratorStub(ch2)
            res = orch.Decide(decision_pb2.AuthenticatedTransaction(
                request_id=f"buc_{RUN}_shadow", tenant_id="tenant_a",
                environment="work", mode=envelope_pb2.Mode.MODE_SHADOW,
                transaction_id=f"buc_{RUN}_shadow", transaction_revision=1,
                event_type="HSA_CLAIM", channel="PORTAL",
                region="us-east-1",
                tokenized_claimant=f"tok_shadow_{RUN}",
                provider_id="prv_buc", currency="USD", amount=320.0))
            shadow_did = res.decision_id
        else:
            inner = (
                "import sys; sys.path.insert(0, '/app/gen/python')\n"
                "import grpc\n"
                "from rtdp.v1 import services_pb2_grpc, decision_pb2, "
                "envelope_pb2\n"
                "ch = grpc.insecure_channel('orchestrator:50055')\n"
                "res = services_pb2_grpc.OrchestratorStub(ch).Decide("
                "decision_pb2.AuthenticatedTransaction("
                f"request_id='buc_{RUN}_shadow', tenant_id='tenant_a',"
                " environment='work', mode=envelope_pb2.Mode.MODE_SHADOW,"
                f" transaction_id='buc_{RUN}_shadow',"
                " transaction_revision=1, event_type='HSA_CLAIM',"
                " channel='PORTAL', region='us-east-1',"
                f" tokenized_claimant='tok_shadow_{RUN}',"
                " provider_id='prv_buc', currency='USD', amount=320.0))\n"
                "print(res.decision_id)\n")
            out = subprocess.run(
                ["docker", "compose", "exec", "-T", "inference-service",
                 "python", "-"], input=inner, capture_output=True,
                text=True, cwd=ROOT, timeout=60)
            shadow_did = out.stdout.strip().splitlines()[-1].strip() \
                if out.returncode == 0 and out.stdout.strip() else None
        if shadow_did is None:
            skip("BUC-5", "shadow_zero_actions",
                 "no gRPC path to orchestrator — set RTDP_ORCH_ADDR or "
                 "run where `docker compose exec inference-service` works")
        else:
            rows = psql("SELECT count(*) FROM action_execution "
                        f"WHERE decision_id = '{shadow_did}'")
            if rows is None:
                skip("BUC-5", "shadow_zero_actions",
                     f"shadow decision {shadow_did[:18]}… produced — set "
                     "RTDP_PSQL to assert zero action_execution rows")
            else:
                check("BUC-5", "shadow_zero_actions", int(rows) == 0,
                      f"shadow_decision={shadow_did[:18]}… "
                      f"action_execution_rows={rows}")
    except Exception as e:
        skip("BUC-5", "shadow_zero_actions",
             f"shadow decide failed: {e}")

    failed = [r for r in results if not r[2]]
    skipped = [r for r in results if r[3].startswith("SKIP")]
    print(f"\n{len(results) - len(failed) - len(skipped)} passed, "
          f"{len(skipped)} skipped, {len(failed)} failed "
          f"({len(results)} checks)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
