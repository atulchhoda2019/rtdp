// Action Dispatcher: durable intent handling, idempotent execution against
// local simulators, and acknowledged/unknown/failure tracking.
//
// Consumes committed action commands, then:
//
//	inbox dedup -> ledger row (one Postgres txn) -> lease claim -> adapter
//	dispatch -> ACK/FAILED/UNKNOWN -> outbox -> rtdp.action.status.v1
package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/twmb/franz-go/pkg/kgo"
	"google.golang.org/protobuf/proto"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/kafkax"
)

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

// --- local simulated adapters ------------------------------------------------
// Synthetic effects only — no real enforcement providers.

type adapter interface {
	// Execute performs the simulated effect. Returns (providerRef, acked,
	// error). A nil error + acked=false means the provider timed out after
	// possibly applying the effect -> UNKNOWN, never blind retry.
	Execute(ctx context.Context, cmd *rtdpv1.ActionCommand) (string, bool, error)
}

type claimSimulator struct{}

func (claimSimulator) Execute(ctx context.Context, cmd *rtdpv1.ActionCommand) (string, bool, error) {
	// Simulate a deterministic claim-response effect keyed on idempotency.
	ref := fmt.Sprintf("claimsim:%x", sha256.Sum256([]byte(cmd.IdempotencyKey)))[:24]
	return ref, true, nil
}

type siuAdapter struct{ db *pgxpool.Pool }

func (a siuAdapter) Execute(ctx context.Context, cmd *rtdpv1.ActionCommand) (string, bool, error) {
	ref := "siu_" + uuid.NewString()[:12]
	// Durable SIU review-case row keyed on the idempotency key; a retry
	// sees the existing row rather than duplicating the case.
	_, err := a.db.Exec(ctx, `
		INSERT INTO execution_event (tenant_id, mode, event_id, kind, detail)
		VALUES ($1, $2, $3, 'siu_case', $4)
		ON CONFLICT (tenant_id, mode, event_id) DO NOTHING`,
		cmd.TenantId, "LIVE", "siu:"+cmd.IdempotencyKey,
		mustJSON(map[string]any{"ref": ref, "decision": cmd.DecisionId}))
	if err != nil {
		return "", false, err
	}
	return ref, true, nil
}

type notifySink struct{}

func (notifySink) Execute(ctx context.Context, cmd *rtdpv1.ActionCommand) (string, bool, error) {
	return "notify:" + uuid.NewString()[:12], true, nil
}

// timeoutSimulator reports a provider timeout after a possible apply —
// exercises the UNKNOWN path (ADR-007: reconcile, never blind retry).
type timeoutSimulator struct{}

func (timeoutSimulator) Execute(ctx context.Context, cmd *rtdpv1.ActionCommand) (string, bool, error) {
	ref := fmt.Sprintf("timeoutsim:%x", sha256.Sum256([]byte(cmd.IdempotencyKey)))[:24]
	return ref, false, nil
}

func mustJSON(v any) []byte { b, _ := json.Marshal(v); return b }

// payloadMap flattens a TypedValue map to plain Go values for hashing/json.
func payloadMap(m map[string]*rtdpv1.TypedValue) map[string]any {
	out := map[string]any{}
	for k, v := range m {
		switch t := v.GetKind().(type) {
		case *rtdpv1.TypedValue_StringValue:
			out[k] = t.StringValue
		case *rtdpv1.TypedValue_DoubleValue:
			out[k] = t.DoubleValue
		case *rtdpv1.TypedValue_IntValue:
			out[k] = t.IntValue
		case *rtdpv1.TypedValue_BoolValue:
			out[k] = t.BoolValue
		case *rtdpv1.TypedValue_StringList:
			out[k] = t.StringList.Values
		case *rtdpv1.TypedValue_DoubleList:
			out[k] = t.DoubleList.Values
		}
	}
	return out
}

func adapterFor(ref string, db *pgxpool.Pool) adapter {
	switch {
	case ref == "local_claim_simulator@1":
		return claimSimulator{}
	case ref == "local_siu_case_adapter@1":
		return siuAdapter{db}
	case ref == "local_timeout_simulator@1":
		return timeoutSimulator{}
	default:
		return notifySink{}
	}
}

// ---------------------------------------------------------------------------

func main() {
	db, err := pgxpool.New(context.Background(), envOr("RTDP_POSTGRES_DSN",
		"postgres://rtdp_runtime:rtdp_runtime@localhost:5432/rtdp?sslmode=disable"))
	if err != nil {
		log.Fatal(err)
	}
	consumer, err := kafkax.NewReader("rtdp-action-dispatcher", kafkax.TopicActionCommands)
	if err != nil {
		log.Fatal(err)
	}
	statusPub, err := kafkax.NewProducer()
	if err != nil {
		log.Fatal(err)
	}

	go func() {
		http.Handle("/metrics", promhttp.Handler())
		http.ListenAndServe(":9090", nil)
	}()

	log.Printf("action-dispatcher consuming %s", kafkax.TopicActionCommands)
	for {
		fetches := consumer.PollFetches(context.Background())
		if fetches.IsClientClosed() {
			return
		}
		fetches.EachError(func(t string, p int32, err error) {
			log.Printf("fetch %s/%d: %v", t, p, err)
		})
		fetches.EachRecord(func(rec *kgo.Record) {
			if err := handle(context.Background(), db, rec, statusPub); err != nil {
				log.Printf("handle: %v", err)
				return
			}
		})
		consumer.CommitRecords(context.Background(), fetches.Records()...)
	}
}

func handle(ctx context.Context, db *pgxpool.Pool, rec *kgo.Record,
	pub *kgo.Client) error {
	var cmd rtdpv1.ActionCommand
	if err := proto.Unmarshal(rec.Value, &cmd); err != nil {
		return fmt.Errorf("decode action command: %w", err)
	}
	if cmd.Mode != rtdpv1.Mode_MODE_LIVE {
		return nil // replay/shadow cannot dispatch live effects
	}
	payloadDigest := fmt.Sprintf("%x", sha256.Sum256(
		mustJSON(payloadMap(cmd.Payload))))

	// Inbox dedup + ledger row in one Postgres transaction.
	tx, err := db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx,
		`SELECT set_config('rtdp.tenant_id', $1, true)`, cmd.TenantId); err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `
		INSERT INTO inbox (consumer_name, tenant_id, message_id, payload_digest)
		VALUES ('action-dispatcher', $1, $2, $3)
		ON CONFLICT (consumer_name, tenant_id, message_id) DO NOTHING`,
		cmd.TenantId, cmd.IdempotencyKey, payloadDigest)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		// Duplicate command: verify same payload, then skip (idempotent).
		var prior string
		err := tx.QueryRow(ctx,
			`SELECT payload_digest FROM inbox
			 WHERE consumer_name='action-dispatcher' AND tenant_id=$1 AND message_id=$2`,
			cmd.TenantId, cmd.IdempotencyKey).Scan(&prior)
		if err == nil && prior != payloadDigest {
			tx.Rollback(ctx)
			return fmt.Errorf("payload conflict for %s", cmd.IdempotencyKey)
		}
		tx.Commit(ctx)
		return nil
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO action_execution
		  (tenant_id, environment, idempotency_key, decision_id,
		   decision_generation, action_type, intent, payload_digest, state,
		   expires_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'PENDING',
		        now() + $9::int8 * interval '1 millisecond')
		ON CONFLICT (tenant_id, environment, idempotency_key) DO NOTHING`,
		cmd.TenantId, cmd.Environment, cmd.IdempotencyKey, cmd.DecisionId,
		cmd.DecisionGeneration, cmd.ActionType, mustJSON(payloadMap(cmd.Payload)),
		payloadDigest, cmd.IntentTtlMs)
	if err != nil {
		return err
	}
	if err := tx.Commit(ctx); err != nil {
		return err
	}

	// Claim with a fencing generation; only current generation may finish.
	var gen int64
	err = db.QueryRow(ctx, `
		UPDATE action_execution
		SET lease_generation = lease_generation + 1,
		    lease_expires_at = now() + interval '30 seconds',
		    state = 'DISPATCHING', updated_at = now()
		WHERE tenant_id=$1 AND environment=$2 AND idempotency_key=$3
		  AND state IN ('PENDING','DISPATCHING')
		RETURNING lease_generation`,
		cmd.TenantId, cmd.Environment, cmd.IdempotencyKey).Scan(&gen)
	if err != nil {
		return err // already terminal or leased
	}

	// Dispatch to the declared adapter.
	adp := adapterFor(cmd.AdapterRef, db)
	provRef, acked, derr := adp.Execute(ctx, &cmd)

	var state string
	switch {
	case derr == nil && acked:
		state = "ACKNOWLEDGED"
	case derr != nil:
		state = "FAILED"
	default:
		state = "UNKNOWN" // timeout after possible dispatch — reconcile, never blind retry
	}
	tag, err = db.Exec(ctx, `
		UPDATE action_execution
		SET state=$4, provider_reference=$5, updated_at=now()
		WHERE tenant_id=$1 AND environment=$2 AND idempotency_key=$3
		  AND lease_generation=$6`,
		cmd.TenantId, cmd.Environment, cmd.IdempotencyKey, state, provRef, gen)
	if err != nil || tag.RowsAffected() == 0 {
		return fmt.Errorf("lease fencing: lost generation")
	}

	// Outbox-style status publication.
	status := map[string]any{
		"tenant_id": cmd.TenantId, "environment": cmd.Environment,
		"idempotency_key": cmd.IdempotencyKey, "state": state,
		"provider_reference": provRef, "decision_id": cmd.DecisionId,
	}
	if b, err := json.Marshal(status); err == nil {
		pub.ProduceSync(ctx, &kgo.Record{
			Topic: kafkax.TopicActionStatus,
			Key:   []byte(cmd.IdempotencyKey), Value: b})
	}
	log.Printf("action %s -> %s", cmd.IdempotencyKey, state)
	return nil
}
