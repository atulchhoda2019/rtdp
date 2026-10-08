// Synthetic read-only backends for the agent gateway. All data is
// fabricated for the AHS demo — no real guests, reservations, CRM
// records, or benefits participants.
//
// Field filtering is scope-driven: every row returns only the fields
// the caller's tool config allows (fields / fields_employee /
// fields_customer). The caller's principal id further narrows rows
// when scope_visibility maps its scope to "own".
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
)

// ---------------- synthetic datasets ----------------

type row = map[string]any

// Reservation rows: one org (`hotel-h`) property with guest
// reservations. `rate_code` and `notes` are employee-only fields —
// the G10a scope-filter check.
var reservations = []row{
	{"reservation_id": "res-9001", "guest_id": "guest-g",
		"room_type": "SUITE", "floor": 12, "status": "BOOKED",
		"rate_code": "VIP-GOLD", "nights": 3,
		"notes": "late arrival; prefers quiet floor"},
	{"reservation_id": "res-9002", "guest_id": "guest-k",
		"room_type": "STANDARD", "floor": 4, "status": "CHECKED_IN",
		"rate_code": "CORP-42", "nights": 2,
		"notes": "accessibility request filed"},
	{"reservation_id": "res-9003", "guest_id": "guest-g",
		"room_type": "STANDARD", "floor": 7, "status": "CANCELLED",
		"rate_code": "BAR", "nights": 1,
		"notes": "cancelled within free window"},
}

// CRM profiles keyed by guest id. `payment` and `history` are
// employee-only fields.
var crmProfiles = map[string]row{
	"guest-g": {"guest_id": "guest-g", "tier": "GOLD",
		"preferences": "high floor, foam pillows",
		"loyalty_no":  "G-88114",
		"payment":     "token_vault:tk-9x",
		"history":     "9 stays, 2 suite upgrades"},
	"guest-k": {"guest_id": "guest-k", "tier": "SILVER",
		"preferences": "ground floor, extra towels",
		"loyalty_no":  "K-55207",
		"payment":     "token_vault:tk-2f",
		"history":     "3 stays"},
}

// Benefits participants keyed by participant id. `phi_notes` is
// employee-only.
var benefitParticipants = map[string]row{
	"participant-p": {"participant_id": "participant-p",
		"plan_id": "hdhp-2026", "coverage_tier": "FAMILY",
		"hsa_balance": 4210.50, "hsa_contribution_pct": 7,
		"phi_notes": "synthetic clinical note — allergy flag"},
	"participant-q": {"participant_id": "participant-q",
		"plan_id": "ppo-2026", "coverage_tier": "SELF",
		"hsa_balance": 380.00, "hsa_contribution_pct": 0,
		"phi_notes": "synthetic clinical note — none"},
}

// ---------------- filtering ----------------

// filterFields keeps only the allow-listed keys, always preserving
// the row's id field so callers can correlate.
func filterFields(r row, fields []string, idKeys ...string) row {
	keep := map[string]bool{}
	for _, f := range fields {
		keep[f] = true
	}
	for _, id := range idKeys {
		keep[id] = true
	}
	out := row{}
	for k, v := range r {
		if keep[k] {
			out[k] = v
		}
	}
	return out
}

// pickFields selects the caller's field set: employee scopes get the
// employee set, customers the customer set, `fields` is the default.
func pickFields(t *Tool, c *caller) []string {
	if len(t.FieldsEmployee) > 0 || len(t.FieldsCustomer) > 0 {
		if allVisible(t, c) {
			return t.FieldsEmployee
		}
		return t.FieldsCustomer
	}
	return t.Fields
}

// allVisible reports whether the caller may see every row rather than
// only rows referencing them (own/customer = row-restricted).
func allVisible(t *Tool, c *caller) bool {
	for scope, vis := range t.ScopeVisibility {
		if vis == "all" && c.has(scope) {
			return true
		}
	}
	return false
}

// acknowledgedAssignments reads the action ledger for released
// ASSIGN_ROOM effects and maps reservation_id -> room_id.
func acknowledgedAssignments(ctx context.Context, tenant string) map[string]string {
	out := map[string]string{}
	rows, err := db.Query(ctx, `
		SELECT intent FROM action_execution
		WHERE tenant_id=$1 AND action_type='ASSIGN_ROOM'
		  AND state='ACKNOWLEDGED'`, tenant)
	if err != nil {
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var intent []byte
		if err := rows.Scan(&intent); err != nil {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal(intent, &m); err != nil {
			continue
		}
		rid, _ := m["reservation_id"].(string)
		room, _ := m["room_id"].(string)
		if rid != "" && room != "" {
			out[rid] = room
		}
	}
	return out
}

// ---------------- dispatch ----------------

// readBackend serves read tools from the synthetic datasets above and
// returns (rows, data-classes-read, error). Errors are DENIED-class —
// they become DENIED agent_call outcomes.
func readBackend(ctx context.Context, t *Tool, c *caller,
	in map[string]any) ([]row, []string, error) {
	fields := pickFields(t, c)
	switch t.Backend {
	case "reservations":
		resID, _ := in["reservation_id"].(string)
		guestID, _ := in["guest_id"].(string)
		out := []row{}
		for _, r := range reservations {
			if resID != "" && r["reservation_id"] != resID {
				continue
			}
			if guestID != "" && r["guest_id"] != guestID {
				continue
			}
			// Row restriction: a caller without an `all` scope sees
			// only rows that reference their own principal id.
			if !allVisible(t, c) && r["guest_id"] != c.principal {
				continue
			}
			out = append(out, filterFields(r, fields, "reservation_id"))
		}
		// World feedback: overlay acknowledged ASSIGN_ROOM effects from
		// the action ledger, so a released assignment is visible on the
		// next lookup — the ledger, not the fixture, is the truth.
		for rid, room := range acknowledgedAssignments(ctx, c.tenant) {
			for _, r := range out {
				if r["reservation_id"] == rid {
					r["room_id"], r["status"] = room, "ROOM_ASSIGNED"
				}
			}
		}
		return out, []string{t.DataClass}, nil

	case "crm":
		guestID, _ := in["guest_id"].(string)
		if guestID == "" {
			guestID = c.principal
		}
		if !allVisible(t, c) && guestID != c.principal {
			return nil, nil, fmt.Errorf(
				"scope limits profile reads to own record")
		}
		r, ok := crmProfiles[guestID]
		if !ok {
			return []row{}, []string{t.DataClass}, nil
		}
		return []row{filterFields(r, fields, "guest_id")},
			[]string{t.DataClass}, nil

	case "benefits":
		pid, _ := in["participant_id"].(string)
		if pid == "" {
			pid = c.principal
		}
		r, ok := benefitParticipants[pid]
		if !ok {
			return []row{}, []string{t.DataClass}, nil
		}
		return []row{filterFields(r, fields, "participant_id")},
			[]string{t.DataClass}, nil

	default:
		return nil, nil, errors.New("unknown backend " + t.Backend)
	}
}
