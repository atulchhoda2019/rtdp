// Approval Service: adjudicates held action intents (ADR-014). Consumes
// ActionCommands with status AWAITING_APPROVAL, exposes POST
// /v1/approvals/{decision_id} for human verdicts, releases READY /
// CANCELLED / EXPIRED commands back onto rtdp.action.commands.v1, and
// writes ApprovalEvent ledger facts to rtdp.approval.events.v1.
//
// Synthetic demo only — approvers are role strings from the pinned
// action policy, there is no SSO. Self-approval is barred against the
// delegation chain's identities carried on the command.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/twmb/franz-go/pkg/kgo"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/kafkax"
)

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func errJSON(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

var db *pgxpool.Pool
var pub *kgo.Client

// consumer: project AWAITING_APPROVAL commands into approval_held.
func consume(ctx context.Context, cl *kgo.Client) {
	for {
		fetches := cl.PollFetches(ctx)
		if fetches.IsClientClosed() {
			return
		}
		fetches.EachRecord(func(rec *kgo.Record) {
			var cmd rtdpv1.ActionCommand
			if err := proto.Unmarshal(rec.Value, &cmd); err != nil {
				return
			}
			if cmd.Status != rtdpv1.IntentStatus_INTENT_AWAITING_APPROVAL {
				return
			}
			breach := cmd.SlaBreachAction
			if breach == "" {
				breach = "EXPIRE"
			}
			// nil proto slices must not reach NOT NULL text[] columns.
			reqs, apps, escs := cmd.RequesterIdentities, cmd.Approvers,
				cmd.EscalationApprovers
			if reqs == nil {
				reqs = []string{}
			}
			if apps == nil {
				apps = []string{}
			}
			if escs == nil {
				escs = []string{}
			}
			_, err := db.Exec(ctx, `
				INSERT INTO approval_held
				  (tenant_id, decision_id, idempotency_key, environment,
				   action_type, decision_generation, command,
				   requester_identities, approvers, escalation_approvers,
				   sla_breach_action, approval_due_at, state)
				VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'HELD')
				ON CONFLICT (tenant_id, decision_id, idempotency_key)
				DO NOTHING`,
				cmd.TenantId, cmd.DecisionId, cmd.IdempotencyKey,
				cmd.Environment, cmd.ActionType, cmd.DecisionGeneration,
				rec.Value, reqs, apps, escs, breach,
				ts(cmd.ApprovalDueAt))
			if err != nil {
				log.Printf("approval_held insert: %v", err)
			}
		})
	}
}

func ts(t *timestamppb.Timestamp) *time.Time {
	if t == nil {
		return nil
	}
	v := t.AsTime()
	return &v
}

func republish(ctx context.Context, raw []byte,
	status rtdpv1.IntentStatus) error {
	var cmd rtdpv1.ActionCommand
	if err := proto.Unmarshal(raw, &cmd); err != nil {
		return err
	}
	cmd.Status = status
	cmd.CommandId = uuid.NewString()
	b, err := proto.Marshal(&cmd)
	if err != nil {
		return err
	}
	return pub.ProduceSync(ctx, &kgo.Record{
		Topic: kafkax.TopicActionCommands,
		Key:   []byte(cmd.IdempotencyKey), Value: b}).FirstErr()
}

func ledgerEvent(ctx context.Context, tenant, decisionID, verdict,
	actor, note string, keys []string) {
	ev := &rtdpv1.ApprovalEvent{
		EventId: uuid.NewString(), TenantId: tenant,
		DecisionId: decisionID, Verdict: verdict,
		ActorIdentity: actor, Note: note, IntentKeys: keys,
		At: timestamppb.Now(),
	}
	b, _ := proto.Marshal(ev)
	_ = pub.ProduceSync(ctx, &kgo.Record{
		Topic: kafkax.TopicApprovalEvents,
		Key:   []byte(tenant + ":" + decisionID), Value: b}).FirstErr()
	_, err := db.Exec(ctx, `
		INSERT INTO approval_event
		  (event_id, tenant_id, decision_id, verdict, actor_identity,
		   note, intent_keys)
		VALUES ($1,$2,$3,$4,$5,$6,$7)`,
		ev.EventId, tenant, decisionID, verdict, actor, note, keys)
	if err != nil {
		log.Printf("approval_event insert: %v", err)
	}
}

var _ = fmt.Sprintf // keep fmt for future log formatting

// approverOK matches an approver entry against the submitted identity.
// Entries are role strings like "role:plan_admin"; an identity satisfies
// it as an exact match or as a bare name carrying that role.
func approverOK(entry, identity string) bool {
	if entry == identity {
		return true
	}
	if strings.HasPrefix(entry, "role:") &&
		strings.TrimPrefix(entry, "role:") == identity {
		return true
	}
	return false
}

// SLA sweeper: rows whose approval_due_at passed are escalated once
// (re-routed to escalation_approvers) or expired — each a ledger fact.
func sweep(ctx context.Context) {
	tick := time.NewTicker(2 * time.Second)
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		rows, err := db.Query(ctx, `
			SELECT tenant_id, decision_id, idempotency_key, command,
			       sla_breach_action, escalated, environment
			FROM approval_held
			WHERE state IN ('HELD','ESCALATED')
			  AND approval_due_at IS NOT NULL
			  AND approval_due_at < now()`)
		if err != nil {
			continue
		}
		type held struct {
			tenant, decision, key, breach string
			escalated                     bool
			cmd                           []byte
		}
		var todo []held
		var env string
		for rows.Next() {
			var h held
			if err := rows.Scan(&h.tenant, &h.decision, &h.key, &h.cmd,
				&h.breach, &h.escalated, &env); err == nil {
				todo = append(todo, h)
			}
		}
		rows.Close()
		for _, h := range todo {
			if h.breach == "ESCALATE" && !h.escalated {
				tag, err := db.Exec(ctx, `
					UPDATE approval_held
					SET state='ESCALATED', escalated=true,
					    approval_due_at = now() + interval '24 hours',
					    updated_at=now()
					WHERE tenant_id=$1 AND decision_id=$2
					  AND idempotency_key=$3
					  AND state IN ('HELD','ESCALATED')`,
					h.tenant, h.decision, h.key)
				if err == nil && tag.RowsAffected() > 0 {
					ledgerEvent(ctx, h.tenant, h.decision, "ESCALATED",
						"", "approval SLA breached", []string{h.key})
				}
				continue
			}
			// EXPIRE (or a second breach after escalation).
			if err := republish(ctx, h.cmd,
				rtdpv1.IntentStatus_INTENT_EXPIRED); err != nil {
				log.Printf("expire republish: %v", err)
				continue
			}
			_, _ = db.Exec(ctx, `
				UPDATE approval_held SET state='EXPIRED', updated_at=now()
				WHERE tenant_id=$1 AND decision_id=$2
				  AND idempotency_key=$3`,
				h.tenant, h.decision, h.key)
			ledgerEvent(ctx, h.tenant, h.decision, "EXPIRED",
				"", "approval SLA breached", []string{h.key})
		}
	}
}

func main() {
	ctx := context.Background()
	var err error
	db, err = pgxpool.New(ctx, envOr("RTDP_POSTGRES_DSN",
		"postgres://rtdp:rtdp@localhost:5432/rtdp?sslmode=disable"))
	if err != nil {
		log.Fatalf("postgres: %v", err)
	}
	pub, err = kafkax.NewProducer()
	if err != nil {
		log.Fatalf("producer: %v", err)
	}
	consumer, err := kafkax.NewReader("rtdp-approval-service",
		kafkax.TopicActionCommands)
	if err != nil {
		log.Fatalf("consumer: %v", err)
	}
	go consume(ctx, consumer)
	go sweep(ctx)

	mux := http.NewServeMux()

	// POST /v1/approvals/{decision_id} {approver_identity, verdict, note}
	mux.HandleFunc("POST /v1/approvals/{decision_id}",
		func(w http.ResponseWriter, r *http.Request) {
			decisionID := r.PathValue("decision_id")
			var body struct {
				Approver string `json:"approver_identity"`
				Verdict  string `json:"verdict"`
				Note     string `json:"note"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil ||
				body.Approver == "" ||
				(body.Verdict != "APPROVE" && body.Verdict != "REJECT") {
				errJSON(w, 400, "approver_identity + verdict APPROVE|REJECT required")
				return
			}
			rows, err := db.Query(r.Context(), `
				SELECT tenant_id, idempotency_key, command,
				       requester_identities, approvers,
				       escalation_approvers, escalated, state
				FROM approval_held
				WHERE decision_id=$1
				  AND state IN ('HELD','ESCALATED')`, decisionID)
			if err != nil {
				errJSON(w, 500, err.Error())
				return
			}
			type row struct {
				tenant, key, state string
				escalated          bool
				cmd                []byte
				requesters         []string
				eligible           []string
			}
			var held []row
			for rows.Next() {
				var rr row
				var apps, escs []string
				if err := rows.Scan(&rr.tenant, &rr.key, &rr.cmd,
					&rr.requesters, &apps, &escs,
					&rr.escalated, &rr.state); err != nil {
					continue
				}
				if rr.escalated {
					rr.eligible = escs
				} else {
					rr.eligible = apps
				}
				held = append(held, rr)
			}
			rows.Close()
			if len(held) == 0 {
				errJSON(w, 404, "no held intents for decision")
				return
			}
			// Self-approval is barred (ADR-014): the requesting agent —
			// or the principal — may never approve its own request.
			for _, h := range held {
				if slices.Contains(h.requesters, body.Approver) {
					errJSON(w, 403, "requester cannot approve its own request")
					return
				}
			}
			eligible := false
			for _, h := range held {
				for _, e := range h.eligible {
					if approverOK(e, body.Approver) {
						eligible = true
					}
				}
			}
			if !eligible {
				errJSON(w, 403, "approver not listed for this decision")
				return
			}
			var keys []string
			var tenant string
			target := rtdpv1.IntentStatus_INTENT_READY
			to := "RELEASED"
			if body.Verdict == "REJECT" {
				target = rtdpv1.IntentStatus_INTENT_CANCELLED
				to = "REJECTED"
			}
			for _, h := range held {
				if err := republish(r.Context(), h.cmd, target); err != nil {
					errJSON(w, 502, "release: "+err.Error())
					return
				}
				tenant = h.tenant
				keys = append(keys, h.key)
			}
			_, _ = db.Exec(r.Context(), `
				UPDATE approval_held SET state=$2, updated_at=now()
				WHERE decision_id=$1
				  AND state IN ('HELD','ESCALATED')`,
				decisionID, to)
			ledgerEvent(r.Context(), tenant, decisionID,
				body.Verdict+"D", body.Approver, body.Note, keys)
			writeJSON(w, 200, map[string]any{
				"decision_id": decisionID, "verdict": to,
				"intents": keys})
		})

	// GET /v1/approvals/pending?tenant_id=
	mux.HandleFunc("GET /v1/approvals/pending",
		func(w http.ResponseWriter, r *http.Request) {
			tenant := r.URL.Query().Get("tenant_id")
			rows, err := db.Query(r.Context(), `
				SELECT decision_id, idempotency_key, action_type, state,
				       approvers, escalation_approvers, escalated,
				       approval_due_at
				FROM approval_held
				WHERE ($1='' OR tenant_id=$1)
				  AND state IN ('HELD','ESCALATED')
				ORDER BY created_at`, tenant)
			if err != nil {
				errJSON(w, 500, err.Error())
				return
			}
			var out []map[string]any
			for rows.Next() {
				var did, key, at, state string
				var apps, escs []string
				var esc bool
				var due *time.Time
				if err := rows.Scan(&did, &key, &at, &state,
					&apps, &escs, &esc, &due); err == nil {
					m := map[string]any{
						"decision_id": did, "idempotency_key": key,
						"action_type": at, "state": state,
						"approvers": apps, "escalated": esc,
					}
					if due != nil {
						m["approval_due_at"] = due.UTC().Format(time.RFC3339)
					}
					out = append(out, m)
				}
			}
			rows.Close()
			writeJSON(w, 200, map[string]any{"pending": out})
		})

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(200)
	})

	addr := ":" + envOr("RTDP_APPROVAL_PORT", "8095")
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("approval-service on %s", addr)
	log.Fatal(http.Serve(ln, mux))
}

var _ = fmt.Sprintf // keep fmt for future log formatting
