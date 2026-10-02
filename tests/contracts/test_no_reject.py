"""No-reject invariant for the receipt-claims product (Slobo legal rule).

AI can approve and fast-track; AI never rejects — anything not approved
goes to a person. Enforced by walking every outcome the configuration can
produce, not by prompt wording. A third outcome (DECLINE/REJECT/DENY)
anywhere in the receipt product's config fails the build.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

ALLOWED_OUTCOMES = {"APPROVE", "REVIEW"}
FORBIDDEN_ACTION_HINTS = ("DECLINE", "REJECT", "DENY", "DENIAL")


def _load(path):
    return yaml.safe_load((ROOT / path).read_text())


def test_ruleset_outcomes_are_approve_or_review_only():
    ruleset = _load("assets/seed/platform/rulesets/receipt_policy/1.yaml")
    for rule in ruleset["rules"]:
        decision = rule["outcome"]["decision"]
        assert decision in ALLOWED_OUTCOMES, (
            f"rule {rule['id']} produces {decision} — the receipt product "
            f"must never reject; unapproved claims route to human review")
    assert ruleset["default_outcome"] in ALLOWED_OUTCOMES
    assert ruleset["missing_required_signal_outcome"] in ALLOWED_OUTCOMES


def test_action_policy_has_no_reject_binding():
    policy = _load("assets/seed/platform/action_policies/receipt_actions/1.yaml")
    for action in policy["allowed_actions"]:
        assert not any(h in action for h in FORBIDDEN_ACTION_HINTS), (
            f"action {action} looks like a rejection path")
    for action, decisions in policy["action_rules"].items():
        for d in decisions:
            assert d in ALLOWED_OUTCOMES, (
                f"{action} bound to {d} — only APPROVE/REVIEW may dispatch")


def test_review_lane_is_bound_and_reconcilable():
    """REVIEW must route somewhere a human owns — the residual BPO queue —
    and failed dispatch lands UNKNOWN/RECONCILE, never silent drop."""
    policy = _load("assets/seed/platform/action_policies/receipt_actions/1.yaml")
    assert "ROUTE_TO_REVIEW_QUEUE" in policy["allowed_actions"]
    assert "REVIEW" in policy["action_rules"]["ROUTE_TO_REVIEW_QUEUE"]
    assert policy["on_unknown_outcome"] == "RECONCILE"


def test_product_routing_has_no_reject_escape():
    product = _load("assets/seed/platform/products/receipt_claim/1.yaml")
    assert product["execution"]["missing_required_signal"] in ALLOWED_OUTCOMES
    for rule in _load(
            "assets/seed/platform/rulesets/receipt_policy/1.yaml")["rules"]:
        for hint in FORBIDDEN_ACTION_HINTS:
            assert hint not in rule["outcome"].get("reason", "")
