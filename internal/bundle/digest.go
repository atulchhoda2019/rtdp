package bundle

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
)

// CanonicalDigest mirrors rtdp_contracts.digest.canonical_digest: sha256 over
// canonical JSON (sorted keys, compact separators, no HTML escaping).
func CanonicalDigest(obj any) (string, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(obj); err != nil {
		return "", err
	}
	// Encoder adds a trailing newline; strip it to match Python's output.
	b := bytes.TrimRight(buf.Bytes(), "\n")
	return fmt.Sprintf("sha256:%x", sha256.Sum256(b)), nil
}
