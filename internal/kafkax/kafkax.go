// Package kafkax wraps franz-go with the settings RTDP requires:
// idempotent transactional producers for the decision commit boundary and
// read_committed consumers for downstream facts.
package kafkax

import (
	"context"
	"crypto/tls"
	"fmt"
	"os"
	"strings"
	"sync"
	"time"

	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/twmb/franz-go/pkg/kadm"
	"github.com/twmb/franz-go/pkg/kgo"
	saslaws "github.com/twmb/franz-go/pkg/sasl/aws"
)

func Brokers() []string {
	b := os.Getenv("RTDP_KAFKA_BROKERS")
	if b == "" {
		b = "localhost:9092"
	}
	return strings.Split(b, ",")
}

// authOpts returns TLS + SASL AWS_MSK_IAM opts when RTDP_KAFKA_AUTH=iam (MSK),
// nil otherwise (local plaintext). Credentials resolve through the default
// AWS chain — Pod Identity / IRSA in-cluster, AWS_PROFILE locally.
func authOpts() ([]kgo.Opt, error) {
	if os.Getenv("RTDP_KAFKA_AUTH") != "iam" {
		return nil, nil
	}
	cfg, err := awsconfig.LoadDefaultConfig(context.Background())
	if err != nil {
		return nil, fmt.Errorf("aws config: %w", err)
	}
	creds := cfg.Credentials
	return []kgo.Opt{
		kgo.DialTLSConfig(&tls.Config{MinVersion: tls.VersionTLS12}),
		kgo.SASL(saslaws.ManagedStreamingIAM(func(ctx context.Context) (saslaws.Auth, error) {
			c, err := creds.Retrieve(ctx)
			if err != nil {
				return saslaws.Auth{}, err
			}
			return saslaws.Auth{
				AccessKey:    c.AccessKeyID,
				SecretKey:    c.SecretAccessKey,
				SessionToken: c.SessionToken,
			}, nil
		})),
	}, nil
}

// Topics from design.md topic contracts.
const (
	TopicIngress        = "rtdp.ingress.v1"
	TopicEgress         = "rtdp.egress.v1"
	TopicFeatureContrib = "rtdp.feature.contrib.v1"
	TopicFeatureUpdates = "rtdp.feature.updates.v1"
	TopicFeatureLate    = "rtdp.feature.late.v1"
	TopicDecisionFacts  = "rtdp.decision.facts.v1"
	TopicActionCommands = "rtdp.action.commands.v1"
	TopicActionStatus   = "rtdp.action.status.v1"
	TopicControlActiv   = "rtdp.control.activation.v1"
	TopicTelemetry      = "rtdp.telemetry.v1"
	TopicDLQ            = "rtdp.dlq.v1"
)

var AllTopics = []string{
	TopicIngress, TopicEgress, TopicFeatureContrib, TopicFeatureUpdates,
	TopicFeatureLate, TopicDecisionFacts, TopicActionCommands,
	TopicActionStatus, TopicControlActiv, TopicTelemetry, TopicDLQ,
}

// NewTransacter returns a client configured for the orchestrator's durable
// commit: decision + audit + action commands + contribution + input offset
// in one Kafka transaction. Transactional producers are idempotent by
// construction.
func NewTransacter(id string) (*kgo.Client, error) {
	opts, err := authOpts()
	if err != nil {
		return nil, err
	}
	return kgo.NewClient(append([]kgo.Opt{
		kgo.SeedBrokers(Brokers()...),
		kgo.TransactionalID(id),
		kgo.TransactionTimeout(time.Minute),
		kgo.RequiredAcks(kgo.AllISRAcks()),
		kgo.ProducerLinger(0),
	}, opts...)...)
}

// NewReader returns a read_committed consumer for downstream consumers.
func NewReader(group string, topics ...string) (*kgo.Client, error) {
	opts, err := authOpts()
	if err != nil {
		return nil, err
	}
	return kgo.NewClient(append([]kgo.Opt{
		kgo.SeedBrokers(Brokers()...),
		kgo.ConsumerGroup(group),
		kgo.ConsumeTopics(topics...),
		kgo.FetchIsolationLevel(kgo.ReadCommitted()),
		kgo.DisableAutoCommit(),
	}, opts...)...)
}

// NewProducer returns a non-transactional idempotent producer for services
// that publish facts outside the orchestrator's commit boundary (e.g. the
// action outbox relay).
func NewProducer() (*kgo.Client, error) {
	opts, err := authOpts()
	if err != nil {
		return nil, err
	}
	return kgo.NewClient(append([]kgo.Opt{
		kgo.SeedBrokers(Brokers()...),
		kgo.RequiredAcks(kgo.AllISRAcks()),
		kgo.ProducerLinger(0),
	}, opts...)...)
}

// BeginTxn wraps f in a Kafka transaction. Commit errors surface to the
// caller — never assert success prematurely (design.md failure behavior).
func BeginTxn(ctx context.Context, cl *kgo.Client,
	f func(ctx context.Context) error) error {
	if err := cl.BeginTransaction(); err != nil {
		return fmt.Errorf("begin txn: %w", err)
	}
	if err := f(ctx); err != nil {
		// Abort with a detached ctx: the request deadline may already be
		// spent, and a cancelled abort would leave the txn open and fence
		// the producer for every subsequent request.
		ac, acancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		cl.EndTransaction(ac, kgo.TryAbort)
		acancel()
		return err
	}
	// Commit likewise outlives the request deadline: a timed-out caller can
	// still get a durable commit (status lookup is the proof path).
	cc, ccancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer ccancel()
	if err := cl.EndTransaction(cc, kgo.TryCommit); err != nil {
		return fmt.Errorf("commit txn: %w", err)
	}
	return nil
}

// TxnPool is a bounded pool of transactional producers. Kafka transactions
// are per-client and cannot interleave on one producer, so concurrent
// Decide calls each check out a producer for the commit boundary.
type TxnPool struct {
	ch chan *txnSlot
}

type txnSlot struct {
	id string
	cl *kgo.Client
}

// NewTxnPool creates n transactional producers with distinct
// transactional.ids (<prefix>-0 .. <prefix>-n-1).
func NewTxnPool(prefix string, n int) (*TxnPool, error) {
	if n < 1 {
		n = 1
	}
	p := &TxnPool{ch: make(chan *txnSlot, n)}
	for i := 0; i < n; i++ {
		id := fmt.Sprintf("%s-%d", prefix, i)
		cl, err := NewTransacter(id)
		if err != nil {
			p.Close()
			return nil, err
		}
		p.ch <- &txnSlot{id: id, cl: cl}
	}
	return p, nil
}

// Get checks out a producer, blocking until one is free or ctx expires.
// The returned release(bad) puts the producer back; a bad producer is
// closed and recreated on the slot's transactional.id — the new client
// fences the old producer_id, the fencing semantics the broker requires.
// If recreation fails the pool shrinks rather than recycle a bad client.
func (p *TxnPool) Get(ctx context.Context) (*kgo.Client,
	func(bad bool), error) {
	select {
	case s := <-p.ch:
		return s.cl, func(bad bool) {
			if bad {
				s.cl.Close()
				if cl, err := NewTransacter(s.id); err == nil {
					s.cl = cl
				} else {
					return // pool shrinks; Get blocks/timeouts on failure
				}
			}
			p.ch <- s
		}, nil
	case <-ctx.Done():
		return nil, nil, ctx.Err()
	}
}

// Warm forces broker connections AND transactional producer IDs up front —
// Ping alone leaves InitProducerId lazy, so the first request per slot would
// still pay it against the decision deadline. An empty begin+abort acquires
// the producer ID without writing records.
func (p *TxnPool) Warm(ctx context.Context) {
	var wg sync.WaitGroup
	for i := 0; i < cap(p.ch); i++ {
		s := <-p.ch
		wg.Add(1)
		go func(s *txnSlot) {
			defer wg.Done()
			if err := s.cl.BeginTransaction(); err == nil {
				s.cl.EndTransaction(ctx, kgo.TryAbort) //nolint:errcheck
			} else {
				s.cl.Ping(ctx) //nolint:errcheck
			}
			p.ch <- s
		}(s)
	}
	wg.Wait()
}

// Close shuts every idle producer in the pool.
func (p *TxnPool) Close() {
	for {
		select {
		case s := <-p.ch:
			s.cl.Close()
		default:
			return
		}
	}
}

// EnsureTopics creates declared topics if missing (seed tooling).
func EnsureTopics(ctx context.Context, partitions int32, rf int16) error {
	opts, err := authOpts()
	if err != nil {
		return err
	}
	cl, err := kgo.NewClient(append([]kgo.Opt{
		kgo.SeedBrokers(Brokers()...),
	}, opts...)...)
	if err != nil {
		return err
	}
	defer cl.Close()
	adm := kadm.NewClient(cl)
	// Pin MinISR at the topic level: AWS Health flags RF == MinISR (any single
	// broker loss stalls writes), and the broker default may drift.
	minISR := "2"
	cfg := map[string]*string{"min.insync.replicas": &minISR}
	_, err = adm.CreateTopics(ctx, partitions, rf, cfg, AllTopics...)
	return err
}

// DeleteTopics removes the declared topics — sandbox bootstrap escape hatch
// for correcting replication factor (RF is immutable post-creation).
func DeleteTopics(ctx context.Context) error {
	opts, err := authOpts()
	if err != nil {
		return err
	}
	cl, err := kgo.NewClient(append([]kgo.Opt{
		kgo.SeedBrokers(Brokers()...),
	}, opts...)...)
	if err != nil {
		return err
	}
	defer cl.Close()
	adm := kadm.NewClient(cl)
	_, err = adm.DeleteTopics(ctx, AllTopics...)
	return err
}
