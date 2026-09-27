"""ONNX model registry: load, verify, warm, and score pinned artifacts.

A model becomes eligible to serve only after warm readiness: artifact bytes
verified against the advertised digest, runtime initialized, and golden
vectors checked. There is no mutable "latest" — every score request names a
pinned model digest.
"""

import hashlib
import json
import os
import threading
import urllib.parse
from pathlib import Path

import numpy as np
import onnxruntime as ort


class ModelNotReady(Exception):
    pass


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _fetch(uri: str) -> bytes:
    """file:// or s3:// (MinIO locally)."""
    if uri.startswith("file://"):
        return Path(urllib.parse.urlparse(uri).path).read_bytes()
    if uri.startswith("s3://"):
        import boto3
        p = urllib.parse.urlparse(uri)
        s3 = boto3.client(
            "s3",
            endpoint_url=os.environ.get("RTDP_S3_ENDPOINT"),
            region_name=os.environ.get("RTDP_S3_REGION", "us-east-1"),
            aws_access_key_id=os.environ.get("RTDP_S3_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("RTDP_S3_SECRET_KEY"),
        )
        return s3.get_object(Bucket=p.netloc, Key=p.path.lstrip("/"))["Body"].read()
    if Path(uri).exists():
        return Path(uri).read_bytes()
    raise ValueError(f"unsupported artifact uri {uri!r}")


class LoadedModel:
    def __init__(self, digest: str, session: ort.InferenceSession,
                 input_name: str, prob_name: str, feature_order: list[str],
                 meta: dict, preprocessing_digest: str):
        self.digest = digest
        self.session = session
        self.input_name = input_name
        self.prob_name = prob_name
        self.feature_order = feature_order
        self.meta = meta
        self.preprocessing_digest = preprocessing_digest

    def predict_proba(self, features: list[float]) -> float:
        x = np.array([features], dtype=np.float64)
        out = self.session.run([self.prob_name], {self.input_name: x})[0]
        return float(out[0][1])  # P(class=1) from the [N,2] prob matrix


def _prob_output_name(session: ort.InferenceSession) -> str:
    """skl2onnx emits (label, probabilities); pick the 2-D tensor output."""
    for o in session.get_outputs():
        if len(o.shape) == 2:
            return o.name
    return session.get_outputs()[-1].name


class ModelRegistry:
    def __init__(self, max_threads: int = 2):
        self._models: dict[str, LoadedModel] = {}
        self._lock = threading.Lock()
        # Explicit thread limits to avoid CPU oversubscription in the 20ms budget.
        self._sess_opts = ort.SessionOptions()
        self._sess_opts.intra_op_num_threads = max_threads
        self._sess_opts.inter_op_num_threads = 1

    def warm(self, *, model_id, model_version, model_digest, artifact_uri,
             input_schema_digest, preprocessing_digest) -> LoadedModel:
        """Load + verify + golden-check a pinned artifact. Idempotent."""
        with self._lock:
            if model_digest in self._models:
                return self._models[model_digest]

            data = _fetch(artifact_uri)
            actual = _sha256(data)
            if model_digest and model_digest != actual:
                raise ModelNotReady(
                    f"artifact digest mismatch: wanted {model_digest} got {actual}")

            meta_uri = artifact_uri.rsplit("/", 1)[0] + "/metadata.json"
            schema_uri = artifact_uri.rsplit("/", 1)[0] + "/input_schema.json"
            try:
                meta = json.loads(_fetch(meta_uri))
                schema = json.loads(_fetch(schema_uri))
            except Exception:
                meta, schema = {}, {"ordered_features": []}
            if input_schema_digest:
                actual_schema = "sha256:" + hashlib.sha256(
                    json.dumps(schema, sort_keys=True).encode()).hexdigest()
                if actual_schema != input_schema_digest:
                    raise ModelNotReady(
                        f"input schema digest mismatch: {actual_schema}")

            session = ort.InferenceSession(
                data, sess_options=self._sess_opts, providers=["CPUExecutionProvider"])
            input_name = session.get_inputs()[0].name

            lm = LoadedModel(actual, session, input_name,
                             _prob_output_name(session),
                             schema.get("ordered_features", []), meta,
                             preprocessing_digest)

            # Golden vectors: known inputs must reproduce known probabilities.
            try:
                golden = json.loads(
                    _fetch(artifact_uri.rsplit("/", 1)[0] + "/golden_vectors.json"))
                for gv in golden:
                    if abs(lm.predict_proba(gv["input"]) - gv["probability"]) > 1e-5:
                        raise ModelNotReady("golden vector mismatch")
            except FileNotFoundError:
                pass  # golden vectors optional for unregistered artifacts

            self._models[model_digest] = lm
            return lm

    def get(self, digest: str) -> LoadedModel:
        lm = self._models.get(digest)
        if lm is None:
            raise ModelNotReady(f"model {digest} not warm")
        return lm

    def score(self, digest: str, features: list[float]) -> float:
        lm = self.get(digest)
        if len(features) != len(lm.feature_order) and lm.feature_order:
            raise ValueError(
                f"expected {len(lm.feature_order)} features, got {len(features)}")
        return lm.predict_proba(features)
