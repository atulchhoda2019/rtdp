// Ingress adapter: HTTP + tenant binding -> orchestrator Decide.
// Tenant identity comes from the authenticated caller fixture
// (X-RTDP-Client-Id -> tenant map), never from the request body.
package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/timestamppb"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
)

// Synthetic tenant fixture: authenticated client id -> authorized tenant.
// Real IdP integration is a later phase; this is the trusted boundary seam.
var clientTenants = map[string]string{
	"demo-client-a": "tenant_a",
	"demo-client-b": "tenant_b",
}

type decideReq struct {
	TransactionID       string         `json:"transaction_id"`
	TransactionRevision int64          `json:"transaction_revision"`
	EventType           string         `json:"event_type"`
	Channel             string         `json:"channel"`
	Region              string         `json:"region"`
	TokenizedClaimant   string         `json:"tokenized_claimant"`
	ProviderID          string         `json:"provider_id"`
	Currency            string         `json:"currency"`
	Amount              float64        `json:"amount"`
	EventTime           string         `json:"event_time"`
	Attributes          map[string]any `json:"attributes"`
}

// Scalar-only: nested structures would silently flatten into the CEL
// input and signal feature spaces.
func toTypedAttrs(in map[string]any) (map[string]*rtdpv1.TypedValue, error) {
	if len(in) == 0 {
		return nil, nil
	}
	out := make(map[string]*rtdpv1.TypedValue, len(in))
	for k, v := range in {
		switch t := v.(type) {
		case string:
			out[k] = &rtdpv1.TypedValue{
				Kind: &rtdpv1.TypedValue_StringValue{StringValue: t}}
		case float64:
			out[k] = &rtdpv1.TypedValue{
				Kind: &rtdpv1.TypedValue_DoubleValue{DoubleValue: t}}
		case bool:
			out[k] = &rtdpv1.TypedValue{
				Kind: &rtdpv1.TypedValue_BoolValue{BoolValue: t}}
		default:
			return nil, fmt.Errorf("attribute %q must be a scalar", k)
		}
	}
	return out, nil
}

//go:embed demo.html
var demoPage []byte

// decideTimeout is an upper bound only — the orchestrator enforces the
// product-level deadline internally (document_intake's SLM path needs up
// to 30s, decisioning ~100ms). Not a behavior contract.
var decideTimeout = func() time.Duration {
	if v, err := strconv.Atoi(os.Getenv("RTDP_DECIDE_TIMEOUT_MS")); err == nil && v > 0 {
		return time.Duration(v) * time.Millisecond
	}
	return 5 * time.Second
}()

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	conn, err := grpc.NewClient(envOr("RTDP_ORCHESTRATOR_ADDR", "localhost:50055"),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatal(err)
	}
	orch := rtdpv1.NewOrchestratorClient(conn)

	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/decide", func(w http.ResponseWriter, r *http.Request) {
		client := r.Header.Get("X-RTDP-Client-Id")
		tenant, ok := clientTenants[client]
		if !ok || client == "" {
			http.Error(w, `{"error":"unauthenticated or unauthorized tenant"}`,
				http.StatusUnauthorized)
			return
		}
		var req decideReq
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, `{"error":"bad json"}`, http.StatusBadRequest)
			return
		}
		if req.TransactionID == "" || req.TokenizedClaimant == "" ||
			req.ProviderID == "" || req.Currency == "" {
			http.Error(w, `{"error":"missing routing fields"}`,
				http.StatusBadRequest)
			return
		}
		et := time.Now().UTC()
		if req.EventTime != "" {
			if t, err := time.Parse(time.RFC3339, req.EventTime); err == nil {
				et = t
			}
		}
		attrs, err := toTypedAttrs(req.Attributes)
		if err != nil {
			http.Error(w, `{"error":"`+err.Error()+`"}`,
				http.StatusBadRequest)
			return
		}
		at := &rtdpv1.AuthenticatedTransaction{
			RequestId:           uuid.NewString(),
			TenantId:            tenant,
			Environment:         envOr("RTDP_ENV", "work"),
			Mode:                rtdpv1.Mode_MODE_LIVE,
			TransactionId:       req.TransactionID,
			TransactionRevision: req.TransactionRevision,
			EventType:           req.EventType,
			Channel:             req.Channel,
			Region:              req.Region,
			TokenizedClaimant:   req.TokenizedClaimant,
			ProviderId:          req.ProviderID,
			Currency:            req.Currency,
			Amount:              req.Amount,
			EventTime:           timestamppb.New(et),
			Attributes:          attrs,
			Traceparent:         r.Header.Get("traceparent"),
		}
		ctx, cancel := context.WithTimeout(r.Context(), decideTimeout)
		defer cancel()
		res, err := orch.Decide(ctx, at)
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadGateway)
			json.NewEncoder(w).Encode(map[string]any{"error": err.Error()})
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{
			"decision_id":    res.DecisionId,
			"outcome":        res.Outcome.String(),
			"reason_codes":   res.ReasonCodes,
			"bundle_digest":  res.BundleDigest,
			"manifest_epoch": res.ManifestEpoch,
			"action_intents": len(res.ActionIntents),
		})
	})
	// Demo page: same-origin /v1/decide calls, no CORS surface.
	// "{$}" is exact-match — a bare "GET /" subtree pattern conflicts
	// with the method-agnostic /metrics and /v1/decide registrations.
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(demoPage)
	})
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	})
	mux.Handle("/metrics", promhttp.Handler())

	addr := ":" + envOr("RTDP_INGRESS_PORT", "8080")
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("ingress on %s", addr)
	log.Fatal(http.Serve(ln, mux))
}
