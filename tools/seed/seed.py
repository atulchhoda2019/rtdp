#!/usr/bin/env python3
"""make seed — bring the local stack to a runnable state.

Steps: create Kafka topics, upload the ONNX artifact to MinIO, patch the
provider binding with real artifact digests, compile per-tenant bundles,
write the activation manifest, submit the Flink job, warm inference, and
wait for readiness (design.md: `make seed` contract).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gen" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "control-plane"))

import yaml  # noqa: E402
import grpc  # noqa: E402
from rtdp.v1 import services_pb2, services_pb2_grpc  # noqa: E402
from rtdp_contracts.compiler import compile_bundle, write_bundle  # noqa: E402
from rtdp_contracts.registry import ContractRegistry, ContractError  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"
MODEL_DIR = BUILD / "models" / "fraud_logistic" / "2"
BINDING = ROOT / "assets/seed/platform/bindings/fraud-primary/3.yaml"
PRODUCT = ROOT / "assets/seed/platform/products/card_fraud/1.yaml"

S3_ENDPOINT = os.environ.get("RTDP_S3_ENDPOINT", "http://localhost:9099")
S3_KEY = os.environ.get("RTDP_S3_ACCESS_KEY", "minioadmin")
S3_SECRET = os.environ.get("RTDP_S3_SECRET_KEY", "minioadmin")
ARTIFACT_BUCKET = "rtdp-artifacts"
KAFKA_CONTAINER = "kafka"
INFERENCE_ADDR = os.environ.get("RTDP_INFERENCE_ADDR", "localhost:50051")
FLINK_REST = os.environ.get("RTDP_FLINK_REST", "http://localhost:8081")
INGRESS = os.environ.get("RTDP_INGRESS", "http://localhost:8080")
JAR = "streaming/flink-features/target/flink-features-1.0.0.jar"

TOPICS = [
    "rtdp.ingress.v1", "rtdp.egress.v1", "rtdp.feature.contrib.v1",
    "rtdp.feature.updates.v1", "rtdp.feature.late.v1", "rtdp.signals.v1",
    "rtdp.decision.facts.v1", "rtdp.action.commands.v1",
    "rtdp.action.status.v1", "rtdp.control.activation.v1",
    "rtdp.telemetry.v1", "rtdp.dlq.v1",
]


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def ensure_topics():
    for t in TOPICS:
        sh(["docker", "compose", "exec", "-T", KAFKA_CONTAINER,
            "/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server",
            "kafka:9092", "--create", "--if-not-exists",
            "--topic", t, "--partitions", "6", "--replication-factor", "1"])
    print(f"topics: {len(TOPICS)} ensured")


def upload_model() -> dict:
    import boto3
    meta = json.loads((MODEL_DIR / "metadata.json").read_text())
    s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT,
                      aws_access_key_id=S3_KEY,
                      aws_secret_access_key=S3_SECRET)
    for name in ("model.onnx", "input_schema.json", "preprocessing.json",
                 "metadata.json", "golden_vectors.json"):
        s3.upload_file(str(MODEL_DIR / name), ARTIFACT_BUCKET,
                       f"fraud_logistic/2/{name}")
    meta["artifact_uri"] = f"s3://{ARTIFACT_BUCKET}/fraud_logistic/2/model.onnx"
    print(f"model uploaded: {meta['model_digest']}")
    return meta


def patch_binding(meta: dict):
    doc = yaml.safe_load(BINDING.read_text())
    doc["model"] = f"fraud_logistic@2"
    doc["input_schema_digest"] = meta["input_schema_digest"]
    doc["preprocessing_digest"] = meta["preprocessing_digest"]
    doc["model_digest"] = meta["model_digest"]
    BINDING.write_text(yaml.safe_dump(doc, sort_keys=False))


def compile_tenants():
    registry = ContractRegistry(ROOT / "contracts", ROOT / "assets")
    product = yaml.safe_load(PRODUCT.read_text())
    activations = []
    epoch = int(time.time())
    for tenant, sub_rev in (("tenant_a", 4), ("tenant_b", 2)):
        overlay = None
        sub_path = (ROOT / "assets/seed/tenants" / tenant / "subscription.yaml")
        sub = yaml.safe_load(sub_path.read_text())
        for ov in sub.get("overlays") or []:
            overlay = ov
        bundle = compile_bundle(registry, product, overlay, tenant)
        bundle["subscription_revision"] = sub_rev
        path = write_bundle(bundle, BUILD / "bundles")
        activations.append({
            "tenant_id": tenant, "environment": "work", "cohort": "champion",
            "epoch": epoch, "bundle_digest": bundle["digest"],
            "bundle": bundle,
        })
        print(f"compiled {tenant}: {bundle['digest'][:23]} -> {path.name}")
    (BUILD / "bundles" / "activations.json").write_text(
        json.dumps(activations, indent=2))
    return activations


def submit_flink():
    if not (ROOT / JAR).exists():
        print(f"flink jar missing ({JAR}) — run `make flink-jar` first; skipping submit")
        return
    import http.client
    host, port = FLINK_REST.replace("http://", "").split(":")
    # Idempotent: a running FeaturesJob on these topics must not be
    # duplicated — duplicate submissions multiply transactional-sink churn.
    try:
        conn0 = http.client.HTTPConnection(host, int(port), timeout=5)
        conn0.request("GET", "/jobs")
        if any(j["status"] == "RUNNING" for j in
               json.loads(conn0.getresponse().read()).get("jobs", [])):
            print("flink job already running; skipping submit")
            return
    except Exception:
        pass  # REST unreachable — attempt submission anyway
    # Upload + run via the JobManager REST API.
    boundary = "----rtdp"
    jar_bytes = (ROOT / JAR).read_bytes()
    body = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="jarfile"; filename="job.jar"\r\n'
            f"Content-Type: application/x-java-archive\r\n\r\n").encode() \
        + jar_bytes + f"\r\n--{boundary}--\r\n".encode()
    conn = http.client.HTTPConnection(host, int(port))
    conn.request("POST", "/jars/upload", body,
                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    resp = json.loads(conn.getresponse().read())
    jar_id = resp["filename"].rsplit("/", 1)[-1]
    conn.request("POST", f"/jars/{jar_id}/run",
                 json.dumps({"entryClass": "com.rtdp.flink.FeaturesJob"}),
                 {"Content-Type": "application/json"})
    run_resp = json.loads(conn.getresponse().read())
    print(f"flink job submitted: {run_resp.get('jobid', run_resp)}")


def warm_inference(meta: dict, activations):
    ch = grpc.insecure_channel(INFERENCE_ADDR)
    stub = services_pb2_grpc.InferenceServiceStub(ch)
    resp = stub.Warm(services_pb2.WarmRequest(
        model_id="fraud_logistic", model_version="2",
        model_digest=meta["model_digest"],
        artifact_uri=meta["artifact_uri"],
        input_schema_digest=meta["input_schema_digest"],
        preprocessing_digest=meta["preprocessing_digest"],
        output_contract="fraud.authorization_probability",
        contract_version="1.1.0"))
    if not resp.ready:
        raise RuntimeError(f"inference warm failed: {resp.error}")
    print(f"inference warm: {resp.model_digest}")


def warm_pipeline():
    """Warm the decision path per tenant: gRPC pools, Redis connections, and
    the CEL engine cache are all cold on a fresh stack and would otherwise
    blow the configured total_deadline_ms on first request (design.md
    warm-readiness contract)."""
    for client_id in ("demo-client-a", "demo-client-b"):
        for attempt in range(40):
            req = urllib.request.Request(
                f"{INGRESS}/v1/decide",
                data=json.dumps({
                    "transaction_id": f"warmup_{client_id}_{attempt}",
                    "transaction_revision": 1,
                    "event_type": "AUTH_REQUEST",
                    "channel": "ECOMMERCE",
                    "region": "us-east-1",
                    "tokenized_pan": f"tok_warm_{client_id}",
                    "merchant_id": "mch_warm",
                    "currency": "USD",
                    "amount": 10.0,
                    "event_time": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }).encode(),
                headers={"Content-Type": "application/json",
                         "X-RTDP-Client-Id": client_id},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    json.loads(r.read())
                    print(f"pipeline warm: {client_id} ok "
                          f"(attempt {attempt + 1})")
                    break
            except Exception:
                if attempt == 39:
                    raise
                time.sleep(0.5)


def main():
    os.chdir(ROOT)
    ensure_topics()
    meta = upload_model()
    patch_binding(meta)
    activations = compile_tenants()
    submit_flink()
    warm_inference(meta, activations)
    warm_pipeline()
    print("seed complete")


if __name__ == "__main__":
    try:
        main()
    except ContractError as e:
        print(f"contract error: {e}", file=sys.stderr)
        sys.exit(1)
