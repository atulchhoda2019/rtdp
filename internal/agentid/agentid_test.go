package agentid

import (
	"testing"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	agentv1 "github.com/rtdp/rtdp/gen/go/rtdp/agent/v1"
)

func testChain() *agentv1.DelegationChain {
	return &agentv1.DelegationChain{
		Principal: &agentv1.Principal{
			Kind: agentv1.Principal_PERSON, Id: "guest-g", TenantId: "tenant_a"},
		Links: []*agentv1.DelegationLink{{
			AgentId: "guest-agent-1", AgentVersion: "1", AgentKind: "CUSTOMER",
			Scopes:   []string{"decide", "reservations"},
			IssuedAt: timestamppb.New(time.Now().Add(-time.Minute)),
			ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
			GrantId:   "grant-1",
		}},
	}
}

func TestMintVerifyRoundTrip(t *testing.T) {
	kid, pub, priv, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	c := testChain()
	tok, err := MintJWS(c, kid, priv)
	if err != nil {
		t.Fatal(err)
	}
	got, gotKid, err := VerifyJWS(tok, pub)
	if err != nil {
		t.Fatal(err)
	}
	if gotKid != kid {
		t.Fatalf("kid %q != %q", gotKid, kid)
	}
	if got.Principal.Id != "guest-g" || len(got.Links) != 1 ||
		got.Links[0].AgentId != "guest-agent-1" ||
		got.Links[0].Scopes[1] != "reservations" {
		t.Fatalf("round-trip mismatch: %+v", got)
	}
	if got.ProofKid != kid {
		t.Fatalf("proof kid not stamped")
	}
}

func TestTamperedLinkFails(t *testing.T) {
	kid, pub, priv, _ := GenerateKey()
	c := testChain()
	tok, _ := MintJWS(c, kid, priv)
	// Flip a byte inside the payload segment.
	parts := []byte(tok)
	for i := 0; i < len(parts); i++ {
		if parts[i] == '.' {
			parts[i+1] ^= 0x01
			break
		}
	}
	if _, _, err := VerifyJWS(string(parts), pub); err == nil {
		t.Fatal("tampered payload verified")
	}
}

func TestWrongKeyFails(t *testing.T) {
	kid, _, priv, _ := GenerateKey()
	_, pub2, _, _ := GenerateKey()
	c := testChain()
	tok, _ := MintJWS(c, kid, priv)
	if _, _, err := VerifyJWS(tok, pub2); err == nil {
		t.Fatal("verified under wrong key")
	}
}

func TestValidateChain(t *testing.T) {
	now := time.Now()
	c := testChain()
	if err := ValidateChain(c, "tenant_a", now); err != nil {
		t.Fatalf("valid chain rejected: %v", err)
	}
	if err := ValidateChain(c, "tenant_b", now); err == nil {
		t.Fatal("cross-tenant chain accepted")
	}
	expired := testChain()
	expired.Links[0].ExpiresAt = timestamppb.New(now.Add(-time.Minute))
	if err := ValidateChain(expired, "tenant_a", now); err == nil {
		t.Fatal("expired link accepted")
	}
	empty := testChain()
	empty.Links = nil
	if err := ValidateChain(empty, "tenant_a", now); err == nil {
		t.Fatal("empty chain accepted")
	}
}
