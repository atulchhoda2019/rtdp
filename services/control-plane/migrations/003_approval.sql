-- ADR-014: approval holds and the human-approval ledger. Held action
-- commands arrive on rtdp.action.commands.v1 with status
-- AWAITING_APPROVAL; approval-service projects them here, adjudicates,
-- and republishes READY / CANCELLED / EXPIRED.

CREATE TABLE IF NOT EXISTS approval_held (
  tenant_id            text NOT NULL,
  decision_id          text NOT NULL,
  idempotency_key      text NOT NULL,
  environment          text NOT NULL,
  action_type          text NOT NULL,
  decision_generation  bigint NOT NULL DEFAULT 1,
  command              bytea NOT NULL,           -- marshaled ActionCommand
  requester_identities text[] NOT NULL DEFAULT '{}',
  approvers            text[] NOT NULL DEFAULT '{}',
  escalation_approvers text[] NOT NULL DEFAULT '{}',
  sla_breach_action    text NOT NULL DEFAULT 'EXPIRE',
  approval_due_at      timestamptz,
  state                text NOT NULL DEFAULT 'HELD'
                       CHECK (state IN ('HELD','ESCALATED','RELEASED',
                                        'REJECTED','EXPIRED')),
  escalated            boolean NOT NULL DEFAULT false,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, decision_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS approval_event (
  event_id        text PRIMARY KEY,
  tenant_id       text NOT NULL,
  decision_id     text NOT NULL,
  verdict         text NOT NULL
                  CHECK (verdict IN ('APPROVED','REJECTED','ESCALATED','EXPIRED')),
  actor_identity  text NOT NULL DEFAULT '',
  note            text NOT NULL DEFAULT '',
  intent_keys     text[] NOT NULL DEFAULT '{}',
  at              timestamptz NOT NULL DEFAULT now()
);
