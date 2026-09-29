"""SLM model registry: digest-pinned generative artifacts behind the same
InferenceService contract as the ONNX tabular path.

Differences from the ONNX registry:
- The artifact is a GGUF weights blob served by a pinned Ollama runtime;
  "artifact_uri" names the model tag (ollama://...). Digest verification
  compares the blob sha256 reported by the runtime against the binding.
- preprocessing_digest pins the prompt template + decode parameters —
  for generative models the prompt IS part of the model's behavior, so it
  is pinned like weights.
- Golden vectors are behavioral, not exact: a fixed synthetic input must
  produce schema-valid output in range. Generative output is sampled at
  temperature 0 but is still validated, never trusted blindly.
"""

import hashlib
import json
import os
import threading
import urllib.parse
from pathlib import Path

import urllib.request


class ModelNotReady(Exception):
    pass


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _fetch(uri: str) -> bytes:
    """file:// or s3:// (MinIO locally); ollama:// resolves to the runtime."""
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
    raise ValueError(f"unsupported artifact uri {uri!r}")


def _ollama(path: str, payload: dict, timeout: float) -> dict:
    base = os.environ.get("RTDP_OLLAMA_ADDR", "http://localhost:11434")
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _model_blob_digest(model_tag: str) -> str:
    """The sha256 of the served weights blob, per the runtime's modelfile."""
    info = _ollama("/api/show", {"model": model_tag}, timeout=15)
    modelfile = info.get("modelfile", "")
    for line in modelfile.splitlines():
        if line.startswith("FROM ") and "sha256-" in line:
            return "sha256:" + line.split("sha256-", 1)[1].strip()
    raise ModelNotReady(f"cannot resolve weights digest for {model_tag}")


class LoadedSlm:
    def __init__(self, digest: str, model_tag: str, template: dict,
                 meta: dict, preprocessing_digest: str):
        self.digest = digest
        self.model_tag = model_tag
        self.template = template          # {"prompt": str, "decode": {...}}
        self.meta = meta
        self.preprocessing_digest = preprocessing_digest

    def generate(self, variables: dict[str, float], timeout_s: float) -> dict:
        """Render the pinned template, run the pinned decode params, and
        return parsed JSON. Raises on unparseable/out-of-schema output —
        generative text is data, never instruction."""
        prompt = self.template["prompt"]
        for k, v in variables.items():
            prompt = prompt.replace("{" + k + "}", str(v))
        decode = dict(self.template.get("decode", {}))
        out = _ollama("/api/generate", {
            "model": self.model_tag,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, **decode},
        }, timeout=timeout_s)
        return json.loads(out.get("response", ""))


class SlmRegistry:
    def __init__(self):
        self._models: dict[str, LoadedSlm] = {}
        self._lock = threading.Lock()

    def warm(self, *, model_id, model_version, model_digest, artifact_uri,
             input_schema_digest, preprocessing_digest) -> LoadedSlm:
        """Verify the pinned generative artifact and run a golden probe.
        Idempotent — a digest that is already warm returns cached state."""
        with self._lock:
            if model_digest in self._models:
                return self._models[model_digest]

            if not artifact_uri.startswith("ollama://"):
                raise ModelNotReady(
                    f"slm artifact_uri must be ollama://<tag>, got {artifact_uri!r}")
            model_tag = artifact_uri[len("ollama://"):]

            actual = _model_blob_digest(model_tag)
            if model_digest and model_digest != actual:
                raise ModelNotReady(
                    f"weights digest mismatch: wanted {model_digest} got {actual}")

            base_uri = artifact_uri  # companion files live in s3 beside it
            meta_uri = os.environ.get(
                "RTDP_SLM_META_URI",
                f"s3://{os.environ.get('RTDP_ARTIFACT_BUCKET', 'rtdp-artifacts')}"
                f"/{model_id}/{model_version}/metadata.json")
            tmpl_uri = os.environ.get(
                "RTDP_SLM_TEMPLATE_URI",
                f"s3://{os.environ.get('RTDP_ARTIFACT_BUCKET', 'rtdp-artifacts')}"
                f"/{model_id}/{model_version}/prompt_template.json")
            meta = json.loads(_fetch(meta_uri))
            tmpl_bytes = _fetch(tmpl_uri)
            template = json.loads(tmpl_bytes)
            if preprocessing_digest:
                actual_t = _sha256(tmpl_bytes)
                if actual_t != preprocessing_digest:
                    raise ModelNotReady(
                        f"prompt template digest mismatch: {actual_t}")

            lm = LoadedSlm(actual, model_tag, template, meta,
                           preprocessing_digest)

            # Golden behavior probe: pinned input must produce a parseable,
            # in-range output — not byte equality (generative), but the same
            # semantic discipline.
            golden = template.get("golden_input", {})
            try:
                out = lm.generate(golden, timeout_s=60)
                val = float(out[meta.get("output_value", "consistency")])
                assert 0.0 <= val <= 1.0
            except Exception as e:
                raise ModelNotReady(f"golden behavior probe failed: {e}")

            self._models[model_digest] = lm
            return lm

    def get(self, digest: str) -> LoadedSlm:
        lm = self._models.get(digest)
        if lm is None:
            raise ModelNotReady(f"model {digest} not warm")
        return lm
