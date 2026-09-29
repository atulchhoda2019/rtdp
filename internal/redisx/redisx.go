// Package redisx wraps go-redis with the transport settings RTDP requires:
// plaintext locally, TLS when RTDP_REDIS_TLS=true (ElastiCache in-transit
// encryption). IAM auth for Valkey is a later phase; TLS terminates at the
// replication-group endpoint.
package redisx

import (
	"crypto/tls"
	"os"

	"github.com/redis/go-redis/v9"
)

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

// New returns a Redis/Valkey client honoring RTDP_REDIS_ADDR and
// RTDP_REDIS_TLS.
func New() *redis.Client {
	opts := &redis.Options{Addr: envOr("RTDP_REDIS_ADDR", "localhost:6379")}
	if os.Getenv("RTDP_REDIS_TLS") == "true" {
		opts.TLSConfig = &tls.Config{MinVersion: tls.VersionTLS12}
	}
	return redis.NewClient(opts)
}
