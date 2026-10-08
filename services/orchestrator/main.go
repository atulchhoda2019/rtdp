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
	"slices"
	"sort"
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

	agentv1 "github.com/rtdp/rtdp/gen/go/rtdp/agent/v1"
	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/bundle"
	"github.com/rtdp/rtdp/internal/kafkax"
	"github.com/rtdp/rtdp/internal/redisx"
	"github.com/rtdp/rtdp/internal/ruleseng"
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

func tvs(s string) *rtdpv1.TypedValue {
	return &rtdpv1.TypedValue{Kind: &rtdpv1.TypedValue_StringValue{StringValue: s}}
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
		"%s|%s|%.6f|%s|%s", req.TokenizedClaimant, req.ProviderId, req.Amount,
		req.Currency, req.GetDelegation().GetProof()))))
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
		// ADR-013: the verified delegation chain travels with the
		// decision fact so audit/replay can reconstruct who acted
		// for whom (G8a).
		Delegation: req.Delegation,
	}
	res.Products = []*rtdpv1.SelectedProduct{{
		ProductId:             m.ProductID,
		SubscriptionRevision:  fmt.Sprint(m.SubscriptionRevision),
		EffectiveConfigDigest: m.Digest,
		BundleId:              m.Digest,
		SelectionReasons:      []string{"routing:event_type", "subscription:active"},
	}}

	// ADR-013: when the product declares required_scope, every link in
	// the chain must cover it — delegation narrows, never widens. The
	// decision fact is still written (spec); only the evaluation is
	// skipped.
	unauthorized := ""
	if req.Delegation != nil {
		if rs, ok := m.EffectiveConfig["required_scope"].(string); ok && rs != "" {
			for _, l := range req.Delegation.Links {
				if !slices.Contains(l.Scopes, rs) {
					unauthorized = l.AgentId
					break
				}
			}
		}
	}
	if unauthorized != "" {
		res.Outcome = rtdpv1.Decision_DECISION_DECLINE_UNAUTHORIZED
		res.ReasonCodes = []string{"DELEGATION_SCOPE_INSUFFICIENT:" + unauthorized}
	} else {

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
		// Caller-supplied attributes join the shared universe as "attr.<key>" —
		// bindings name them in input_features and prompt templates reference
		// {attr.<key>}. Sorted: map order is random and the snapshot digest must
		// be identical across an idempotent retry.
		attrKeys := make([]string, 0, len(req.Attributes))
		for k := range req.Attributes {
			attrKeys = append(attrKeys, k)
		}
		sort.Strings(attrKeys)
		for _, k := range attrKeys {
			featNames = append(featNames, "attr."+k)
			featVals = append(featVals, req.Attributes[k])
		}
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
		// Top-level scalar config is also exposed (e.g. t1_limit for
		// ADR-014 autonomy-tier conditions).
		for k, v := range m.EffectiveConfig {
			if _, taken := cfg[k]; taken {
				continue
			}
			switch t := v.(type) {
			case float64:
				cfg[k] = tvf(t)
			case string:
				cfg[k] = tvs(t)
			case bool:
				cfg[k] = &rtdpv1.TypedValue{Kind: &rtdpv1.TypedValue_BoolValue{BoolValue: t}}
			}
		}
		// Rules see attributes under input["attr.<key>"]. The prefix keeps
		// caller-supplied names from shadowing reserved input keys the rules
		// service reads (e.g. missing_required_signal_outcome).
		ruleInput := map[string]*rtdpv1.TypedValue{}
		for _, k := range attrKeys {
			ruleInput["attr."+k] = req.Attributes[k]
		}
		// Reserved platform keys: the claimed amount as submitted on the
		// transaction, so rules never depend on a caller-duplicated attribute.
		ruleInput["txn.amount"] = tvf(req.Amount)
		// ADR-013: actor.* context for rules — principal and last-hop agent
		// from the verified chain. Absent chain = legacy service caller, so
		// the actor var is empty (CEL sees null).
		actorMap := map[string]*rtdpv1.TypedValue{}
		if req.Delegation != nil && req.Delegation.Principal != nil {
			actorMap["principal.kind"] = tvs(req.Delegation.Principal.Kind.String())
			actorMap["principal.id"] = tvs(req.Delegation.Principal.Id)
			if n := len(req.Delegation.Links); n > 0 {
				last := req.Delegation.Links[n-1]
				actorMap["agent.id"] = tvs(last.AgentId)
				actorMap["agent.kind"] = tvs(last.AgentKind)
				actorMap["agent.scopes"] = &rtdpv1.TypedValue{
					Kind: &rtdpv1.TypedValue_StringList{
						StringList: &rtdpv1.StringList{Values: last.Scopes}}}
				actorMap["chain_depth"] = tvi(int64(n))
			}
		}
		// ADR-014: timer.approval_due_at — the earliest approval SLA among
		// actions this policy could hold, so rules can reason about the
		// clock. Absent when no action can be held.
		timerMap := map[string]*rtdpv1.TypedValue{}
		if m.ActionPolicy != nil {
			var minSla int64
			for _, aa := range m.ActionPolicy.Spec.Actions {
				if aa.SlaMinutes > 0 && (minSla == 0 || aa.SlaMinutes < minSla) {
					minSla = aa.SlaMinutes
				}
			}
			if minSla > 0 {
				timerMap["approval_due_at"] = tvs(time.Now().
					Add(time.Duration(minSla) * time.Minute).
					UTC().Format(time.RFC3339))
			}
		}
		eval, err := s.rules.EvaluateRules(ctx, &rtdpv1.EvaluateRulesRequest{
			RulesetDigest:        m.Ruleset.Digest,
			RulesetSpecJson:      m.Ruleset.SpecRaw,
			Features:             feat.Features,
			Signals:              sigValues,
			PresentSignalAliases: keys(present),
			Cfg:                  cfg,
			Input:                ruleInput,
			Actor:                actorMap,
			Timer:                timerMap,
			ExecutionBudgetMs:    time.Until(deadline).Milliseconds(),
		})
		if err != nil {
			decisions.WithLabelValues("rules_error").Inc()
			return nil, fmt.Errorf("rules: %w", err)
		}
		// ADR-014: a rule may return APPROVE_WITH_APPROVAL — the
		// orchestrator maps it to PENDING_APPROVAL (a human gate on an
		// otherwise-approvable request).
		forceApproval := eval.Decision == "APPROVE_WITH_APPROVAL"
		res.Outcome = parseDecision(eval.Decision)
		res.ReasonCodes = eval.ReasonCodes

		// 7. Permitted action intents only.
		if m.ActionPolicy != nil {
			pol := m.ActionPolicy.Spec
			for _, at := range allowedFor(res.Outcome.String(), pol) {
				// ADR-015: several gateway tools share an outcome —
				// the action's `when` selector picks the right intent.
				if !actionApplies(at, pol, req.Amount, cfg,
					ruleInput, actorMap) {
					continue
				}
				key := fmt.Sprintf("%s:%s:%s:%d:%s:%s", req.TenantId, req.Environment,
					res.DecisionId, res.DecisionGeneration, at, req.ProviderId)
				ai := &rtdpv1.ActionIntent{
					ActionType:     at,
					IdempotencyKey: key,
					AdapterRef:     pol.Adapters[at],
					TtlMs:          pol.IntentTTLMs[at],
					Status:         rtdpv1.IntentStatus_INTENT_READY,
					// The intent carries the caller's attributes so the
					// dispatcher's ledger records what was asked for.
					Payload: req.Attributes,
				}
				// ADR-014 tier resolution: held intents get approval
				// metadata so approval-service stays bundle-free.
				if req.Mode == rtdpv1.Mode_MODE_LIVE &&
					!completesAlone(at, req.Delegation, pol, req.Amount,
						cfg, ruleInput, actorMap) {
					aa := pol.Actions[at]
					ai.Status = rtdpv1.IntentStatus_INTENT_AWAITING_APPROVAL
					ai.Approvers = aa.Approvers
					if len(ai.Approvers) == 0 {
						// Held without configured approvers (e.g. a T2
						// action capped by the agent's ceiling): the
						// policy owner is the accountable fallback.
						ai.Approvers = []string{pol.Owner.Identity}
					}
					ai.EscalationApprovers = aa.EscalationApprovers
					ai.SlaBreachAction = aa.OnSlaBreach
					ai.ApprovalDueAt = timestamppb.New(
						res.DecidedAt.AsTime().Add(slaFor(aa)))
				}
				res.ActionIntents = append(res.ActionIntents, ai)
			}

			// Any held intent -> the decision lands PENDING_APPROVAL;
			// only the held commands wait (ready intents still
			// dispatch — per-intent autonomy, ADR-014).
			held := false
			for _, ai := range res.ActionIntents {
				if ai.Status == rtdpv1.IntentStatus_INTENT_AWAITING_APPROVAL {
					held = true
					res.ReasonCodes = append(res.ReasonCodes,
						"APPROVAL_REQUIRED:"+ai.ActionType)
				}
			}
			// PENDING_APPROVAL maps APPROVE-with-held per spec; a
			// REVIEW/DECLINE outcome keeps its own state even while
			// its intents wait (REVIEW is already a human path).
			if held && res.Outcome == rtdpv1.Decision_DECISION_APPROVE {
				res.Outcome = rtdpv1.Decision_DECISION_PENDING_APPROVAL
			}
			if forceApproval && !held {
				// Rule asked for a human gate but no action held it —
				// hold every emitted intent so the decision is releasable.
				for _, ai := range res.ActionIntents {
					aa := pol.Actions[ai.ActionType]
					ai.Status = rtdpv1.IntentStatus_INTENT_AWAITING_APPROVAL
					ai.Approvers = aa.Approvers
					ai.EscalationApprovers = aa.EscalationApprovers
					ai.SlaBreachAction = aa.OnSlaBreach
					ai.ApprovalDueAt = timestamppb.New(res.DecidedAt.AsTime().
						Add(slaFor(aa)))
				}
				res.Outcome = rtdpv1.Decision_DECISION_PENDING_APPROVAL
				res.ReasonCodes = append(res.ReasonCodes, "APPROVAL_REQUIRED")
			}
		}
		if forceApproval && res.Outcome == rtdpv1.Decision_DECISION_APPROVE {
			res.Outcome = rtdpv1.Decision_DECISION_PENDING_APPROVAL
		}
	} // end authorized evaluation branch

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
				// ADR-014: release state + approval metadata travel
				// with the durable command.
				Status:              ai.Status,
				Approvers:           ai.Approvers,
				EscalationApprovers: ai.EscalationApprovers,
				SlaBreachAction:     ai.SlaBreachAction,
				ApprovalDueAt:       ai.ApprovalDueAt,
				RequesterIdentities: requesterIdentities(req.Delegation),
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
	// A spent request deadline does not mean the producer is broken —
	// BeginTxn already aborts on a detached ctx, leaving the client clean.
	// Recycling it forces metadata + InitProducerId on the next request and
	// perpetuates deadline misses on high-latency brokers. Only discard on
	// non-context errors.
	release(err != nil && !errors.Is(err, context.DeadlineExceeded) &&
		!errors.Is(err, context.Canceled))
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

// completesAlone resolves ADR-014 autonomy for one action: the agent's
// registered ceiling (last chain link; legacy callers are platform T2)
// caps the action's tier, then the tier's declared policy applies —
// T0 never completes alone, T1 only when every condition holds, T2
// unconditionally. Undeclared action metadata defaults to T2 (v2.1
// behavior).
func completesAlone(actionType string, chain *agentv1.DelegationChain,
	pol bundle.ActionPolicySpec, amount float64,
	cfg, input, actor map[string]*rtdpv1.TypedValue) bool {
	aa, ok := pol.Actions[actionType]
	if !ok || aa.Tier == "" {
		return true
	}
	rank := map[string]int{"T0": 0, "T1": 1, "T2": 2}
	agentMax := "T2"
	if chain != nil && len(chain.Links) > 0 {
		if v := chain.Links[len(chain.Links)-1].MaxAutonomy; v != "" {
			agentMax = v
		}
	}
	if rank[agentMax] < rank[aa.Tier] {
		return false
	}
	tier, ok := pol.AutonomyTiers[aa.Tier]
	if !ok {
		return aa.Tier != "T0"
	}
	if !tier.CompletesAlone {
		return false
	}
	for _, cond := range tier.Conditions {
		ok, err := ruleseng.EvalCondition(cond,
			condVars(amount, cfg, input, actor))
		if err != nil || !ok {
			return false // fail closed: a broken condition holds for review
		}
	}
	return true
}

// condVars builds the shared CEL scope for policy conditions —
// input.attr.* / input.txn.* resolve through the nested rule-input
// map, same names rules see.
func condVars(amount float64, cfg, input,
	actor map[string]*rtdpv1.TypedValue) map[string]any {
	nestedIn := nestFlat(anyMap(input))
	inVars := map[string]any{"amount": amount}
	if a, ok := nestedIn["attr"]; ok {
		inVars["attr"] = a
	}
	if t, ok := nestedIn["txn"]; ok {
		inVars["txn"] = t
	}
	return map[string]any{
		"features": map[string]any{},
		"signals":  map[string]any{},
		"cfg":      nestFlat(anyMap(cfg)),
		"input":    inVars,
		"actor":    nestFlat(anyMap(actor)),
		"timer":    map[string]any{},
	}
}

// actionApplies evaluates the action's optional `when` selector — the
// intent is emitted only when it is absent or evaluates true. An
// unevaluable selector fails closed (no intent) so a misconfigured
// policy cannot emit the wrong action.
func actionApplies(actionType string, pol bundle.ActionPolicySpec,
	amount float64, cfg, input,
	actor map[string]*rtdpv1.TypedValue) bool {
	aa, ok := pol.Actions[actionType]
	if !ok || aa.When == "" {
		return true
	}
	ok, err := ruleseng.EvalCondition(aa.When,
		condVars(amount, cfg, input, actor))
	return err == nil && ok
}

// slaFor resolves an action's approval window: sla_minutes +
// sla_seconds, defaulting to 24h when neither is declared.
func slaFor(aa bundle.ActionAutonomy) time.Duration {
	d := time.Duration(aa.SlaMinutes)*time.Minute +
		time.Duration(aa.SlaSeconds)*time.Second
	if d <= 0 {
		return 24 * time.Hour
	}
	return d
}

// requesterIdentities lists the identities barred from approving a
// held intent (ADR-014 self-approval ban): every agent in the chain
// plus the principal.
func requesterIdentities(c *agentv1.DelegationChain) []string {
	if c == nil {
		return nil
	}
	var out []string
	if c.Principal != nil && c.Principal.Id != "" {
		out = append(out, c.Principal.Id)
	}
	for _, l := range c.Links {
		out = append(out, l.AgentId)
	}
	return out
}

func anyMap(m map[string]*rtdpv1.TypedValue) map[string]any {
	out := map[string]any{}
	for k, v := range m {
		out[k] = tvToAny(v)
	}
	return out
}

func tvToAny(v *rtdpv1.TypedValue) any {
	if v == nil {
		return nil
	}
	switch k := v.Kind.(type) {
	case *rtdpv1.TypedValue_DoubleValue:
		return k.DoubleValue
	case *rtdpv1.TypedValue_IntValue:
		return k.IntValue
	case *rtdpv1.TypedValue_StringValue:
		return k.StringValue
	case *rtdpv1.TypedValue_BoolValue:
		return k.BoolValue
	case *rtdpv1.TypedValue_StringList:
		return k.StringList.Values
	case *rtdpv1.TypedValue_DoubleList:
		return k.DoubleList.Values
	}
	return nil
}

// nestFlat unflattens dotted keys into nested maps for CEL vars
// (actor.agent.id etc.) — mirrors rules-service's nest.
func nestFlat(flat map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range flat {
		parts := strings.Split(k, ".")
		m := out
		for _, p := range parts[:len(parts)-1] {
			next, ok := m[p].(map[string]any)
			if !ok {
				next = map[string]any{}
				m[p] = next
			}
			m = next
		}
		m[parts[len(parts)-1]] = v
	}
	return out
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
		rdb:      redisx.New(),
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
