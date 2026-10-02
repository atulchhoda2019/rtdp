# Demo: receipt claims at scale (docling → SLM → rules → BPO cost model)

The document-intake story applied to claims volume: a participant uploads a
photo of a pharmacy receipt, the platform extracts, validates, and either
auto-adjudicates or routes a pre-filled packet to the residual human lane.
Every claim carries a cost record so the demo can show measured cost per
document next to the Textract-plus-LLM counterfactual it replaces.

All data is synthetic. Receipts are rendered in-process and degraded
(blur, skew, noise, coffee stains) to emulate phone photos of paper.

## The pipeline

```
receipt image
  → docling OCR (self-hosted, CPU)            — cost: compute seconds
  → extraction post-pass (regex + lexicon)    — cost: ~zero
       TOTAL line, printed HSA/FSA eligible subtotal,
       per-line eligibility flags
  → POST /v1/decide  (attributes: receipt_text, ocr_confidence,
                       extracted_total, extracted_eligible)
  → SLM signal: receipt.ocr_consistency       — cost: small-model seconds
       "does this document support this claim?"
  → pinned ruleset (receipt_policy@1)         — cost: zero
  → DECISION_APPROVE  → ISSUE_REIMBURSEMENT
    DECISION_REVIEW   → ROUTE_TO_REVIEW_QUEUE (pre-filled packet)
```

Two outcome classes exist. There is no reject path — enforced by
`tests/contracts/test_no_reject.py`, which walks every rule outcome,
action binding, and default in the product's config.

## What the rules encode

| Rule | Fires when | Outcome |
|---|---|---|
| `no_extraction_review` | claim arrived with no extraction attributes | REVIEW |
| `unreadable_review` | `attr.ocr_confidence` below `min_ocr_confidence` | REVIEW |
| `extraction_mismatch` | SLM consistency below `refer_below` | REVIEW |
| `eligible_amount_gap` | claimed > `attr.extracted_eligible` — ineligible lines claimed | REVIEW |
| `over_client_limit` | `txn.amount` above the tenant's per-claim cap | REVIEW |
| `clean_auto_approve` | consistency ≥ `auto_accept` and confidence ok | APPROVE |

Tenant B subscribes with a stricter overlay (`auto_approve_limit: $50`
vs tenant A's `$100`) — the per-client plan difference is configuration,
not code.

## Run it

```bash
make up && make seed          # stack + synthetic tenants + warmed SLMs

# Corpus: N synthetic receipts, degraded, with ground-truth JSON
.venv-docling/bin/python tools/demo/receipt_corpus.py tests/fixtures/receipts 500

# Pipeline: extract → decide → metrics + cost model
.venv-docling/bin/python tools/demo/receipts_pipeline.py \
    --corpus tests/fixtures/receipts --limit 120

# Interactive UI
.venv-docling/bin/streamlit run tools/demo/receipt_app.py
```

Requires the docling venv: `uv venv --python 3.11 .venv-docling &&
uv pip install docling pillow streamlit --python .venv-docling/bin/python`.

## The numbers that matter

`docs/validation/receipts-at-scale.json` after a run:

- `auto_adjudication_rate` — share of claims a person never touches
- `eligible_amount_accuracy` — extracted eligible subtotal vs ground truth
- `lexicon_line_share` — share of item lines decided by the deterministic
  lexicon vs the model (rules-first, model for the tail)
- `tamper_catch_rate` — tampered claims (declared ≠ receipt) that were
  NOT auto-approved
- `measured_usd_per_doc` vs `counterfactual_usd_per_doc` — the 27¢-style
  comparison: per-page OCR fee + LLM-on-every-rule + BPO-everything vs
  measured compute plus a BPO touch on review-route only
- `per_1m_docs_usd` — the volume projection

The demo line: *"extraction stopped being the expensive part once it
stopped being a per-page fee; validation stopped being expensive once
rules did the deterministic work and the model only saw the fuzzy tail."*

## What the demo shows that a slide can't

- `bundle_digest` + `manifest_epoch` on every response — replayable
- Threshold/per-claim-limit changes are config (`thresholds:` in the
  product YAML); no image or migration change
- SLM is a signal provider on a pinned contract — rules hold the
  authority, including on `UNKNOWN`/timeouts
- Degraded documents produce *graceful* REVIEWs with reason codes, not
  errors — the review packet already carries extracted fields
