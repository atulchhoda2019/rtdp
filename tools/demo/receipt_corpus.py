#!/usr/bin/env python3
"""Synthetic receipt corpus for the claims-intake demo.

Generates PNG receipts with ground-truth JSON, then degraded copies
(blur / skew / noise / low-contrast / glare / jpeg artifacts) to emulate
phone photos of paper receipts. All data is synthetic.
"""
import io
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageEnhance, ImageFont

MERCHANTS = [
    ("CVS PHARMACY", "pharmacy"), ("WALGREENS", "pharmacy"),
    ("RITE AID", "pharmacy"), ("PUBLIX PHARMACY", "pharmacy"),
    ("COSTCO PHARMACY", "pharmacy"), ("QUEST DIAGNOSTICS", "lab"),
    ("URGENT CARE CENTER", "clinic"), ("CITY MEDICAL IMAGING", "imaging"),
]
# Abbreviated line items as printed on real pharmacy receipts.
# eligible = HSA/FSA-eligible under the synthetic plan rules.
ITEM_POOL = [
    ("IBUPROF 200MG 50CT", 6.99, True), ("TUSSIN DM 4OZ", 9.49, True),
    ("BANDAGE FLEX 30CT", 8.25, True), ("PRESCRIPTION RX", 24.50, True),
    ("THERMOMETER DIGTL", 12.99, True), ("COVID TEST 2PK", 19.99, True),
    ("FIRST AID KIT", 14.75, True), ("ALLERGY RLF 30CT", 11.29, True),
    ("CONTACT SOLUTION", 9.85, True), ("WRIST BRACE", 16.40, True),
    ("DORITOS NACHO", 4.29, False), ("MILK 2% GAL", 3.89, False),
    ("SHAMPOO 12OZ", 7.99, False), ("ENERGY DRINK", 3.49, False),
    ("CANDY BAR", 1.79, False), ("SODA 6PK", 5.99, False),
    ("MAGAZINE", 5.50, False), ("SUNSCREEN SPF50", 10.99, True),
]
TAX_RATE = 0.07  # tax applies to ineligible lines only

W, MARGIN = 420, 24


def _font(sz):
    for p in ("/System/Library/Fonts/Courier.ttc",
              "/System/Library/Fonts/Menlo.ttc"):
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            pass
    return ImageFont.load_default()


def render_receipt(rng):
    merchant, mtype = rng.choice(MERCHANTS)
    items = [(n, round(p * rng.uniform(0.8, 1.4), 2), e)
             for n, p, e in rng.sample(ITEM_POOL, rng.randint(2, 6))]
    eligible_amt = round(sum(p for _, p, e in items if e), 2)
    taxable = round(sum(p for _, p, e in items if not e), 2)
    tax = round(taxable * TAX_RATE, 2)
    subtotal = round(sum(p for _, p, _ in items), 2)
    total = round(subtotal + tax, 2)
    # ~half of receipts print the HSA/FSA eligible subtotal line
    prints_subtotal = rng.random() < 0.5
    dt = datetime.now() - timedelta(days=rng.randint(0, 60))
    lines = [merchant, f"STORE #{rng.randint(100,9999)}",
             dt.strftime("%m/%d/%Y %H:%M"), "-" * 38]
    for n, p, e in items:
        # real receipts mark eligible lines with a trailing F/H flag
        lines.append(f"{n[:25]:<26}{'F' if e else ' '} {p:>8.2f}")
    lines.append("-" * 38)
    if taxable > 0:
        lines.append(f"{'SUBTOTAL':<28}{subtotal:>8.2f}")
        lines.append(f"{'TAX':<28}{tax:>8.2f}")
    if prints_subtotal:
        lines.append(f"{'HSA/FSA ELIGIBLE':<28}{eligible_amt:>8.2f}")
    lines += [f"{'TOTAL':<28}{total:>8.2f}",
              f"{rng.choice(['VISA','MC','AMEX'])} ****{rng.randint(1000,9999)}",
              "THANK YOU!"]
    h = MARGIN * 2 + len(lines) * 26 + 40
    img = Image.new("L", (W, h), 255)
    d = ImageDraw.Draw(img)
    f = _font(20)
    for i, ln in enumerate(lines):
        d.text((MARGIN, MARGIN + i * 26), ln, fill=20, font=f)
    truth = {"merchant": merchant, "merchant_type": mtype,
             "date": dt.strftime("%Y-%m-%d"), "total": total,
             "subtotal": subtotal, "tax": tax,
             "eligible_amount": eligible_amt,
             "prints_eligible_subtotal": prints_subtotal,
             "items": [{"name": n, "price": p, "eligible": e}
                       for n, p, e in items]}
    return img.convert("RGB"), truth


def degrade(img, rng, level):
    """Return a degraded copy. level 0=clean 1=mild 2=bad 3=terrible."""
    out = img
    if level >= 1:
        out = out.rotate(rng.uniform(-6, 6) * level, fillcolor=(245, 245, 240),
                         resample=Image.BILINEAR, expand=False)
        out = ImageEnhance.Contrast(out).enhance(max(0.35, 1 - 0.2 * level))
    if level >= 2:
        out = out.filter(ImageFilter.GaussianBlur(rng.uniform(0.8, 1.6)))
        px = out.load()
        for _ in range(int(out.width * out.height * 0.02 * level)):
            x, y = rng.randrange(out.width), rng.randrange(out.height)
            v = rng.randint(0, 60) if rng.random() < 0.5 else rng.randint(200, 255)
            px[x, y] = (v, v, v)
    if level >= 3:
        # coffee stain + heavy jpeg artifacts + vignette
        d = ImageDraw.Draw(out, "RGBA")
        cx, cy = rng.randrange(out.width), rng.randrange(out.height)
        r_ = rng.randint(40, 110)
        d.ellipse([cx - r_, cy - r_, cx + r_, cy + r_], fill=(139, 90, 43, 60))
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=rng.randint(5, 15))
        out = Image.open(buf).convert("RGB")
        out = ImageEnhance.Brightness(out).enhance(rng.uniform(0.6, 0.8))
    return out


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/receipts"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    seed = int(sys.argv[3]) if len(sys.argv) > 3 else 42
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    for i in range(n):
        img, truth = render_receipt(rng)
        # Realistic upload mix: most phone photos are legible; the bad tail
        # is what routes to review.
        level = rng.choices([0, 1, 2, 3], weights=[40, 35, 18, 7])[0]
        img = degrade(img, rng, level)
        rid = f"rcpt_{i:04d}"
        img.save(os.path.join(out_dir, f"{rid}.png"))
        truth.update({"receipt_id": rid, "degrade_level": level,
                      "file": f"{rid}.png"})
        manifest.append(truth)
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    lv = [m["degrade_level"] for m in manifest]
    print(f"wrote {n} receipts to {out_dir} "
          f"(levels: clean={lv.count(0)} mild={lv.count(1)} "
          f"bad={lv.count(2)} terrible={lv.count(3)})")


if __name__ == "__main__":
    main()
