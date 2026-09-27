-- RTDP Phase 0/1 schema. Starting migration for key boundaries (design.md).
-- Tenant-private tables carry tenant_id and enable RLS. The runtime connects
-- as rtdp_runtime (non-owner, cannot bypass RLS); migrations run as rtdp.

CREATE TABLE IF NOT EXISTS tenant (
  tenant_id text PRIMARY KEY,
  status text NOT NULL,
  region_policy jsonb NOT NULL DEFAULT '{}',
  quotas jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS asset_version (
  owner_scope text NOT NULL,
  kind text NOT NULL,
  asset_id text NOT NULL,
  version text NOT NULL,
  spec jsonb NOT NULL,
  content_digest text NOT NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_scope, kind, asset_id, version)
);

CREATE TABLE IF NOT EXISTS subscription (
  tenant_id text NOT NULL,
  subscription_id text NOT NULL,
  revision integer NOT NULL,
  product_id text NOT NULL,
  product_version text NOT NULL,
  status text NOT NULL,
  spec jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, subscription_id, revision)
);

CREATE TABLE IF NOT EXISTS tenant_overlay (
  tenant_id text NOT NULL,
  overlay_id text NOT NULL,
  version integer NOT NULL,
  spec jsonb NOT NULL,
  PRIMARY KEY (tenant_id, overlay_id, version)
);

CREATE TABLE IF NOT EXISTS activation (
  tenant_id text NOT NULL,
  environment text NOT NULL,
  cohort text NOT NULL,
  epoch bigint NOT NULL,
  bundle_digest text NOT NULL,
  bundle jsonb NOT NULL,
  activated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, environment, cohort, epoch)
);

CREATE TABLE IF NOT EXISTS signal_event (
  tenant_id text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('LIVE','SHADOW','REPLAY')),
  signal_event_id text NOT NULL,
  envelope jsonb NOT NULL,
  payload_digest text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, mode, signal_event_id)
);

CREATE TABLE IF NOT EXISTS decision_fact (
  tenant_id text NOT NULL,
  mode text NOT NULL CHECK (mode IN ('LIVE','SHADOW','REPLAY')),
  decision_id text NOT NULL,
  generation integer NOT NULL CHECK (generation > 0),
  transaction_id text NOT NULL,
  transaction_revision integer NOT NULL,
  bundle_digest text NOT NULL,
  manifest_epoch bigint NOT NULL,
  decided_at timestamptz NOT NULL,
  outcome text NOT NULL,
  input_snapshot jsonb NOT NULL,
  signal_snapshot jsonb NOT NULL,
  result jsonb NOT NULL,
  payload_digest text NOT NULL,
  PRIMARY KEY (tenant_id, mode, decision_id, generation)
);

CREATE TABLE IF NOT EXISTS execution_event (
  tenant_id text NOT NULL,
  mode text NOT NULL,
  event_id text NOT NULL,
  kind text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, mode, event_id)
);

CREATE TABLE IF NOT EXISTS action_execution (
  tenant_id text NOT NULL,
  environment text NOT NULL,
  idempotency_key text NOT NULL,
  decision_id text NOT NULL,
  decision_generation integer NOT NULL,
  action_type text NOT NULL,
  intent jsonb NOT NULL,
  payload_digest text NOT NULL,
  state text NOT NULL,
  lease_generation bigint NOT NULL DEFAULT 0,
  lease_expires_at timestamptz,
  provider_reference text,
  expires_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, environment, idempotency_key)
);

CREATE TABLE IF NOT EXISTS action_transition (
  tenant_id text NOT NULL,
  environment text NOT NULL,
  idempotency_key text NOT NULL,
  seq integer NOT NULL,
  from_state text,
  to_state text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}',
  at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, environment, idempotency_key, seq)
);

CREATE TABLE IF NOT EXISTS flow_instance (
  tenant_id text NOT NULL,
  environment text NOT NULL,
  mode text NOT NULL,
  flow_id text NOT NULL,
  subject_id text NOT NULL,
  pinned_flow_version integer NOT NULL,
  bundle_digest text NOT NULL,
  state text NOT NULL,
  state_version bigint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, environment, mode, flow_id, subject_id)
);

CREATE TABLE IF NOT EXISTS flow_transition (
  tenant_id text NOT NULL,
  mode text NOT NULL,
  instance_key text NOT NULL,
  transition_seq integer NOT NULL,
  event_id text NOT NULL,
  guard_inputs jsonb NOT NULL DEFAULT '{}',
  at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, mode, instance_key, transition_seq)
);

CREATE TABLE IF NOT EXISTS inbox (
  consumer_name text NOT NULL,
  tenant_id text NOT NULL,
  message_id text NOT NULL,
  payload_digest text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer_name, tenant_id, message_id)
);

CREATE TABLE IF NOT EXISTS outbox (
  outbox_id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  topic text NOT NULL,
  message_key text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_at timestamptz
);

-- Runtime role: non-owner, cannot bypass RLS.
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'rtdp_runtime') THEN
    CREATE ROLE rtdp_runtime LOGIN PASSWORD 'rtdp_runtime';
  END IF;
END
$$;
GRANT CONNECT ON DATABASE rtdp TO rtdp_runtime;
GRANT USAGE ON SCHEMA public TO rtdp_runtime;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO rtdp_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO rtdp_runtime;

-- RLS on every tenant-private table.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'subscription','tenant_overlay','activation','signal_event',
    'decision_fact','execution_event','action_execution','action_transition',
    'flow_instance','flow_transition','inbox','outbox']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'DROP POLICY IF EXISTS %I ON %I', t || '_tenant_policy', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I USING (tenant_id = current_setting(''rtdp.tenant_id'', true))',
      t || '_tenant_policy', t);
  END LOOP;
END
$$;
