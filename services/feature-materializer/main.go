// Feature materializer: consumes committed feature.updates (read_committed)
// and stores absolute tile values with source offset. Duplicate or older
// offsets for a key are no-ops; the consumer offset commits only after the
// store write (design.md recovery and materialization).
package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"os"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/redis/go-redis/v9"
	"github.com/twmb/franz-go/pkg/kgo"
	"google.golang.org/protobuf/proto"

	rtdpv1 "github.com/rtdp/rtdp/gen/go/rtdp/v1"
	"github.com/rtdp/rtdp/internal/kafkax"
)

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}

func main() {
	rdb := redis.NewClient(&redis.Options{
		Addr: envOr("RTDP_REDIS_ADDR", "localhost:6379")})
	consumer, err := kafkax.NewReader("rtdp-materializer",
		kafkax.TopicFeatureUpdates)
	if err != nil {
		log.Fatal(err)
	}
	go func() {
		http.Handle("/metrics", promhttp.Handler())
		http.ListenAndServe(":9090", nil)
	}()

	ctx := context.Background()
	log.Printf("materializer consuming %s", kafkax.TopicFeatureUpdates)
	for {
		fetches := consumer.PollFetches(ctx)
		if fetches.IsClientClosed() {
			return
		}
		var done []*kgo.Record
		fetches.EachRecord(func(rec *kgo.Record) {
			var u rtdpv1.FeatureUpdate
			if err := proto.Unmarshal(rec.Value, &u); err != nil {
				log.Printf("decode: %v", err)
				return
			}
			mode := u.Mode.String()
			if len(mode) > 5 {
				mode = mode[5:]
			}
			bucket := u.TileStart.AsTime().Unix() / 60
			key := fmt.Sprintf("rtdp:t2:{%s:%s}:%s@%d:%s:%s:%d",
				u.TenantId, mode, u.FeatureName, u.FeatureVersion,
				u.EntityId, u.Currency, bucket)
			offKey := key + ":off"

			// Store absolute tile value + source offset only if this offset
			// is newer — older/duplicate offsets are no-ops.
			cur, _ := rdb.Get(ctx, offKey).Result()
			newOff := fmt.Sprintf("%d:%d", rec.Partition, rec.Offset)
			if cur != "" && cur >= newOff {
				return
			}
			pipe := rdb.Pipeline()
			pipe.Set(ctx, key, u.Value, 2*3600*1e9) // 2h retention
			pipe.Set(ctx, offKey, newOff, 2*3600*1e9)
			if _, err := pipe.Exec(ctx); err != nil {
				log.Printf("store %s: %v", key, err)
				return
			}
			done = append(done, rec)
		})
		if len(done) > 0 {
			consumer.CommitRecords(ctx, done...)
		}
	}
}
