#!/usr/bin/env python3
"""Phase 1 acceptance gates — each gate emits raw evidence to
docs/validation/phase1-gates.json plus a human-readable log.

Gates:
  G1 e2e path            (delegates to tests/e2e assertions inline)
  G2 tenant isolation    — client-bound tenant, no payload spoofing,
                           tenant-scoped durable facts
  G3 config-only change  — threshold flip with identical image digests
                           and schema hash, no restart
  G4 semantic incompat   — binding outside accepted_contracts rejected
  G5 stream recovery     — Flink TM restart, window output without
                           feature inflation
  G6 model parity        — ONNX runtime vs sklearn reference, |dp| bound
  G7 action ambiguity    — provider timeout -> UNKNOWN, never ACK
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import psycopg
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/control-plane"))
sys.path.insert(0, str(ROOT / "gen/python"))

from rtdp_contracts.compiler import compile_bundle, write_bundle  # noqa: E402
from rtdp_contracts.registry import ContractError, ContractRegistry  # noqa: E402

INGRESS = "http://localhost:8080"
PG = "postgresql://rtdp:rtdp@localhost:5432/rtdp"
BUILD = ROOT / "build"
EVIDENCE = ROOT / "docs/validation"

log_lines = []


def log(msg):
    print(msg, flush=True)
    log_lines.append(msg)


def sh(cmd, check=True, capture=True, timeout=120):
    r = subprocess.run(cmd, shell=True, capture_output=capture,
                       text=True, timeout=timeout, cwd=ROOT)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}\n{r.stderr}")
    return r.stdout.strip()


def decide(client_id, txn):
    req = urllib.request.Request(
        f"{INGRESS}/v1/decide", data=json.dumps(txn).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client_id}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # SLM intake path
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def decide_ok(client_id, txn, attempts=10):
    """Deadline rejection is designed behavior under the 100ms budget;
    retry it like a real caller. Structural failures return at once."""
    status, body = 0, {}
    for _ in range(attempts):
        status, body = decide(client_id, txn)
        if "DeadlineExceeded" in str(body.get("error", "")):
            time.sleep(0.25)
            continue
        break
    assert status == 200, f"decision failed after retries: {body}"
    return body


def make_txn(tag, claimant=None, provider="prv_gate", amount=129.99,
             event_offset_s=0):
    return {
        "transaction_id": f"txn_{tag}_{int(time.time()*1000)}",
        "transaction_revision": 1,
        "event_type": "CLAIM_SUBMISSION",
        "channel": "PORTAL", "region": "us-east-1",
        "tokenized_claimant": claimant or f"tok_{tag}_{int(time.time())}",
        "provider_id": provider, "currency": "USD",
        "amount": amount,
        "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(time.time() + event_offset_s)),
    }


PRODUCT = yaml.safe_load(
    (ROOT / "assets/seed/platform/products/claim_decisioning/1.yaml").read_text())


def registry(assets=None):
    return ContractRegistry(ROOT / "contracts", assets or ROOT / "assets")


def tenant_overlay(tenant):
    sub = yaml.safe_load(
        (ROOT / f"assets/seed/tenants/{tenant}/subscription.yaml")
        .read_text())
    ovs = sub.get("overlays") or []
    return ovs[0] if ovs else None


def image_digests():
    """Sorted repo-digest/id list for every running rtdp container image."""
    ids = sh("docker ps --filter name=rtdp- --format '{{.ID}}'")
    dig = {}
    for cid in ids.splitlines():
        if not cid:
            continue
        name = sh(f"docker inspect -f '{{{{.Name}}}}' {cid}").lstrip("/")
        img = sh(f"docker inspect -f '{{{{.Image}}}}' {cid}")
        dig[name] = img
    return dig


def schema_hash():
    dump = sh("docker exec rtdp-postgres-1 pg_dump -s -U rtdp rtdp")
    return "sha256:" + hashlib.sha256(dump.encode()).hexdigest()


def pg(sql):
    with psycopg.connect(PG) as c:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() if cur.description else []


def redis(pattern):
    out = sh("docker exec rtdp-redis-1 redis-cli "
             f"--scan --pattern '{pattern}'")
    return [k for k in out.splitlines() if k]


def redis_get(key):
    return sh(f"docker exec rtdp-redis-1 redis-cli GET '{key}'")


def flink_jobs(state="RUNNING"):
    with urllib.request.urlopen("http://localhost:8081/jobs") as r:
        jobs = json.loads(r.read())["jobs"]
    return [j for j in jobs if j["status"] == state]


def write_activation(tenant, bundle, epoch):
    acts_path = BUILD / "bundles" / "activations.json"
    acts = json.loads(acts_path.read_text())
    for a in acts:
        if (a["tenant_id"] == tenant
                and a["bundle"]["product_id"] == bundle["product_id"]):
            a["bundle_digest"] = bundle["digest"]
            a["epoch"] = epoch
            a["bundle"] = bundle
    acts_path.write_text(json.dumps(acts, indent=2))


def g1_e2e_path(ev):
    log("== G1 e2e decision path ==")
    txn = make_txn("g1")
    status, _ = decide("rogue-client", txn)
    assert status == 401, f"unknown client not rejected: {status}"
    a = decide_ok("demo-client-a", {**txn, "transaction_id":
                                    txn["transaction_id"] + "_a"})
    b = decide_ok("demo-client-b", {**txn, "transaction_id":
                                    txn["transaction_id"] + "_b"})
    assert a["bundle_digest"] != b["bundle_digest"], \
        "tenants must pin different bundles"
    a2 = decide_ok("demo-client-a", {**txn, "transaction_id":
                                     txn["transaction_id"] + "_a"})
    assert a2["decision_id"] == a["decision_id"], "retry not idempotent"
    status, _ = decide("demo-client-a", {**txn, "amount": 99999.0,
                                         "transaction_id":
                                         txn["transaction_id"] + "_a"})
    assert status in (400, 502), "payload conflict not rejected"

    # Multi-product routing: event_type selects the pinned product bundle.
    digest_to_product = {
        x["bundle_digest"]: x["bundle"]["product_id"]
        for x in json.loads(
            (BUILD / "bundles/activations.json").read_text())
        if x["tenant_id"] == "tenant_a"}
    routing = {}
    for ev_type, expected in (("CLAIM_SUBMISSION", "claim_decisioning"),
                              ("POLICY_APPLICATION", "underwriting_decisioning"),
                              ("QUOTE_REQUEST", "risk_pricing"),
                              ("CLAIM_DOCUMENT_INTAKE", "document_intake")):
        r = decide_ok("demo-client-a", {
            **txn, "transaction_id":
            f"{txn['transaction_id']}_{ev_type}", "event_type": ev_type})
        actual = digest_to_product.get(r["bundle_digest"])
        assert actual == expected, \
            f"{ev_type} routed to {actual}, want {expected}"
        routing[ev_type] = {"product": actual, "outcome": r["outcome"]}
    # An event type no subscribed product routes must fail closed.
    status, _ = decide("demo-client-a", {**txn,
                                         "transaction_id":
                                         txn["transaction_id"] + "_no",
                                         "event_type": "NO_SUCH_EVENT"})
    assert status == 502, f"unrouted event not rejected: {status}"

    ev.update({"unauthorized_status": 401,
               "tenant_a": a, "tenant_b": b,
               "idempotent_retry": True, "conflict_rejected": status,
               "product_routing": routing})
    log(f"  tenant_a -> {a['outcome']} | tenant_b -> {b['outcome']} "
        f"| digests differ | idempotent | conflict rejected "
        f"| routed {list(routing)}")


def g2_tenant_isolation(ev):
    log("== G2 tenant isolation ==")
    # Payload has no tenant field — tenant comes from the client binding.
    spoof = make_txn("g2spoof")
    spoof["tenant_id"] = "tenant_b"
    res = decide_ok("demo-client-a", spoof)
    a_act = [a for a in json.loads(
        (BUILD / "bundles/activations.json").read_text())
        if a["tenant_id"] == "tenant_a"
        and a["bundle"]["product_id"] == "claim_decisioning"][0]
    assert res["bundle_digest"] == a_act["bundle_digest"], \
        "payload tenant spoof changed the pinned bundle"
    # Durable facts carry the authenticated tenant.
    row = pg("SELECT tenant_id FROM decision_fact "
             f"WHERE decision_id = '{res['decision_id']}'")
    assert row and row[0][0] == "tenant_a", f"fact tenant: {row}"
    keys = redis(f"rtdp:dedup:{{tenant_a:LIVE}}:{spoof['transaction_id']}:*")
    assert keys, "no tenant-scoped dedup key"
    ev.update({"spoofed_payload_tenant": "tenant_b",
               "effective_bundle_digest": res["bundle_digest"],
               "decision_fact_tenant": row[0][0],
               "dedup_key": keys[0]})
    log("  payload spoof ignored; decision_fact tenant_a; "
        "dedup key tenant-scoped")


def g3_config_only(ev):
    log("== G3 config-only change ==")
    images_before = image_digests()
    schema_before = schema_hash()
    txn = make_txn("g3")
    before = decide_ok("demo-client-b", {**txn, "transaction_id":
                                         txn["transaction_id"] + "_pre"})

    # Flip tenant_b's decline threshold: any positive score now declines.
    baseline_overlay = tenant_overlay("tenant_b")
    flipped = {**baseline_overlay,
               "overrides": {**baseline_overlay["overrides"],
                             "thresholds.decline_probability": 0.0}}
    bundle = compile_bundle(registry(), PRODUCT, flipped, "tenant_b")
    bundle["subscription_revision"] = 2
    write_bundle(bundle, BUILD / "bundles")
    write_activation("tenant_b", bundle, epoch=int(time.time()))

    time.sleep(0.3)  # activations.json is read per request; let FS settle
    after = decide_ok("demo-client-b", {**txn, "transaction_id":
                                        txn["transaction_id"] + "_post"})
    assert after["bundle_digest"] == bundle["digest"], \
        "decision not pinned to the new bundle"
    assert after["outcome"] == "DECISION_DECLINE", \
        f"threshold flip had no effect: {after['outcome']}"
    images_after = image_digests()
    schema_after = schema_hash()
    assert images_after == images_before, "image digests changed!"
    assert schema_after == schema_before, "schema changed!"

    # Restore the seeded overlay so later gates see baseline config.
    bundle2 = compile_bundle(registry(), PRODUCT, baseline_overlay,
                             "tenant_b")
    bundle2["subscription_revision"] = 2
    write_bundle(bundle2, BUILD / "bundles")
    write_activation("tenant_b", bundle2, epoch=int(time.time()))

    ev.update({"before": before, "after": after,
               "new_bundle_digest": bundle["digest"],
               "images_unchanged": True, "schema_hash": schema_after})
    log(f"  {before['outcome']} -> {after['outcome']} on threshold flip; "
        f"image digests + schema hash identical")


def g4_semantic_incompat(ev):
    log("== G4 semantic incompatibility rejection ==")
    # A binding whose output_contract is outside the product's
    # accepted_contracts must fail at compile time — never runtime.
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(ROOT / "assets/seed", tmp / "seed")
        binding = (tmp / "seed/platform/bindings/claim-fraud-primary/3.yaml")
        doc = yaml.safe_load(binding.read_text())
        doc["output_contract"] = "claim.fraud_probability@9.9.9"
        binding.write_text(yaml.safe_dump(doc))
        try:
            compile_bundle(registry(tmp), PRODUCT, None, "tenant_a")
            raise AssertionError("incompatible binding compiled!")
        except ContractError as e:
            ev["compiler_error"] = str(e)
            log(f"  rejected: {e}")
        # Contract not in registry at all -> same rejection.
        doc["output_contract"] = "no.such.contract@1.0.0"
        binding.write_text(yaml.safe_dump(doc))
        try:
            compile_bundle(registry(tmp), PRODUCT, None, "tenant_a")
            raise AssertionError("missing contract compiled!")
        except ContractError as e:
            ev["missing_contract_error"] = str(e)
            log(f"  rejected: {e}")
    finally:
        shutil.rmtree(tmp)


def g5_stream_recovery(ev):
    log("== G5 stream recovery ==")
    # Drive the streaming path directly: Tier 1 correctly rejects
    # future-dated events, so contributions are produced to Kafka with
    # controlled event times (the gate under test is Flink, not Tier 1).
    from kafka import KafkaProducer
    from google.protobuf.timestamp_pb2 import Timestamp
    from rtdp.v1 import decision_pb2, envelope_pb2

    provider = f"prv_rec_{int(time.time())}"
    base = int(time.time()) - 5  # inside the current minute window
    p = KafkaProducer(bootstrap_servers="localhost:29092")

    def contrib(tag, event_epoch):
        c = decision_pb2.FeatureContribution(
            contribution_id=f"g5-{tag}-{event_epoch}",
            tenant_id="tenant_a", environment="work",
            mode=envelope_pb2.MODE_LIVE,
            transaction_id=f"txn_g5_{tag}",
            transaction_revision=1, tokenized_claimant="tok_g5",
            provider_id=provider, currency="USD", amount=10.0)
        c.event_time.CopyFrom(Timestamp(seconds=event_epoch))
        p.send("rtdp.feature.contrib.v1", key=provider.encode(),
               value=c.SerializeToString())

    for i in range(3):
        contrib(i, base)
    p.flush()
    jobs_before = flink_jobs()
    assert len(jobs_before) == 1, f"expected 1 running job: {jobs_before}"
    job_id = jobs_before[0]["id"]

    restart_at = time.time()
    sh("docker restart rtdp-flink-taskmanager-1", timeout=180)

    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            jobs = flink_jobs()
            if jobs and jobs[0]["id"] == job_id:
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        raise AssertionError("Flink job did not recover")

    # RUNNING alone doesn't prove the redeployed pipeline is consuming —
    # wait for a checkpoint completed after the restart, so the flush
    # contribution isn't stranded while the source still re-registers.
    def checkpointed_after(ts):
        with urllib.request.urlopen(
                f"http://localhost:8081/jobs/{job_id}/checkpoints") as r:
            latest = (json.loads(r.read()).get("latest") or {})
            done = latest.get("completed") or {}
            trig = done.get("trigger_timestamp") or 0
            return trig / 1000 > ts

    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            if checkpointed_after(restart_at):
                break
        except Exception:
            pass
        time.sleep(5)
    else:
        raise AssertionError("no post-restart checkpoint completed")

    # Close the window: one contribution past window end + out-of-orderness.
    contrib("flush", base + 90)
    p.flush()
    p.close()

    # Wait for the materialized tile; count must equal exactly 3 events in
    # the base window — replay must not inflate absolute tile values.
    tile = (f"rtdp:t2:{{tenant_a:LIVE}}:provider_claim_count_1h@1:"
            f"{provider}:USD:{base // 60}")
    count = None
    deadline = time.time() + 480  # 60s checkpoint commit + materializer lag
    while time.time() < deadline:
        val = redis_get(tile)
        if val:
            count = int(float(val))
            break
        time.sleep(10)
    assert count is not None, "no tier2 tile materialized after recovery"
    assert count == 3, f"tile count {count} != 3 (inflation/loss)"
    ev.update({"job_id": job_id, "provider": provider, "tile": tile,
               "tile_count": count})
    log(f"  job {job_id[:12]} recovered; tile count == 3 exactly")


def g6_model_parity(ev):
    log("== G6 model parity ==")
    import numpy as np
    import onnxruntime as ort
    sys.path.insert(0, str(ROOT / "ml/seed-model"))
    from train import MODELS, synth_data

    results = {}
    for model_id, spec in MODELS.items():
        X, y = synth_data(model_id)
        est = spec["estimator"]()
        est.fit(X, y)
        sess = ort.InferenceSession(
            str(ROOT / "build/models" / model_id / spec["version"]
                / "model.onnx"))
        probe, _ = synth_data(model_id, n=256, seed=99)
        if spec["kind"] == "binary_probability":
            ref = est.predict_proba(probe)[:, 1]
            out = sess.run(["probabilities"],
                           {sess.get_inputs()[0].name: probe})[0]
            got = out[:, 1] if out.ndim == 2 else out
        else:
            ref = est.predict(probe)
            got = sess.run([sess.get_outputs()[0].name],
                           {sess.get_inputs()[0].name: probe})[0].ravel()
        max_delta = float(np.max(np.abs(got - ref)))
        assert max_delta < 1e-5, f"{model_id} parity delta {max_delta}"
        results[model_id] = max_delta
        log(f"  {model_id}: 256 probes, max |d| = {max_delta:.3e}")
    ev.update({"n_samples": 256, "max_abs_delta": results,
               "features": {m: s["features"] for m, s in MODELS.items()}})


def g7_action_ambiguity(ev):
    log("== G7 action ambiguity ==")
    from kafka import KafkaProducer
    from rtdp.v1 import decision_pb2, envelope_pb2

    idem = f"gate-amb-{int(time.time()*1000)}"
    cmd = decision_pb2.ActionCommand(
        command_id=f"cmd-{idem}", tenant_id="tenant_a",
        environment="work", mode=envelope_pb2.MODE_LIVE,
        decision_id=f"dec-{idem}", decision_generation=1,
        action_type="CLAIM_RESPONSE", idempotency_key=idem,
        adapter_ref="local_timeout_simulator@1",
        intent_ttl_ms=3600_000)
    cmd.created_at.GetCurrentTime()
    p = KafkaProducer(bootstrap_servers="localhost:29092")
    p.send("rtdp.action.commands.v1", key=idem.encode(),
           value=cmd.SerializeToString()).get(30)
    p.close()

    deadline = time.time() + 60
    state = None
    while time.time() < deadline:
        rows = pg("SELECT state, provider_reference FROM action_execution "
                  f"WHERE idempotency_key = '{idem}'")
        if rows and rows[0][0] in ("ACKNOWLEDGED", "FAILED", "UNKNOWN"):
            state, ref = rows[0]
            break
        time.sleep(2)
    assert state == "UNKNOWN", f"timeout must yield UNKNOWN, got {state}"
    assert ref, "provider_reference missing"
    # Reconcile path may follow; the ledger must never claim ACK.
    time.sleep(3)
    rows = pg("SELECT state FROM action_execution "
              f"WHERE idempotency_key = '{idem}'")
    assert rows[0][0] == "UNKNOWN", f"state mutated to {rows[0][0]}"
    ev.update({"idempotency_key": idem, "final_state": state,
               "provider_reference": ref})
    log(f"  {idem} -> UNKNOWN (ref {ref}), never ACKNOWLEDGED")


GATES = [
    ("g1_e2e_path", g1_e2e_path),
    ("g2_tenant_isolation", g2_tenant_isolation),
    ("g3_config_only", g3_config_only),
    ("g4_semantic_incompat", g4_semantic_incompat),
    ("g5_stream_recovery", g5_stream_recovery),
    ("g6_model_parity", g6_model_parity),
    ("g7_action_ambiguity", g7_action_ambiguity),
]


def main():
    only = set(sys.argv[1:])
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    result = {"run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "gates": {}}
    failures = []
    for name, fn in GATES:
        if only and name not in only:
            continue
        ev = {}
        t0 = time.time()
        try:
            fn(ev)
            result["gates"][name] = {"status": "pass",
                                     "seconds": round(time.time() - t0, 1),
                                     "evidence": ev}
        except Exception as e:
            result["gates"][name] = {"status": "FAIL",
                                     "seconds": round(time.time() - t0, 1),
                                     "error": str(e), "evidence": ev}
            failures.append(name)
            log(f"  FAIL: {e}")
    (EVIDENCE / "phase1-gates.json").write_text(
        json.dumps(result, indent=2, default=str))
    (EVIDENCE / "phase1-gates.log").write_text("\n".join(log_lines) + "\n")
    log(f"\n{'ALL GATES PASS' if not failures else 'FAILED: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
