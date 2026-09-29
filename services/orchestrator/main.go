// Orchestrator: pinned context, dependency plan, deadlines, aggregation,
// and the durable Kafka commit boundary for decisions, contributions, and
// action commands.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
	"github.com/twmb/franz-go/pkg/kgo"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/bundle"
	"github.com/rtdp/rtdp/internal/kafkax"
)

var (
	decisions = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "rtdp_decisions_total",
	}, []string{"outcome"})
	latency = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "rtdp_decision_latency_seconds",
		Buckets: prometheus.ExponentialBuckets(0.0005, 2, 14),
	})
)

type server struct {
	rtdpv1.UnimplementedOrchestratorServer
	bundles  *bundle.Store
	features rtdpv1.FeatureServiceClient
	resolver rtdpv1.SignalResolverClient
	rules    rtdpv1.RulesServiceClient
	txnPool  *kafkax.TxnPool
	rdb      *redis.Client

	mu     sync.Mutex
	actEnv string
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func dial(addr string) *grpc.ClientConn {
	conn, err := grpc.NewClient(addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("dial %s: %v", addr, err)
	}
	return conn
}

func tvf(x float64) *rtdpv1.TypedValue {
	return &rtdpv1.TypedValue{Kind: &rtdpv1.TypedValue_DoubleValue{DoubleValue: x}}
}

func mustJSON(v any) []byte { b, _ := json.Marshal(v); return b }
func tvi(x int64) *rtdpv1.TypedValue {
	return &rtdpv1.TypedValue{Kind: &rtdpv1.TypedValue_IntValue{IntValue: x}}
}

func (s *server) Decide(ctx context.Context,
	req *rtdpv1.AuthenticatedTransaction) (*rtdpv1.DecisionResult, error) {
	started := time.Now()
	defer func() { latency.Observe(time.Since(started).Seconds()) }()

	mode := req.Mode.String()[5:]

	// 1. Pin the activation manifest for tenant+env+cohort.
	act, err := s.bundles.ActivationFor(req.TenantId, envOr("RTDP_ENV", "work"),
		req.EventType)
	if err != nil {
		decisions.WithLabelValues("reject").Inc()
		return nil, fmt.Errorf("activation: %w", err)
	}
	m := act.Bundle

	// The total deadline is executable config (product.execution
	// .total_deadline_ms), not a code constant.
	deadlineMs := 100
	if ex, ok := m.EffectiveConfig["execution"].(map[string]any); ok {
		if d, ok := ex["total_deadline_ms"].(float64); ok && d > 0 {
			deadlineMs = int(d)
		}
	}
	deadline := started.Add(time.Duration(deadlineMs) * time.Millisecond)
	ctx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()

	// 2. Dedup: same txn id+revision returns the pinned result; a differing
	// payload is a conflict.
	dedupKey := fmt.Sprintf("rtdp:dedup:{%s:%s}:%s:%d",
		req.TenantId, mode, req.TransactionId, req.TransactionRevision)
	payloadHash := fmt.Sprintf("%x", sha256.Sum256([]byte(fmt.Sprintf(
		"%s|%s|%.6f|%s", req.TokenizedClaimant, req.ProviderId, req.Amount,
		req.Currency))))
	prior, _ := s.rdb.HGet(ctx, dedupKey, "payload").Result()
	if prior != "" && prior != payloadHash {
		return nil, fmt.Errorf("CONFLICT: same transaction id, different payload")
	}
	if prior == payloadHash {
		if rj, err := s.rdb.HGet(ctx, dedupKey, "result").Bytes(); err == nil {
			var cached rtdpv1.DecisionResult
			if proto.Unmarshal(rj, &cached) == nil && cached.DecisionId != "" {
				return &cached, nil
			}
		}
	}

	res := &rtdpv1.DecisionResult{
		DecisionId:          uuid.NewString(),
		DecisionGeneration:  1,
		TenantId:            req.TenantId,
		Environment:         req.Environment,
		Mode:                req.Mode,
		TransactionId:       req.TransactionId,
		TransactionRevision: req.TransactionRevision,
		BundleDigest:        m.Digest,
		ManifestEpoch:       act.Epoch,
		DecidedAt:           timestamppb.Now(),
		Traceparent:         req.Traceparent,
	}
	res.Products = []*rtdpv1.SelectedProduct{{
		ProductId:             m.ProductID,
		SubscriptionRevision:  fmt.Sprint(m.SubscriptionRevision),
		EffectiveConfigDigest: m.Digest,
		BundleId:              m.Digest,
		SelectionReasons:      []string{"routing:event_type", "subscription:active"},
	}}

	// 3. Features: Tier 1 atomic update + Tier 2 reads.
	frefs := make([]*rtdpv1.FeatureRef, 0, len(m.Features))
	for _, f := range m.Features {
		frefs = append(frefs, &rtdpv1.FeatureRef{
			Name: f.Name, Version: int64(f.Version), Tier: f.Tier})
	}
	feat, err := s.features.ResolveFeatures(ctx, &rtdpv1.ResolveFeaturesRequest{
		TenantId:            req.TenantId,
		Environment:         req.Environment,
		Mode:                req.Mode,
		TransactionId:       req.TransactionId,
		TransactionRevision: req.TransactionRevision,
		TokenizedClaimant:   req.TokenizedClaimant,
		ProviderId:          req.ProviderId,
		Currency:            req.Currency,
		Amount:              req.Amount,
		EventTime:           req.EventTime,
		RequiredFeatures:    frefs,
	})
	if err != nil {
		decisions.WithLabelValues("feature_error").Inc()
		return nil, fmt.Errorf("features: %w", err)
	}

	// 4. Shared feature universe for signal inputs. Each binding orders its
	// own vector via input_features; "txn.*" names resolve from the request.
	featNames := make([]string, 0, len(feat.Features)+1)
	var featVals []*rtdpv1.TypedValue
	for _, f := range m.Features {
		featNames = append(featNames, f.Name)
		v := feat.Features[f.Name]
		if v == nil {
			v = tvf(0)
		}
		featVals = append(featVals, v)
	}
	featNames = append(featNames, "txn.amount")
	featVals = append(featVals, tvf(req.Amount))
	inputSnap := fmt.Sprintf("sha256:%x", sha256.Sum256([]byte(
		fmt.Sprintf("%v", featVals))))

	// 5. Signals via pinned bindings.
	specs := make([]*rtdpv1.SignalSpec, 0, len(m.Signals))
	for _, sig := range m.Signals {
		specs = append(specs, &rtdpv1.SignalSpec{
			Alias: sig.Alias, Contract: sig.Contract,
			AcceptedContracts:   sig.AcceptedContracts,
			ContractDigest:      sig.ContractDigest,
			Binding:             sig.Binding,
			BindingDigest:       sig.BindingDigest,
			TimeoutMs:           int64(sig.TimeoutMs),
			Required:            sig.Required,
			Provider:            sig.Provider,
			EndpointRef:         sig.EndpointRef,
			Model:               sig.Model,
			ModelDigest:         sig.ModelDigest,
			InputSchemaDigest:   sig.InputSchemaDigest,
			PreprocessingDigest: sig.PreprocDigest,
			MaximumAgeMs:        int64(sig.MaxAgeMs),
			InputFeatures:       sig.InputFeatures,
			ValueSchemaJson:     mustJSON(sig.ValueSchema),
		})
	}
	remaining := time.Until(deadline).Milliseconds()
	sigs, err := s.resolver.ResolveSignals(ctx, &rtdpv1.ResolveSignalsRequest{
		TenantId:            req.TenantId,
		Environment:         req.Environment,
		Mode:                req.Mode,
		TransactionId:       req.TransactionId,
		TransactionRevision: req.TransactionRevision,
		DecisionContextId:   res.DecisionId,
		EventTime:           req.EventTime,
		DeadlineMs:          remaining,
		Specs:               specs,
		FeatureNames:        featNames,
		FeatureValues:       featVals,
		InputSnapshotDigest: inputSnap,
		Traceparent:         req.Traceparent,
	})
	if err != nil {
		decisions.WithLabelValues("signal_error").Inc()
		return nil, fmt.Errorf("signals: %w", err)
	}

	present := map[string]bool{}
	sigValues := map[string]*rtdpv1.ValueMap{}
	for _, rs := range sigs.Signals {
		if rs.Ok && rs.Envelope != nil {
			present[rs.Alias] = true
			sigValues[rs.Alias] = &rtdpv1.ValueMap{Values: rs.Envelope.Values}
			res.Signals = append(res.Signals, rs.Envelope)
		}
	}

	// 6. Rules + aggregation.
	cfg := map[string]*rtdpv1.TypedValue{}
	if th, ok := m.EffectiveConfig["thresholds"].(map[string]any); ok {
		for k, v := range th {
			if f, ok := v.(float64); ok {
				cfg[k] = tvf(f)
			}
		}
	}
	eval, err := s.rules.EvaluateRules(ctx, &rtdpv1.EvaluateRulesRequest{
		RulesetDigest:        m.Ruleset.Digest,
		RulesetSpecJson:      m.Ruleset.SpecRaw,
		Features:             feat.Features,
		Signals:              sigValues,
		PresentSignalAliases: keys(present),
		Cfg:                  cfg,
		Input:                map[string]*rtdpv1.TypedValue{},
		ExecutionBudgetMs:    time.Until(deadline).Milliseconds(),
	})
	if err != nil {
		decisions.WithLabelValues("rules_error").Inc()
		return nil, fmt.Errorf("rules: %w", err)
	}
	res.Outcome = parseDecision(eval.Decision)
	res.ReasonCodes = eval.ReasonCodes

	// 7. Permitted action intents only.
	if m.ActionPolicy != nil {
		for _, at := range allowedFor(res.Outcome.String(), m.ActionPolicy.Spec) {
			key := fmt.Sprintf("%s:%s:%s:%d:%s:%s", req.TenantId, req.Environment,
				res.DecisionId, res.DecisionGeneration, at, req.ProviderId)
			res.ActionIntents = append(res.ActionIntents, &rtdpv1.ActionIntent{
				ActionType:     at,
				IdempotencyKey: key,
				AdapterRef:     m.ActionPolicy.Spec.Adapters[at],
				TtlMs:          m.ActionPolicy.Spec.IntentTTLMs[at],
			})
		}
	}

	// 8. Durable commit: decision + contribution + action commands + egress
	// in one Kafka transaction. Abort propagates — never assert early.
	contrib := &rtdpv1.FeatureContribution{
		ContributionId:      uuid.NewString(),
		TenantId:            req.TenantId,
		Environment:         req.Environment,
		Mode:                req.Mode,
		TransactionId:       req.TransactionId,
		TransactionRevision: req.TransactionRevision,
		TokenizedClaimant:   req.TokenizedClaimant,
		ProviderId:          req.ProviderId,
		Currency:            req.Currency,
		Amount:              req.Amount,
		EventTime:           req.EventTime,
	}
	dres, _ := proto.Marshal(res)
	cres, _ := proto.Marshal(contrib)
	recs := []*kgo.Record{
		{Topic: kafkax.TopicDecisionFacts,
			Key: []byte(req.TenantId + ":" + res.DecisionId), Value: dres},
		{Topic: kafkax.TopicEgress,
			Key: []byte(req.TenantId + ":" + req.TransactionId), Value: dres},
		{Topic: kafkax.TopicFeatureContrib,
			Key: []byte(req.TenantId + ":" + req.ProviderId), Value: cres},
	}
	if req.Mode != rtdpv1.Mode_MODE_LIVE {
		// Shadow/replay cannot emit live action commands.
	} else {
		for _, ai := range res.ActionIntents {
			cmd := &rtdpv1.ActionCommand{
				CommandId:          uuid.NewString(),
				TenantId:           req.TenantId,
				Environment:        req.Environment,
				Mode:               req.Mode,
				DecisionId:         res.DecisionId,
				DecisionGeneration: res.DecisionGeneration,
				ActionType:         ai.ActionType,
				IdempotencyKey:     ai.IdempotencyKey,
				AdapterRef:         ai.AdapterRef,
				Payload:            ai.Payload,
				IntentTtlMs:        ai.TtlMs,
				CreatedAt:          timestamppb.Now(),
			}
			cb, _ := proto.Marshal(cmd)
			recs = append(recs, &kgo.Record{
				Topic: kafkax.TopicActionCommands,
				Key:   []byte(ai.IdempotencyKey), Value: cb})
		}
	}
	// Transactions are per-producer — check one out so concurrent Decide
	// calls never interleave on the same transactional client.
	prod, release, err := s.txnPool.Get(ctx)
	if err != nil {
		decisions.WithLabelValues("commit_error").Inc()
		return nil, fmt.Errorf("durable commit: %w", err)
	}
	err = kafkax.BeginTxn(ctx, prod, func(ctx context.Context) error {
		// One batch: sequential ProduceSync per record pays a round-trip
		// each inside the decision budget.
		return prod.ProduceSync(ctx, recs...).FirstErr()
	})
	release(err != nil)
	if err != nil {
		decisions.WithLabelValues("commit_error").Inc()
		return nil, fmt.Errorf("durable commit: %w", err)
	}

	// 9. Cache result for idempotent retry/status lookup. Detached ctx: the
	// request deadline is usually spent by now, and losing this write breaks
	// retry idempotency.
	wctx, wcancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	pipe := s.rdb.Pipeline()
	pipe.HSet(wctx, dedupKey, "payload", payloadHash)
	if rb, err := proto.Marshal(res); err == nil {
		pipe.HSet(wctx, dedupKey, "result", rb)
	}
	pipe.Expire(wctx, dedupKey, 24*time.Hour)
	if _, err := pipe.Exec(wctx); err != nil {
		log.Printf("dedup cache write: %v", err)
	}
	wcancel()

	decisions.WithLabelValues(res.Outcome.String()).Inc()
	return res, nil
}

func allowedFor(outcome string, p bundle.ActionPolicySpec) []string {
	var out []string
	for _, a := range p.AllowedActions {
		if rules, ok := p.ActionRules[a]; ok && len(rules) > 0 {
			for _, o := range rules {
				if "DECISION_"+o == outcome {
					out = append(out, a)
					break
				}
			}
			continue
		}
		// Default policy: a synchronous *-RESPONSE action applies to every
		// outcome; any other action fires only on REVIEW.
		if strings.HasSuffix(a, "_RESPONSE") || outcome == "DECISION_REVIEW" {
			out = append(out, a)
		}
	}
	return out
}

func parseDecision(s string) rtdpv1.Decision {
	switch s {
	case "DECLINE":
		return rtdpv1.Decision_DECISION_DECLINE
	case "REVIEW":
		return rtdpv1.Decision_DECISION_REVIEW
	case "NOT_APPLICABLE":
		return rtdpv1.Decision_DECISION_NOT_APPLICABLE
	default:
		return rtdpv1.Decision_DECISION_APPROVE
	}
}

func keys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func (s *server) GetDecisionStatus(ctx context.Context,
	req *rtdpv1.GetDecisionStatusRequest) (*rtdpv1.GetDecisionStatusResponse, error) {
	return nil, errors.New("status lookup via projector/event-api")
}

func main() {
	conn := func(addr string) *grpc.ClientConn { return dial(addr) }
	// One transactional producer per in-flight commit: Kafka transactions
	// are per-client and cannot interleave on a shared producer.
	poolSize := 32
	if v := os.Getenv("RTDP_TXN_POOL"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			poolSize = n
		}
	}
	txnPool, err := kafkax.NewTxnPool("rtdp-orchestrator-"+
		uuid.NewString()[:8], poolSize)
	if err != nil {
		log.Fatalf("kafka: %v", err)
	}
	wctx, wcancel := context.WithTimeout(context.Background(), 30*time.Second)
	txnPool.Warm(wctx)
	wcancel()
	s := &server{
		bundles:  bundle.NewStore(envOr("RTDP_BUNDLE_DIR", "build/bundles")),
		features: rtdpv1.NewFeatureServiceClient(conn(envOr("RTDP_FEATURE_ADDR", "localhost:50052"))),
		resolver: rtdpv1.NewSignalResolverClient(conn(envOr("RTDP_RESOLVER_ADDR", "localhost:50054"))),
		rules:    rtdpv1.NewRulesServiceClient(conn(envOr("RTDP_RULES_ADDR", "localhost:50053"))),
		txnPool:  txnPool,
		rdb:      redis.NewClient(&redis.Options{Addr: envOr("RTDP_REDIS_ADDR", "localhost:6379")}),
	}
	go func() {
		http.Handle("/metrics", promhttp.Handler())
		http.ListenAndServe(":9090", nil)
	}()
	lis, err := net.Listen("tcp", ":"+envOr("RTDP_ORCHESTRATOR_PORT", "50055"))
	if err != nil {
		log.Fatal(err)
	}
	gs := grpc.NewServer()
	rtdpv1.RegisterOrchestratorServer(gs, s)
	log.Printf("orchestrator on :%s", envOr("RTDP_ORCHESTRATOR_PORT", "50055"))
	log.Fatal(gs.Serve(lis))
}
