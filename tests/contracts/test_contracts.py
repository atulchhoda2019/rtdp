"""Contract registry acceptance tests (Phase 0 gate).

Valid contracts load; malformed/semantically-invalid contracts fail with
structured diagnostics. Cross-tenant fixtures never collide.
"""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services" / "control-plane"))

from rtdp_contracts.registry import (ContractRegistry, ContractError,  # noqa: E402
                                     load_yaml, validate_signal_contract)
from rtdp_contracts.envelope import validate_envelope  # noqa: E402
from rtdp_contracts.compiler import compile_bundle, apply_overlay  # noqa: E402
from rtdp_contracts.digest import canonical_digest  # noqa: E402

REG = ContractRegistry(ROOT / "contracts", ROOT / "assets")


def test_signal_contract_validates():
    docs = REG.load_all("signal_contract")
    assert any(d["signal_name"] == "claim.fraud_probability"
               for d in docs)


def test_feature_definitions_have_tiers():
    docs = {d["feature_name"]: d for d in REG.load_all("feature_definition")}
    assert docs["claimant_claim_count_1h"]["tier"] == "tier1"
    assert docs["provider_claim_count_1h"]["tier"] == "tier2"


@pytest.mark.parametrize("bad, code", [
    ({"signal_name": "x", "version": "not-semver"}, "BAD_VERSION"),
    ({"signal_name": "x", "version": "1.0.0", "value_schema": {},
      "semantics": {}, "allowed_statuses": ["OK"]}, "BAD_SCHEMA"),
    ({"signal_name": "UPPERCASE!", "version": "1.0.0"}, "BAD_NAME"),
    ({"signal_name": "x", "version": "1.0.0",
      "value_schema": {"p": {"type": "float64", "minimum": 0, "maximum": 1}},
      "semantics": {"meaning": "m", "population": "p", "target_label": "t",
                    "label_observation_horizon": "P30D", "unit": "probability",
                    "higher_means": "more_risk"},
      "allowed_statuses": ["MADE_UP"]}, "BAD_STATUSES"),
])
def test_invalid_contracts_rejected(bad, code):
    with pytest.raises(ContractError) as e:
        validate_signal_contract(bad, "test-asset")
    assert e.value.code == code


def test_envelope_validation():
    contract = REG.get("signal_contract", "claim.fraud_probability",
                       "1.1.0")
    good = {
        "signal_name": "claim.fraud_probability",
        "contract_version": "1.1.0",
        "status": "OK",
        "transaction_revision": 1,
        "values": {"probability": 0.87},
    }
    assert validate_envelope(good, contract)["probability"] == 0.87

    with pytest.raises(ContractError):  # wrong unit semantics: [0,100]
        validate_envelope({**good, "values": {"probability": 42.0}}, contract)
    with pytest.raises(ContractError):  # wrong contract version
        validate_envelope({**good, "contract_version": "2.0.0"}, contract)
    with pytest.raises(ContractError):  # disallowed status
        validate_envelope({**good, "status": "DEGRADED"}, contract)


def test_overlay_allowlist():
    product = yaml.safe_load(
        (ROOT / "assets/seed/platform/products/claim_decisioning/1.yaml").read_text())
    ok = apply_overlay(product, {"overrides":
                                 {"thresholds.decline_probability": 0.85}})
    assert ok["thresholds"]["decline_probability"] == 0.85
    with pytest.raises(ContractError):
        apply_overlay(product, {"overrides":
                                {"execution.total_deadline_ms": 5000}})


def test_bundle_compile_and_digest_stable():
    product = yaml.safe_load(
        (ROOT / "assets/seed/platform/products/claim_decisioning/1.yaml").read_text())
    b1 = compile_bundle(REG, product, None, "tenant_a")
    b2 = compile_bundle(REG, product, None, "tenant_a")
    assert b1["digest"] == b2["digest"]  # content-addressed, deterministic
    assert b1["ruleset"]["digest"].startswith("sha256:")


def test_cross_tenant_bundles_differ():
    product = yaml.safe_load(
        (ROOT / "assets/seed/platform/products/claim_decisioning/1.yaml").read_text())
    sub_b = yaml.safe_load(
        (ROOT / "assets/seed/tenants/tenant_b/subscription.yaml").read_text())
    a = compile_bundle(REG, product, None, "tenant_a")
    b = compile_bundle(REG, product, sub_b["overlays"][0], "tenant_b")
    assert a["digest"] != b["digest"]
    assert a["effective_config"]["thresholds"]["decline_probability"] == 0.90
    assert b["effective_config"]["thresholds"]["decline_probability"] == 0.85


def test_digest_is_sha256_canonical():
    d = canonical_digest({"b": 1, "a": 2})
    assert d == canonical_digest({"a": 2, "b": 1})
