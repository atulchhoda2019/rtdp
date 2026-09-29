"""Train the synthetic seed models and export each to ONNX.

Deterministic seeds; artifacts are demo fixtures, not efficacy claims.
Writes to build/models/<model_id>/<version>/ per model:

  model.onnx
  input_schema.json      (ordered_features + types)
  preprocessing.json
  metadata.json          (digests, output contract/kind, provenance)
  golden_vectors.json    (fixed inputs -> expected outputs)

Usage: train.py [model_id ...]  (default: all)
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LinearRegression, LogisticRegression
from skl2onnx import to_onnx

BUILD = Path("build/models")

# ---------------------------------------------------------------------------
# Synthetic generators. Each returns (X, y) aligned to the model's FEATURES.
# ---------------------------------------------------------------------------

def _claim_fraud(n, rng):
    """Label: synthetic fraud flag on a claim submission."""
    claimant_count = rng.poisson(3, n).astype(np.float64)
    claimant_amt = rng.gamma(2.0, 1500.0, n)
    provider_count = rng.poisson(20, n).astype(np.float64)
    provider_amt = rng.gamma(2.0, 30000.0, n)
    X = np.column_stack(
        [claimant_count, claimant_amt, provider_count, provider_amt])
    # Fraud risk grows with claimant velocity and amount, attenuated at
    # high-volume providers.
    z = (-2.2 + 0.35 * np.log1p(claimant_count)
         + 0.9 * np.log1p(claimant_amt / 2000.0)
         - 0.4 * np.log1p(provider_count / 50.0)
         + 0.5 * np.log1p(provider_amt / 50000.0)
         + rng.normal(0, 0.6, n))
    y = (1.0 / (1.0 + np.exp(-z)) > rng.uniform(0, 1, n)).astype(np.int64)
    return X, y


def _uw_eligibility(n, rng):
    """Label: synthetic auto-eligibility for an underwriting application.
    Higher output = more eligible for straight-through binding."""
    claimant_count = rng.poisson(2, n).astype(np.float64)
    claimant_amt = rng.gamma(2.0, 2000.0, n)
    provider_count = rng.poisson(10, n).astype(np.float64)
    provider_amt = rng.gamma(2.0, 20000.0, n)
    X = np.column_stack(
        [claimant_count, claimant_amt, provider_count, provider_amt])
    # Eligibility falls with applicant claim velocity and amounts.
    z = (1.8 - 0.5 * np.log1p(claimant_count)
         - 0.8 * np.log1p(claimant_amt / 3000.0)
         - 0.3 * np.log1p(provider_count / 20.0)
         + rng.normal(0, 0.5, n))
    y = (1.0 / (1.0 + np.exp(-z)) > rng.uniform(0, 1, n)).astype(np.int64)
    return X, y


def _premium(n, rng):
    """Output: synthetic premium estimate (currency units) for a quote."""
    amount = rng.gamma(3.0, 3000.0, n)          # txn.amount: coverage requested
    claimant_count = rng.poisson(2, n).astype(np.float64)
    claimant_amt = rng.gamma(2.0, 2000.0, n)
    X = np.column_stack([amount, claimant_count, claimant_amt])
    premium = (250.0 + 0.045 * amount
               + 60.0 * claimant_count
               + 0.008 * claimant_amt
               + rng.normal(0, 80.0, n))
    return X, np.clip(premium, 25.0, None)


MODELS = {
    "claim_fraud_logistic": {
        "version": "2",
        "kind": "binary_probability",
        "output_value": "probability",
        "output_contract": "claim.fraud_probability@1.1.0",
        "features": ["claimant_claim_count_1h", "claimant_amount_sum_24h",
                     "provider_claim_count_1h", "provider_amount_sum_1h"],
        "estimator": lambda: LogisticRegression(max_iter=1000,
                                                random_state=42),
        "data": _claim_fraud,
    },
    "uw_eligibility_logistic": {
        "version": "1",
        "kind": "binary_probability",
        "output_value": "probability",
        "output_contract": "underwriting.eligibility_probability@1.0.0",
        "features": ["claimant_claim_count_1h", "claimant_amount_sum_24h",
                     "provider_claim_count_1h", "provider_amount_sum_1h"],
        "estimator": lambda: LogisticRegression(max_iter=1000,
                                                random_state=42),
        "data": _uw_eligibility,
    },
    "premium_linear": {
        "version": "1",
        "kind": "regression",
        "output_value": "premium",
        "output_contract": "pricing.premium_estimate@1.0.0",
        "features": ["txn.amount", "claimant_claim_count_1h",
                     "claimant_amount_sum_24h"],
        "estimator": lambda: LinearRegression(),
        "data": _premium,
    },
}


def sha256_file(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def synth_data(model_id: str, n: int = 20000, seed: int = 42):
    return MODELS[model_id]["data"](n, np.random.default_rng(seed))


def _probe(model_id: str, X: np.ndarray):
    """Model output the same way the inference service reads it."""
    if MODELS[model_id]["kind"] == "binary_probability":
        return X[:, 1] if X.ndim == 2 else X
    return X.ravel()


def train(model_id: str):
    spec = MODELS[model_id]
    seed = int(os.environ.get("RTDP_TRAIN_SEED", "42"))
    X, y = spec["data"](20000, np.random.default_rng(seed))
    est = spec["estimator"]()
    est.fit(X, y)

    out_dir = BUILD / model_id / spec["version"]
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {"zipmap": False} if spec["kind"] == "binary_probability" else None
    onnx_model = to_onnx(est, X[:1].astype(np.float64),
                         options=opts, target_opset=17)
    model_path = out_dir / "model.onnx"
    model_path.write_bytes(onnx_model.SerializeToString())
    model_digest = sha256_file(model_path)

    features = spec["features"]
    input_schema = {"ordered_features": features,
                    "types": {f: "float64" for f in features}}
    (out_dir / "input_schema.json").write_text(
        json.dumps(input_schema, indent=2))
    input_schema_digest = "sha256:" + hashlib.sha256(
        json.dumps(input_schema, sort_keys=True).encode()).hexdigest()

    preprocessing = {"kind": "none",
                     "note": "features arrive already normalized"}
    (out_dir / "preprocessing.json").write_text(
        json.dumps(preprocessing, indent=2))
    preproc_digest = "sha256:" + hashlib.sha256(
        json.dumps(preprocessing, sort_keys=True).encode()).hexdigest()

    # Golden vectors for warm-readiness checks: fixed inputs -> outputs.
    rng = np.random.default_rng(7)
    golden_X = rng.uniform(0, 10, (5, X.shape[1]))
    golden_X[:, 0] *= 400.0  # txn.amount scale for premium_linear
    if spec["kind"] == "binary_probability":
        golden_out = est.predict_proba(golden_X)[:, 1].tolist()
    else:
        golden_out = est.predict(golden_X).tolist()
    (out_dir / "golden_vectors.json").write_text(json.dumps(
        [{"input": x.tolist(), "value": v}
         for x, v in zip(golden_X, golden_out)], indent=2))

    provenance = {
        "kind": "synthetic",
        "generator": "ml/seed-model/train.py",
        "training_seed": seed,
    }
    if hasattr(est, "coef_"):
        provenance["sklearn_coefficients"] = np.atleast_2d(
            est.coef_).tolist()
        provenance["sklearn_intercept"] = np.atleast_1d(
            est.intercept_).tolist()

    meta = {
        "model_id": model_id,
        "model_version": spec["version"],
        "model_digest": model_digest,
        "input_schema_digest": input_schema_digest,
        "preprocessing_digest": preproc_digest,
        "output_contract": spec["output_contract"],
        "output_kind": spec["kind"],
        "output_value": spec["output_value"],
        "provenance": provenance,
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2))
    return {"model": model_id, "digest": model_digest,
            "input_schema": input_schema_digest,
            "preprocessing": preproc_digest}


def main():
    ids = sys.argv[1:] or list(MODELS)
    results = []
    for model_id in ids:
        if model_id not in MODELS:
            raise SystemExit(f"unknown model {model_id!r}")
        results.append(train(model_id))
        print(json.dumps(results[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
