// Package envelope validates SignalEnvelopes against the bundle's pinned
// signal specs — the resolver's semantic gate. Matching only transaction_id
// is never sufficient (design.md: signal provider abstraction).
package envelope

import (
	"fmt"
	"time"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/bundle"
)

type Reject struct {
	Code   string
	Detail string
}

func (r *Reject) Error() string { return fmt.Sprintf("%s: %s", r.Code, r.Detail) }

// Validate checks one envelope against the pinned signal spec and request
// context. now is injected for replayability.
func Validate(env *rtdpv1.SignalEnvelope, sig bundle.Signal,
	tenantID, envName string, mode rtdpv1.Mode,
	txnID string, txnRev int64, ctxID string, now time.Time) error {
	if env == nil {
		return &Reject{"NO_ENVELOPE", "nil envelope"}
	}
	bad := func(code, detail string) error { return &Reject{code, detail} }

	if env.TenantId != tenantID {
		return bad("TENANT", "envelope tenant mismatch")
	}
	if env.Environment != envName {
		return bad("ENV", "envelope environment mismatch")
	}
	if env.Mode != mode {
		return bad("MODE", "envelope mode mismatch")
	}
	if env.TransactionId != txnID || env.TransactionRevision != txnRev {
		return bad("REVISION", "transaction id/revision mismatch")
	}
	if env.DecisionContextId != ctxID {
		return bad("CONTEXT", "decision context mismatch")
	}
	if env.SignalName != sig.Contract {
		return bad("SIGNAL", fmt.Sprintf("got %q want %q", env.SignalName, sig.Contract))
	}
	ok := false
	for _, v := range sig.AcceptedContracts {
		if env.ContractVersion == v {
			ok = true
		}
	}
	if !ok {
		return bad("CONTRACT_VERSION",
			fmt.Sprintf("version %q not in %v", env.ContractVersion, sig.AcceptedContracts))
	}
	if sig.ContractDigest != "" && env.ContractDigest != "" &&
		env.ContractDigest != sig.ContractDigest {
		return bad("CONTRACT_DIGEST", "contract digest mismatch")
	}
	if env.BindingId+"" != "" && fmt.Sprintf("%s@%d", env.BindingId, env.BindingVersion) != sig.Binding {
		return bad("BINDING", fmt.Sprintf("binding %s@%d not pinned %s",
			env.BindingId, env.BindingVersion, sig.Binding))
	}
	if env.ModelDigest != "" && env.ModelDigest != sig.ModelDigest {
		return bad("MODEL_DIGEST", "model digest not the approved artifact")
	}
	if env.GetExpiresAt() != nil && now.After(env.ExpiresAt.AsTime()) {
		return bad("EXPIRED", "signal expired")
	}
	if env.GetEventTime() != nil && env.EventTime.AsTime().After(now.Add(5*time.Minute)) {
		return bad("FUTURE", "event_time impossibly far in the future")
	}
	if env.Status == rtdpv1.SignalStatus_SIGNAL_STATUS_OK {
		// Validate each value against the pinned contract's value_schema —
		// required fields present, numeric fields in their declared range.
		// The contract governs, not a hardcoded probability check.
		for field, spec := range sig.ValueSchema {
			v, present := env.Values[field]
			if spec.Required && !present {
				return bad("VALUE",
					fmt.Sprintf("required value %q missing", field))
			}
			if !present {
				continue
			}
			if spec.Type == "float64" || spec.Type == "int64" {
				var val float64
				switch k := v.Kind.(type) {
				case *rtdpv1.TypedValue_DoubleValue:
					if spec.Type == "int64" {
						return bad("VALUE",
							fmt.Sprintf("%s has double, want int64", field))
					}
					val = k.DoubleValue
				case *rtdpv1.TypedValue_IntValue:
					val = float64(k.IntValue)
				default:
					return bad("VALUE",
						fmt.Sprintf("%s has non-numeric kind", field))
				}
				if spec.Minimum != nil && val < *spec.Minimum {
					return bad("VALUE",
						fmt.Sprintf("%s=%v below minimum %v", field, val,
							*spec.Minimum))
				}
				if spec.Maximum != nil && val > *spec.Maximum {
					return bad("VALUE",
						fmt.Sprintf("%s=%v above maximum %v", field, val,
							*spec.Maximum))
				}
			}
		}
	}
	return nil
}
