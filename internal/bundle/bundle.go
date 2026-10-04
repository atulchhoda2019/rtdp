// Package bundle loads compiled RuntimeBundle manifests and activation
// state. The decision path pins bundle digests and manifest epochs; there
// is no mutable "latest" lookup at request time.
package bundle

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// Manifest mirrors the compiler's bundle output (services/control-plane
// rtdp_contracts.compiler).
type Manifest struct {
	Kind                 string         `json:"kind"`
	ProductID            string         `json:"product_id"`
	ProductVersion       int            `json:"product_version"`
	TenantID             string         `json:"tenant_id"`
	SubscriptionRevision int            `json:"subscription_revision"`
	Digest               string         `json:"digest"`
	EffectiveConfig      map[string]any `json:"effective_config"`
	Features             []Feature      `json:"features"`
	Signals              []Signal       `json:"signals"`
	Ruleset              Ruleset        `json:"ruleset"`
	ActionPolicy         *ActionPolicy  `json:"action_policy"`
	Aggregation          string         `json:"aggregation"`
	MissingSignalOutcome string         `json:"missing_required_signal"`
}

type Feature struct {
	Name        string         `json:"name"`
	Version     int            `json:"version"`
	Tier        string         `json:"tier"`
	Digest      string         `json:"digest"`
	ValueType   string         `json:"value_type"`
	Computation map[string]any `json:"computation"`
	Scope       []string       `json:"scope"`
}

// ValueField is one field of a signal contract's value_schema.
type ValueField struct {
	Type     string   `json:"type"`
	Required bool     `json:"required"`
	Minimum  *float64 `json:"minimum"`
	Maximum  *float64 `json:"maximum"`
}

type Signal struct {
	Alias             string   `json:"alias"`
	Contract          string   `json:"contract"`
	AcceptedContracts []string `json:"accepted_contracts"`
	ContractDigest    string   `json:"contract_digest"`
	Binding           string   `json:"binding"`
	BindingDigest     string   `json:"binding_digest"`
	TimeoutMs         int      `json:"timeout_ms"`
	Required          bool     `json:"required"`
	Provider          string   `json:"provider"`
	EndpointRef       string   `json:"endpoint_ref"`
	Model             string   `json:"model"`
	ModelDigest       string   `json:"model_digest"`
	InputSchemaDigest string   `json:"input_schema_digest"`
	PreprocDigest     string   `json:"preprocessing_digest"`
	MaxAgeMs          int      `json:"maximum_age_ms"`
	InputFeatures     []string `json:"input_features"`
	// ValueSchema is the pinned contract's value map: field -> type/required/
	// range. The resolver validates envelopes against it — no hardcoded
	// "probability" assumption (regressions and extraction scores differ).
	ValueSchema map[string]ValueField `json:"value_schema"`
}

type Ruleset struct {
	ID      string          `json:"id"`
	Version int             `json:"version"`
	Digest  string          `json:"digest"`
	Spec    RulesetSpec     `json:"-"`
	SpecRaw json.RawMessage `json:"spec"`
}

// UnmarshalJSON captures the raw spec bytes (for digest verification) and
// also decodes the typed form.
func (r *Ruleset) UnmarshalJSON(b []byte) error {
	var aux struct {
		ID      string          `json:"id"`
		Version int             `json:"version"`
		Digest  string          `json:"digest"`
		Spec    json.RawMessage `json:"spec"`
	}
	if err := json.Unmarshal(b, &aux); err != nil {
		return err
	}
	r.ID, r.Version, r.Digest, r.SpecRaw = aux.ID, aux.Version, aux.Digest, aux.Spec
	return json.Unmarshal(aux.Spec, &r.Spec)
}

type RulesetSpec struct {
	RulesetID            string              `json:"ruleset_id"`
	Version              int                 `json:"version"`
	Requires             []SignalRequirement `json:"requires"`
	Rules                []Rule              `json:"rules"`
	Evaluation           string              `json:"evaluation"`
	Default              string              `json:"default_outcome"`
	MissingSignalOutcome string              `json:"missing_required_signal_outcome"`
}

type SignalRequirement struct {
	Signal   string `json:"signal"`
	Contract string `json:"contract"`
	Alias    string `json:"alias"`
	Optional bool   `json:"optional"`
}

type Rule struct {
	ID              string            `json:"id"`
	When            string            `json:"when"`
	Outcome         map[string]string `json:"outcome"`
	RequiresSignals []string          `json:"requires_signals"`
}

type ActionPolicy struct {
	ID      string           `json:"id"`
	Version int              `json:"version"`
	Digest  string           `json:"digest"`
	Spec    ActionPolicySpec `json:"-"`
	SpecRaw json.RawMessage  `json:"spec"`
}

func (a *ActionPolicy) UnmarshalJSON(b []byte) error {
	var aux struct {
		ID      string          `json:"id"`
		Version int             `json:"version"`
		Digest  string          `json:"digest"`
		Spec    json.RawMessage `json:"spec"`
	}
	if err := json.Unmarshal(b, &aux); err != nil {
		return err
	}
	a.ID, a.Version, a.Digest, a.SpecRaw = aux.ID, aux.Version, aux.Digest, aux.Spec
	return json.Unmarshal(aux.Spec, &a.Spec)
}

type ActionPolicySpec struct {
	PolicyID       string            `json:"action_policy_id"`
	Version        int               `json:"version"`
	AllowedActions []string          `json:"allowed_actions"`
	Adapters       map[string]string `json:"adapters"`
	// ActionRules maps action type -> outcomes that may emit it
	// (e.g. OPEN_SIU_CASE only on REVIEW).
	ActionRules map[string][]string `json:"action_rules"`
	Domain      string              `json:"authoritative_decision_domain"`
	IntentTTLMs map[string]int64    `json:"intent_ttl_ms"`
	Retries     map[string]struct {
		MaxAttempts int    `json:"maximum_attempts"`
		Strategy    string `json:"strategy"`
	} `json:"retries"`
	OnUnknown          string `json:"on_unknown_outcome"`
	LiveEffectsAllowed bool   `json:"live_effects_allowed"`

	// ADR-014: accountable owner (required — compile fails without it)
	// and per-action autonomy tiers with approval metadata.
	Owner         Owner                     `json:"owner"`
	AutonomyTiers map[string]AutonomyTier   `json:"autonomy_tiers"`
	Actions       map[string]ActionAutonomy `json:"actions"`
}

// Owner is the accountable human for a policy version (ADR-014).
type Owner struct {
	Role     string `json:"role"`
	Identity string `json:"identity"`
}

// AutonomyTier declares whether an action at this tier may complete
// without human approval, and under what conditions.
type AutonomyTier struct {
	CompletesAlone bool     `json:"completes_alone"`
	Conditions     []string `json:"conditions"` // CEL over input/cfg/actor
}

// ActionAutonomy is per-action approval metadata (ADR-014).
type ActionAutonomy struct {
	Tier                string   `json:"tier"` // T0|T1|T2 (default T2)
	Approvers           []string `json:"approvers"`
	EscalationApprovers []string `json:"escalation_approvers"`
	SlaMinutes          int64    `json:"sla_minutes"`
	SlaSeconds          int64    `json:"sla_seconds"`   // sub-minute SLAs
	OnSlaBreach         string   `json:"on_sla_breach"` // ESCALATE|EXPIRE
}

// Activation is the tenant/env/cohort -> bundle assignment the orchestrator
// pins per request.
type Activation struct {
	TenantID     string    `json:"tenant_id"`
	Environment  string    `json:"environment"`
	Cohort       string    `json:"cohort"`
	Epoch        int64     `json:"epoch"`
	BundleDigest string    `json:"bundle_digest"`
	Bundle       *Manifest `json:"bundle"`
}

// Store is a local file-backed activation+manifest cache. Production swaps
// this for the control plane's replicated cache; the interface is identical.
type Store struct {
	dir string
}

func NewStore(dir string) *Store { return &Store{dir: dir} }

func (s *Store) LoadManifest(path string) (*Manifest, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("parse manifest %s: %w", path, err)
	}
	if m.Digest == "" {
		return nil, fmt.Errorf("manifest %s has no digest", path)
	}
	return &m, nil
}

// ActivationFor returns the pinned activation for tenant/env/eventType —
// a tenant may activate several products; routing.event_types inside each
// pinned bundle selects which one applies to this request.
func (s *Store) ActivationFor(tenant, env, eventType string) (*Activation, error) {
	b, err := os.ReadFile(filepath.Join(s.dir, "activations.json"))
	if err != nil {
		return nil, err
	}
	var acts []Activation
	if err := json.Unmarshal(b, &acts); err != nil {
		return nil, err
	}
	var fallback *Activation
	for i := range acts {
		a := &acts[i]
		if a.TenantID != tenant || a.Environment != env || a.Bundle == nil {
			continue
		}
		routing, _ := a.Bundle.EffectiveConfig["routing"].(map[string]any)
		types, _ := routing["event_types"].([]any)
		for _, t := range types {
			if t == eventType {
				return a, nil
			}
		}
		// A bundle with no event_types constraint matches anything.
		if len(types) == 0 && fallback == nil {
			fallback = a
		}
	}
	if fallback != nil {
		return fallback, nil
	}
	return nil, fmt.Errorf("no activation for tenant=%s env=%s "+
		"event_type=%s", tenant, env, eventType)
}
