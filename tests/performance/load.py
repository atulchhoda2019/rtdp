#!/usr/bin/env python3
"""Load gate: N TPS for a duration, unique requests, honest percentiles.

make load TPS=500 DURATION=5m
Reports p50/p95/p99/p99.99, unique results after drain, duplicates, errors.
Evidence lands in docs/validation/load-<ts>.json.
"""

import argparse
import json
import statistics
import threading
import time
import urllib.request
from pathlib import Path

INGRESS = "http://localhost:8080"


def worker(client_id, txn_base, rate, stop, lat, errors, seen):
    interval = 1.0 / rate
    i = 0
    while not stop.is_set():
        i += 1
        txn = dict(txn_base)
        txn["transaction_id"] = f"{txn_base['transaction_id']}_{i}"
        t0 = time.perf_counter()
        try:
            req = urllib.request.Request(
                f"{INGRESS}/v1/decide", data=json.dumps(txn).encode(),
                headers={"Content-Type": "application/json",
                         "X-RTDP-Client-Id": client_id}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read())
            lat.append((time.perf_counter() - t0) * 1000)
            seen[body.get("decision_id")] = body.get("outcome")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error", "")[:160]
            except Exception:
                detail = ""
            errors.append(f"HTTP {e.code}: {detail}")
        except Exception as e:
            errors.append(str(e)[:200])
        elapsed = time.perf_counter() - t0
        if elapsed < interval:
            time.sleep(interval - elapsed)


def pct(vals, q):
    if not vals:
        return float("nan")
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * q / 100))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tps", type=int, default=500)
    ap.add_argument("--duration", default="5m")
    ap.add_argument("--threads", type=int, default=16)
    args = ap.parse_args()
    dur_s = (float(args.duration[:-1]) * 60 if args.duration.endswith("m")
             else float(args.duration.rstrip("s")))

    txn_base = {
        "transaction_revision": 1, "event_type": "AUTH_REQUEST",
        "channel": "ECOMMERCE", "region": "us-east-1",
        "tokenized_pan": "tok_load", "merchant_id": "mch_load",
        "currency": "USD", "amount": 42.0,
        "event_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    lat, errors, seen = [], [], {}
    stop = threading.Event()
    per_thread = args.tps / args.threads
    ts = []
    for i in range(args.threads):
        client = "demo-client-a" if i % 2 == 0 else "demo-client-b"
        base = dict(txn_base)
        base["transaction_id"] = f"load_{int(time.time())}_t{i}"
        base["tokenized_pan"] = f"tok_load_{i}"
        t = threading.Thread(target=worker,
                             args=(client, base, per_thread, stop,
                                   lat, errors, seen))
        t.start()
        ts.append(t)
    time.sleep(dur_s)
    stop.set()
    for t in ts:
        t.join()
    time.sleep(5)  # drain

    report = {
        "tps_target": args.tps, "duration_s": dur_s,
        "requests_completed": len(lat), "unique_decisions": len(seen),
        "duplicates": len(lat) - len(seen), "errors": len(errors),
        "error_sample": errors[:10],
        "latency_ms": {
            "p50": pct(lat, 50), "p95": pct(lat, 95),
            "p99": pct(lat, 99), "p99_99": pct(lat, 99.99),
        },
        "hardware": "local-colima",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    out = Path("docs/validation") / f"load-{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
