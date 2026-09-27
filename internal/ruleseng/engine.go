// Package ruleseng evaluates declarative CEL rulesets against canonical
// inputs. Rules are configuration, not code — but they are not trusted
// merely because they are configuration: expressions are compiled at load
// with declaration checks, size limits, and evaluation cost limits.
package ruleseng

import (
	"fmt"

	"cel.dev/cel-go/cel"
	"cel.dev/cel-go/common/types"

	"github.com/rtdp/rtdp/internal/bundle"
)

const (
	// Sandbox limits (design.md: enforce expression size, supported
	// operators, instruction/cost limits).
	maxExprBytes = 4096
	celCostLimit = 10_000_000
)

// Outcome is one fired rule's decision.
type Outcome struct {
	RuleID   string
	Decision string
	Reason   string
	Skipped  bool // declared missing required inputs — not "evaluated false"
	SkipWhy  string
}

// Result is the rules stage output before product-level aggregation.
type Result struct {
	Fired     []Outcome
	Skipped   []Outcome
	Defaulted bool
}

// Engine holds compiled programs for one pinned ruleset digest.
type Engine struct {
	spec  bundle.RulesetSpec
	progs map[string]cel.Program
	order []string
}

func env() (*cel.Env, error) {
	dynMap := cel.MapType(cel.StringType, cel.DynType)
	return cel.NewEnv(
		cel.Variable("features", dynMap),
		cel.Variable("signals", dynMap),
		cel.Variable("cfg", dynMap),
		cel.Variable("input", dynMap),
		cel.Variable("actor", dynMap),
		cel.Variable("timer", dynMap),
	)
}

// Compile validates and compiles a ruleset. Fails closed: a rule that does
// not type-check is a compile diagnostic, not a runtime surprise.
func Compile(spec bundle.RulesetSpec) (*Engine, error) {
	e, err := env()
	if err != nil {
		return nil, err
	}
	progs := map[string]cel.Program{}
	var order []string
	for _, r := range spec.Rules {
		if len(r.When) > maxExprBytes {
			return nil, fmt.Errorf("rule %s exceeds expression size limit", r.ID)
		}
		ast, iss := e.Compile(r.When)
		if iss.Err() != nil {
			return nil, fmt.Errorf("rule %s CEL compile: %w", r.ID, iss.Err())
		}
		if !ast.OutputType().IsExactType(cel.BoolType) {
			return nil, fmt.Errorf("rule %s must evaluate to bool", r.ID)
		}
		prg, err := e.Program(ast,
			cel.CostTracking(nil),
			cel.CostLimit(celCostLimit),
			cel.InterruptCheckFrequency(128))
		if err != nil {
			return nil, fmt.Errorf("rule %s program: %w", r.ID, err)
		}
		progs[r.ID] = prg
		order = append(order, r.ID)
	}
	return &Engine{spec: spec, progs: progs, order: order}, nil
}

// Evaluate runs all rules whose required signals are present. Missing
// required inputs mark dependent rules skipped; they never read zero values.
func (e *Engine) Evaluate(features map[string]any,
	signals map[string]map[string]any,
	presentSignals map[string]bool,
	cfg, input, actor map[string]any) (*Result, error) {

	res := &Result{}
	ruleByID := map[string]bundle.Rule{}
	for _, r := range e.spec.Rules {
		ruleByID[r.ID] = r
	}
	sigMap := map[string]any{}
	for k, v := range signals {
		sigMap[k] = v
	}
	vars := map[string]any{
		"features": features, "signals": sigMap, "cfg": cfg,
		"input": input, "actor": actor, "timer": map[string]any{},
	}
	for _, id := range e.order {
		r := ruleByID[id]
		missing := false
		for _, alias := range r.RequiresSignals {
			if !presentSignals[alias] {
				missing = true
			}
		}
		if missing {
			res.Skipped = append(res.Skipped, Outcome{
				RuleID: id, Skipped: true,
				SkipWhy: "required signal absent",
			})
			continue
		}
		out, _, err := e.progs[id].Eval(vars)
		if err != nil {
			return nil, fmt.Errorf("rule %s eval: %w", id, err)
		}
		if out == types.True {
			res.Fired = append(res.Fired, Outcome{
				RuleID:   id,
				Decision: r.Outcome["decision"],
				Reason:   r.Outcome["reason"],
			})
		}
	}
	return res, nil
}

// Aggregate applies the pinned precedence policy. Within the claim
// decisioning domain: DECLINE > REVIEW > APPROVE (design.md worked
// example). The configured default applies only when inputs were valid —
// a missing required signal yields the declared missing-signal outcome.
func (e *Engine) Aggregate(res *Result,
	missingRequiredOutcome string) (string, []string) {
	precedence := map[string]int{"DECLINE": 3, "REVIEW": 2, "APPROVE": 1}
	best, bestRank := "", 0
	var reasons []string
	for _, f := range res.Fired {
		reasons = append(reasons, f.Reason)
		if precedence[f.Decision] > bestRank {
			best, bestRank = f.Decision, precedence[f.Decision]
		}
	}
	if best != "" {
		return best, reasons
	}
	if len(res.Skipped) > 0 && missingRequiredOutcome != "" {
		return missingRequiredOutcome,
			append(reasons, "MISSING_REQUIRED_SIGNAL")
	}
	res.Defaulted = true
	return e.spec.Default, reasons
}
