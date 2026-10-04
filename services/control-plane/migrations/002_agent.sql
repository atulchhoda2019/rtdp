-- ADR-013/019: agent registry. Agents, their scoped credentials, the
-- grants principals give them, and the revocation feed. Revocations are
-- also projected to the Redis set rtdp:agent:revoked by agent-registry
-- for request-path checks.

CREATE TABLE IF NOT EXISTS agent (
  agent_id      text PRIMARY KEY,
  tenant_id     text NOT NULL,
  kind          text NOT NULL CHECK (kind IN ('CUSTOMER','EMPLOYEE','VENDOR','PLATFORM')),
  owner_role    text NOT NULL,
  owner_identity text NOT NULL,
  status        text NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','SUSPENDED','REVOKED')),
  allowed_scopes text[] NOT NULL DEFAULT '{}',
  max_autonomy  text NOT NULL DEFAULT 'T1',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_credential (
  cred_id    text PRIMARY KEY,   -- the JWS kid for tokens this credential signs
  agent_id   text NOT NULL REFERENCES agent(agent_id),
  not_before timestamptz NOT NULL,
  not_after  timestamptz NOT NULL,
  revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS agent_grant (
  grant_id     text PRIMARY KEY,
  principal_id text NOT NULL,
  agent_id     text NOT NULL REFERENCES agent(agent_id),
  scopes       text[] NOT NULL,
  purpose      text NOT NULL,
  not_after    timestamptz NOT NULL,
  revoked_at   timestamptz
);

CREATE TABLE IF NOT EXISTS agent_revocation (
  target     text PRIMARY KEY,   -- cred_id (kid) or grant_id
  revoked_at timestamptz NOT NULL DEFAULT now(),
  reason     text NOT NULL DEFAULT ''
);

-- Registry's signing keys. Private key stored for the synthetic sandbox
-- only; AWS mints under a KMS asymmetric key and stores no private
-- material here (ADR-013).
CREATE TABLE IF NOT EXISTS agent_key (
  kid         text PRIMARY KEY,
  public_key  bytea NOT NULL,
  private_key bytea NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  retired_at  timestamptz
);

-- Per-tenant agent-plane settings. max_chain_depth defaults to 2
-- (ADR-013); mirrored to Redis rtdp:agent:maxdepth:{tenant} for the
-- request path.
CREATE TABLE IF NOT EXISTS agent_tenant_config (
  tenant_id       text PRIMARY KEY,
  max_chain_depth integer NOT NULL DEFAULT 2
);
