// Rules Service: sandboxed CEL evaluation of pinned rulesets.
// Verifies the spec bytes against the declared digest before evaluating —
// a runtime never substitutes its own version.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/bundle"
	"github.com/rtdp/rtdp/internal/ruleseng"
)

var evals = promauto.NewCounterVec(prometheus.CounterOpts{
	Name: "rtdp_rules_evaluations_total",
}, []string{"result"})

type server struct {
	rtdpv1.UnimplementedRulesServiceServer
	mu      sync.Mutex
	engines map[string]*ruleseng.Engine // by verified digest
}

func tv(v *rtdpv1.TypedValue) any {
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

func mapOf(m map[string]*rtdpv1.TypedValue) map[string]any {
	out := map[string]any{}
	for k, v := range m {
		out[k] = tv(v)
	}
	return out
}

// nest unflattens dotted wire keys into nested maps so CEL can write
// actor.agent.id / timer.approval_due_at (ADR-013/014). Keys without
// dots pass through unchanged.
func nest(flat map[string]any) map[string]any {
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

func (s *server) engineFor(digest string, specJSON []byte) (*ruleseng.Engine, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if e, ok := s.engines[digest]; ok {
		return e, nil
	}
	var specMap map[string]any
	if err := json.Unmarshal(specJSON, &specMap); err != nil {
		return nil, fmt.Errorf("ruleset spec decode: %w", err)
	}
	actual, err := bundle.CanonicalDigest(specMap)
	if err != nil {
		return nil, err
	}
	if actual != digest {
		return nil, fmt.Errorf("ruleset digest mismatch: declared %s computed %s",
			digest, actual)
	}
	var spec bundle.RulesetSpec
	if err := json.Unmarshal(specJSON, &spec); err != nil {
		return nil, fmt.Errorf("ruleset spec decode: %w", err)
	}
	e, err := ruleseng.Compile(spec)
	if err != nil {
		return nil, err
	}
	s.engines[digest] = e
	return e, nil
}

func (s *server) EvaluateRules(ctx context.Context,
	req *rtdpv1.EvaluateRulesRequest) (*rtdpv1.EvaluateRulesResponse, error) {
	e, err := s.engineFor(req.RulesetDigest, req.RulesetSpecJson)
	if err != nil {
		evals.WithLabelValues("reject").Inc()
		return nil, err
	}

	present := map[string]bool{}
	for _, a := range req.PresentSignalAliases {
		present[a] = true
	}
	// signals: alias -> {value-name: scalar}
	signals := map[string]map[string]any{}
	for alias, vm := range req.Signals {
		signals[alias] = mapOf(vm.GetValues())
	}

	res, err := e.Evaluate(mapOf(req.Features), signals, present,
		mapOf(req.Cfg), mapOf(req.Input),
		nest(mapOf(req.Actor)), nest(mapOf(req.Timer)))
	if err != nil {
		evals.WithLabelValues("error").Inc()
		return nil, err
	}
	decision, reasons := e.Aggregate(res, res0missing(res, req))

	resp := &rtdpv1.EvaluateRulesResponse{
		Decision:    decision,
		ReasonCodes: reasons,
		Defaulted:   res.Defaulted,
	}
	for _, f := range res.Fired {
		resp.Fired = append(resp.Fired, &rtdpv1.FiredRule{
			RuleId: f.RuleID, Decision: f.Decision, Reason: f.Reason})
	}
	for _, sk := range res.Skipped {
		resp.SkippedRuleIds = append(resp.SkippedRuleIds, sk.RuleID)
	}
	evals.WithLabelValues("ok").Inc()
	return resp, nil
}

func res0missing(res *ruleseng.Result, req *rtdpv1.EvaluateRulesRequest) string {
	if m, ok := req.Input["missing_required_signal_outcome"]; ok {
		if s, isStr := tv(m).(string); isStr {
			return s
		}
	}
	return "REVIEW"
}

func main() {
	s := &server{engines: map[string]*ruleseng.Engine{}}
	go func() {
		http.Handle("/metrics", promhttp.Handler())
		http.ListenAndServe(":9090", nil)
	}()
	lis, err := net.Listen("tcp", ":"+envOr("RTDP_RULES_PORT", "50053"))
	if err != nil {
		log.Fatal(err)
	}
	gs := grpc.NewServer()
	rtdpv1.RegisterRulesServiceServer(gs, s)
	log.Printf("rules-service on :%s", envOr("RTDP_RULES_PORT", "50053"))
	log.Fatal(gs.Serve(lis))
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
