FROM flink:1.20.0-java17
# Enable the bundled S3 filesystem plugin (MinIO checkpoints locally, S3 on AWS).
RUN mkdir -p /opt/flink/plugins/s3-fs-presto \
 && cp /opt/flink/opt/flink-s3-fs-presto-*.jar /opt/flink/plugins/s3-fs-presto/ \
 && cp /opt/flink/opt/flink-s3-fs-base-*.jar /opt/flink/plugins/s3-fs-presto/ 2>/dev/null || true
