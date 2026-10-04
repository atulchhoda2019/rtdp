// Package agentid implements the ADR-013 delegation-chain proof format and
// the revocation/depth checks shared by agent-registry (minting) and
// ingress (verification).
//
// The proof is a compact JWS (EdDSA) whose payload is the canonical JSON
// encoding of the chain — principal + ordered links. Signatures are
// ed25519: locally an ephemeral keypair the registry persists in Postgres;
// on AWS an asymmetric KMS key. No JWT library: the wire format is
// standard JWS but the claims set is ours alone.
package agentid

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"google.golang.org/protobuf/types/known/timestamppb"

	agentv1 "github.com/rtdp/rtdp/gen/go/rtdp/agent/v1"
)

// RevocationSetKey is the Redis set holding revoked credential ids (kids)
// and grant ids. agent-registry SADDs on revoke; ingress SISMEMBERs per
// request — propagation is synchronous, far inside the 5s bound in G8.
const RevocationSetKey = "rtdp:agent:revoked"

// MaxDepthKey returns the per-tenant chain-depth ceiling; absent = 2.
func MaxDepthKey(tenant string) string { return "rtdp:agent:maxdepth:{" + tenant + "}" }

const DefaultMaxChainDepth = 2

// chainPayload is the canonical signed form. Field order is fixed by the
// struct declaration so marshalling is byte-stable.
type chainPayload struct {
	Principal payloadPrincipal `json:"principal"`
	Links     []payloadLink    `json:"links"`
}
type payloadPrincipal struct {
	Kind     string `json:"kind"`
	ID       string `json:"id"`
	TenantID string `json:"tenant_id"`
}
type payloadLink struct {
	AgentID      string   `json:"agent_id"`
	AgentVersion string   `json:"agent_version"`
	AgentKind    string   `json:"agent_kind"`
	Scopes       []string `json:"scopes"`
	MaxAutonomy  string   `json:"max_autonomy,omitempty"`
	IssuedAt     string   `json:"issued_at"` // RFC3339
	ExpiresAt    string   `json:"expires_at"`
	GrantID      string   `json:"grant_id"`
}

func kindString(k agentv1.Principal_Kind) string { return k.String() }

func toPayload(c *agentv1.DelegationChain) chainPayload {
	p := chainPayload{
		Principal: payloadPrincipal{
			Kind:     kindString(c.Principal.Kind),
			ID:       c.Principal.Id,
			TenantID: c.Principal.TenantId,
		},
	}
	for _, l := range c.Links {
		p.Links = append(p.Links, payloadLink{
			AgentID:      l.AgentId,
			AgentVersion: l.AgentVersion,
			AgentKind:    l.AgentKind,
			Scopes:       l.Scopes,
			MaxAutonomy:  l.MaxAutonomy,
			IssuedAt:     l.IssuedAt.AsTime().UTC().Format(time.RFC3339),
			ExpiresAt:    l.ExpiresAt.AsTime().UTC().Format(time.RFC3339),
			GrantID:      l.GrantId,
		})
	}
	return p
}

func kindFromString(s string) (agentv1.Principal_Kind, error) {
	if v, ok := agentv1.Principal_Kind_value[s]; ok {
		return agentv1.Principal_Kind(v), nil
	}
	return agentv1.Principal_KIND_UNSPECIFIED, fmt.Errorf("principal kind %q", s)
}

func fromPayload(p chainPayload) (*agentv1.DelegationChain, error) {
	kind, err := kindFromString(p.Principal.Kind)
	if err != nil {
		return nil, err
	}
	c := &agentv1.DelegationChain{Principal: &agentv1.Principal{
		Kind: kind, Id: p.Principal.ID, TenantId: p.Principal.TenantID}}
	for _, l := range p.Links {
		iat, err := time.Parse(time.RFC3339, l.IssuedAt)
		if err != nil {
			return nil, fmt.Errorf("link issued_at: %w", err)
		}
		exp, err := time.Parse(time.RFC3339, l.ExpiresAt)
		if err != nil {
			return nil, fmt.Errorf("link expires_at: %w", err)
		}
		c.Links = append(c.Links, &agentv1.DelegationLink{
			AgentId: l.AgentID, AgentVersion: l.AgentVersion,
			AgentKind: l.AgentKind, Scopes: l.Scopes,
			MaxAutonomy: l.MaxAutonomy,
			IssuedAt:    timestamppb.New(iat), ExpiresAt: timestamppb.New(exp),
			GrantId: l.GrantID,
		})
	}
	return c, nil
}

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

// MintJWS signs the chain: payload = canonical JSON, header carries kid so
// verifiers select the right key. Sets Proof/ProofKid on the chain too.
func MintJWS(c *agentv1.DelegationChain, kid string, priv ed25519.PrivateKey) (string, error) {
	if c.Principal == nil || len(c.Links) == 0 {
		return "", errors.New("chain needs a principal and at least one link")
	}
	hdr, _ := json.Marshal(map[string]string{
		"alg": "EdDSA", "typ": "JWT", "kid": kid})
	payload, err := json.Marshal(toPayload(c))
	if err != nil {
		return "", err
	}
	body := b64(hdr) + "." + b64(payload)
	sig := ed25519.Sign(priv, []byte(body))
	tok := body + "." + b64(sig)
	c.Proof = tok
	c.ProofKid = kid
	return tok, nil
}

// KidOf extracts the signing kid from a JWS header without verifying —
// used to select the verification key.
func KidOf(token string) (string, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return "", errors.New("malformed JWS")
	}
	hdrB, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return "", errors.New("bad JWS header")
	}
	var hdr struct {
		Kid string `json:"kid"`
	}
	if err := json.Unmarshal(hdrB, &hdr); err != nil || hdr.Kid == "" {
		return "", errors.New("no kid")
	}
	return hdr.Kid, nil
}

// VerifyJWS verifies the compact JWS and returns the embedded chain.
// Returns the signing kid as well so callers can check revocation.
func VerifyJWS(token string, pub ed25519.PublicKey) (*agentv1.DelegationChain, string, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, "", errors.New("malformed JWS")
	}
	hdrB, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return nil, "", errors.New("bad JWS header")
	}
	var hdr struct {
		Alg string `json:"alg"`
		Kid string `json:"kid"`
	}
	if err := json.Unmarshal(hdrB, &hdr); err != nil || hdr.Alg != "EdDSA" {
		return nil, "", errors.New("unsupported JWS header")
	}
	sig, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil || !ed25519.Verify(pub,
		[]byte(parts[0]+"."+parts[1]), sig) {
		return nil, "", errors.New("bad signature")
	}
	payloadB, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, "", errors.New("bad JWS payload")
	}
	var p chainPayload
	if err := json.Unmarshal(payloadB, &p); err != nil {
		return nil, "", errors.New("bad chain payload")
	}
	c, err := fromPayload(p)
	if err != nil {
		return nil, "", err
	}
	c.Proof = token
	c.ProofKid = hdr.Kid
	return c, hdr.Kid, nil
}

// ValidateChain checks structural invariants verified proof can't cover:
// expiry windows, non-empty scopes, tenant match. Depth is checked by the
// caller (tenant-specific ceiling).
func ValidateChain(c *agentv1.DelegationChain, tenantID string, now time.Time) error {
	if c.Principal == nil {
		return errors.New("missing principal")
	}
	if c.Principal.TenantId != tenantID {
		return fmt.Errorf("principal tenant %q != request tenant %q",
			c.Principal.TenantId, tenantID)
	}
	if len(c.Links) == 0 {
		return errors.New("empty delegation chain")
	}
	for i, l := range c.Links {
		if l.AgentId == "" || l.GrantId == "" {
			return fmt.Errorf("link %d missing agent/grant", i)
		}
		if len(l.Scopes) == 0 {
			return fmt.Errorf("link %d has no scopes", i)
		}
		if now.Before(l.IssuedAt.AsTime()) || now.After(l.ExpiresAt.AsTime()) {
			return fmt.Errorf("link %d outside validity window", i)
		}
	}
	return nil
}

// Revoked reports whether the proof's kid or any grant in the chain is in
// the revocation set. One round trip via SMISMEMBER.
func Revoked(ctx context.Context, rdb *redis.Client, c *agentv1.DelegationChain) (bool, error) {
	members := []interface{}{c.ProofKid}
	for _, l := range c.Links {
		members = append(members, l.GrantId)
	}
	res, err := rdb.SMIsMember(ctx, RevocationSetKey, members...).Result()
	if err != nil {
		return false, err
	}
	for _, hit := range res {
		if hit {
			return true, nil
		}
	}
	return false, nil
}

// MaxDepth returns the tenant's delegation depth ceiling (default 2).
func MaxDepth(ctx context.Context, rdb *redis.Client, tenantID string) int {
	v, err := rdb.Get(ctx, MaxDepthKey(tenantID)).Result()
	if err == nil {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return DefaultMaxChainDepth
}

// GenerateKey creates a fresh ed25519 keypair; kid is the first 16 bytes
// of the public key, hex.
func GenerateKey() (kid string, pub, priv []byte, err error) {
	p, s, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return "", nil, nil, err
	}
	return fmt.Sprintf("agt-%x", p[:8]), p, s, nil
}
