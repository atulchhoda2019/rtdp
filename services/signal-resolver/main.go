// Signal Resolver: provider abstraction + envelope validation + correlation.
// Verifies tenant/mode/revision/context/binding/contract/expiry — matching
// only transaction_id is insufficient (design.md).
package main

import (
	"context"
	"log"
	"net"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/bundle"
	"github.com/rtdp/rtdp/internal/envelope"
)

var resolved = promauto.NewCounterVec(prometheus.CounterOpts{
	Name: "rtdp_signals_resolved_total",
}, []string{"result"})

// endpointCatalog is administrator-approved: tenant config cannot introduce
// arbitrary URLs.
var endpointCatalog = map[string]string{
	"inference-local": envOr("RTDP_INFERENCE_ADDR", "localhost:50051"),
}

type server struct {
	rtdpv1.UnimplementedSignalResolverServer
	mu      sync.Mutex
	clients map[string]rtdpv1.InferenceServiceClient
}

func (s *server) inferenceFor(endpointRef string) (rtdpv1.InferenceServiceClient, error) {
	addr, ok := endpointCatalog[endpointRef]
	if !ok {
		return nil, &envelope.Reject{Code: "ENDPOINT",
			Detail: "endpoint_ref not in approved catalog"}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if c, ok := s.clients[addr]; ok {
		return c, nil
	}
	conn, err := grpc.NewClient(addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	c := rtdpv1.NewInferenceServiceClient(conn)
	s.clients[addr] = c
	return c, nil
}

func (s *server) ResolveSignals(ctx context.Context,
	req *rtdpv1.ResolveSignalsRequest) (*rtdpv1.ResolveSignalsResponse, error) {
	resp := &rtdpv1.ResolveSignalsResponse{}
	var wg sync.WaitGroup
	out := make([]*rtdpv1.ResolvedSignal, len(req.Specs))
	now := time.Now()

	for i, spec := range req.Specs {
		i, spec := i, spec
		wg.Add(1)
		go func() {
			defer wg.Done()
			rs := &rtdpv1.ResolvedSignal{Alias: spec.Alias}
			out[i] = rs

			if spec.Provider != "grpc_inference" {
				rs.RejectCode = "PROVIDER_UNSUPPORTED"
				resolved.WithLabelValues("unsupported").Inc()
				return
			}
			client, err := s.inferenceFor(spec.EndpointRef)
			if err != nil {
				rs.RejectCode = "ENDPOINT"
				resolved.WithLabelValues("reject").Inc()
				return
			}
			deadline := spec.TimeoutMs
			if req.DeadlineMs > 0 && req.DeadlineMs < deadline {
				deadline = req.DeadlineMs
			}
			sctx, cancel := context.WithTimeout(ctx,
				time.Duration(deadline)*time.Millisecond)
			defer cancel()

			modelID, modelVer := splitRef(spec.Model)
			bindID, bindVer := splitRefI(spec.Binding)
			sr, err := client.Score(sctx, &rtdpv1.ScoreRequest{
				TenantId:            req.TenantId,
				Environment:         req.Environment,
				Mode:                req.Mode,
				TransactionId:       req.TransactionId,
				TransactionRevision: req.TransactionRevision,
				DecisionContextId:   req.DecisionContextId,
				BindingId:           bindID,
				BindingVersion:      bindVer,
				ModelId:             modelID,
				ModelVersion:        modelVer,
				ModelDigest:         spec.ModelDigest,
				PreprocessingDigest: spec.PreprocessingDigest,
				InputSnapshotDigest: req.InputSnapshotDigest,
				FeatureNames:        req.FeatureNames,
				FeatureValues:       req.FeatureValues,
				EventTime:           req.EventTime,
				DeadlineMs:          deadline,
				Traceparent:         req.Traceparent,
			})
			if err != nil {
				rs.RejectCode = "TIMED_OUT"
				resolved.WithLabelValues("timeout").Inc()
				return
			}
			rs.Envelope = sr.Envelope
			sig := bundle.Signal{
				Alias:             spec.Alias,
				Contract:          spec.Contract,
				AcceptedContracts: spec.AcceptedContracts,
				ContractDigest:    spec.ContractDigest,
				Binding:           spec.Binding,
				ModelDigest:       spec.ModelDigest,
			}
			if err := envelope.Validate(sr.Envelope, sig, req.TenantId,
				req.Environment, req.Mode, req.TransactionId,
				req.TransactionRevision, req.DecisionContextId, now); err != nil {
				if r, ok := err.(*envelope.Reject); ok {
					rs.RejectCode = r.Code
				} else {
					rs.RejectCode = "INVALID"
				}
				resolved.WithLabelValues("reject").Inc()
				return
			}
			rs.Ok = sr.Envelope.Status == rtdpv1.SignalStatus_SIGNAL_STATUS_OK
			resolved.WithLabelValues("ok").Inc()
		}()
	}
	wg.Wait()
	resp.Signals = out
	return resp, nil
}

func splitRef(ref string) (string, string) {
	id, _, v := "", "", ""
	for i := len(ref) - 1; i >= 0; i-- {
		if ref[i] == '@' {
			id, v = ref[:i], ref[i+1:]
			break
		}
	}
	return id, v
}

func splitRefI(ref string) (string, int64) {
	id, v := splitRef(ref)
	var n int64
	for _, c := range v {
		if c >= '0' && c <= '9' {
			n = n*10 + int64(c-'0')
		}
	}
	return id, n
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	s := &server{clients: map[string]rtdpv1.InferenceServiceClient{}}
	go func() {
		http.Handle("/metrics", promhttp.Handler())
		http.ListenAndServe(":9090", nil)
	}()
	lis, err := net.Listen("tcp", ":"+envOr("RTDP_RESOLVER_PORT", "50054"))
	if err != nil {
		log.Fatal(err)
	}
	gs := grpc.NewServer()
	rtdpv1.RegisterSignalResolverServer(gs, s)
	log.Printf("signal-resolver on :%s", envOr("RTDP_RESOLVER_PORT", "50054"))
	log.Fatal(gs.Serve(lis))
}
