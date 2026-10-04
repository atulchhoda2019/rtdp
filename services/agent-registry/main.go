// Agent registry (ADR-013/019): registered agents, their scoped
// credentials, the grants principals delegate, and the revocation feed.
// Mints signed delegation chains (compact EdDSA JWS) consumed by ingress.
// Synthetic sandbox service — no real identities.
package main

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/protobuf/types/known/timestamppb"

	agentv1 "github.com/rtdp/rtdp/gen/go/rtdp/agent/v1"
	"github.com/rtdp/rtdp/internal/agentid"
	"github.com/rtdp/rtdp/internal/redisx"
)

var db *pgxpool.Pool

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

type keyRing struct {
	kid  string
	pub  ed25519.PublicKey
	priv ed25519.PrivateKey
}

// loadOrCreateKey returns the active signing key, minting and persisting
// one if absent. Sandbox stores private material in Postgres; AWS mints
// under KMS (ADR-013).
func loadOrCreateKey(ctx context.Context) (*keyRing, error) {
	kr := &keyRing{}
	err := db.QueryRow(ctx,
		`SELECT kid, public_key, private_key FROM agent_key
		 WHERE retired_at IS NULL ORDER BY created_at DESC LIMIT 1`).
		Scan(&kr.kid, &kr.pub, &kr.priv)
	if err == nil {
		return kr, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	kid, pub, priv, err := agentid.GenerateKey()
	if err != nil {
		return nil, err
	}
	if _, err := db.Exec(ctx,
		`INSERT INTO agent_key (kid, public_key, private_key) VALUES ($1,$2,$3)`,
		kid, pub, priv); err != nil {
		return nil, err
	}
	log.Printf("agent-registry: minted signing key %s", kid)
	return &keyRing{kid: kid, pub: pub, priv: priv}, nil
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("content-type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func errJSON(w http.ResponseWriter, code int, msg string) {
	writeJSON(w, code, map[string]string{"error": msg})
}

var scopesOverlap = func(grant, allowed []string) bool {
	set := map[string]bool{}
	for _, a := range allowed {
		set[a] = true
	}
	for _, s := range grant {
		if !set[s] {
			return false
		}
	}
	return true
}

func main() {
	ctx := context.Background()
	var err error
	db, err = pgxpool.New(ctx, envOr("RTDP_POSTGRES_DSN",
		"postgres://rtdp:rtdp@localhost:5432/rtdp?sslmode=disable"))
	if err != nil {
		log.Fatalf("postgres: %v", err)
	}
	rdb := redisx.New()
	// The 002_agent schema may not be applied yet on first boot (seed /
	// migration job ordering) — retry instead of crash-looping.
	var kr *keyRing
	for i := 0; i < 60; i++ {
		kr, err = loadOrCreateKey(ctx)
		if err == nil {
			break
		}
		if i == 59 {
			log.Fatalf("signing key: %v", err)
		}
		time.Sleep(2 * time.Second)
	}

	mux := http.NewServeMux()

	// --- agents -------------------------------------------------------------
	type agentReq struct {
		AgentID       string   `json:"agent_id"`
		TenantID      string   `json:"tenant_id"`
		Kind          string   `json:"kind"`
		OwnerRole     string   `json:"owner_role"`
		OwnerIdentity string   `json:"owner_identity"`
		MaxAutonomy   string   `json:"max_autonomy"`
		AllowedScopes []string `json:"allowed_scopes"`
	}
	mux.HandleFunc("POST /v1/agents", func(w http.ResponseWriter, r *http.Request) {
		var a agentReq
		if err := json.NewDecoder(r.Body).Decode(&a); err != nil ||
			a.AgentID == "" || a.TenantID == "" || a.Kind == "" {
			errJSON(w, 400, "bad agent")
			return
		}
		if a.MaxAutonomy == "" {
			a.MaxAutonomy = "T1"
		}
		_, err := db.Exec(r.Context(),
			`INSERT INTO agent (agent_id, tenant_id, kind, owner_role,
			   owner_identity, max_autonomy, allowed_scopes)
			 VALUES ($1,$2,$3,$4,$5,$6,$7)
			 ON CONFLICT (agent_id) DO UPDATE SET
			   kind=EXCLUDED.kind, owner_role=EXCLUDED.owner_role,
			   owner_identity=EXCLUDED.owner_identity,
			   max_autonomy=EXCLUDED.max_autonomy,
			   allowed_scopes=EXCLUDED.allowed_scopes, status='ACTIVE'`,
			a.AgentID, a.TenantID, a.Kind, a.OwnerRole, a.OwnerIdentity,
			a.MaxAutonomy, a.AllowedScopes)
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		writeJSON(w, 201, map[string]string{"agent_id": a.AgentID, "status": "ACTIVE"})
	})

	mux.HandleFunc("GET /v1/agents", func(w http.ResponseWriter, r *http.Request) {
		rows, err := db.Query(r.Context(),
			`SELECT agent_id, tenant_id, kind, status, max_autonomy,
			        allowed_scopes, owner_role
			 FROM agent ORDER BY agent_id`)
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		defer rows.Close()
		out := []map[string]any{}
		for rows.Next() {
			var id, t, k, s, m, role string
			var sc []string
			if err := rows.Scan(&id, &t, &k, &s, &m, &sc, &role); err == nil {
				out = append(out, map[string]any{
					"agent_id": id, "tenant_id": t, "kind": k, "status": s,
					"max_autonomy": m, "allowed_scopes": sc, "owner_role": role})
			}
		}
		writeJSON(w, 200, map[string]any{"agents": out})
	})

	// Suspend/revoke an agent: all its credentials enter the revocation set
	// so the next request carrying them is denied (G14c uses this too).
	mux.HandleFunc("POST /v1/agents/{id}/status", func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		var body struct {
			Status string `json:"status"`
			Reason string `json:"reason"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil ||
			(body.Status != "SUSPENDED" && body.Status != "REVOKED" &&
				body.Status != "ACTIVE") {
			errJSON(w, 400, "status must be ACTIVE|SUSPENDED|REVOKED")
			return
		}
		res, err := db.Exec(r.Context(),
			`UPDATE agent SET status=$2 WHERE agent_id=$1`, id, body.Status)
		if err != nil || res.RowsAffected() == 0 {
			errJSON(w, 404, "unknown agent")
			return
		}
		if body.Status != "ACTIVE" {
			var targets []string
			rows, qerr := db.Query(r.Context(),
				`SELECT cred_id FROM agent_credential WHERE agent_id=$1
				   AND revoked_at IS NULL
				 UNION
				 SELECT grant_id FROM agent_grant WHERE agent_id=$1
				   AND revoked_at IS NULL`, id)
			if qerr == nil {
				for rows.Next() {
					var k string
					if rows.Scan(&k) == nil {
						targets = append(targets, k)
					}
				}
				rows.Close()
				for _, k := range targets {
					_, _ = db.Exec(r.Context(),
						`INSERT INTO agent_revocation (target, reason)
						 VALUES ($1,$2) ON CONFLICT (target) DO NOTHING`,
						k, "agent "+body.Status+": "+body.Reason)
				}
				if len(targets) > 0 {
					members := make([]interface{}, len(targets))
					for i, k := range targets {
						members[i] = k
					}
					_ = rdb.SAdd(r.Context(), agentid.RevocationSetKey, members...).Err()
				}
			}
		}
		writeJSON(w, 200, map[string]string{"agent_id": id, "status": body.Status})
	})

	// --- grants -------------------------------------------------------------
	type grantReq struct {
		PrincipalID string   `json:"principal_id"`
		AgentID     string   `json:"agent_id"`
		Scopes      []string `json:"scopes"`
		Purpose     string   `json:"purpose"`
		TTLHours    int      `json:"ttl_hours"`
	}
	mux.HandleFunc("GET /v1/grants", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		var clauses []string
		var args []interface{}
		if v := q.Get("principal_id"); v != "" {
			args = append(args, v)
			clauses = append(clauses, fmt.Sprintf("principal_id=$%d", len(args)))
		}
		if v := q.Get("agent_id"); v != "" {
			args = append(args, v)
			clauses = append(clauses, fmt.Sprintf("agent_id=$%d", len(args)))
		}
		where := ""
		if len(clauses) > 0 {
			where = " WHERE " + strings.Join(clauses, " AND ")
		}
		rows, err := db.Query(r.Context(),
			`SELECT grant_id, principal_id, agent_id, scopes, purpose,
				not_after, revoked_at
			 FROM agent_grant`+where+` ORDER BY grant_id`, args...)
		if err != nil {
			errJSON(w, http.StatusInternalServerError, err.Error())
			return
		}
		var out []map[string]interface{}
		for rows.Next() {
			var gid, pid, aid, purpose string
			var scopes []string
			var nat time.Time
			var rat *time.Time
			if rows.Scan(&gid, &pid, &aid, &scopes, &purpose, &nat,
				&rat) != nil {
				continue
			}
			out = append(out, map[string]interface{}{
				"grant_id": gid, "principal_id": pid,
				"agent_id": aid, "scopes": scopes, "purpose": purpose,
				"not_after": nat.UTC().Format(time.RFC3339),
				"revoked":   rat != nil,
			})
		}
		rows.Close()
		writeJSON(w, http.StatusOK, map[string]interface{}{"grants": out})
	})

	mux.HandleFunc("POST /v1/grants", func(w http.ResponseWriter, r *http.Request) {
		var g grantReq
		if err := json.NewDecoder(r.Body).Decode(&g); err != nil ||
			g.PrincipalID == "" || g.AgentID == "" || len(g.Scopes) == 0 {
			errJSON(w, 400, "bad grant")
			return
		}
		var allowed []string
		var status string
		if err := db.QueryRow(r.Context(),
			`SELECT allowed_scopes, status FROM agent WHERE agent_id=$1`,
			g.AgentID).Scan(&allowed, &status); err != nil {
			errJSON(w, 404, "unknown agent")
			return
		}
		if status != "ACTIVE" {
			errJSON(w, 409, "agent not ACTIVE")
			return
		}
		if !scopesOverlap(g.Scopes, allowed) {
			errJSON(w, 400, "grant scopes exceed agent allowed_scopes")
			return
		}
		if g.TTLHours <= 0 {
			g.TTLHours = 24 * 30
		}
		gid := "gr-" + uuid.NewString()
		_, err := db.Exec(r.Context(),
			`INSERT INTO agent_grant (grant_id, principal_id, agent_id,
			   scopes, purpose, not_after)
			 VALUES ($1,$2,$3,$4,$5, now() + ($6 || ' hours')::interval)`,
			gid, g.PrincipalID, g.AgentID, g.Scopes, g.Purpose, fmt.Sprint(g.TTLHours))
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		writeJSON(w, 201, map[string]string{"grant_id": gid})
	})

	// --- revocation ---------------------------------------------------------
	mux.HandleFunc("POST /v1/revocations", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Target string `json:"target"` // grant_id or cred kid
			Reason string `json:"reason"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Target == "" {
			errJSON(w, 400, "target required")
			return
		}
		_, err := db.Exec(r.Context(),
			`INSERT INTO agent_revocation (target, reason) VALUES ($1,$2)
			 ON CONFLICT (target) DO NOTHING`, body.Target, body.Reason)
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		// Per-grant revocation: a person withdraws authority without the
		// tenant revoking the agent (ADR-013).
		_, _ = db.Exec(r.Context(),
			`UPDATE agent_grant SET revoked_at=now()
			 WHERE grant_id=$1 AND revoked_at IS NULL`, body.Target)
		_, _ = db.Exec(r.Context(),
			`UPDATE agent_credential SET revoked_at=now()
			 WHERE cred_id=$1 AND revoked_at IS NULL`, body.Target)
		if err := rdb.SAdd(r.Context(), agentid.RevocationSetKey,
			body.Target).Err(); err != nil {
			errJSON(w, 500, "revocation projection: "+err.Error())
			return
		}
		writeJSON(w, 200, map[string]string{"revoked": body.Target})
	})

	// --- credentials --------------------------------------------------------
	mux.HandleFunc("POST /v1/credentials", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			AgentID  string `json:"agent_id"`
			TTLHours int    `json:"ttl_hours"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.AgentID == "" {
			errJSON(w, 400, "agent_id required")
			return
		}
		var status string
		if err := db.QueryRow(r.Context(),
			`SELECT status FROM agent WHERE agent_id=$1`,
			body.AgentID).Scan(&status); err != nil || status != "ACTIVE" {
			errJSON(w, 404, "unknown or inactive agent")
			return
		}
		if body.TTLHours <= 0 {
			body.TTLHours = 24
		}
		cred := "cred-" + uuid.NewString()
		_, err := db.Exec(r.Context(),
			`INSERT INTO agent_credential (cred_id, agent_id, not_before, not_after)
			 VALUES ($1,$2, now(), now() + ($3 || ' hours')::interval)`,
			cred, body.AgentID, fmt.Sprint(body.TTLHours))
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		writeJSON(w, 201, map[string]string{"cred_id": cred})
	})

	// --- delegation mint ----------------------------------------------------
	// Resolves grants for (principal, agent_id) pairs, builds the ordered
	// chain, signs it. The caller presents the returned token as
	// delegation_token on /v1/decide.
	type mintReq struct {
		PrincipalKind string   `json:"principal_kind"` // PERSON|ORG|SERVICE
		PrincipalID   string   `json:"principal_id"`
		TenantID      string   `json:"tenant_id"`
		AgentIDs      []string `json:"agent_ids"` // ordered: outermost first
		TTLMinutes    int      `json:"ttl_minutes"`
		TTLSecs       int      `json:"ttl_seconds"`
	}
	mux.HandleFunc("POST /v1/delegations/mint", func(w http.ResponseWriter, r *http.Request) {
		var m mintReq
		if err := json.NewDecoder(r.Body).Decode(&m); err != nil ||
			m.PrincipalID == "" || m.TenantID == "" || len(m.AgentIDs) == 0 {
			errJSON(w, 400, "bad mint request")
			return
		}
		if m.TTLSecs <= 0 && m.TTLMinutes <= 0 {
			m.TTLMinutes = 60
		}
		var maxDepth int
		if err := db.QueryRow(r.Context(),
			`SELECT max_chain_depth FROM agent_tenant_config WHERE tenant_id=$1`,
			m.TenantID).Scan(&maxDepth); err != nil {
			maxDepth = agentid.DefaultMaxChainDepth
		}
		if len(m.AgentIDs) > maxDepth {
			errJSON(w, 400, fmt.Sprintf("chain depth %d exceeds tenant max %d",
				len(m.AgentIDs), maxDepth))
			return
		}
		kind, ok := agentv1.Principal_Kind_value[m.PrincipalKind]
		if !ok {
			errJSON(w, 400, "bad principal_kind")
			return
		}
		now := time.Now()
		exp := now.Add(time.Duration(m.TTLMinutes) * time.Minute)
		if m.TTLSecs > 0 {
			exp = now.Add(time.Duration(m.TTLSecs) * time.Second)
		}
		chain := &agentv1.DelegationChain{Principal: &agentv1.Principal{
			Kind: agentv1.Principal_Kind(kind), Id: m.PrincipalID,
			TenantId: m.TenantID}}
		for _, aid := range m.AgentIDs {
			var grantID, akind, astatus, aversion string
			var scopes []string
			var gExp time.Time
			err := db.QueryRow(r.Context(),
				`SELECT g.grant_id, g.scopes, g.not_after, a.kind, a.status,
				        COALESCE(a.max_autonomy,'T1')
				 FROM agent_grant g JOIN agent a ON a.agent_id=g.agent_id
				 WHERE g.principal_id=$1 AND g.agent_id=$2
				   AND g.revoked_at IS NULL AND g.not_after > now()
				 ORDER BY g.not_after DESC LIMIT 1`,
				m.PrincipalID, aid).Scan(&grantID, &scopes, &gExp,
				&akind, &astatus, &aversion)
			if errors.Is(err, pgx.ErrNoRows) {
				errJSON(w, 403, "no live grant for principal->agent "+aid)
				return
			}
			if err != nil {
				errJSON(w, 500, err.Error())
				return
			}
			if astatus != "ACTIVE" {
				errJSON(w, 403, "agent "+aid+" not ACTIVE")
				return
			}
			if gExp.Before(exp) {
				exp = gExp
			}
			chain.Links = append(chain.Links, &agentv1.DelegationLink{
				AgentId: aid, AgentVersion: "1", AgentKind: akind,
				Scopes:   scopes,
				IssuedAt: timestamppb.New(now), ExpiresAt: timestamppb.New(exp),
				GrantId: grantID,
			})
		}
		tok, err := agentid.MintJWS(chain, kr.kid, kr.priv)
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{
			"delegation_token": tok, "kid": kr.kid,
			"expires_at": exp.UTC().Format(time.RFC3339)})
	})

	// --- JWKS ---------------------------------------------------------------
	mux.HandleFunc("GET /v1/jwks", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"keys": []map[string]string{{
			"kid": kr.kid, "kty": "OKP", "crv": "Ed25519",
			"x": base64.RawURLEncoding.EncodeToString(kr.pub)}}})
	})

	// --- tenant config ------------------------------------------------------
	mux.HandleFunc("PUT /v1/tenants/{id}/config", func(w http.ResponseWriter, r *http.Request) {
		id := r.PathValue("id")
		var body struct {
			MaxChainDepth int `json:"max_chain_depth"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.MaxChainDepth < 1 {
			errJSON(w, 400, "max_chain_depth >= 1 required")
			return
		}
		_, err := db.Exec(r.Context(),
			`INSERT INTO agent_tenant_config (tenant_id, max_chain_depth)
			 VALUES ($1,$2) ON CONFLICT (tenant_id)
			 DO UPDATE SET max_chain_depth=$2`, id, body.MaxChainDepth)
		if err != nil {
			errJSON(w, 500, err.Error())
			return
		}
		_ = rdb.Set(r.Context(), agentid.MaxDepthKey(id),
			fmt.Sprint(body.MaxChainDepth), 0).Err()
		writeJSON(w, 200, map[string]any{
			"tenant_id": id, "max_chain_depth": body.MaxChainDepth})
	})

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := db.Ping(r.Context()); err != nil {
			errJSON(w, 503, "db")
			return
		}
		w.WriteHeader(200)
	})

	port := envOr("RTDP_AGENT_REGISTRY_PORT", "8090")
	log.Printf("agent-registry on :%s (kid %s)", port, kr.kid)
	log.Fatal(http.ListenAndServe(":"+port, mux))
}
