// Projector: writes committed decision facts to Postgres (append-only) and
// maintains the read model for status lookups. Facts are immutable —
// generation + payload digest guard replays.
package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"log"
	"net/http"
	"os"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/twmb/franz-go/pkg/kgo"
	"google.golang.org/protobuf/encoding/protojson"
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

func main() {
	db, err := pgxpool.New(context.Background(), envOr("RTDP_POSTGRES_DSN",
		"postgres://rtdp_runtime:rtdp_runtime@localhost:5432/rtdp?sslmode=disable"))
	if err != nil {
		log.Fatal(err)
	}
	consumer, err := kafkax.NewReader("rtdp-projector",
		kafkax.TopicDecisionFacts)
	if err != nil {
		log.Fatal(err)
	}
	go func() {
		http.Handle("/metrics", promhttp.Handler())
		http.ListenAndServe(":9090", nil)
	}()

	ctx := context.Background()
	log.Printf("projector consuming %s", kafkax.TopicDecisionFacts)
	for {
		fetches := consumer.PollFetches(ctx)
		if fetches.IsClientClosed() {
			return
		}
		var done []*kgo.Record
		fetches.EachRecord(func(rec *kgo.Record) {
			var res rtdpv1.DecisionResult
			if err := proto.Unmarshal(rec.Value, &res); err != nil {
				log.Printf("decode decision: %v", err)
				return
			}
			if err := writeFact(ctx, db, &res, rec); err != nil {
				log.Printf("decision_fact write: %v", err)
				return
			}
			done = append(done, rec)
		})
		if len(done) > 0 {
			consumer.CommitRecords(ctx, done...)
		}
	}
}

func writeFact(ctx context.Context, db *pgxpool.Pool,
	res *rtdpv1.DecisionResult, rec *kgo.Record) error {
	snap, _ := protojson.Marshal(res)
	mode := res.Mode.String()
	if len(mode) > 5 {
		mode = mode[5:]
	}
	payloadDigest := fmt.Sprintf("%x", sha256.Sum256(rec.Value))

	tx, err := db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx,
		`SELECT set_config('rtdp.tenant_id', $1, true)`,
		res.TenantId); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `
		INSERT INTO decision_fact
		  (tenant_id, mode, decision_id, generation, transaction_id,
		   transaction_revision, bundle_digest, manifest_epoch, decided_at,
		   outcome, input_snapshot, signal_snapshot, result, payload_digest)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
		ON CONFLICT (tenant_id, mode, decision_id, generation) DO NOTHING`,
		res.TenantId, mode, res.DecisionId, res.DecisionGeneration,
		res.TransactionId, res.TransactionRevision, res.BundleDigest,
		res.ManifestEpoch, res.DecidedAt.AsTime(), res.Outcome.String(),
		`{}`, snap, snap, payloadDigest)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}
