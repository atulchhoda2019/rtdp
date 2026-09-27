// Package store implements the Tier 1 synchronous feature contract:
// atomic per-entity update/dedup/returned-vector via a single Lua script,
// all keys under one tenant/entity hash tag.
package store

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

//go:embed tier1.lua
var tier1Script string

var (
	ErrConflict     = errors.New("dedup conflict: same event id, different payload")
	ErrOutOfHorizon = errors.New("event outside the accepted 24h horizon")
)

const (
	dedupHorizon = 24 * time.Hour
	countWindowM = 60   // pan_txn_count_1h
	amtWindowM   = 1440 // pan_amount_sum_24h
)

// Vector is the Tier 1 returned feature vector for one event.
type Vector struct {
	PanTxnCount1h   int64   `json:"pan_txn_count_1h"`
	PanAmountSum24h float64 `json:"pan_amount_sum_24h"`
	AsOfBucket      int64   `json:"as_of_bucket"`
}

// Tier1 executes atomic dedup+update+read against Redis.
type Tier1 struct {
	rdb    *redis.Client
	script *redis.Script
}

func NewTier1(rdb *redis.Client) *Tier1 {
	// NewScript handles EvalSha with automatic NOSCRIPT fallback to Eval.
	return &Tier1{rdb: rdb, script: redis.NewScript(tier1Script)}
}

func (t *Tier1) Ping(ctx context.Context) error { return t.rdb.Ping(ctx).Err() }

func hashTag(tenant, env, mode, pan string) string {
	return fmt.Sprintf("%s:%s:%s:%s", tenant, env, mode, pan)
}

// Apply atomically deduplicates eventID, applies this event's contribution,
// and returns the feature vector the decision should use. A retry with the
// same event id and payload returns the cached vector; a payload mismatch is
// a conflict.
func (t *Tier1) Apply(ctx context.Context, tenant, env, mode string,
	liveOnly bool, pan, currency, eventID, payloadDigest string,
	eventTime time.Time, amount float64) (*Vector, error) {

	scope := hashTag(tenant, env, mode, pan)
	keys := []string{
		fmt.Sprintf("rtdp:t1:{%s}:dedup:%s", scope, pan),
		fmt.Sprintf("rtdp:t1:{%s}:count:%s", scope, pan),
		fmt.Sprintf("rtdp:t1:{%s}:amt:%s:%s", scope, pan, currency),
		fmt.Sprintf("rtdp:t1:{%s}:vec:%s", scope, pan),
	}
	live := "0"
	if liveOnly {
		live = "1"
	}
	args := []any{
		eventID, payloadDigest, eventTime.Unix() / 60, amount,
		int64(dedupHorizon.Seconds()), countWindowM, amtWindowM, live,
	}

	res, err := t.script.Run(ctx, t.rdb, keys, args...).Result()
	if err != nil {
		if strings.HasPrefix(err.Error(), "CONFLICT:") {
			return nil, ErrConflict
		}
		if strings.HasPrefix(err.Error(), "OUT_OF_HORIZON:") {
			return nil, ErrOutOfHorizon
		}
		return nil, err
	}
	var v Vector
	if err := json.Unmarshal([]byte(res.(string)), &v); err != nil {
		return nil, fmt.Errorf("tier1 vector decode: %w", err)
	}
	return &v, nil
}

// Tier2 reads absolute tile values materialized by the Flink pipeline.
type Tier2 struct {
	rdb *redis.Client
}

func NewTier2(rdb *redis.Client) *Tier2 { return &Tier2{rdb: rdb} }

// TileKey mirrors the materializer's key format:
// rtdp:t2:{tenant:mode}:<feature>@<ver>:<entity>:<currency>
func tileKey(tenant, mode, feature string, ver int, entity, currency string) string {
	return fmt.Sprintf("rtdp:t2:{%s:%s}:%s@%d:%s:%s",
		tenant, mode, feature, ver, entity, currency)
}

// Window sums the trailing 1-minute tiles covering [now-window, now).
// Returns (value, tilesSeen, tilesExpected) — coverage evidence for the
// staleness policy; a quiet entity is not proof the pipeline is stale.
func (t *Tier2) Window(ctx context.Context, tenant, mode, feature string,
	ver int, entity, currency string, window time.Duration,
	now time.Time) (float64, int, error) {

	minutes := int(window.Minutes())
	endBucket := now.Unix() / 60
	startBucket := endBucket - int64(minutes)
	pipe := t.rdb.Pipeline()
	cmds := make([]*redis.StringCmd, 0, minutes)
	for b := startBucket; b < endBucket; b++ {
		key := fmt.Sprintf("%s:%d",
			tileKey(tenant, mode, feature, ver, entity, currency), b)
		cmds = append(cmds, pipe.Get(ctx, key))
	}
	if _, err := pipe.Exec(ctx); err != nil && !errors.Is(err, redis.Nil) {
		return 0, 0, err
	}
	var total float64
	seen := 0
	for _, c := range cmds {
		s, err := c.Result()
		if errors.Is(err, redis.Nil) {
			continue
		}
		if err != nil {
			return 0, seen, err
		}
		v, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return 0, seen, fmt.Errorf("tile decode: %w", err)
		}
		total += v
		seen++
	}
	return total, seen, nil
}
