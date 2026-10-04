// Agent Gateway: the one governed door for agent callers (ADR-015).
//
// Callers authenticate with an EdDSA delegation token (minted by
// agent-registry — callers never sign anything themselves; POST
// /v1/session proxies mint for callers that hold a principal+grant).
// Every tool call is verified against the chain (signature, expiry,
// tenant, depth, revocation — the same edge checks as ingress),
// authorised against the tool's required scope, rate-limited per agent,
// and recorded as a durable agent_call fact.
//
// Rules of the door: mutating tools route ONLY through /v1/decide —
// the gateway holds no backend write credentials. Read tools are
// served by the synthetic read-only backends in backends.go with
// scope-filtered fields. MCP tools/list is filtered by caller scope.
package main

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
	"github.com/twmb/franz-go/pkg/kgo"
	"go.yaml.in/yaml/v3"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	agentv1 "github.com/rtdp/rtdp/gen/go/rtdp/agent/v1"
	"github.com/rtdp/rtdp/internal/agentid"
	"github.com/rtdp/rtdp/internal/kafkax"
	"github.com/rtdp/rtdp/internal/redisx"
)

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func errJSON(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

// ---------------------------------------------------------------------
// Tool config (assets/seed/agent/gateway_tools.yaml)
// ---------------------------------------------------------------------

type Tool struct {
	Kind            string            `yaml:"kind"` // read|decide|proxy
	Backend         string            `yaml:"backend"`
	RequiredScope   string            `yaml:"required_scope"`
	DataClass       string            `yaml:"data_class"`
	Purpose         string            `yaml:"purpose"`
	EventType       string            `yaml:"event_type"`
	ProductAction   string            `yaml:"product_action"`
	ScopeVisibility map[string]string `yaml:"scope_visibility"`
	Fields          []string          `yaml:"fields"`
	FieldsEmployee  []string          `yaml:"fields_employee"`
	FieldsCustomer  []string          `yaml:"fields_customer"`
	ProxyEndpoint   string            `yaml:"proxy_endpoint"`
	HumanOnly       bool              `yaml:"human_only"`
	CallsPerMinute  int               `yaml:"calls_per_minute"`
	Concurrent      int               `yaml:"concurrent"`
	DeadlineMs      int               `yaml:"deadline_ms"`
	Description     string            `yaml:"description"`
}

type ToolsCfg struct {
	Defaults struct {
		CallsPerMinute int `yaml:"calls_per_minute"`
		Concurrent     int `yaml:"concurrent"`
		DeadlineMs     int `yaml:"deadline_ms"`
	} `yaml:"defaults"`
	Tools map[string]*Tool `yaml:"tools"`
}

// tenantClients maps the verified chain tenant to the client identity
// ingress trusts — tenant identity comes from the authenticated chain,
// never from a caller-supplied field.
var tenantClients = map[string]string{
	"tenant_a": "demo-client-a",
	"tenant_b": "demo-client-b",
}

// ---------------------------------------------------------------------
// JWKS cache (same shape as ingress)
// ---------------------------------------------------------------------

type jwksCache struct {
	addr string
	mu   sync.Mutex
	keys map[string]ed25519.PublicKey
	at   time.Time
}

func (j *jwksCache) get(kid string) (ed25519.PublicKey, bool) {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.keys == nil || time.Since(j.at) > 5*time.Minute {
		if err := j.refresh(); err != nil {
			log.Printf("jwks refresh: %v", err)
		}
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

// ---------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------

var (
	db      *pgxpool.Pool
	rdb     *redis.Client
	pub     *kgo.Client
	cfg     *ToolsCfg
	jwks    *jwksCache
	regAddr = envOr("RTDP_AGENT_REGISTRY_ADDR", "http://localhost:8090")
	ingAddr = envOr("RTDP_INGRESS_ADDR", "http://localhost:8080")
	apprURL = envOr("RTDP_APPROVAL_ADDR", "http://localhost:8095")
)

// caller is the verified identity for one request.
type caller struct {
	chain     *agentv1.DelegationChain
	token     string
	tenant    string
	principal string
	agentID   string
	version   string
	kind      string
	scopes    map[string]bool
	depth     int
}

func (c *caller) has(scope string) bool { return c.scopes[scope] }

// verify performs the full ADR-013 edge check on the bearer token and
// returns the caller identity. Tenant comes from the chain's principal.
func verify(r *http.Request) (*caller, error) {
	tok := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if tok == "" || tok == r.Header.Get("Authorization") {
		return nil, errors.New("Bearer delegation token required")
	}
	kid, err := agentid.KidOf(tok)
	if err != nil {
		return nil, errors.New("malformed delegation token")
	}
	pk, ok := jwks.get(kid)
	if !ok {
		return nil, errors.New("unknown signing key")
	}
	chain, _, err := agentid.VerifyJWS(tok, pk)
	if err != nil {
		return nil, errors.New("bad delegation signature")
	}
	tenant := chain.Principal.GetTenantId()
	if tenant == "" {
		return nil, errors.New("chain carries no tenant")
	}
	if err := agentid.ValidateChain(chain, tenant, time.Now()); err != nil {
		return nil, err
	}
	if d := agentid.MaxDepth(r.Context(), rdb, tenant); len(chain.Links) > d {
		return nil, fmt.Errorf("chain depth %d exceeds tenant max %d",
			len(chain.Links), d)
	}
	if rev, err := agentid.Revoked(r.Context(), rdb, chain); err != nil {
		return nil, fmt.Errorf("revocation check: %w", err)
	} else if rev {
		return nil, errors.New("delegation revoked")
	}
	c := &caller{chain: chain, token: tok, tenant: tenant,
		principal: chain.Principal.Id, depth: len(chain.Links),
		scopes: map[string]bool{}}
	if n := len(chain.Links); n > 0 {
		last := chain.Links[n-1]
		c.agentID, c.version, c.kind = last.AgentId, last.AgentVersion,
			last.AgentKind
		for _, s := range last.Scopes {
			c.scopes[s] = true
		}
	}
	return c, nil
}

// ---------------------------------------------------------------------
// agent_call fact (Postgres + Kafka — the pane reads this table alone)
// ---------------------------------------------------------------------

type callFact struct {
	callID      string
	taskID      string
	purpose     string
	tool        string
	backend     string
	outcome     string
	decisionID  string
	latencyMs   int64
	costUnits   int64
	classesRead []string
	detail      string
}

func recordFact(ctx context.Context, c *caller, f callFact) {
	ev := &agentv1.AgentCall{
		CallId: f.callID, At: timestamppb.Now(), TenantId: c.tenant,
		Environment: envOr("RTDP_ENV", "work"), TaskId: f.taskID,
		Purpose: f.purpose, PrincipalId: c.principal, AgentId: c.agentID,
		AgentVersion: c.version, ChainDepth: int32(c.depth),
		Tool: f.tool, Backend: f.backend, Outcome: f.outcome,
		DecisionId: f.decisionID, LatencyMs: f.latencyMs,
		CostUnits: f.costUnits, ClassesRead: f.classesRead,
		Detail: f.detail,
	}
	if len(f.detail) > 200 {
		ev.Detail = f.detail[:200]
	}
	b, err := proto.Marshal(ev)
	if err == nil {
		_ = pub.ProduceSync(ctx, &kgo.Record{
			Topic: kafkax.TopicAgentCalls,
			Key:   []byte(c.tenant + ":" + f.callID), Value: b}).FirstErr()
	}
	classes := f.classesRead
	if classes == nil {
		classes = []string{}
	}
	var did *string
	if f.decisionID != "" {
		did = &f.decisionID
	}
	_, err = db.Exec(ctx, `
		INSERT INTO agent_call
		  (call_id, tenant_id, environment, task_id, purpose,
		   principal_id, agent_id, agent_version, chain_depth, tool,
		   backend, outcome, decision_id, latency_ms, cost_units,
		   classes_read, detail)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)`,
		f.callID, c.tenant, ev.Environment, f.taskID, f.purpose,
		c.principal, c.agentID, c.version, c.depth, f.tool, f.backend,
		f.outcome, did, f.latencyMs, f.costUnits, classes, ev.Detail)
	if err != nil {
		log.Printf("agent_call insert: %v", err)
	}
}

// ---------------------------------------------------------------------
// Quotas (per-agent calls/minute + concurrent, Valkey)
// ---------------------------------------------------------------------

func quotaOK(ctx context.Context, c *caller, t *Tool) bool {
	perMin := t.CallsPerMinute
	if perMin <= 0 {
		perMin = cfg.Defaults.CallsPerMinute
	}
	key := fmt.Sprintf("rtdp:gw:q:{%s:%s}:%d", c.tenant, c.agentID,
		time.Now().Unix()/60)
	n, err := rdb.Incr(ctx, key).Result()
	if err == nil && n == 1 {
		rdb.Expire(ctx, key, 90*time.Second)
	}
	return err == nil && n <= int64(perMin)
}

func acquire(ctx context.Context, c *caller, t *Tool) func() {
	max := t.Concurrent
	if max <= 0 {
		max = cfg.Defaults.Concurrent
	}
	key := fmt.Sprintf("rtdp:gw:cc:{%s:%s}", c.tenant, c.agentID)
	n, err := rdb.Incr(ctx, key).Result()
	if err != nil || n > int64(max) {
		rdb.Decr(ctx, key)
		return nil
	}
	return func() { rdb.Decr(ctx, key) }
}

// ---------------------------------------------------------------------
// Decide routing — the ONLY path mutating tools can take
// ---------------------------------------------------------------------

func callDecide(ctx context.Context, c *caller, t *Tool,
	input map[string]any) (map[string]any, int) {
	client, ok := tenantClients[c.tenant]
	if !ok {
		return nil, 403
	}
	claimant, _ := input["claimant"].(string)
	if claimant == "" {
		claimant = c.principal
	}
	amount, _ := input["amount"].(float64)
	attrs, _ := input["attributes"].(map[string]any)
	if attrs == nil {
		attrs = map[string]any{}
	}
	if t.ProductAction != "" {
		attrs["requested_action"] = t.ProductAction
	}
	eventType := t.EventType
	if eventType == "" {
		eventType, _ = input["event_type"].(string)
	}
	if eventType == "" {
		return map[string]any{"error": "event_type required"}, 400
	}
	body := map[string]any{
		"transaction_id":       "gw_" + uuid.NewString(),
		"transaction_revision": 1,
		"event_type":           eventType,
		"channel":              "PORTAL",
		"region":               "us-east-1",
		"tokenized_claimant":   claimant,
		"provider_id":          "gw",
		"currency":             "USD",
		"amount":               amount,
		"event_time": time.Now().UTC().Format(
			"2006-01-02T15:04:05Z"),
		"attributes":       attrs,
		"delegation_token": c.token,
	}
	b, _ := json.Marshal(body)
	req, _ := http.NewRequestWithContext(ctx, "POST",
		ingAddr+"/v1/decide", strings.NewReader(string(b)))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-RTDP-Client-Id", client)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return map[string]any{"error": err.Error()}, 502
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return out, res.StatusCode
}

// ---------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------

type toolReq struct {
	TaskID  string         `json:"task_id"`
	Purpose string         `json:"purpose"`
	Input   map[string]any `json:"input"`
}

func decideOutcome(r map[string]any, status int) (outcome, decisionID string) {
	did, _ := r["decision_id"].(string)
	out, _ := r["outcome"].(string)
	switch {
	case status == 401 || out == "DECISION_DECLINE_UNAUTHORIZED":
		return "DENIED", did
	case out == "DECISION_PENDING_APPROVAL":
		return "PENDING_APPROVAL", did
	case strings.HasPrefix(out, "DECISION_PROPOSE"):
		return "PROPOSED", did
	case strings.HasPrefix(out, "DECISION_"):
		return "OK", did
	default:
		return "ERROR", did
	}
}

// handleTool is the one door. Verification, scope, quota, dispatch,
// and the durable agent_call fact — in that order, always.
func handleTool(name string, c *caller, req toolReq) (int, map[string]any) {
	started := time.Now()
	callID := uuid.NewString()
	fact := callFact{callID: callID, taskID: req.TaskID,
		purpose: req.Purpose, tool: name}
	finish := func(code int, out map[string]any) (int, map[string]any) {
		fact.latencyMs = time.Since(started).Milliseconds()
		fact.costUnits = 1
		recordFact(context.Background(), c, fact)
		return code, out
	}

	t, ok := cfg.Tools[name]
	if !ok {
		fact.outcome, fact.detail = "DENIED", "unknown tool"
		fact.backend = "none"
		return finish(404, map[string]any{"error": "unknown tool"})
	}
	fact.backend = t.Backend
	if t.RequiredScope != "" && !c.has(t.RequiredScope) {
		fact.outcome, fact.detail = "DENIED",
			"scope "+t.RequiredScope+" required"
		return finish(403, map[string]any{
			"error": "scope " + t.RequiredScope + " required"})
	}
	if t.HumanOnly && c.chain.Principal.Kind != agentv1.Principal_PERSON {
		fact.outcome, fact.detail = "DENIED", "human identities only"
		return finish(403, map[string]any{
			"error": "human identities only"})
	}
	if !quotaOK(context.Background(), c, t) {
		fact.outcome, fact.detail = "THROTTLED", "calls/min quota"
		return finish(429, map[string]any{
			"error": "quota exceeded", "retry_after_s": 60})
	}
	release := acquire(context.Background(), c, t)
	if release == nil {
		fact.outcome, fact.detail = "THROTTLED", "concurrency quota"
		return finish(429, map[string]any{
			"error": "too many concurrent calls"})
	}
	defer release()

	deadline := t.DeadlineMs
	if deadline <= 0 {
		deadline = cfg.Defaults.DeadlineMs
	}
	ctx, cancel := context.WithTimeout(context.Background(),
		time.Duration(deadline)*time.Millisecond)
	defer cancel()

	switch t.Kind {
	case "read":
		rows, classes, err := readBackend(ctx, t, c, req.Input)
		if err != nil {
			fact.outcome, fact.detail = "DENIED", err.Error()
			return finish(403, map[string]any{"error": err.Error()})
		}
		fact.outcome, fact.classesRead = "OK", classes
		return finish(200, map[string]any{
			"tool": name, "data_class": t.DataClass,
			"purpose": t.Purpose, "task_id": req.TaskID,
			"result": rows})
	case "decide":
		res, code := callDecide(ctx, c, t, req.Input)
		fact.outcome, fact.decisionID = decideOutcome(res, code)
		if fact.outcome == "ERROR" {
			fact.detail, _ = res["error"].(string)
		}
		return finish(code, res)
	case "proxy":
		res, code := callProxy(ctx, c, t, req.Input)
		if code >= 400 {
			fact.outcome = "DENIED"
			fact.detail, _ = res["error"].(string)
		} else {
			fact.outcome = "OK"
		}
		return finish(code, res)
	default:
		fact.outcome, fact.detail = "DENIED", "bad tool kind"
		return finish(500, map[string]any{"error": "bad tool kind"})
	}
}

// callProxy forwards to a governed internal service (approvals). The
// gateway adds no privileges — the callee enforces its own rules.
func callProxy(ctx context.Context, c *caller, t *Tool,
	input map[string]any) (map[string]any, int) {
	ep := t.ProxyEndpoint
	method, path, _ := strings.Cut(ep, " ")
	for k, v := range input {
		if s, ok := v.(string); ok {
			path = strings.ReplaceAll(path, "{"+k+"}", s)
		}
	}
	url := apprURL + path
	if method == "GET" {
		url += "?tenant_id=" + c.tenant
	}
	var body io.Reader
	if method == "POST" {
		b, _ := json.Marshal(input)
		body = strings.NewReader(string(b))
	}
	req, _ := http.NewRequestWithContext(ctx, method, url, body)
	req.Header.Set("Content-Type", "application/json")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return map[string]any{"error": err.Error()}, 502
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return out, res.StatusCode
}

// ---------------------------------------------------------------------
// MCP (streamable HTTP): initialize, tools/list (scope-filtered),
// tools/call — same door, same checks.
// ---------------------------------------------------------------------

type rpcReq struct {
	ID     any            `json:"id"`
	Method string         `json:"method"`
	Params map[string]any `json:"params"`
}

func visibleTools(c *caller) []map[string]any {
	var out []map[string]any
	names := make([]string, 0, len(cfg.Tools))
	for n := range cfg.Tools {
		names = append(names, n)
	}
	sort.Strings(names) // stable order for tests/UI
	for _, n := range names {
		t := cfg.Tools[n]
		if t.RequiredScope != "" && !c.has(t.RequiredScope) {
			continue
		}
		out = append(out, map[string]any{
			"name":        n,
			"description": t.Description,
			"inputSchema": map[string]any{"type": "object"},
		})
	}
	return out
}

func handleMCP(w http.ResponseWriter, r *http.Request) {
	c, err := verify(r)
	if err != nil {
		errJSON(w, 401, err.Error())
		return
	}
	var req rpcReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		errJSON(w, 400, "bad json-rpc")
		return
	}
	reply := func(result any) {
		writeJSON(w, 200, map[string]any{
			"jsonrpc": "2.0", "id": req.ID, "result": result})
	}
	switch req.Method {
	case "initialize":
		reply(map[string]any{
			"protocolVersion": "2025-03-26",
			"serverInfo": map[string]any{"name": "rtdp-agent-gateway",
				"version": "1.0.0"},
			"capabilities": map[string]any{"tools": map[string]any{}},
		})
	case "tools/list":
		reply(map[string]any{"tools": visibleTools(c)})
	case "tools/call":
		name, _ := req.Params["name"].(string)
		args, _ := req.Params["arguments"].(map[string]any)
		code, out := handleTool(name, c, toolReq{
			TaskID:  fmt.Sprint(args["task_id"]),
			Purpose: fmt.Sprint(args["purpose"]),
			Input:   args})
		text, _ := json.Marshal(out)
		reply(map[string]any{
			"content": []map[string]any{{
				"type": "text", "text": string(text)}},
			"isError": code >= 400,
		})
	default:
		reply(map[string]any{})
	}
}

// ---------------------------------------------------------------------

func main() {
	var err error
	raw, err := os.ReadFile(envOr("RTDP_GATEWAY_TOOLS",
		"assets/seed/agent/gateway_tools.yaml"))
	if err != nil {
		log.Fatalf("tools config: %v", err)
	}
	cfg = &ToolsCfg{}
	if err := yaml.Unmarshal(raw, cfg); err != nil {
		log.Fatalf("tools config parse: %v", err)
	}
	db, err = pgxpool.New(context.Background(), envOr("RTDP_POSTGRES_DSN",
		"postgres://rtdp:rtdp@localhost:5432/rtdp?sslmode=disable"))
	if err != nil {
		log.Fatalf("postgres: %v", err)
	}
	rdb = redisx.New()
	pub, err = kafkax.NewProducer()
	if err != nil {
		log.Fatalf("producer: %v", err)
	}
	jwks = &jwksCache{addr: regAddr}

	mux := http.NewServeMux()

	// Callers never sign — the gateway brokers minting through the
	// registry (grant membership is enforced there, not here).
	mux.HandleFunc("POST /v1/session", func(w http.ResponseWriter,
		r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		res, err := http.Post(regAddr+"/v1/delegations/mint",
			"application/json", strings.NewReader(string(body)))
		if err != nil {
			errJSON(w, 502, err.Error())
			return
		}
		defer res.Body.Close()
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(res.StatusCode)
		io.Copy(w, res.Body)
	})

	mux.HandleFunc("GET /v1/tools", func(w http.ResponseWriter,
		r *http.Request) {
		c, err := verify(r)
		if err != nil {
			errJSON(w, 401, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{"tools": visibleTools(c)})
	})

	mux.HandleFunc("POST /v1/tools/{tool}", func(w http.ResponseWriter,
		r *http.Request) {
		name := r.PathValue("tool") // e.g. pms.room.assign
		c, err := verify(r)
		if err != nil {
			errJSON(w, 401, err.Error())
			return
		}
		var req toolReq
		_ = json.NewDecoder(r.Body).Decode(&req)
		code, out := handleTool(name, c, req)
		writeJSON(w, code, out)
	})

	mux.HandleFunc("POST /mcp", handleMCP)

	mux.HandleFunc("GET /v1/calls", func(w http.ResponseWriter,
		r *http.Request) {
		c, err := verify(r)
		if err != nil {
			errJSON(w, 401, err.Error())
			return
		}
		rows, err := db.Query(r.Context(), `
			SELECT call_id, tool, backend, outcome, agent_id,
			       principal_id, decision_id, at
			FROM agent_call WHERE tenant_id=$1
			ORDER BY at DESC LIMIT 100`, c.tenant)
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		var out []map[string]any
		for rows.Next() {
			var id, tool, be, oc, ag, pr, at string
			var did *string
			if err := rows.Scan(&id, &tool, &be, &oc, &ag, &pr,
				&did, &at); err == nil {
				m := map[string]any{"call_id": id, "tool": tool,
					"backend": be, "outcome": oc, "agent_id": ag,
					"principal_id": pr, "at": at}
				if did != nil {
					m["decision_id"] = *did
				}
				out = append(out, m)
			}
		}
		rows.Close()
		writeJSON(w, 200, map[string]any{"calls": out})
	})

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter,
		_ *http.Request) {
		w.WriteHeader(200)
	})

	addr := ":" + envOr("RTDP_GATEWAY_PORT", "8096")
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("agent-gateway on %s", addr)
	log.Fatal(http.Serve(ln, mux))
}
