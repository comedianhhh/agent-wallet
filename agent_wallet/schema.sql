-- agent-wallet schema. Idempotent: safe to run on every start.
-- Money is integer cents (bigint), one currency (CAD). No floats anywhere.

CREATE TABLE IF NOT EXISTS users (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Every balance lives in an account. A user's wallet, each merchant, and the outside world
-- ("funding": where top-ups come from) are all accounts, so every movement is a balanced entry.
CREATE TABLE IF NOT EXISTS accounts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL CHECK (kind IN ('wallet', 'merchant', 'funding')),
    user_id     uuid REFERENCES users(id),
    name        text NOT NULL,
    currency    text NOT NULL DEFAULT 'CAD' CHECK (currency = 'CAD'),
    created_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind = 'wallet') = (user_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_wallet_per_user ON accounts(user_id) WHERE kind = 'wallet';
CREATE UNIQUE INDEX IF NOT EXISTS one_funding_account ON accounts(kind) WHERE kind = 'funding';

CREATE TABLE IF NOT EXISTS merchants (
    id          text PRIMARY KEY CHECK (id ~ '^[a-z0-9][a-z0-9_-]{1,62}$'),
    name        text NOT NULL,
    account_id  uuid NOT NULL UNIQUE REFERENCES accounts(id)
);

-- Double-entry ledger. One journal entry = one business event; its postings must sum to zero.
CREATE TABLE IF NOT EXISTS journal_entries (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL CHECK (kind IN ('fund', 'payment', 'refund')),
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
    status           text NOT NULL CHECK (status IN ('pending_confirmation', 'settled', 'declined', 'expired')),
    idempotency_key  text NOT NULL,
    request_hash     text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    settled_at       timestamptz,
    UNIQUE (key_id, idempotency_key),
    CHECK (refunded_cents BETWEEN 0 AND amount_cents),
    CHECK (refunded_cents = 0 OR status = 'settled')
);
CREATE INDEX IF NOT EXISTS payments_by_key_time ON payments(key_id, created_at);

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
    outcome       text NOT NULL CHECK (outcome IN ('settled', 'pending_confirmation', 'replayed', 'denied')),
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

-- 2. A user's wallet never goes below zero (merchants and funding may).
CREATE OR REPLACE FUNCTION aw_check_wallet_nonnegative() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (SELECT kind FROM accounts WHERE id = NEW.account_id) = 'wallet'
       AND (SELECT SUM(amount_cents) FROM postings WHERE account_id = NEW.account_id) < 0 THEN
        RAISE EXCEPTION 'wallet % would go negative', NEW.account_id USING ERRCODE = 'check_violation';
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
