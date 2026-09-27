"""Train the synthetic fraud_logistic model and export to ONNX.

Deterministic seed; the artifact is a demo fixture, not a fraud-efficacy
claim. Writes to build/models/ and (optionally) uploads to MinIO.

Outputs:
  build/models/fraud_logistic/2/model.onnx
  build/models/fraud_logistic/2/input_schema.json
  build/models/fraud_logistic/2/preprocessing.json
  build/models/fraud_logistic/2/metadata.json   (digests, provenance)
  build/models/fraud_logistic/2/golden_vectors.json
"""

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from skl2onnx import to_onnx

MODEL_ID = "fraud_logistic"
MODEL_VERSION = "2"
FEATURES = [
    "pan_txn_count_1h",
    "pan_amount_sum_24h",
    "merchant_txn_count_1h",
    "merchant_amount_sum_1h",
]
OUT = Path("build/models") / MODEL_ID / MODEL_VERSION


def synth_data(n: int = 20000, seed: int = 42):
    """Synthetic card-authorization data. Label is a synthetic fraud flag."""
    rng = np.random.default_rng(seed)
    pan_count = rng.poisson(3, n).astype(np.float64)
    pan_amt = rng.gamma(2.0, 150.0, n)
    merch_count = rng.poisson(20, n).astype(np.float64)
    merch_amt = rng.gamma(2.0, 3000.0, n)
    X = np.column_stack([pan_count, pan_amt, merch_count, merch_amt])
    # Synthetic rule: fraud risk grows with card velocity and spend,
    # attenuated at busy merchants (high merchant volume lowers marginal risk).
    z = (-2.2 + 0.35 * np.log1p(pan_count)
         + 0.9 * np.log1p(pan_amt / 200.0)
         - 0.4 * np.log1p(merch_count / 50.0)
         + 0.5 * np.log1p(merch_amt / 5000.0)
         + rng.normal(0, 0.6, n))
    y = (1.0 / (1.0 + np.exp(-z)) > rng.uniform(0, 1, n)).astype(np.int64)
    return X, y


def sha256_file(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def main():
    X, y = synth_data()
    clf = LogisticRegression(max_iter=1000, random_state=42)
    clf.fit(X, y)

    OUT.mkdir(parents=True, exist_ok=True)
    onnx_model = to_onnx(clf, X[:1].astype(np.float64),
                         options={"zipmap": False},
                         target_opset=17)
    model_path = OUT / "model.onnx"
    model_path.write_bytes(onnx_model.SerializeToString())
    model_digest = sha256_file(model_path)

    input_schema = {"ordered_features": FEATURES, "types": {f: "float64" for f in FEATURES}}
    (OUT / "input_schema.json").write_text(json.dumps(input_schema, indent=2))
    input_schema_digest = "sha256:" + hashlib.sha256(
        json.dumps(input_schema, sort_keys=True).encode()).hexdigest()

    preprocessing = {"kind": "none", "note": "features arrive already normalized"}
    (OUT / "preprocessing.json").write_text(json.dumps(preprocessing, indent=2))
    preproc_digest = "sha256:" + hashlib.sha256(
        json.dumps(preprocessing, sort_keys=True).encode()).hexdigest()

    # Golden vectors for warm-readiness checks: fixed inputs -> probabilities.
    rng = np.random.default_rng(7)
    golden_X = rng.uniform(0, 10, (5, 4))
    golden_p = clf.predict_proba(golden_X)[:, 1].tolist()
    (OUT / "golden_vectors.json").write_text(json.dumps(
        [{"input": x.tolist(), "probability": p}
         for x, p in zip(golden_X, golden_p)], indent=2))

    meta = {
        "model_id": MODEL_ID,
        "model_version": MODEL_VERSION,
        "model_digest": model_digest,
        "input_schema_digest": input_schema_digest,
        "preprocessing_digest": preproc_digest,
        "output_contract": "fraud.authorization_probability@1.1.0",
        "provenance": {
            "kind": "synthetic",
            "generator": "ml/seed-model/train.py",
            "sklearn_coefficients": clf.coef_.tolist(),
            "sklearn_intercept": clf.intercept_.tolist(),
            "training_seed": 42,
        },
    }
    (OUT / "metadata.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps({"model": model_digest,
                      "input_schema": input_schema_digest,
                      "preprocessing": preproc_digest}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
