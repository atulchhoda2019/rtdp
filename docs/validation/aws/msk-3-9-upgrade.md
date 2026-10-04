# MSK 3.7.x → 3.9.x upgrade — rtdp-sandbox (us-east-1)

Date: 2026-10-03→04 (UTC). Driver: AWS Health
`AWS_KAFKA_PLANNED_LIFECYCLE_EVENT` — Kafka 3.7.0 EOL 2026-09-01,
auto-upgrade after 2026-09-08 without notice.

## Change

- `infra/terraform/modules/msk/main.tf`: `kafka_version` default
  `3.7.x` → `3.9.x`; `aws_msk_configuration` renamed to
  `rtdp-sandbox-cfg-<version>` + `create_before_destroy` (MSK refuses
  deletion of a configuration still attached to a cluster — the naive
  replace ordering failed on first apply).
- `terraform apply`: cluster `Modifications complete after 2h0m37s`
  (rolling broker patch + restart, 3 brokers sequential).
- `terraform plan` post-apply: **No changes** — zero drift.

## Verified

- `describe-cluster-v2`: `ACTIVE`, `CurrentBrokerSoftwareInfo.KafkaVersion = 3.9.x`.
- All 11 `rtdp.*` topics confirmed at `rf=3`, `min.insync.replicas=2`
  via `rtdp-topics` DESCRIBE pod (resolves the earlier 2026-09-30 AWS
  Health RF==MinISR notice; the `rtdp-topic-fix` job had recreated
  topics with pinned minISR=2 that day).
- Zero pod restarts in `rtdp` namespace during the roll; public
  `https://ancient-implemented-olympic-fancy.trycloudflare.com/healthz`
  stayed 200 throughout.
- Post-upgrade live decision: `POST /v1/decide` (demo-client-a,
  `CLAIM_SUBMISSION`) → 200 `DECISION_APPROVE`,
  `decision_id 0077b4a5-21cc-4c48-9815-8106de9bbb71`.
- SNS alarm-email subscription to `rtdp-rtdp-sandbox-alarms`
  (atulchhoda@gmail.com) confirmed — closes the pre-existing
  drift where the subscription was unconfirmed.

## Cost impact

None — version upgrades are free; same broker instance types.
