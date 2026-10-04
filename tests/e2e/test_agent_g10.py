#!/usr/bin/env python3
"""G10 — ADR-015 governed agent gateway.

Runs against a seeded local stack:

    RTDP_GATEWAY=http://localhost:8096 \\
    RTDP_REGISTRY=http://localhost:8090 \\
        python tests/e2e/test_agent_g10.py

Requires `make seed` (hotel agents guest-agent-g / hotel-ops-dot, the
hotel_stay@1 product subscription, gateway_tools.yaml) plus
agent-gateway, ingress, and approval-service. The static no-write check
inspects the gateway source tree — run from the repo root so
services/agent-gateway resolves.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import urllib.error

GATEWAY = os.environ.get("RTDP_GATEWAY", "http://localhost:8096")
REGISTRY = os.environ.get("RTDP_REGISTRY", "http://localhost:8090")
PSQL = os.environ.get(
    "RTDP_PSQL",
    "docker compose exec -T postgres psql -U rtdp -d rtdp -tAc")
REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
RUN = f"{int(time.time())}"

results = []


def check(uc, name, ok, detail=""):
    results.append((uc, name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {uc} {name}  {detail}")


def _req(method, base, path, body=None, headers=None, timeout=20):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def gw(method, path, token=None, body=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return _req(method, GATEWAY, path, body, headers)


def session(principal_kind, principal_id, agent_ids):
    """Mint a delegation through the gateway's session endpoint — the
    caller never sees or signs a key."""
    st, r = gw("POST", "/v1/session", body={
        "principal_kind": principal_kind, "principal_id": principal_id,
        "tenant_id": "tenant_a", "agent_ids": agent_ids})
    assert st == 200, f"session failed: {st} {r}"
    return r["delegation_token"]


def call_fact(tool, outcome, timeout_s=15):
    sql = ("SELECT outcome FROM agent_call WHERE tool='%s' "
           "ORDER BY at DESC LIMIT 1" % tool)
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        out = subprocess.run(
            shlex.split(PSQL) + [sql], capture_output=True, text=True,
            cwd=REPO)
        last = out.stdout.strip()
        if last == outcome:
            return last
        time.sleep(0.5)
    return last


def last_decision_call(tool):
    sql = ("SELECT outcome || '|' || COALESCE(decision_id,'') "
           "FROM agent_call WHERE tool='%s' "
           "ORDER BY at DESC LIMIT 1" % tool)
    out = subprocess.run(
        shlex.split(PSQL) + [sql], capture_output=True, text=True,
        cwd=REPO)
    return out.stdout.strip()


# ----------------------------------------------------------------------
# Tokens — gateway-minted (callers never sign).
# ----------------------------------------------------------------------

guest_tok = session("PERSON", "guest-g", ["guest-agent-g"])
ops_tok = session("ORG", "hotel-h", ["hotel-ops-dot"])
print("tokens minted: guest=%dB ops=%dB" % (len(guest_tok), len(ops_tok)))

# ----------------------------------------------------------------------
# G10a — same read tool, different scopes -> different filtered results
# ----------------------------------------------------------------------

st, guest_res = gw("POST", "/v1/tools/reservations.lookup",
                   guest_tok, {"task_id": f"g10a-{RUN}", "input": {}})
guest_rows = guest_res.get("result", [])
check("G10a", "customer sees own rows only", st == 200 and len(guest_rows) == 2
      and all("rate_code" not in r and "notes" not in r
              for r in guest_rows),
      f"{len(guest_rows)} rows fields={sorted(guest_rows[0].keys()) if guest_rows else []}")

st, ops_res = gw("POST", "/v1/tools/reservations.lookup",
                 ops_tok, {"task_id": f"g10a-{RUN}", "input": {}})
ops_rows = ops_res.get("result", [])
check("G10a", "employee sees all rows + rate_code",
      st == 200 and len(ops_rows) == 3
      and all("rate_code" in r for r in ops_rows),
      f"{len(ops_rows)} rows rate_codes={[r.get('rate_code') for r in ops_rows]}")

st, r = gw("POST", "/v1/tools/crm.profile.read", guest_tok,
           {"task_id": f"g10a-{RUN}", "input": {"guest_id": "guest-g"}})
check("G10a", "customer scope DENIED on employee-only tool",
      st == 403 and call_fact("crm.profile.read", "DENIED") == "DENIED",
      f"status={st} fact={call_fact('crm.profile.read', 'DENIED')}")

st, r = gw("POST", "/v1/tools/crm.profile.read", ops_tok,
           {"task_id": f"g10a-{RUN}", "input": {"guest_id": "guest-g"}})
crm_rows = r.get("result", [])
check("G10a", "employee crm read field-filtered (no payment/history)",
      st == 200 and crm_rows and "payment" not in crm_rows[0]
      and "history" not in crm_rows[0]
      and "preferences" in crm_rows[0],
      f"status={st} fields={sorted(crm_rows[0].keys()) if crm_rows else []}")

# ----------------------------------------------------------------------
# G10b — mutating tool routes through /v1/decide (decision + intent)
# ----------------------------------------------------------------------

st, r = gw("POST", "/v1/tools/pms.room.assign", ops_tok, {
    "task_id": f"g10b-{RUN}", "purpose": "PREPARE_STAY",
    "input": {"attributes": {"reservation_id": "res-9001",
                             "room_id": "r-1204",
                             "preferred_floor": 10}}})
decision_id = r.get("decision_id", "")
intents = [i.get("action_type") for i in r.get("intents", [])]
check("G10b", "pms.room.assign -> decision fact + ASSIGN_ROOM intent",
      st in (200, 202) and decision_id and "ASSIGN_ROOM" in intents,
      f"status={st} outcome={r.get('outcome')} intents={intents}")

row = last_decision_call("pms.room.assign")
oc, _, did = row.partition("|")
check("G10b", "agent_call links tool to decision",
      oc in ("OK", "PENDING_APPROVAL") and did == decision_id,
      f"fact={row}")

# Static no-write check: the gateway source may reference backends only
# through the decide path — no direct writes, no backend write client.
src = ""
for fn in ("main.go", "backends.go"):
    p = os.path.join(REPO, "services", "agent-gateway", fn)
    with open(p) as f:
        src += f.read() + "\n"
violations = []
if re.search(r'INSERT INTO (?!agent_call)', src):
    violations.append("INSERT INTO non-agent_call table")
if re.search(r'\b(DELETE|UPDATE|DROP|TRUNCATE)\b(?!\s+--)', src):
    violations.append("DML/DDL verb in gateway source")
# Mutating tool dispatch must go through callDecide — no other writer:
n_callDecide = src.count("callDecide(")
violations += [] if n_callDecide >= 2 else [
    "callDecide not the sole decide path"]
# The http client may only reach ingress / registry / approval hosts.
bad_hosts = re.findall(r'https?://(?!localhost|\$\{?)[\w.-]+', src)
bad_hosts = [h for h in bad_hosts
             if not h.startswith(("ingress", "agent-registry",
                                  "approval-service", "localhost"))]
violations += bad_hosts
check("G10b", "static: no direct backend write path in gateway",
      not violations, f"violations={violations}")

# ----------------------------------------------------------------------
# G10c — quota breach: HTTP 429 + THROTTLED agent_call
# ----------------------------------------------------------------------

throttled = None
for i in range(35):  # default calls_per_minute=30
    st, r = gw("POST", "/v1/tools/reservations.lookup", ops_tok,
               {"task_id": f"g10c-{RUN}", "input": {}})
    if st == 429:
        throttled = st
        break
check("G10c", "quota breach returns HTTP 429", throttled == 429,
      f"last status={st} after {i+1} calls")
check("G10c", "agent_call records THROTTLED",
      call_fact("reservations.lookup", "THROTTLED") == "THROTTLED",
      f"fact={call_fact('reservations.lookup','THROTTLED')}")

# ----------------------------------------------------------------------
# G10d — tool list filtered by scope (REST + MCP)
# ----------------------------------------------------------------------

st, r = gw("GET", "/v1/tools", guest_tok)
guest_tools = {t["name"] for t in r.get("tools", [])}
check("G10d", "customer tool list lacks ops tools",
      st == 200
      and {"decide", "reservations.lookup",
           "reservations.create"} <= guest_tools
      and not ({"pms.room.assign", "bms.hvac.set",
                "housekeeping.task.add", "approvals.list",
                "crm.profile.read", "benefits.participant.read",
                "benefits.contribution.change"} & guest_tools),
      f"tools={sorted(guest_tools)}")

st, r = gw("GET", "/v1/tools", ops_tok)
ops_tools = {t["name"] for t in r.get("tools", [])}
check("G10d", "employee tool list includes pms + approvals",
      st == 200 and {"pms.room.assign", "approvals.list",
                     "approvals.decide", "bms.hvac.set",
                     "housekeeping.task.add",
                     "crm.profile.read"} <= ops_tools,
      f"tools={sorted(ops_tools)}")

# MCP streamable-HTTP smoke: initialize -> tools/list -> tools/call.
st, r = gw("POST", "/mcp", ops_tok,
           {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
ok_init = st == 200 and r.get("result", {}).get("serverInfo", {}) \
    .get("name") == "rtdp-agent-gateway"
st, r = gw("POST", "/mcp", guest_tok,
           {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
mcp_guest = {t["name"] for t in r.get("result", {}).get("tools", [])}
# guest token for tools/call — the ops quota was spent in G10c and a
# guest sees their own two rows.
st2, r2 = gw("POST", "/mcp", guest_tok,
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "reservations.lookup",
                         "arguments": {"task_id": f"mcp-{RUN}"}}})
payload = r2.get("result", {})
rows = 0
try:
    text = payload["content"][0]["text"]
    rows = len(json.loads(text).get("result", []))
except Exception:
    pass
check("G10d", "MCP initialize + scope-filtered tools/list + tools/call",
      ok_init
      and "pms.room.assign" not in mcp_guest
      and "reservations.lookup" in mcp_guest
      and rows == 2,
      f"init={ok_init} guest_tools={sorted(mcp_guest)} rows={rows}")

# ----------------------------------------------------------------------
# Unauthenticated / bad-chain callers are denied.
# ----------------------------------------------------------------------

st, _ = gw("GET", "/v1/tools")
check("G10", "no token -> 401", st == 401)
st, _ = gw("POST", "/v1/tools/reservations.lookup", "bogus.token.here",
           {"input": {}})
check("G10", "bogus token -> 401", st == 401)

print()
fails = [x for x in results if not x[2]]
print(f"G10: {len(results)-len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)
