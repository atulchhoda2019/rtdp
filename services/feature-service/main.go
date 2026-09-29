// Feature Service: atomic Tier 1 update + versioned Tier 2 reads.
// Owns no rule decisions and publishes no duplicate contributions
// (design.md service responsibilities).
package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/redisx"
	"github.com/rtdp/rtdp/internal/store"
)

var (
	reqs = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "rtdp_feature_requests_total",
		Help: "ResolveFeatures calls by tier outcome",
	}, []string{"result"})
	lat = promauto.NewHistogram(prometheus.HistogramOpts{
		Name:    "rtdp_feature_latency_seconds",
		Buckets: prometheus.ExponentialBuckets(0.0005, 2, 12),
	})
)

type server struct {
	rtdpv1.UnimplementedFeatureServiceServer
	t1 *store.Tier1
	t2 *store.Tier2
}

func (s *server) ResolveFeatures(ctx context.Context,
	req *rtdpv1.ResolveFeaturesRequest) (*rtdpv1.ResolveFeaturesResponse, error) {
	start := time.Now()
	defer func() { lat.Observe(time.Since(start).Seconds()) }()

	resp := &rtdpv1.ResolveFeaturesResponse{
		Features: map[string]*rtdpv1.TypedValue{},
	}
	mode := req.GetMode().String()
	if len(mode) > 5 {
		mode = mode[5:] // strip MODE_ prefix
	}
	liveOnly := req.GetMode() == rtdpv1.Mode_MODE_LIVE

	// Tier 1: one atomic dedup+update+vector per entity.
	var t1v *store.Vector
	eventID := fmt.Sprintf("%s:%s:%d", req.TenantId, req.TransactionId,
		req.TransactionRevision)
	digest := fmt.Sprintf("%x", sha256.Sum256([]byte(fmt.Sprintf(
		"%s|%s|%s|%.6f|%d", req.TokenizedClaimant, req.ProviderId,
		req.Currency,
		req.Amount, req.EventTime.GetSeconds()))))
	needT1 := false
	for _, f := range req.RequiredFeatures {
		if f.Tier == "tier1" {
			needT1 = true
		}
	}
	if needT1 {
		v, err := s.t1.Apply(ctx, req.TenantId, req.Environment, mode,
			liveOnly, req.TokenizedClaimant, req.Currency, eventID, digest,
			req.EventTime.AsTime(), req.Amount)
		if err != nil {
			reqs.WithLabelValues("tier1_error").Inc()
			return nil, fmt.Errorf("tier1: %w", err)
		}
		t1v = v
	}

	now := time.Now()
	for _, f := range req.RequiredFeatures {
		switch {
		case f.Tier == "tier1" && f.Name == "claimant_claim_count_1h" && t1v != nil:
			resp.Features[f.Name] = &rtdpv1.TypedValue{
				Kind: &rtdpv1.TypedValue_IntValue{IntValue: t1v.ClaimantClaimCount1h}}
		case f.Tier == "tier1" && f.Name == "claimant_amount_sum_24h" && t1v != nil:
			resp.Features[f.Name] = &rtdpv1.TypedValue{
				Kind: &rtdpv1.TypedValue_DoubleValue{DoubleValue: t1v.ClaimantAmountSum24h}}
		case f.Tier == "tier2":
			entity := req.ProviderId
			val, _, err := s.t2.Window(ctx, req.TenantId, mode, f.Name,
				int(f.Version), entity, req.Currency, time.Hour, now)
			if err != nil {
				resp.MissingFeatures = append(resp.MissingFeatures, f.Name)
				continue
			}
			resp.Features[f.Name] = &rtdpv1.TypedValue{
				Kind: &rtdpv1.TypedValue_DoubleValue{DoubleValue: val}}
		}
	}
	b, _ := json.Marshal(resp.Features)
	resp.SnapshotDigest = fmt.Sprintf("sha256:%x", sha256.Sum256(b))
	reqs.WithLabelValues("ok").Inc()
	return resp, nil
}

func main() {
	rdb := redisx.New()
	s := &server{t1: store.NewTier1(rdb), t2: store.NewTier2(rdb)}

	go func() {
		http.Handle("/metrics", promhttp.Handler())
		http.ListenAndServe(":9090", nil)
	}()

	lis, err := net.Listen("tcp", ":"+envOr("RTDP_FEATURE_PORT", "50052"))
	if err != nil {
		log.Fatal(err)
	}
	gs := grpc.NewServer()
	rtdpv1.RegisterFeatureServiceServer(gs, s)
	log.Printf("feature-service on :%s", envOr("RTDP_FEATURE_PORT", "50052"))
	log.Fatal(gs.Serve(lis))
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
