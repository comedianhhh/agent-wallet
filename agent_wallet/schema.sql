-- agent-wallet schema. Idempotent: safe to run on every start.
-- Money is integer cents (bigint), one currency (CAD). No floats anywhere.

CREATE TABLE IF NOT EXISTS users (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Every balance lives in an account. A user's wallet, the user's held funds (authorized, not yet
-- paid out), each merchant, and the outside world ("funding": where top-ups come from) are all
-- accounts, so every movement is a balanced entry.
CREATE TABLE IF NOT EXISTS accounts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL CHECK (kind IN ('wallet', 'hold', 'merchant', 'funding')),
    user_id     uuid REFERENCES users(id),
    name        text NOT NULL,
    currency    text NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind IN ('wallet', 'hold')) = (user_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_wallet_per_user ON accounts(user_id) WHERE kind = 'wallet';
CREATE UNIQUE INDEX IF NOT EXISTS one_hold_per_user ON accounts(user_id) WHERE kind = 'hold';
CREATE UNIQUE INDEX IF NOT EXISTS one_funding_account ON accounts(kind) WHERE kind = 'funding';

CREATE TABLE IF NOT EXISTS merchants (
    id          text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{1,62}$'),
    name        text NOT NULL,
    account_id  uuid NOT NULL UNIQUE REFERENCES accounts(id)
);

-- Double-entry ledger. One journal entry = one business event; its postings must sum to zero.
CREATE TABLE IF NOT EXISTS journal_entries (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- payment lifecycle: hold (wallet -> hold), then capture (hold -> merchant) or release (hold -> wallet)
    kind        text NOT NULL CHECK (kind IN ('fund', 'hold', 'capture', 'release', 'refund')),
    ref_id      uuid,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS postings (
    id            bigserial PRIMARY KEY,
    entry_id      uuid NOT NULL REFERENCES journal_entries(id),
    account_id    uuid NOT NULL REFERENCES accounts(id),
    amount_cents  bigint NOT NULL CHECK (amount_cents <> 0)
);
CREATE INDEX IF NOT EXISTS postings_by_account ON postings(account_id);
CREATE INDEX IF NOT EXISTS postings_by_entry ON postings(entry_id);

-- Agent API keys. Only a SHA-256 of the secret is stored. The limits sit on the key, so each
-- agent gets its own budget, and a leaked key can lose at most its own daily cap.
CREATE TABLE IF NOT EXISTS agent_keys (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                uuid NOT NULL REFERENCES users(id),
    wallet_id              uuid NOT NULL REFERENCES accounts(id),
    label                  text NOT NULL,
    key_prefix             text NOT NULL,
    key_hash               bytea NOT NULL UNIQUE,
    scope                  text NOT NULL CHECK (scope IN ('read', 'pay')),
    per_payment_cap_cents  bigint NOT NULL CHECK (per_payment_cap_cents >= 0),
    daily_cap_cents        bigint NOT NULL CHECK (daily_cap_cents >= 0),
    confirm_above_cents    bigint CHECK (confirm_above_cents >= 0),  -- NULL = never ask
    expires_at             timestamptz,
    revoked_at             timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now()
);

-- Added in 0.3. ALTER ... IF NOT EXISTS so existing databases pick it up without a reset.
ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS rate_limit_per_min int NOT NULL DEFAULT 60
    CHECK (rate_limit_per_min > 0);
-- Added in 0.4: a second payment to the same merchant for the same amount within this many minutes
-- waits for the user (the red-team eval showed models paying one order several times on request).
ALTER TABLE agent_keys ADD COLUMN IF NOT EXISTS confirm_repeat_minutes int CHECK (confirm_repeat_minutes > 0);

-- Calls per key per minute (fixed window). Checked before every tool call, so a looping or
-- hijacked agent can't flood the wallet, the rail, or the decisions log.
CREATE TABLE IF NOT EXISTS rate_windows (
    key_id        uuid NOT NULL REFERENCES agent_keys(id),
    window_start  timestamptz NOT NULL,
    count         int NOT NULL,
    PRIMARY KEY (key_id, window_start)
);

CREATE TABLE IF NOT EXISTS key_merchants (
    key_id       uuid NOT NULL REFERENCES agent_keys(id),
    merchant_id  text NOT NULL REFERENCES merchants(id),
    PRIMARY KEY (key_id, merchant_id)
);

CREATE TABLE IF NOT EXISTS payments (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id           uuid NOT NULL REFERENCES agent_keys(id),
    wallet_id        uuid NOT NULL REFERENCES accounts(id),
    merchant_id      text NOT NULL REFERENCES merchants(id),
    amount_cents     bigint NOT NULL CHECK (amount_cents > 0),
    refunded_cents   bigint NOT NULL DEFAULT 0,
    memo             text NOT NULL DEFAULT '',
    -- pending_confirmation -> processing -> settled | failed      (funds held from the start)
    -- pending_confirmation -> declined | expired                  (hold released)
    status           text NOT NULL CHECK (status IN
                         ('pending_confirmation', 'processing', 'settled', 'failed', 'declined', 'expired')),
    idempotency_key  text NOT NULL,
    request_hash     text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    settled_at       timestamptz,
    -- outbox: a 'processing' payment IS the job. The worker claims it by pushing next_attempt_at
    -- forward (a lease), calls the rail with the payment id as idempotency key, then applies the result.
    attempts         int NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz,
    last_error       text NOT NULL DEFAULT '',
    rail_ref         text,
    UNIQUE (key_id, idempotency_key),
    CHECK (refunded_cents BETWEEN 0 AND amount_cents),
    CHECK (refunded_cents = 0 OR status = 'settled')
);
CREATE INDEX IF NOT EXISTS payments_by_key_time ON payments(key_id, created_at);
CREATE INDEX IF NOT EXISTS payments_due ON payments(next_attempt_at) WHERE status = 'processing';
ALTER TABLE payments ADD COLUMN IF NOT EXISTS confirm_reason text NOT NULL DEFAULT '';

-- What the last reconciliation against the rail's settlement report found. Empty run = clean.
CREATE TABLE IF NOT EXISTS recon_issues (
    id          bigserial PRIMARY KEY,
    run_id      uuid NOT NULL,
    payment_id  uuid,
    rail_key    text,
    kind        text NOT NULL,
    detail      text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS refunds (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id       uuid NOT NULL REFERENCES payments(id),
    amount_cents     bigint NOT NULL CHECK (amount_cents > 0),
    reason           text NOT NULL DEFAULT '',
    idempotency_key  text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (payment_id, idempotency_key)
);

-- Every attempt the agent makes, allowed or not, with the reason. Denials are the interesting rows.
CREATE TABLE IF NOT EXISTS decisions (
    id            bigserial PRIMARY KEY,
    key_id        uuid REFERENCES agent_keys(id),
    action        text NOT NULL,
    merchant_id   text,
    amount_cents  bigint,
    outcome       text NOT NULL CHECK (outcome IN ('processing', 'pending_confirmation', 'replayed', 'denied')),
    reason        text NOT NULL DEFAULT '',
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------- invariants enforced by the database, not just the app ----------

-- 1. Every journal entry balances (checked at commit, after all its postings are in).
CREATE OR REPLACE FUNCTION aw_check_entry_balanced() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT COALESCE(SUM(amount_cents), 0) FROM postings WHERE entry_id = NEW.entry_id) <> 0 THEN
        RAISE EXCEPTION 'journal entry % does not balance', NEW.entry_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS postings_balanced ON postings;
CREATE CONSTRAINT TRIGGER postings_balanced AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aw_check_entry_balanced();

-- 2. A user's wallet and held funds never go below zero (merchants and funding may).
CREATE OR REPLACE FUNCTION aw_check_wallet_nonnegative() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT kind FROM accounts WHERE id = NEW.account_id) IN ('wallet', 'hold')
       AND (SELECT SUM(amount_cents) FROM postings WHERE account_id = NEW.account_id) < 0 THEN
        RAISE EXCEPTION 'user account % would go negative', NEW.account_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS wallet_nonnegative ON postings;
CREATE CONSTRAINT TRIGGER wallet_nonnegative AFTER INSERT ON postings
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION aw_check_wallet_nonnegative();

-- 3. The ledger is append-only: mistakes are fixed with a new entry (a refund), never an edit.
CREATE OR REPLACE FUNCTION aw_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;
DROP TRIGGER IF EXISTS postings_append_only ON postings;
CREATE TRIGGER postings_append_only BEFORE UPDATE OR DELETE ON postings
    FOR EACH ROW EXECUTE FUNCTION aw_append_only();
DROP TRIGGER IF EXISTS journal_append_only ON journal_entries;
CREATE TRIGGER journal_append_only BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION aw_append_only();
