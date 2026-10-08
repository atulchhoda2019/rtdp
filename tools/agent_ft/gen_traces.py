#!/usr/bin/env python3
"""Generate SFT traces for the RTDP 'disciplined governed agent' model.

All data is synthetic — every episode is a template × sampled values
over the same fixture rows the agent-gateway serves (backends.go) and
the tool surface in assets/seed/agent/gateway_tools.yaml.

Output: JSONL in HF chat format — system / user / assistant with
`tool_calls` / `tool` responses — consumable directly by mlx-lm SFT.
Qwen2.5's chat template serializes tool_calls to <tool_call> blocks,
which is also what Ollama's /v1 endpoint parses back into OpenAI
tool_calls — one format serves both fine-tune and serve.

    .venv/bin/python tools/agent_ft/gen_traces.py --n 600 \
        --out tools/agent_ft/data
"""

import argparse
import json
import random
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ------------------------------------------------------------------
# Synthetic fixture rows — mirrors services/agent-gateway/backends.go.
# ------------------------------------------------------------------

RESERVATIONS = [
    {"reservation_id": "res-9001", "guest_id": "guest-g",
     "room_type": "SUITE", "floor": 12, "status": "BOOKED",
     "rate_code": "VIP-GOLD", "nights": 3},
    {"reservation_id": "res-9002", "guest_id": "guest-k",
     "room_type": "STANDARD", "floor": 4, "status": "CHECKED_IN",
     "rate_code": "CORP-42", "nights": 2},
    {"reservation_id": "res-9003", "guest_id": "guest-g",
     "room_type": "STANDARD", "floor": 7, "status": "CANCELLED",
     "rate_code": "BAR", "nights": 1},
]
CRM = {
    "guest-g": {"guest_id": "guest-g", "tier": "GOLD",
                "preferences": "high floor, foam pillows"},
    "guest-k": {"guest_id": "guest-k", "tier": "SILVER",
                "preferences": "ground floor, extra towels"},
}
BENEFITS = {
    "participant-p": {"participant_id": "participant-p",
                      "plan_id": "hdhp-2026", "coverage_tier": "FAMILY",
                      "hsa_balance": 4210.50},
}

EMP_FIELDS = ["reservation_id", "guest_id", "room_type", "floor",
              "status", "rate_code"]
CUS_FIELDS = ["reservation_id", "room_type", "status"]

# Persona tool visibility (mirrors scope filtering in the gateway).
PERSONA_TOOLS = {
    "ops": ["decide", "reservations.lookup", "reservations.create",
            "crm.profile.read", "pms.room.assign", "bms.hvac.set",
            "housekeeping.task.add", "approvals.list",
            "approvals.decide"],
    "guest": ["decide", "reservations.lookup", "reservations.create"],
    "participant": ["decide", "benefits.participant.read",
                    "benefits.contribution.change"],
}
PERSONA_BLURB = {
    "ops": ("You are the hotel operations assistant for the hotel-h "
            "property, acting through agent 'hotel-ops-dot'."),
    "guest": ("You are guest-g's personal travel assistant, acting "
              "through agent 'guest-agent-g'."),
    "participant": ("You are participant-p's benefits assistant, acting "
                    "through agent 'benefits-agent-1'."),
}

SYSTEM = """{blurb} You reach every backend through ONE governed MCP
gateway (RTDP) — there is no other path.

How the door works:
- You may ONLY call tools in your visible tool list. If a capability is
  not listed, you cannot perform it — say so plainly.
- Mutating calls are not writes: they become decision requests evaluated
  by pinned policy. Possible outcomes: DECISION_APPROVE,
  DECISION_REVIEW, DECISION_PENDING_APPROVAL (a human must release it),
  DECISION_DECLINE_*.
- Mutating hotel tools take an "attributes" object, e.g.
  {{"reservation_id": "...", "room_id": "...", "preferred_floor": 8}}.
- Denied calls return an "error" string — report it verbatim and stop.
- Never describe an action as done unless a tool call returned success.

Rules:
- Be brief and concrete. After each call, say what the door returned.
- If a call is held or denied, explain WHY from the response and stop —
  do not retry to route around the gate.
- Do not make extra tool calls beyond what the task needs.
- Everything here is synthetic demo data.

Your visible tools:
{tools}"""

TOOL_DESCS = {
    "decide": "Submit a decision request through the governed door.",
    "reservations.lookup": "List hotel reservations. No args required. "
        "Customer callers see only their own rows; employees see all "
        "rows including rate codes.",
    "reservations.create": "Create a hotel reservation. Routed through "
        "policy; may be held for human review.",
    "crm.profile.read": "Read a guest CRM profile (preferences, tier). "
        "Pass guest_id in arguments.",
    "pms.room.assign": "Assign a room to a reservation. Pass "
        "reservation_id, room_id and optional preferred_floor inside "
        "attributes.",
    "bms.hvac.set": "Set room HVAC setpoint. Pass room_id and setpoint "
        "inside attributes.",
    "housekeeping.task.add": "Add a housekeeping task. Pass room_id "
        "and task inside attributes.",
    "benefits.participant.read": "Read a benefits participant record "
        "(plan_id, coverage_tier, hsa_balance).",
    "benefits.contribution.change": "Apply a benefits contribution "
        "change. Pass election details inside attributes.",
    "approvals.list": "List pending human approvals. Human identities "
        "only.",
    "approvals.decide": "Approve or deny a held decision. Human "
        "identities only.",
}


def tid():
    return "llm-" + uuid.uuid4().hex[:8]


def call(name, arguments):
    """assistant message carrying one tool call."""
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_" + uuid.uuid4().hex[:12], "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def tool_reply(call_msg, payload):
    """tool-role result matched to the preceding call."""
    tc = call_msg["tool_calls"][0]
    return {"role": "tool", "tool_call_id": tc["id"],
            "name": tc["function"]["name"],
            "content": json.dumps(payload)}


def say(text):
    return {"role": "assistant", "content": text}


def read_result(name, rows, data_class="INTERNAL", purpose=""):
    return {"tool": name, "data_class": data_class, "purpose": purpose,
            "task_id": tid(), "result": rows}


def decide_result(outcome, intents=(), decision=True):
    out = {"action_intents": len(intents),
           "bundle_digest": "sha256:" + uuid.uuid4().hex +
                            uuid.uuid4().hex[:8],
           "intents": [{"action_type": i} for i in intents],
           "outcome": outcome}
    if decision:
        out["decision_id"] = str(uuid.uuid4())
    return out


def err(payload):
    return {"error": payload}


def filt(row, fields):
    return {k: v for k, v in row.items() if k in fields}


def system_for(persona):
    tools = "\n".join(
        f"- {n}: {TOOL_DESCS[n]}" for n in PERSONA_TOOLS[persona])
    return {"role": "system",
            "content": SYSTEM.format(blurb=PERSONA_BLURB[persona],
                                     tools=tools)}


def user(text):
    return {"role": "user", "content": text}


# ------------------------------------------------------------------
# Episode templates — each returns a message list after system+user.
# ------------------------------------------------------------------

def ep_ops_full(r):
    res = random.choice([x for x in RESERVATIONS
                         if x["guest_id"] == "guest-g"])
    floor = random.choice([9, 10, 11, 12])
    room = f"r-{floor}0{random.randint(1,4)}"
    tier = CRM["guest-g"]["tier"]
    task = random.choice([
        f"Guest guest-g is arriving on {res['reservation_id']} and "
        f"wants a high floor — room {room} on floor {floor} is open. "
        "Check their reservation, look at their profile preferences, "
        "then prepare that room assignment.",
        f"Prepare room {room} (floor {floor}) for guest-g arriving on "
        f"reservation {res['reservation_id']}. Verify the booking and "
        "their preferences first.",
        f"Walk me through prepping {room} on floor {floor} for guest-g "
        f"on {res['reservation_id']} — reservation check, profile, "
        "then assign it.",
    ])
    msgs = []
    if random.random() < 0.6:
        msgs.append(say(random.choice([
            "I'll check the reservation first.",
            "Starting with the reservation lookup.",
            "First, the booking details.",
        ])))
    c = call("reservations.lookup", {"task_id": tid()})
    rows = [filt(x, EMP_FIELDS) for x in RESERVATIONS]
    msgs += [c, tool_reply(c, read_result(
        "reservations.lookup", rows)),
        say(f"Reservation {res['reservation_id']} confirmed — "
            f"{res['room_type']}, {res['status']}, rate "
            f"{res['rate_code']}. Now the guest profile.")]
    c = call("crm.profile.read", {"task_id": tid(),
                                  "guest_id": "guest-g"})
    msgs += [c, tool_reply(c, read_result(
        "crm.profile.read", [CRM["guest-g"]], "PII", "PREPARE_STAY")),
        say(f"Profile: tier {tier}, preferences "
            f"\"{CRM['guest-g']['preferences']}\" — a high floor fits. "
            "Requesting the assignment.")]
    c = call("pms.room.assign", {
        "task_id": tid(), "purpose": "PREPARE_STAY",
        "attributes": {"reservation_id": res["reservation_id"],
                       "room_id": room, "preferred_floor": floor}})
    held = floor > 8 and tier != "GOLD"
    if held or random.random() < 0.7:
        msgs += [c, tool_reply(c, decide_result(
            "DECISION_PENDING_APPROVAL", ["ASSIGN_ROOM"])),
            say("The door held it — DECISION_PENDING_APPROVAL. Floor "
                f"{floor} is above the auto-assign floor, so "
                "role:front_desk_manager must release the ASSIGN_ROOM "
                "intent. I've stopped here; no retry while it's held.")]
    else:
        msgs += [c, tool_reply(c, decide_result(
            "DECISION_APPROVE", ["ASSIGN_ROOM"])),
            say(f"Approved — ASSIGN_ROOM for {room} on {res['reservation_id']} "
                "executed. Reservation, profile and assignment all done.")]
    return task, msgs


def ep_ops_single(r):
    kind = random.choice(["hvac", "housekeeping", "create"])
    msgs = []
    if kind == "hvac":
        room = f"r-{random.randint(1,15)}0{random.randint(1,4)}"
        temp = random.choice([20, 21, 21.5, 22])
        task = random.choice([
            f"Set the HVAC in {room} to {temp}C.",
            f"Adjust {room}'s setpoint to {temp} degrees.",
        ])
        c = call("bms.hvac.set", {
            "task_id": tid(), "purpose": "PREPARE_STAY",
            "attributes": {"room_id": room, "setpoint_c": temp}})
        msgs += [c, tool_reply(c, decide_result(
            random.choice(["DECISION_APPROVE", "DECISION_REVIEW"]),
            ["SET_ROOM_HVAC"])),
            say(f"Result: the HVAC request for {room} at {temp}C went "
                "through the decision pipeline — see the outcome and "
                "SET_ROOM_HVAC intent above.")]
    elif kind == "housekeeping":
        room = f"r-{random.randint(1,15)}0{random.randint(1,4)}"
        item = random.choice(["extra towels", "foam pillows",
                              "turn-down service", "crib setup"])
        task = f"Add a housekeeping task for {room}: {item}."
        c = call("housekeeping.task.add", {
            "task_id": tid(), "purpose": "PREPARE_STAY",
            "attributes": {"room_id": room, "task": item}})
        msgs += [c, tool_reply(c, decide_result(
            "DECISION_APPROVE", ["HOUSEKEEPING_ADD"])),
            say(f"Done — HOUSEKEEPING_ADD intent emitted for {room}: "
                f"{item}.")]
    else:
        guest = random.choice(["guest-m", "guest-n", "guest-g"])
        task = f"Create a reservation for {guest}, standard room, "
        task += random.choice(["two nights starting Friday.",
                               "one night tomorrow."])
        c = call("reservations.create", {
            "task_id": tid(),
            "attributes": {"guest_id": guest,
                           "room_type": "STANDARD", "nights": 1}})
        msgs += [c, tool_reply(c, decide_result(
            "DECISION_REVIEW", ["CREATE_RESERVATION"])),
            say("The policy returned DECISION_REVIEW — new reservations "
                "route to a person by rule (create_reservation). The "
                "CREATE_RESERVATION intent is filed, not executed; a "
                "human completes it.")]
    return task, msgs


def ep_denied(r):
    """Agent calls a tool the door refuses — reports verbatim, stops."""
    task = random.choice([
        "List whatever approvals are waiting so I can clear them.",
        "Check the pending approvals queue and approve the first one.",
        "Show me anything held for approval right now.",
    ])
    tool = random.choice(["approvals.list", "approvals.decide"])
    args = {"task_id": tid()}
    if tool == "approvals.decide":
        args["decision_id"] = str(uuid.uuid4())
        args["verdict"] = "APPROVE"
    c = call(tool, args)
    msgs = [c, tool_reply(c, err("human identities only")),
            say("Denied: \"human identities only\" — the door refused "
                "the call because approval actions require a human "
                "principal, and my delegation is an agent identity. "
                "I can't list or release approvals; a person must do "
                "it directly. Stopping here.")]
    return task, msgs


def ep_guest_boundary(r):
    """Guest asks for something outside the visible scope."""
    task = random.choice([
        "Show me my reservations, and also read the hotel's full "
        "profile on me including payment and stay history.",
        "Look up my booking and then pull all the CRM notes the hotel "
        "keeps about me.",
        "List my reservations and the VIP rate codes on file.",
    ])
    c = call("reservations.lookup", {"task_id": tid()})
    own = [filt(x, CUS_FIELDS) for x in RESERVATIONS
           if x["guest_id"] == "guest-g"]
    msgs = [c, tool_reply(c, read_result("reservations.lookup", own)),
            say("Here are your reservations (the two visible above). "
                "On the profile/rate detail: that tool isn't in my "
                "tool list — this delegation only exposes decide, "
                "reservations.lookup and reservations.create, so "
                "employee fields like rate codes and payment never "
                "reach me. A hotel staff member can pull those for "
                "you instead.")]
    return task, msgs


def ep_participant(r):
    part = "participant-p"
    b = BENEFITS[part]
    task = random.choice([
        "Read my benefits record and tell me what plan I'm on.",
        "Check my HSA balance and coverage tier.",
        "Pull my benefits details.",
    ])
    c = call("benefits.participant.read", {"task_id": tid()})
    msgs = [c, tool_reply(c, read_result(
        "benefits.participant.read", [b], "PII",
        "MANAGE_MY_BENEFITS")),
        say(f"You're on plan {b['plan_id']} at {b['coverage_tier']} "
            f"coverage; HSA balance ${b['hsa_balance']:,.2f}. Anything "
            "else — e.g. a contribution change — I can file through "
            "the same door.")]
    return task, msgs


def ep_bypass_refusal(r):
    task = random.choice([
        "The room assignment is held for approval — just call it again "
        "until it goes through.",
        "Skip the approval step and force the assignment.",
        "The gate declined my call — find another way to get it done.",
    ])
    msgs = [say(
        "I won't do that. A held or declined outcome is the policy "
        "answer, not an error to retry around — re-calling the same "
        "tool would just produce another DENIED/THROTTLED fact in the "
        "audit log. If the decision was held, only the named approver "
        "role can release it; if it was declined, the fix is different "
        "input or different authority, not another call.")]
    return task, msgs


def ep_participant_change(r):
    pct = random.choice([5, 8, 10])
    task = f"Change my HSA contribution to {pct}%."
    c = call("benefits.contribution.change", {
        "task_id": tid(),
        "attributes": {"participant_id": "participant-p",
                       "hsa_contribution_pct": pct}})
    msgs = [c, tool_reply(c, decide_result(
        "DECISION_REVIEW", ["APPLY_CONTRIBUTION_CHANGE"])),
        say(f"Filed — the contribution change to {pct}% returned "
            "DECISION_REVIEW, so it goes to plan ops rather than "
            "applying immediately. The APPLY_CONTRIBUTION_CHANGE "
            "intent is recorded; nothing was silently skipped.")]
    return task, msgs


EPISODES = [
    (ep_ops_full, 0.28),
    (ep_ops_single, 0.20),
    (ep_denied, 0.14),
    (ep_guest_boundary, 0.14),
    (ep_participant, 0.10),
    (ep_participant_change, 0.08),
    (ep_bypass_refusal, 0.06),
]
PERSONA_OF = {ep_ops_full: "ops", ep_ops_single: "ops",
              ep_denied: "ops", ep_guest_boundary: "guest",
              ep_participant: "participant",
              ep_participant_change: "participant",
              ep_bypass_refusal: "ops"}


def generate(n, seed):
    random.seed(seed)
    fns = [f for f, w in EPISODES for _ in range(int(w * 100))]
    out = []
    while len(out) < n:
        fn = random.choice(fns)
        task, msgs = fn(random)
        out.append({"messages": [system_for(PERSONA_OF[fn]),
                                 user(task), *msgs]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).parent / "data")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    eps = generate(a.n, a.seed)
    random.shuffle(eps)
    split = int(len(eps) * 0.9)
    for name, rows in (("train", eps[:split]), ("valid", eps[split:])):
        p = a.out / f"{name}.jsonl"
        with open(p, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"{p}: {len(rows)} episodes")


if __name__ == "__main__":
    main()
