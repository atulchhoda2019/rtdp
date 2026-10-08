#!/usr/bin/env python3
"""RTDP demo agent — a real LLM driving the governed MCP gateway.

The point of the demo: the model is a *client* of the same governed door
every other caller uses. It mints a delegation token, sees only the tools
its scope exposes, and every call it makes lands as an agent_call fact —
scoped, quota'd, and (for mutations) routed through /v1/decide where
policy can hold it for a human.

    .venv/bin/python tools/demo/agent_client.py \
        --provider anthropic --persona ops --mode scenario

    ANTHROPIC_API_KEY / OPENAI_API_KEY  — read from env, never args.

Flags:
    --provider   anthropic | openai           (default anthropic)
    --model      provider model id            (defaults per provider)
    --persona    ops | guest | participant    (default ops)
    --mode       scenario | repl              (default scenario)
    --gateway    base URL                     (env RTDP_GATEWAY or the
                 public quick-tunnel default below)
"""

import argparse
import json
import os
import sys
import urllib.request
import uuid

DEFAULT_GATEWAY = (
    "https://timer-invitation-rebates-surveys.trycloudflare.com")

# Who the *model* is acting as. The principal is the delegating identity;
# the agent_ids pick which registered agent the chain runs through — the
# union of their allowed scopes is what tools/list will show.
PERSONAS = {
    "ops": {
        "principal_kind": "ORG", "principal_id": "hotel-h",
        "agent_ids": ["hotel-ops-dot"],
        "blurb": ("You are the hotel operations assistant for the hotel-h "
                  "property, acting through agent 'hotel-ops-dot'."),
    },
    "guest": {
        "principal_kind": "PERSON", "principal_id": "guest-g",
        "agent_ids": ["guest-agent-g"],
        "blurb": ("You are guest-g's personal travel assistant, acting "
                  "through agent 'guest-agent-g'."),
    },
    "participant": {
        "principal_kind": "PERSON", "principal_id": "participant-p",
        "agent_ids": ["benefits-agent-1"],
        "blurb": ("You are participant-p's benefits assistant, acting "
                  "through agent 'benefits-agent-1'."),
    },
}

SCENARIOS = {
    "ops": (
        "Do these steps, calling the right tool for each: (1) call "
        "reservations.lookup to confirm guest-g's reservation res-9001; "
        "(2) call crm.profile.read with guest_id guest-g; (3) call "
        "pms.room.assign with attributes reservation_id=res-9001, "
        "room_id=r-1204, preferred_floor=10. Report each result."
    ),
    "guest": (
        "Show me my hotel reservations, then try to read my full guest "
        "profile. If anything is refused, tell me what the refusal said."
    ),
    "participant": (
        "Read my benefits record and tell me what you can see. Then try "
        "to change my contribution election and report the outcome."
    ),
}

SYSTEM = """{blurb} You reach every backend through ONE governed MCP
gateway (RTDP) — there is no other path.

How the door works:
- tools/list shows ONLY what your delegation scopes allow. If a tool is
  not listed you cannot call it at all.
- tools/call arguments: {{"task_id": "<short-id>", "purpose": "<why>",
  ...tool args}}. Read tools take no args. Mutating tools accept an
  "attributes" object (e.g. reservation_id, room_id, preferred_floor).
- Mutating calls are not writes: they become decision requests evaluated
  by pinned policy. Possible outcomes: DECISION_APPROVE,
  DECISION_REVIEW, DECISION_PENDING_APPROVAL (a human must release it),
  DECISION_DECLINE_*.
- Denied calls return HTTP errors with an "error" string — report it
  verbatim.

Rules for you:
- ALWAYS act by calling tools — never describe an action as done unless
  a tools/call returned success for it. Narrating an assignment without
  calling pms.room.assign accomplishes nothing.
- Be brief and concrete. After each call, say what the door returned.
- If a call is held for approval or denied, explain WHY from the
  response and stop — do not retry to route around the gate.
- Do not make extra tool calls beyond what the task needs.
- Everything here is synthetic demo data."""

C_BOLD, C_DIM, C_CYAN, C_GREEN, C_YEL, C_RED, C_OFF = (
    "\033[1m", "\033[2m", "\033[36m", "\033[32m",
    "\033[33m", "\033[31m", "\033[0m")


def _post(base, path, body, token=None, timeout=30):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {"error": str(e)}


class Gateway:
    """Minimal MCP client — one authed JSON-RPC POST per method."""

    def __init__(self, base, token):
        self.base, self.token = base, token
        self._id = 0

    def rpc(self, method, params=None, show=True):
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        if show:
            print(f"{C_DIM}--> POST /mcp {json.dumps(body)[:200]}{C_OFF}")
        st, r = _post(self.base, "/mcp", body, self.token)
        if st != 200:
            return st, {"isError": True, "content": [
                {"type": "text",
                 "text": json.dumps(r or {"error": f"HTTP {st}"})}]}
        if show:
            print(f"{C_DIM}<-- {json.dumps(r)[:240]}{C_OFF}")
        return st, r.get("result", {})

    def call_tool(self, name, args):
        st, res = self.rpc("tools/call",
                           {"name": name, "arguments": args})
        text = ""
        try:
            text = res["content"][0]["text"]
        except Exception:
            pass
        return res.get("isError", st != 200), text or json.dumps(res)


def mint_session(base, persona):
    st, r = _post(base, "/v1/session", {
        "principal_kind": persona["principal_kind"],
        "principal_id": persona["principal_id"],
        "tenant_id": "tenant_a",
        "agent_ids": persona["agent_ids"]})
    if st != 200:
        sys.exit(f"session mint failed {st}: {r}")
    return r["delegation_token"]


# ---------------------------------------------------------------------
# Provider loops — same wire, two brains.
# ---------------------------------------------------------------------

def run_anthropic(model, system, tools, first_msg, do_call, repl):
    import anthropic
    client = anthropic.Anthropic()
    api_tools = [{"name": t["name"], "description": t["description"],
                  "input_schema": t.get("inputSchema",
                                        {"type": "object"})}
                 for t in tools]
    msgs = [{"role": "user", "content": first_msg}]
    while True:
        resp = client.messages.create(
            model=model, max_tokens=1024, system=system,
            tools=api_tools, messages=msgs)
        msgs.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for blk in resp.content:
            if blk.type == "tool_use":
                err, out = do_call(blk.name, blk.input)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": blk.id,
                    "content": out, "is_error": err})
            elif blk.type == "text" and blk.text.strip():
                print(f"\n{C_GREEN}{blk.text}{C_OFF}\n")
        if not tool_results:
            break
        msgs.append({"role": "user", "content": tool_results})
    if repl:
        repl_loop(msgs, lambda m: run_anthropic_msgs(
            client, model, system, api_tools, msgs, do_call))
    return msgs


def run_anthropic_msgs(client, model, system, api_tools, msgs, do_call):
    while True:
        resp = client.messages.create(
            model=model, max_tokens=1024, system=system,
            tools=api_tools, messages=msgs)
        msgs.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for blk in resp.content:
            if blk.type == "tool_use":
                err, out = do_call(blk.name, blk.input)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": blk.id,
                    "content": out, "is_error": err})
            elif blk.type == "text" and blk.text.strip():
                print(f"\n{C_GREEN}{blk.text}{C_OFF}\n")
        if not tool_results:
            return
        msgs.append({"role": "user", "content": tool_results})


def run_openai(model, system, tools, first_msg, do_call, repl,
               base_url=None):
    import openai
    # base_url lets the same loop run against any OpenAI-compatible
    # endpoint — e.g. the in-cluster Ollama (port-forward :11434/v1)
    # when provider credits aren't available for the demo.
    client = openai.OpenAI(base_url=base_url,
                           api_key=os.environ.get("OPENAI_API_KEY",
                                                  "ollama"))
    api_tools = [{"type": "function", "function": {
        "name": t["name"].replace(".", "_"),
        "description": t["description"],
        "parameters": t.get("inputSchema", {"type": "object"})}}
        for t in tools]
    real_names = {t["name"].replace(".", "_"): t["name"] for t in tools}
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": first_msg}]
    repeats = {}

    def step():
        resp = client.chat.completions.create(
            model=model, messages=msgs, tools=api_tools,
            tool_choice="auto", temperature=0.2, max_tokens=1024)
        m = resp.choices[0].message
        msgs.append(m)
        if m.content:
            print(f"\n{C_GREEN}{m.content}{C_OFF}\n")
        elif not m.tool_calls:
            print(f"{C_DIM}(model returned an empty response){C_OFF}")
        for tc in (m.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
                if isinstance(args, str):  # double-encoded JSON
                    args = json.loads(args)
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            name = real_names.get(
                tc.function.name,
                tc.function.name.replace("_", "."))
            sig = name + json.dumps(args, sort_keys=True)
            repeats[sig] = repeats.get(sig, 0) + 1
            if repeats[sig] > 2:
                print(f"{C_DIM}(identical call repeated "
                      f"— stopping loop){C_OFF}")
                return False
            err, out = do_call(name, args)
            msgs.append({"role": "tool", "tool_call_id": tc.id,
                         "content": out})
        return bool(m.tool_calls)

    for _ in range(12):
        if not step():
            break
    if repl:
        repl_loop(msgs, lambda m: step_loop(step))
    return msgs


def step_loop(step, max_steps=12):
    for _ in range(max_steps):
        if not step():
            return


def repl_loop(msgs, advance):
    print(f"{C_BOLD}— REPL: type a task (empty line exits) —{C_OFF}")
    while True:
        try:
            line = input(f"{C_CYAN}you> {C_OFF}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            return
        msgs.append({"role": "user", "content": line})
        try:
            advance(msgs)
        except Exception as e:
            print(f"{C_RED}provider error: {e}{C_OFF}")
            return


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=
                                 argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", choices=["anthropic", "openai"],
                    default="anthropic")
    ap.add_argument("--model", default=None)
    ap.add_argument("--persona", choices=sorted(PERSONAS), default="ops")
    ap.add_argument("--mode", choices=["scenario", "repl"],
                    default="scenario")
    ap.add_argument("--gateway", default=os.environ.get(
        "RTDP_GATEWAY", DEFAULT_GATEWAY))
    ap.add_argument("--base-url", default=None,
                    help="OpenAI-compatible endpoint override "
                         "(e.g. http://localhost:11434/v1 for Ollama)")
    a = ap.parse_args()

    persona = PERSONAS[a.persona]
    model = a.model or ("claude-sonnet-4-5" if a.provider == "anthropic"
                        else "qwen2.5:1.5b" if a.base_url else "gpt-4o")
    key_var = ("ANTHROPIC_API_KEY" if a.provider == "anthropic"
               else "OPENAI_API_KEY")
    if not os.environ.get(key_var) and not a.base_url:
        sys.exit(f"{key_var} not set — export it first (keys stay local)")

    print(f"{C_BOLD}agent-client{C_OFF}  provider={a.provider} "
          f"model={model} persona={a.persona}")
    print(f"{C_DIM}gateway={a.gateway}{C_OFF}")

    token = mint_session(a.gateway, persona)
    gw = Gateway(a.gateway, token)

    st, init = gw.rpc("initialize", show=False)
    print(f"{C_DIM}mcp initialize: {init.get('serverInfo')}{C_OFF}")
    st, tl = gw.rpc("tools/list", show=False)
    tools = tl.get("tools", [])
    print(f"{C_YEL}tools/list ({len(tools)} visible under this "
          f"delegation):{C_OFF}")
    for t in tools:
        print(f"  {C_CYAN}{t['name']:<32}{C_OFF}{C_DIM}"
              f"{t.get('description','')[:60]}{C_OFF}")

    system = SYSTEM.format(blurb=persona["blurb"])

    def do_call(name, args):
        args = dict(args or {})
        args.setdefault("task_id", "llm-" + uuid.uuid4().hex[:8])
        err, out = gw.call_tool(name, args)
        short = out if len(out) < 300 else out[:300] + "…"
        col = C_RED if err else C_GREEN
        print(f"{C_DIM}  tools/call {name} {json.dumps(args)[:160]}{C_OFF}")
        print(f"{col}  -> {short}{C_OFF}")
        return err, out

    if a.mode == "scenario":
        task = SCENARIOS[a.persona]
        print(f"\n{C_BOLD}task>{C_OFF} {task}\n")
        first = task
        repl = False
    else:
        first = ("Introduce yourself in one sentence and say what you "
                 "can do here.")
        repl = True

    if a.provider == "anthropic":
        run_anthropic(model, system, tools, first, do_call, repl)
    else:
        run_openai(model, system, tools, first, do_call, repl,
                   base_url=a.base_url)


if __name__ == "__main__":
    main()
