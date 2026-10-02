#!/usr/bin/env python3
"""Claims-at-scale demo: docling extraction -> RTDP decision -> cost model.

For each receipt in the corpus: extract text with docling, submit a
RECEIPT_CLAIM decision with the extraction as request attributes
(attr.receipt_text, attr.ocr_confidence, attr.extracted_total), then
aggregate auto-adjudication rate and a per-document cost model against a
BPO baseline. Evidence lands in docs/validation/receipts-at-scale.json.

Usage:
  python tools/demo/receipt_corpus.py tests/fixtures/receipts 40
  .venv-docling/bin/python tools/demo/receipts_pipeline.py \
      --corpus tests/fixtures/receipts --ingress http://localhost:8080
"""
import argparse
import json
import re
import statistics
import sys
import time
import urllib.request
import uuid

# Manual data-entry outsourcing typically prices $0.40-$1.50 per document;
# ~3-4 min/receipt at loaded offshore rates. Default is the midpoint.
BPO_USD_PER_DOC = 0.85
# Counterfactual per-document cost of the stack being replaced (config-
# priced, not Slobo's figures): Textract ~$0.07/page (public list price)
# + a frontier LLM checking business rules ~$0.20/doc.
TEXTRACT_USD_PER_PAGE = 0.07
LLM_RULES_USD_PER_DOC = 0.20
# Amortized CPU compute per document: docling (~1-3s) + SLM (~3-8s) on
# commodity instances at ~$0.10/hr plus orchestration overhead.
COMPUTE_USD_PER_CPU_SEC = 0.10 / 3600
AUTO_USD_PER_DOC = 0.004
MAX_TEXT_CHARS = 1200  # keep prompts small; receipts are short documents

# OCR routinely drops decimal points on degraded receipts ("19.99" -> "1999"
# or "19"), so TOTAL tolerates a bare integer. The fallback still requires
# a decimal point to avoid matching store numbers, dates, or card digits.
AMOUNT_RE = re.compile(r"(\d{1,6}[.,]\d{2})")
TOTAL_RE = re.compile(r"T[O0]TA[L1I!][^\d]*?(\d{1,6}(?:[.,]\d{1,2})?)", re.I)
ELIGIBLE_SUB_RE = re.compile(
    r"(?:HSA|FSA|H\s*S\s*A)[^\d]{0,20}?(\d{1,6}(?:[.,]\d{1,2})?)", re.I)
# Item line: "IBUPROF 200MG 50CT      F    6.99" — trailing F flags
# eligible on the printed receipt; price may lose its decimal under OCR.
LINE_RE = re.compile(
    r"^([A-Z0-9 %&./'-]{3,}?)\s+([FH])?\s*(\d{1,6}(?:[.,]\d{1,2})?)$")

# Eligibility lexicon — deterministic first pass over line items.
ELIGIBLE_KW = ["IBUPROF", "TUSSIN", "BANDAGE", "PRESCRIPTION", "RX ",
               "THERMOMETER", "COVID TEST", "FIRST AID", "ALLERGY",
               "CONTACT SOL", "WRIST BRACE", "SUNSCREEN"]
INELIGIBLE_KW = ["DORITOS", "MILK", "SHAMPOO", "ENERGY DRINK", "CANDY",
                 "SODA", "MAGAZINE", "CHIPS", "BEER", "WATER BOTTLE"]
SKIP_LINE_KW = ["STORE #", "TOTAL", "SUBTOTAL", "TAX", "VISA", "AMEX",
                "MC ", "THANK", "HSA", "FSA", "ELIGIBLE"]

def _num(s):
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _money(s):
    """Parse a currency amount, undoing the classic OCR decimal-drop:
    pharmacy receipts rarely exceed $500, so a bare-integer reading like
    '1106' that came from '11.06' rescales by /100."""
    v = _num(s)
    if v is not None and v > 500:
        v = round(v / 100, 2)
    return v


def extract(converter, path):
    """docling -> dict(text, total, eligible, lines, conf, ocr_ms).

    Line fidelity comes from doc.texts (one OCR block per receipt line);
    markdown flattens to a single line and loses item boundaries.
    confidence is our extraction-quality estimate, not docling's native
    score: TOTAL present + parseable amount + sane text length.
    """
    t0 = time.time()
    doc = converter.convert(path).document
    lines = [t.text.strip() for t in getattr(doc, "texts", [])
             if getattr(t, "text", "").strip()]
    text = "\n".join(lines) if lines else (doc.export_to_markdown() or "")
    ms = int((time.time() - t0) * 1000)

    total = _money(TOTAL_RE.search(text).group(1)) if TOTAL_RE.search(text) \
        else None
    if total is None:
        amts = [_num(a) for a in AMOUNT_RE.findall(text)]
        amts = [a for a in amts if a is not None]
        total = max(amts) if amts else None

    # FR-2.1: printed HSA/FSA eligible subtotal is the primary signal.
    eligible = None
    m = ELIGIBLE_SUB_RE.search(text)
    if m:
        eligible = _money(m.group(1))

    # FR-2.2: lexicon classifies each parsed line; unsure lines are the
    # model's tail. Eligibility sum is the fallback when no printed
    # subtotal exists.
    items, decided, unsure = [], 0, 0
    for ln in lines:
        lm = LINE_RE.match(ln)
        if not lm or any(k in ln.upper() for k in SKIP_LINE_KW):
            continue
        name, flag, price_s = lm.group(1).strip(), lm.group(2), lm.group(3)
        price = _money(price_s)
        if price is None:
            continue
        up = name.upper()
        if flag == "F" or any(k in up for k in ELIGIBLE_KW):
            elig, decided = True, decided + 1
        elif flag == "H" or any(k in up for k in INELIGIBLE_KW):
            elig, decided = False, decided + 1
        else:
            elig, unsure = None, unsure + 1
        items.append({"name": name, "price": price, "eligible": elig})
    if eligible is None and items:
        s = sum(i["price"] for i in items if i["eligible"])
        eligible = round(s, 2) if s > 0 else None

    conf = 0.0
    if TOTAL_RE.search(text):
        conf += 0.4
    if total is not None:
        conf += 0.2
    if items:
        conf += 0.2
    if 80 <= len(text) <= 3000:
        conf += 0.2
    return {"text": text[:MAX_TEXT_CHARS], "total": total,
            "eligible": eligible, "items": items, "unsure": unsure,
            "decided": decided, "conf": round(conf, 2), "ocr_ms": ms}


def decide(ingress, client, txn, timeout=45):
    req = urllib.request.Request(
        f"{ingress}/v1/decide", data=json.dumps(txn).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), int(
                (time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"error": f"http {e.code}"}
        return e.code, body, int((time.time() - t0) * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="tests/fixtures/receipts")
    ap.add_argument("--ingress", default="http://localhost:8080")
    ap.add_argument("--client", default="demo-client-a")
    ap.add_argument("--limit", type=int, default=0, help="process N receipts")
    ap.add_argument("--tamper-rate", type=float, default=0.15,
                    help="fraction of claims where declared != receipt total")
    ap.add_argument("--bpo-rate", type=float, default=BPO_USD_PER_DOC)
    ap.add_argument("--evidence",
                    default="docs/validation/receipts-at-scale.json")
    args = ap.parse_args()

    from docling.document_converter import DocumentConverter
    converter = DocumentConverter()

    manifest = json.load(open(f"{args.corpus}/manifest.json"))
    if args.limit:
        manifest = manifest[:args.limit]
    run = uuid.uuid4().hex[:8]
    rows = []
    import random
    rng = random.Random(7)
    for m in manifest:
        ex = extract(converter, f"{args.corpus}/{m['file']}")
        # An honest HSA claim asks for the eligible amount, not the receipt
        # total — ineligible lines (groceries, cosmetics) aren't reimbursable.
        claimed = m.get("eligible_amount") or m["total"]
        tampered = rng.random() < args.tamper_rate
        if tampered:
            claimed = round(m["total"] * rng.uniform(1.6, 2.4), 2)
        txn = {
            "transaction_id": f"rcpt_{run}_{m['receipt_id']}",
            "transaction_revision": 1,
            "event_type": "RECEIPT_CLAIM",
            "channel": "PORTAL",
            "region": "us-east-1",
            "tokenized_claimant": f"tok_rcpt_{m['receipt_id']}",
            "provider_id": "prv_rcpt",
            "currency": "USD",
            "amount": claimed,
            "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()),
            "attributes": {
                "receipt_text": ex["text"],
                "ocr_confidence": ex["conf"],
                "extracted_total": ex["total"] or 0.0,
                "extracted_eligible": ex["eligible"] or 0.0,
            },
        }
        status, body, dec_ms = decide(args.ingress, args.client, txn)
        rows.append({
            "receipt_id": m["receipt_id"],
            "degrade_level": m["degrade_level"],
            "truth_total": m["total"],
            "truth_eligible": m.get("eligible_amount"),
            "claimed": claimed,
            "tampered": tampered,
            "extracted_total": ex["total"],
            "extracted_eligible": ex["eligible"],
            "lines_decided": ex["decided"],
            "lines_unsure": ex["unsure"],
            "ocr_confidence": ex["conf"],
            "text_chars": len(ex["text"]),
            "docling_ms": ex["ocr_ms"],
            "decide_ms": dec_ms,
            "http": status,
            "outcome": body.get("outcome"),
            "reasons": body.get("reason_codes"),
            "bundle_digest": body.get("bundle_digest"),
        })
        print(f"{m['receipt_id']} L{m['degrade_level']} "
              f"conf={ex['conf']:.2f} ext={ex['total']} elig={ex['eligible']} "
              f"claimed={claimed} -> "
              f"{status} {body.get('outcome')} {body.get('reason_codes')} "
              f"(ocr {ex['ocr_ms']}ms + decide {dec_ms}ms)")

    n = len(rows) or 1
    auto = sum(1 for r in rows if r["outcome"] == "DECISION_APPROVE")
    review = sum(1 for r in rows if r["outcome"] == "DECISION_REVIEW")
    tampered = [r for r in rows if r["tampered"]]
    tamper_caught = sum(1 for r in tampered
                        if r["outcome"] != "DECISION_APPROVE")
    total_match = sum(1 for r in rows
                      if r["extracted_total"] is not None
                      and abs(r["extracted_total"] - r["truth_total"]) < 1.0)
    elig_known = [r for r in rows
                  if r["truth_eligible"] is not None
                  and r["extracted_eligible"] is not None]
    elig_match = sum(1 for r in elig_known
                     if abs(r["extracted_eligible"] - r["truth_eligible"])
                     < 1.0)
    lines_total = sum(r["lines_decided"] + r["lines_unsure"] for r in rows)
    lines_decided = sum(r["lines_decided"] for r in rows)

    # Measured cost: docling CPU-seconds + decision-path share of the SLM.
    measured_doc = statistics.mean(
        r["docling_ms"] / 1000 * COMPUTE_USD_PER_CPU_SEC
        + AUTO_USD_PER_DOC for r in rows)
    # Counterfactual: Textract per page + a frontier LLM on every rule,
    # and every document still gets a BPO touch in the manual flow.
    counterfactual_doc = (TEXTRACT_USD_PER_PAGE + LLM_RULES_USD_PER_DOC
                          + args.bpo_rate)
    per_doc = (auto * measured_doc
               + review * (measured_doc + args.bpo_rate)) / n
    summary = {
        "run_id": run,
        "receipts": len(rows),
        "auto_adjudication_rate": round(auto / n, 3),
        "review_rate": round(review / n, 3),
        "tamper_catch_rate": round(tamper_caught / len(tampered), 3)
        if tampered else None,
        "ocr_total_match_rate": round(total_match / n, 3),
        "eligible_amount_accuracy": round(elig_match / len(elig_known), 3)
        if elig_known else None,
        "lexicon_line_share": round(lines_decided / lines_total, 3)
        if lines_total else None,
        "docling_ms": {"mean": int(statistics.mean(r["docling_ms"]
                                                 for r in rows))},
        "decide_ms": {"mean": int(statistics.mean(r["decide_ms"]
                                                for r in rows))},
        "cost_model": {
            "bpo_usd_per_doc": args.bpo_rate,
            "measured_usd_per_doc": round(measured_doc, 4),
            "counterfactual_usd_per_doc": round(counterfactual_doc, 4),
            "counterfactual_note":
                "Textract per-page + LLM on every rule + BPO on every doc",
            "blended_usd_per_doc": round(per_doc, 4),
            "per_1m_docs_usd": round(per_doc * 1_000_000),
            "counterfactual_per_1m_docs_usd": int(
                counterfactual_doc * 1_000_000),
            "savings_pct_at_1m": round(
                100 * (1 - per_doc / counterfactual_doc), 1),
        },
        "by_degrade_level": {},
        "sample_decisions": rows[:3],
    }
    for lv in range(4):
        sub = [r for r in rows if r["degrade_level"] == lv]
        if sub:
            summary["by_degrade_level"][lv] = {
                "n": len(sub),
                "auto_rate": round(
                    sum(1 for r in sub
                        if r["outcome"] == "DECISION_APPROVE")
                    / len(sub), 2),
            }
    with open(args.evidence, "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary["cost_model"], indent=1))
    print(f"auto={summary['auto_adjudication_rate']} "
          f"review={summary['review_rate']} "
          f"match={summary['ocr_total_match_rate']} "
          f"-> {args.evidence}")


if __name__ == "__main__":
    sys.exit(main())
