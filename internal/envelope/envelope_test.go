package envelope

import (
	"testing"
	"time"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/bundle"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func f64p(v float64) *float64 { return &v }

func baseSpec(schema map[string]bundle.ValueField) bundle.Signal {
	return bundle.Signal{
		Alias:             "sig",
		Contract:          "test.signal",
		AcceptedContracts: []string{"1.0.0"},
		ContractDigest:    "sha256:abc",
		Binding:           "bind@1",
		ModelDigest:       "sha256:model",
		ValueSchema:       schema,
	}
}

func baseEnvelope(now time.Time) *rtdpv1.SignalEnvelope {
	return &rtdpv1.SignalEnvelope{
		TenantId:            "t",
		Environment:         "work",
		Mode:                rtdpv1.Mode_MODE_LIVE,
		TransactionId:       "txn",
		TransactionRevision: 1,
		DecisionContextId:   "ctx",
		SignalName:          "test.signal",
		ContractVersion:     "1.0.0",
		ContractDigest:      "sha256:abc",
		BindingId:           "bind",
		BindingVersion:      1,
		ModelDigest:         "sha256:model",
		Status:              rtdpv1.SignalStatus_SIGNAL_STATUS_OK,
		ExpiresAt:           timestamppb.New(now.Add(time.Minute)),
		EventTime:           timestamppb.New(now),
		Values:              map[string]*rtdpv1.TypedValue{},
	}
}

func dv(v float64) *rtdpv1.TypedValue {
	return &rtdpv1.TypedValue{Kind: &rtdpv1.TypedValue_DoubleValue{DoubleValue: v}}
}

func check(t *testing.T, env *rtdpv1.SignalEnvelope, sig bundle.Signal,
	wantCode string) {
	t.Helper()
	err := Validate(env, sig, "t", "work", rtdpv1.Mode_MODE_LIVE,
		"txn", 1, "ctx", time.Now())
	if wantCode == "" {
		if err != nil {
			t.Fatalf("want ok, got %v", err)
		}
		return
	}
	r, ok := err.(*Reject)
	if !ok || r.Code != wantCode {
		t.Fatalf("want reject %s, got %v", wantCode, err)
	}
}

func TestProbabilitySignal(t *testing.T) {
	schema := map[string]bundle.ValueField{
		"probability": {Type: "float64", Required: true,
			Minimum: f64p(0), Maximum: f64p(1)},
	}
	env := baseEnvelope(time.Now())
	env.Values["probability"] = dv(0.42)
	check(t, env, baseSpec(schema), "")
}

func TestPremiumSignal(t *testing.T) {
	schema := map[string]bundle.ValueField{
		"premium": {Type: "float64", Required: true,
			Minimum: f64p(0), Maximum: f64p(1_000_000)},
	}
	env := baseEnvelope(time.Now())
	env.Values["premium"] = dv(3210.50)
	check(t, env, baseSpec(schema), "")
}

func TestConsistencySignal(t *testing.T) {
	schema := map[string]bundle.ValueField{
		"consistency": {Type: "float64", Required: true,
			Minimum: f64p(0), Maximum: f64p(1)},
	}
	env := baseEnvelope(time.Now())
	env.Values["consistency"] = dv(0.91)
	check(t, env, baseSpec(schema), "")
}

func TestMissingRequiredField(t *testing.T) {
	schema := map[string]bundle.ValueField{
		"premium": {Type: "float64", Required: true,
			Minimum: f64p(0), Maximum: f64p(1_000_000)},
	}
	env := baseEnvelope(time.Now())
	check(t, env, baseSpec(schema), "VALUE")
}

func TestOutOfRange(t *testing.T) {
	schema := map[string]bundle.ValueField{
		"probability": {Type: "float64", Required: true,
			Minimum: f64p(0), Maximum: f64p(1)},
	}
	env := baseEnvelope(time.Now())
	env.Values["probability"] = dv(1.7)
	check(t, env, baseSpec(schema), "VALUE")
}

func TestWrongKind(t *testing.T) {
	schema := map[string]bundle.ValueField{
		"premium": {Type: "float64", Required: true,
			Minimum: f64p(0), Maximum: f64p(1_000_000)},
	}
	env := baseEnvelope(time.Now())
	env.Values["premium"] = &rtdpv1.TypedValue{
		Kind: &rtdpv1.TypedValue_StringValue{StringValue: "high"}}
	check(t, env, baseSpec(schema), "VALUE")
}

func TestOptionalFieldAbsent(t *testing.T) {
	schema := map[string]bundle.ValueField{
		"premium": {Type: "float64", Required: true,
			Minimum: f64p(0), Maximum: f64p(1_000_000)},
		"confidence": {Type: "float64", Required: false,
			Minimum: f64p(0), Maximum: f64p(1)},
	}
	env := baseEnvelope(time.Now())
	env.Values["premium"] = dv(100)
	check(t, env, baseSpec(schema), "")
}
