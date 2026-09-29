#!/usr/bin/env python3
"""Config-change demo — run on the seeded local stack.

Proves the platform's core claim end to end: a tenant rule change and a
platform model change each compile to a new pinned bundle, activate under a
new epoch, and change decisions — with no service rebuild or restart, while
Flink tier-2 tiles keep materializing underneath.

Phases:
  0  baseline CLAIM_SUBMISSION for tenant_a + Flink burst started
  1  tenant overlay: thresholds.decline_probability -> same input declines
  2  model change: train claim_fraud_logistic@3, binding@4, product v2,
     tenant-scoped rollout — tenant_b keeps running model@2
  3  Flink evidence: tiles + checkpoints accumulated during the run
  4  replay: resend phase-0 transaction -> identical pinned decision
  5  restore: tenant_a re-activates the original bundle (rollback demo)

Evidence: docs/validation/config-change-<epoch>.json
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gen" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                    / "services" / "control-plane"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ml" / "seed-model"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools" / "seed"))

import yaml  # noqa: E402
import grpc  # noqa: E402
from rtdp.v1 import services_pb2, services_pb2_grpc  # noqa: E402
from rtdp_contracts.compiler import compile_bundle, write_bundle  # noqa: E402
from rtdp_contracts.registry import ContractRegistry  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
INGRESS = os.environ.get("RTDP_INGRESS", "http://localhost:8080")
INFERENCE_ADDR = os.environ.get("RTDP_INFERENCE_ADDR", "localhost:50051")

PRODUCT_YAML = (ROOT / "assets/seed/platform/products"
                / "claim_decisioning/1.yaml")
BINDING_DIR = (ROOT / "assets/seed/platform/bindings"
               / "claim-fraud-primary")

ev = {"run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True,
                          cwd=ROOT, **kw)


def decide(client, txn_id, event_type="CLAIM_SUBMISSION",
           claimant="tok_demo", provider="prv_demo", amount=2500.0):
    req = urllib.request.Request(
        f"{INGRESS}/v1/decide",
        data=json.dumps({
            "transaction_id": txn_id, "transaction_revision": 1,
            "event_type": event_type, "channel": "PORTAL",
            "region": "us-east-1", "tokenized_claimant": claimant,
            "provider_id": provider, "currency": "USD", "amount": amount,
            "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def signal_snapshot(decision_id):
    out = sh(["docker", "compose", "exec", "-T", "postgres", "psql",
              "-U", "rtdp", "-d", "rtdp", "-tA", "-c",
              "SELECT signal_snapshot->'signals'->0 FROM decision_fact"
              f" WHERE decision_id = '{decision_id}'"]).stdout.strip()
    return json.loads(out) if out else {}


def load_acts():
    return json.loads((BUILD / "bundles" / "activations.json").read_text())


def activate(bundle):
    """Pin a freshly compiled bundle for its tenant+product under a new
    epoch — the only write the decision path sees."""
    acts_path = BUILD / "bundles" / "activations.json"
    acts = json.loads(acts_path.read_text())
    for a in acts:
        if (a["tenant_id"] == bundle["tenant_id"]
                and a["bundle"]["product_id"] == bundle["product_id"]):
            a["bundle_digest"] = bundle["digest"]
            a["bundle"] = bundle
            a["epoch"] = int(time.time())
    acts_path.write_text(json.dumps(acts, indent=2))


def compile_claim_bundle(product, overlay=None):
    registry = ContractRegistry(ROOT / "contracts", ROOT / "assets")
    bundle = compile_bundle(registry, product, overlay, "tenant_a")
    bundle["subscription_revision"] = 4
    write_bundle(bundle, BUILD / "bundles")
    return bundle


def image_digests():
    out = sh(["docker", "compose", "images", "--format", "json"]).stdout
    return {img["Repository"]: img["ID"] for img in json.loads(out)
            if img["Repository"].startswith("rtdp-")}


def redis_scan(pattern):
    out = sh(["docker", "compose", "exec", "-T", "redis", "redis-cli",
              "--scan", "--pattern", pattern]).stdout
    return sorted(l for l in out.splitlines() if l)


def flink_checkpoint_count():
    with urllib.request.urlopen(
            "http://localhost:8081/jobs/overview") as r:
        job = json.loads(r.read())["jobs"][0]
    with urllib.request.urlopen(
            f"http://localhost:8081/jobs/{job['jid']}/checkpoints") as r:
        return job["jid"], json.loads(r.read())["counts"]["completed"]


def send_burst(n=8):
    """Drive contrib traffic so Tier-2 tiles materialize during the demo."""
    for i in range(n):
        decide("demo-client-a", f"burst_{int(time.time())}_{i}",
               claimant="tok_burst", provider="prv_demo_burst")


def main():
    product_v1 = yaml.safe_load(PRODUCT_YAML.read_text())
    tag = int(time.time())

    print("== phase 0: baseline ==")
    jid, ckpt0 = flink_checkpoint_count()
    tiles0 = set(redis_scan("rtdp:t2:*prv_demo_burst*"))
    send_burst()
    base = decide("demo-client-a", f"demo_{tag}_base")
    sig = signal_snapshot(base["decision_id"])
    prob = sig.get("values", {}).get("probability", {}).get("doubleValue")
    model0 = sig.get("modelDigest")
    print(f"  baseline: {base['outcome']} prob={prob:.4f} "
          f"epoch={base['manifest_epoch']} bundle={base['bundle_digest'][:23]}"
          f" model={model0[:23]}")
    ev["baseline"] = {"outcome": base["outcome"], "probability": prob,
                      "epoch": base["manifest_epoch"],
                      "bundle": base["bundle_digest"], "model": model0,
                      "txn": f"demo_{tag}_base"}

    print("== phase 1: tenant overlay — rule change ==")
    images_before = image_digests()
    new_threshold = max(0.01, prob - 0.10)
    overlay = {"overlay_id": "ovl_demo", "version": 1,
               "product": "claim_decisioning@1",
               "overrides": {"thresholds.decline_probability": new_threshold}}
    bundle1 = compile_claim_bundle(product_v1, overlay)
    activate(bundle1)
    r1 = decide("demo-client-a", f"demo_{tag}_overlay")
    print(f"  decline_probability 0.90 -> {new_threshold:.2f}: "
          f"{r1['outcome']} reasons={r1.get('reason_codes')} "
          f"epoch={r1['manifest_epoch']} bundle={r1['bundle_digest'][:23]}")
    assert r1["bundle_digest"] == bundle1["digest"]
    assert r1["manifest_epoch"] != base["manifest_epoch"]
    assert image_digests() == images_before, "no image may change"
    ev["rule_change"] = {"threshold": new_threshold,
                         "outcome": r1["outcome"],
                         "reason_codes": r1.get("reason_codes"),
                         "epoch": r1["manifest_epoch"],
                         "bundle": r1["bundle_digest"],
                         "images_unchanged": True}

    print("== phase 2: model change — claim_fraud_logistic@3 ==")
    import train as trainmod
    trainmod.MODELS["claim_fraud_logistic"]["version"] = "3"
    os.environ["RTDP_TRAIN_SEED"] = "99"
    trainmod.train("claim_fraud_logistic")
    meta = json.loads((BUILD / "models/claim_fraud_logistic/3"
                       / "metadata.json").read_text())

    import boto3
    s3 = boto3.client("s3", endpoint_url=os.environ.get(
        "RTDP_S3_ENDPOINT", "http://localhost:9099"),
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin")
    for name in ("model.onnx", "input_schema.json", "preprocessing.json",
                 "metadata.json", "golden_vectors.json"):
        s3.upload_file(str(BUILD / "models/claim_fraud_logistic/3" / name),
                       "rtdp-artifacts", f"claim_fraud_logistic/3/{name}")

    bind3 = yaml.safe_load((BINDING_DIR / "3.yaml").read_text())
    bind3["version"] = 4
    bind3["model"] = "claim_fraud_logistic@3"
    bind3["model_digest"] = meta["model_digest"]
    bind3["input_schema_digest"] = meta["input_schema_digest"]
    bind3["preprocessing_digest"] = meta["preprocessing_digest"]
    (BINDING_DIR / "4.yaml").write_text(yaml.safe_dump(bind3,
                                                     sort_keys=False))

    product_v2 = dict(product_v1)
    product_v2["version"] = 2
    import copy
    product_v2["execution"] = copy.deepcopy(product_v1["execution"])
    product_v2["execution"]["signals"]["fraud"]["binding"] = (
        "claim-fraud-primary@4")

    ch = grpc.insecure_channel(INFERENCE_ADDR)
    stub = services_pb2_grpc.InferenceServiceStub(ch)
    resp = stub.Warm(services_pb2.WarmRequest(
        model_id="claim_fraud_logistic", model_version="3",
        model_digest=meta["model_digest"],
        artifact_uri="s3://rtdp-artifacts/claim_fraud_logistic/3/model.onnx",
        input_schema_digest=meta["input_schema_digest"],
        preprocessing_digest=meta["preprocessing_digest"],
        output_contract="claim.fraud_probability",
        contract_version="1.1.0"))
    if not resp.ready:
        raise RuntimeError(f"model@3 warm failed: {resp.error}")
    print(f"  warmed claim_fraud_logistic@3 {meta['model_digest'][:23]}")

    # Tenant-scoped rollout: recompile tenant_a on binding@4 with the
    # phase-1 overlay still active; tenant_b keeps claim_fraud_logistic@2.
    bundle2 = compile_claim_bundle(product_v2, overlay)
    activate(bundle2)
    r2 = decide("demo-client-a", f"demo_{tag}_model3")
    sig2 = signal_snapshot(r2["decision_id"])
    prob2 = sig2.get("values", {}).get("probability", {}).get("doubleValue")
    print(f"  tenant_a -> {r2['outcome']} prob={prob2:.4f} "
          f"model={sig2.get('modelDigest', '')[:23]}")
    rb = decide("demo-client-b", f"demo_{tag}_tb")
    sigb = signal_snapshot(rb["decision_id"])
    prob_b = sigb.get("values", {}).get("probability", {}).get("doubleValue")
    print(f"  tenant_b -> {rb['outcome']} prob={prob_b:.4f} "
          f"model={sigb.get('modelDigest', '')[:23]} (unchanged)")
    assert sig2.get("modelDigest") == meta["model_digest"]
    assert sigb.get("modelDigest") == model0
    ev["model_change"] = {
        "tenant_a": {"model_digest": sig2.get("modelDigest"),
                     "probability": prob2, "outcome": r2["outcome"],
                     "epoch": r2["manifest_epoch"]},
        "tenant_b": {"model_digest": sigb.get("modelDigest"),
                     "probability": prob_b, "outcome": rb["outcome"]}}

    print("== phase 3: Flink tier-2 evidence ==")
    deadline = time.time() + 240
    tiles_new = set()
    while time.time() < deadline:
        tiles_new = set(redis_scan("rtdp:t2:*prv_demo_burst*")) - tiles0
        if tiles_new:
            break
        time.sleep(10)
    jid2, ckpt1 = flink_checkpoint_count()
    print(f"  job {jid2[:12]}: checkpoints {ckpt0} -> {ckpt1}; "
          f"new burst tiles: {len(tiles_new)}")
    for t in sorted(tiles_new)[:4]:
        val = sh(["docker", "compose", "exec", "-T", "redis", "redis-cli",
                  "get", t]).stdout.strip()
        print(f"    {t.split('}')[-1]} = {val}")
    ev["flink"] = {"job": jid2, "checkpoints": [ckpt0, ckpt1],
                   "new_tiles": sorted(tiles_new)}

    print("== phase 4: replay — pinned decision immutability ==")
    rep = decide("demo-client-a", f"demo_{tag}_base")
    assert rep["decision_id"] == base["decision_id"]
    assert rep["bundle_digest"] == base["bundle_digest"]
    print(f"  resend demo_{tag}_base -> same decision_id "
          f"{rep['decision_id'][:18]}…, same bundle digest "
          f"(activation moved on, decision did not)")
    ev["replay"] = {"decision_id": rep["decision_id"],
                    "bundle_digest": rep["bundle_digest"],
                    "identical": True}

    print("== phase 5: restore — tenant_a back to baseline bundle ==")
    bundle0 = compile_claim_bundle(product_v1)
    activate(bundle0)
    r5 = decide("demo-client-a", f"demo_{tag}_restore")
    print(f"  rollback: {r5['outcome']} bundle={r5['bundle_digest'][:23]} "
          f"(baseline was {base['bundle_digest'][:23]})")
    ev["restore"] = {"outcome": r5["outcome"],
                     "bundle": r5["bundle_digest"]}

    out = ROOT / "docs" / "validation" / f"config-change-{tag}.json"
    out.write_text(json.dumps(ev, indent=2))
    print(f"\nevidence -> {out}")


if __name__ == "__main__":
    main()
