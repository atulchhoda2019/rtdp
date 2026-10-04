// Ingress adapter: HTTP + tenant binding -> orchestrator Decide.
// Tenant identity comes from the authenticated caller fixture
// (X-RTDP-Client-Id -> tenant map), never from the request body.
package main

import (
	"context"
	"crypto/ed25519"
	_ "embed"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/types/known/timestamppb"

	agentv1 "github.com/rtdp/rtdp/gen/go/rtdp/agent/v1"
	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/agentid"
	"github.com/rtdp/rtdp/internal/redisx"
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
	// ADR-013: compact EdDSA JWS minted by agent-registry; carries the
	// principal→agent(s) chain. Absent = legacy service caller.
	DelegationToken string `json:"delegation_token"`
}

// jwksCache holds registry public keys by kid; refreshed lazily on miss
// and periodically. Single-flight per refresh — delegation traffic is
// low-volume relative to decide throughput.
type jwksCache struct {
	mu   sync.Mutex
	addr string
	keys map[string]ed25519.PublicKey
	at   time.Time
}

func (j *jwksCache) get(kid string) (ed25519.PublicKey, bool) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if k, ok := j.keys[kid]; ok && time.Since(j.at) < 60*time.Second {
		return k, true
	}
	if err := j.refresh(); err != nil {
		log.Printf("jwks refresh: %v", err)
	}
	k, ok := j.keys[kid]
	return k, ok
}

func (j *jwksCache) refresh() error {
	res, err := http.Get(j.addr + "/v1/jwks")
	if err != nil {
		return err
	}
	defer res.Body.Close()
	var body struct {
		Keys []struct {
			Kid string `json:"kid"`
			X   string `json:"x"`
		} `json:"keys"`
	}
	if err := json.NewDecoder(res.Body).Decode(&body); err != nil {
		return err
	}
	for _, k := range body.Keys {
		raw, err := base64.RawURLEncoding.DecodeString(k.X)
		if err != nil || len(raw) != ed25519.PublicKeySize {
			continue
		}
		if j.keys == nil {
			j.keys = map[string]ed25519.PublicKey{}
		}
		j.keys[k.Kid] = ed25519.PublicKey(raw)
	}
	j.at = time.Now()
	return nil
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

// verifyDelegation runs the ADR-013 edge checks: JWS signature against
// the registry's published key, structural validation (expiry windows,
// tenant match, non-empty scopes), delegation depth against the
// tenant ceiling, and the Redis revocation set. Edge rejections return
// DECLINE_UNAUTHORIZED to the caller; only a fully verified chain is
// attached to the envelope.
func verifyDelegation(ctx context.Context, tok, tenant string,
	jwks *jwksCache, rdb *redis.Client) (*agentv1.DelegationChain, error) {
	kid, err := agentid.KidOf(tok)
	if err != nil {
		return nil, err
	}
	pub, ok := jwks.get(kid)
	if !ok {
		return nil, fmt.Errorf("unknown signing key %s", kid)
	}
	chain, _, err := agentid.VerifyJWS(tok, pub)
	if err != nil {
		return nil, err
	}
	if err := agentid.ValidateChain(chain, tenant, time.Now()); err != nil {
		return nil, err
	}
	if d := agentid.MaxDepth(ctx, rdb, tenant); len(chain.Links) > d {
		return nil, fmt.Errorf("chain depth %d exceeds tenant max %d",
			len(chain.Links), d)
	}
	rev, err := agentid.Revoked(ctx, rdb, chain)
	if err != nil {
		return nil, err
	}
	if rev {
		return nil, fmt.Errorf("credential or grant revoked")
	}
	return chain, nil
}

func main() {
	conn, err := grpc.NewClient(envOr("RTDP_ORCHESTRATOR_ADDR", "localhost:50055"),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatal(err)
	}
	orch := rtdpv1.NewOrchestratorClient(conn)
	rdb := redisx.New()
	jwks := &jwksCache{addr: envOr("RTDP_AGENT_REGISTRY_ADDR",
		"http://localhost:8090")}

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
		var chain *agentv1.DelegationChain
		if req.DelegationToken != "" {
			var err error
			chain, err = verifyDelegation(r.Context(), req.DelegationToken,
				tenant, jwks, rdb)
			if err != nil {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusUnauthorized)
				json.NewEncoder(w).Encode(map[string]any{
					"outcome": "DECISION_DECLINE_UNAUTHORIZED",
					"reason":  err.Error(),
				})
				return
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
			Delegation:          chain,
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
		resp := map[string]any{
			"decision_id":    res.DecisionId,
			"outcome":        res.Outcome.String(),
			"reason_codes":   res.ReasonCodes,
			"bundle_digest":  res.BundleDigest,
			"manifest_epoch": res.ManifestEpoch,
			"action_intents": len(res.ActionIntents),
		}
		if res.Delegation != nil && res.Delegation.Principal != nil {
			links := []map[string]any{}
			for _, l := range res.Delegation.Links {
				links = append(links, map[string]any{
					"agent_id":      l.AgentId,
					"agent_version": l.AgentVersion,
					"agent_kind":    l.AgentKind,
					"scopes":        l.Scopes,
					"grant_id":      l.GrantId,
				})
			}
			resp["delegation"] = map[string]any{
				"principal": map[string]any{
					"kind": res.Delegation.Principal.Kind.String(),
					"id":   res.Delegation.Principal.Id},
				"links": links}
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(resp)
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
