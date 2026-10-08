#!/usr/bin/env python3
"""Watch the effect leg of a governed decision end to end.

Polls Aurora for the action_execution rows of a decision and prints each
state transition live, so a demo shows:

    HELD (approval_held) -> RELEASED -> DISPATCHING -> ACKNOWLEDGED <prov-ref>

Usage:
    python tools/demo/effect_watch.py --decision-id dec-...
    python tools/demo/effect_watch.py --latest        # newest held intent

SQL is run through $RTDP_PSQL when set (e.g. the kubectl psql wrapper used
by tests/e2e/test_agent_g10.py), else `psql $RTDP_DB_DSN`.
"""

import argparse
import os
import shlex
import subprocess
import sys
import time

C_DIM, C_GREEN, C_YELLOW, C_OFF = "\033[2m", "\033[32m", "\033[33m", "\033[0m"


def psql(sql):
    if os.environ.get("RTDP_PSQL"):
        out = subprocess.run(
            [os.environ["RTDP_PSQL"], sql],
            capture_output=True, text=True)
    else:
        dsn = os.environ.get("RTDP_DB_DSN")
        if not dsn:
            sys.exit("set RTDP_PSQL or RTDP_DB_DSN")
        out = subprocess.run(
            ["psql", dsn, "-tAc", sql], capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"psql failed: {out.stderr.strip()}")
    return [l for l in out.stdout.splitlines() if l.strip()]


def rows(sql):
    return [l.split("|") for l in psql(sql)]


def latest_held():
    r = rows("""
        SELECT decision_id FROM approval_held
        WHERE state='HELD' ORDER BY created_at DESC LIMIT 1""")
    return r[0][0] if r else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--decision-id")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    decision_id = a.decision_id
    if a.latest or not decision_id:
        print("waiting for a HELD intent ...", flush=True)
        deadline = time.time() + a.timeout
        while not decision_id:
            if time.time() > deadline:
                sys.exit("no held intent appeared")
            decision_id = latest_held()
            time.sleep(1)
    print(f"{C_YELLOW}watching decision {decision_id}{C_OFF}", flush=True)

    seen = set()
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        held = rows("""
            SELECT idempotency_key, action_type, state FROM approval_held
            WHERE decision_id=%s ORDER BY idempotency_key""" %
                  ("'%s'" % decision_id))
        for key, atype, state in held:
            tag = f"hold:{key}:{state}"
            if tag not in seen:
                seen.add(tag)
                print(f"  {C_DIM}approval_held {atype:<18} {state}{C_OFF}",
                      flush=True)
        ex = rows("""
            SELECT idempotency_key, action_type, state,
                   COALESCE(provider_reference,'-') FROM action_execution
            WHERE decision_id=%s ORDER BY idempotency_key""" %
                  ("'%s'" % decision_id))
        done = True
        for key, atype, state, ref in ex:
            tag = f"exec:{key}:{state}:{ref}"
            if tag not in seen:
                seen.add(tag)
                mark = (C_GREEN if state == "ACKNOWLEDGED" else C_DIM)
                print(f"  {mark}action_execution {atype:<15} {state:<18}"
                      f" ref={ref}{C_OFF}", flush=True)
            if state not in ("ACKNOWLEDGED", "FAILED", "UNKNOWN",
                             "CANCELLED", "EXPIRED"):
                done = False
        if ex and done:
            print(f"{C_GREEN}effect leg complete{C_OFF}", flush=True)
            return
        time.sleep(1)
    sys.exit("timed out before effects reached a terminal state")


if __name__ == "__main__":
    main()
