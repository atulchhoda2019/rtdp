-- ADR-015/019: governed-door call facts. Every agent-gateway tool call
-- lands one row here (also produced to rtdp.agent.calls.v1). The call
-- graph and metering projections read from this table alone.

CREATE TABLE IF NOT EXISTS agent_call (
  call_id        text PRIMARY KEY,
  at             timestamptz NOT NULL DEFAULT now(),
  tenant_id      text NOT NULL,
  environment    text NOT NULL,
  task_id        text NOT NULL DEFAULT '',
  purpose        text NOT NULL DEFAULT '',
  principal_id   text NOT NULL,
  agent_id       text NOT NULL,
  agent_version  text NOT NULL DEFAULT '',
  chain_depth    integer NOT NULL DEFAULT 0,
  tool           text NOT NULL,
  backend        text NOT NULL,
  outcome        text NOT NULL
                 CHECK (outcome IN ('OK','DENIED','THROTTLED','ERROR',
                                    'PROPOSED','PENDING_APPROVAL')),
  decision_id    text,
  latency_ms     integer NOT NULL DEFAULT 0,
  cost_units     integer NOT NULL DEFAULT 0,
  classes_read   text[] NOT NULL DEFAULT '{}',
  detail         text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS agent_call_agent ON agent_call (tenant_id, agent_id, at);
CREATE INDEX IF NOT EXISTS agent_call_task  ON agent_call (tenant_id, task_id);
