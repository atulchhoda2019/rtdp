// rtdp-topics creates the declared RTDP topics. Runs locally (plaintext) or
// in-cluster against MSK (RTDP_KAFKA_AUTH=iam). Bootstrap tooling — not a
// request-path component.
package main

import (
	"context"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/rtdp/rtdp/internal/kafkax"
)

func main() {
	partitions := int32(6)
	if v := os.Getenv("RTDP_TOPIC_PARTITIONS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			partitions = int32(n)
		}
	}
	rf := int16(1)
	if v := os.Getenv("RTDP_TOPIC_RF"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			rf = int16(n)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if os.Getenv("RTDP_TOPIC_DESCRIBE") == "1" {
		desc, err := kafkax.DescribeTopics(ctx)
		if err != nil {
			log.Fatalf("describe topics: %v", err)
		}
		for _, topic := range kafkax.AllTopics {
			log.Printf("%s %s", topic, desc[topic])
		}
		return
	}
	if os.Getenv("RTDP_TOPIC_DELETE") == "1" {
		if err := kafkax.DeleteTopics(ctx); err != nil {
			log.Fatalf("delete topics: %v", err)
		}
		log.Printf("topics deleted; waiting for propagation")
		time.Sleep(15 * time.Second)
	}
	if err := kafkax.EnsureTopics(ctx, partitions, rf); err != nil {
		log.Fatalf("ensure topics: %v", err)
	}
	log.Printf("topics ensured: %d (rf=%d)", len(kafkax.AllTopics), rf)
}
