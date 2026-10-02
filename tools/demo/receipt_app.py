#!/usr/bin/env python3
"""Receipt-claims demo front: pick a corpus receipt or upload your own,
extract with docling, decide through RTDP, show the packet + cost.

  .venv-docling/bin/streamlit run tools/demo/receipt_app.py
"""
import json
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from receipts_pipeline import (BPO_USD_PER_DOC, extract,  # noqa: E402
                               COMPUTE_USD_PER_CPU_SEC, AUTO_USD_PER_DOC,
                               TEXTRACT_USD_PER_PAGE, LLM_RULES_USD_PER_DOC)

CORPUS = Path(__file__).resolve().parents[2] / "tests/fixtures/receipts"


@st.cache_resource
def converter():
    from docling.document_converter import DocumentConverter
    return DocumentConverter()


def decide(ingress, client, txn):
    req = urllib.request.Request(
        f"{ingress}/v1/decide", data=json.dumps(txn).encode(),
        headers={"Content-Type": "application/json",
                 "X-RTDP-Client-Id": client}, method="POST")
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


st.set_page_config(page_title="Receipt claims intake", layout="wide")
st.title("Receipt claims intake — RTDP")
st.caption("Synthetic demo. docling extracts → SLM scores consistency → "
           "pinned rules decide. AI can approve; it cannot reject — "
           "everything else routes to the human review queue.")

with st.sidebar:
    ingress = st.text_input("Ingress", "http://localhost:8080")
    client = st.selectbox("Client", ["demo-client-a", "demo-client-b"],
                          help="demo-client-b carries the stricter $50 "
                               "tenant overlay")
    manifest = json.load(open(CORPUS / "manifest.json"))
    by_id = {m["receipt_id"]: m for m in manifest}
    pick = st.selectbox("Corpus receipt", list(by_id))
    uploaded = st.file_uploader("…or upload a receipt image",
                              type=["png", "jpg", "jpeg"])

img_path = None
if uploaded:
    img_path = f"/tmp/rtdp_upload_{uuid.uuid4().hex[:6]}.png"
    Path(img_path).write_bytes(uploaded.read())
    truth = None
elif pick:
    img_path = str(CORPUS / by_id[pick]["file"])
    truth = by_id[pick]

if img_path:
    col_img, col_ex = st.columns([1, 1.4])
    col_img.image(img_path, caption=Path(img_path).name, width=280)
    ex = extract(converter(), img_path)
    with col_ex:
        st.subheader("Extracted")
        c1, c2, c3 = st.columns(3)
        c1.metric("Total", f"${ex['total'] or 0:.2f}")
        c2.metric("Eligible", f"${ex['eligible']:.2f}"
                  if ex["eligible"] is not None else "—")
        c3.metric("OCR conf", f"{ex['conf']:.2f}")
        if ex["items"]:
            st.dataframe([{"line": i["name"], "price": i["price"],
                           "eligible": {True: "yes", False: "no",
                                        None: "unsure"}[i["eligible"]]}
                          for i in ex["items"]], hide_index=True)
        with st.expander("Raw OCR text"):
            st.code(ex["text"])

    claimed = st.number_input(
        "Claimed amount $", value=float(truth["total"] if truth else
                                        ex["total"] or 0.0), step=1.0)
    if truth and truth.get("eligible_amount") is not None:
        st.caption(f"Truth: total ${truth['total']:.2f} · "
                   f"eligible ${truth['eligible_amount']:.2f} · "
                   f"degradation L{truth['degrade_level']}")

    if st.button("Submit claim", type="primary"):
        txn = {
            "transaction_id": f"ui_{uuid.uuid4().hex[:10]}",
            "transaction_revision": 1, "event_type": "RECEIPT_CLAIM",
            "channel": "PORTAL", "region": "us-east-1",
            "tokenized_claimant": "tok_ui", "provider_id": "prv_rcpt",
            "currency": "USD", "amount": claimed,
            "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                        time.gmtime()),
            "attributes": {"receipt_text": ex["text"],
                           "ocr_confidence": ex["conf"],
                           "extracted_total": ex["total"] or 0.0,
                           "extracted_eligible": ex["eligible"] or 0.0},
        }
        t0 = time.time()
        body = decide(ingress, client, txn)
        ms = int((time.time() - t0) * 1000)
        out = body.get("outcome", "?")
        if out == "DECISION_APPROVE":
            st.success(f"**{out}** — auto-adjudicated")
        elif out == "DECISION_REVIEW":
            st.warning(f"**{out}** — routed to human review queue")
        else:
            st.error(out)
        st.json({"decision_id": body.get("decision_id"),
                 "reasons": body.get("reason_codes"),
                 "bundle_digest": body.get("bundle_digest"),
                 "manifest_epoch": body.get("manifest_epoch"),
                 "action_intents": body.get("action_intents"),
                 "decide_ms": ms})

        st.subheader("Cost of this document")
        measured = (ex["ocr_ms"] / 1000 * COMPUTE_USD_PER_CPU_SEC
                    + AUTO_USD_PER_DOC)
        cf = TEXTRACT_USD_PER_PAGE + LLM_RULES_USD_PER_DOC + BPO_USD_PER_DOC
        c1, c2 = st.columns(2)
        route_cost = measured if out == "DECISION_APPROVE" \
            else measured + BPO_USD_PER_DOC
        c1.metric("This pipeline", f"${route_cost:.4f}",
                  help="docling compute + decision path"
                       + (" + BPO touch" if out == "DECISION_REVIEW"
                          else ""))
        c2.metric("Textract + LLM + BPO baseline", f"${cf:.2f}",
                  help="Counterfactual: per-page OCR fee + frontier LLM "
                       "on every rule + manual review on every doc")
        st.caption("Decision, extraction record and cost are pinned to "
                   "the bundle digest above — replayable.")
