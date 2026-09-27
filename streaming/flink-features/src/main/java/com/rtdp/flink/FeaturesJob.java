package com.rtdp.flink;

import com.rtdp.proto.v1.FeatureContribution;
import com.rtdp.proto.v1.FeatureUpdate;
import com.rtdp.proto.v1.Mode;
import com.google.protobuf.Timestamp;
import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.api.common.serialization.AbstractDeserializationSchema;
import org.apache.flink.api.common.serialization.SerializationSchema;
import org.apache.flink.connector.base.DeliveryGuarantee;
import org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaSink;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.datastream.SingleOutputStreamOperator;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;
import org.apache.flink.streaming.api.functions.windowing.ProcessWindowFunction;
import org.apache.flink.streaming.api.windowing.assigners.TumblingEventTimeWindows;
import org.apache.flink.streaming.api.windowing.time.Time;
import org.apache.flink.streaming.api.windowing.windows.TimeWindow;
import org.apache.flink.util.Collector;
import org.apache.flink.util.OutputTag;

import java.io.IOException;
import java.time.Duration;
import java.util.Properties;

/**
 * RTDP streaming features (design.md "Flink and feature consistency"):
 *
 *   committed tokenized contributions
 *     -> validate + dedup
 *     -> keyBy(tenant, mode, provider, currency)
 *     -> event-time 1-minute tiles, watermarks, allowed lateness
 *     -> transactional feature.updates sink (absolute tile values)
 *     -> read_committed materializer -> versioned Tier 2 store
 *
 * Proposed tuning: out-of-orderness 2s, allowed lateness 60s,
 * source idleness 10s.
 */
public class FeaturesJob {

    static final OutputTag<FeatureContribution> LATE =
        new OutputTag<>("late-contributions") {};

    public static void main(String[] args) throws Exception {
        String brokers = envOr("RTDP_KAFKA_BROKERS", "kafka:9092");

        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.enableCheckpointing(60000);

        KafkaSource<FeatureContribution> source = KafkaSource.<FeatureContribution>builder()
            .setBootstrapServers(brokers)
            .setTopics("rtdp.feature.contrib.v1")
            .setGroupId("rtdp-flink-features")
            .setStartingOffsets(OffsetsInitializer.committedOffsets(
                org.apache.kafka.clients.consumer.OffsetResetStrategy.EARLIEST))
            .setDeserializer(org.apache.flink.connector.kafka.source.reader
                .deserializer.KafkaRecordDeserializationSchema.valueOnly(
                    new ContributionDeserializer()))
            .build();

        // Watermarks: 2s out-of-orderness, 10s source idleness.
        WatermarkStrategy<FeatureContribution> wm = WatermarkStrategy
            .<FeatureContribution>forBoundedOutOfOrderness(Duration.ofSeconds(2))
            .withTimestampAssigner((c, ts) -> c.getEventTime().getSeconds() * 1000L
                + c.getEventTime().getNanos() / 1_000_000L)
            .withIdleness(Duration.ofSeconds(10));

        DataStream<FeatureContribution> contribs = env
            .fromSource(source, wm, "feature-contributions");

        SingleOutputStreamOperator<FeatureUpdate> tiles = contribs
            .keyBy(c -> c.getTenantId() + "|" + c.getMode() + "|"
                + c.getProviderId() + "|" + c.getCurrency())
            .window(TumblingEventTimeWindows.of(Time.minutes(1)))
            .allowedLateness(Time.seconds(60))
            .sideOutputLateData(LATE)
            .process(new TileAggregator())
            .returns(FeatureUpdate.class);

        // Transactional sink: absolute tile values, committed with checkpoints.
        Properties txnProps = new Properties();
        KafkaSink<FeatureUpdate> sink = KafkaSink.<FeatureUpdate>builder()
            .setBootstrapServers(brokers)
            .setRecordSerializer(KafkaRecordSerializationSchema
                .<FeatureUpdate>builder()
                .setTopic("rtdp.feature.updates.v1")
                .setKeySerializationSchema((FeatureUpdate u) ->
                    (u.getTenantId() + ":" + u.getEntityId()).getBytes())
                .setValueSerializationSchema(new UpdateSerializer())
                .build())
            .setDeliveryGuarantee(DeliveryGuarantee.EXACTLY_ONCE)
            .setTransactionalIdPrefix("rtdp-flink-features")
            .setKafkaProducerConfig(txnProps)
            .build();

        tiles.sinkTo(sink).name("feature-updates");

        // Beyond-lateness events -> late topic for investigation/backfill.
        DataStream<FeatureContribution> late = tiles.getSideOutput(LATE);
        KafkaSink<FeatureContribution> lateSink = KafkaSink.<FeatureContribution>builder()
            .setBootstrapServers(brokers)
            .setRecordSerializer(KafkaRecordSerializationSchema.builder()
                .setTopic("rtdp.feature.late.v1")
                .setValueSerializationSchema(new ContributionSerializer())
                .build())
            .setDeliveryGuarantee(DeliveryGuarantee.AT_LEAST_ONCE)
            .build();
        late.sinkTo(lateSink).name("late-contributions");

        env.execute("rtdp-flink-features");
    }

    /** Aggregate one 1-minute tile: count + amount sum per key. */
    static class TileAggregator
        extends ProcessWindowFunction<FeatureContribution, FeatureUpdate,
                                      String, TimeWindow> {
        @Override
        public void process(String key, Context ctx,
                            Iterable<FeatureContribution> elements,
                            Collector<FeatureUpdate> out) {
            long count = 0;
            double sum = 0.0;
            FeatureContribution first = null;
            for (FeatureContribution c : elements) {
                count++;
                sum += c.getAmount();
                if (first == null) first = c;
            }
            if (first == null) return;
            Timestamp start = Timestamp.newBuilder()
                .setSeconds(ctx.window().getStart() / 1000).build();
            Timestamp end = Timestamp.newBuilder()
                .setSeconds(ctx.window().getEnd() / 1000).build();

            FeatureUpdate.Builder base = FeatureUpdate.newBuilder()
                .setTenantId(first.getTenantId())
                .setMode(first.getMode())
                .setEntityId(first.getProviderId())
                .setCurrency(first.getCurrency())
                .setFeatureVersion(1)
                .setTileStart(start)
                .setTileEnd(end)
                .setFeatureDefinitionDigest("seed-tiles-v1");

            out.collect(base.clone()
                .setFeatureName("provider_claim_count_1h")
                .setValue(count).build());
            out.collect(base.clone()
                .setFeatureName("provider_amount_sum_1h")
                .setValue(sum).build());
        }
    }

    static class ContributionDeserializer
        extends AbstractDeserializationSchema<FeatureContribution> {
        @Override
        public FeatureContribution deserialize(byte[] bytes) throws IOException {
            return FeatureContribution.parseFrom(bytes);
        }
    }

    static class UpdateSerializer implements SerializationSchema<FeatureUpdate> {
        @Override
        public byte[] serialize(FeatureUpdate u) { return u.toByteArray(); }
    }

    static class ContributionSerializer implements SerializationSchema<FeatureContribution> {
        @Override
        public byte[] serialize(FeatureContribution c) { return c.toByteArray(); }
    }

    static String envOr(String k, String d) {
        String v = System.getenv(k);
        return v == null ? d : v;
    }
}
