-- Krezzcut partner reporting and dashboard schema
-- This migration is additive and idempotent. It never drops a table or row.
-- Customer identity, shipping details, and STL data do not belong here.

CREATE TABLE IF NOT EXISTS salons (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    salon_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    location_label TEXT NOT NULL DEFAULT '',
    salon_share_cents INTEGER NOT NULL DEFAULT 0
        CHECK (salon_share_cents >= 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (salon_code ~ '^[a-z0-9][a-z0-9_-]*$')
);

ALTER TABLE salons
    ADD COLUMN IF NOT EXISTS location_label TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS stylists (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    salon_id BIGINT NOT NULL
        REFERENCES salons(id) ON DELETE RESTRICT,
    stylist_code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    stylist_credit_cents INTEGER NOT NULL DEFAULT 0
        CHECK (stylist_credit_cents >= 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (salon_id, stylist_code),
    UNIQUE (id, salon_id),
    CHECK (stylist_code ~ '^[a-z0-9][a-z0-9_-]*$')
);

CREATE TABLE IF NOT EXISTS partner_orders (
    order_id TEXT PRIMARY KEY,
    stripe_checkout_session_id TEXT NOT NULL UNIQUE,
    stripe_event_id TEXT NOT NULL UNIQUE,
    salon_id BIGINT NOT NULL
        REFERENCES salons(id) ON DELETE RESTRICT,
    stylist_id BIGINT NOT NULL,
    amount_subtotal_cents INTEGER NOT NULL
        CHECK (amount_subtotal_cents >= 0),
    discount_cents INTEGER NOT NULL DEFAULT 0
        CHECK (discount_cents >= 0),
    tax_cents INTEGER NOT NULL DEFAULT 0
        CHECK (tax_cents >= 0),
    amount_total_cents INTEGER NOT NULL
        CHECK (amount_total_cents >= 0),
    partner_revenue_cents INTEGER NOT NULL
        CHECK (partner_revenue_cents >= 0),
    stylist_credit_cents INTEGER NOT NULL
        CHECK (stylist_credit_cents >= 0),
    salon_share_cents INTEGER NOT NULL
        CHECK (salon_share_cents >= 0),
    krezzcut_share_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'usd'
        CHECK (currency ~ '^[a-z]{3}$'),
    payment_status TEXT NOT NULL,
    livemode BOOLEAN NOT NULL,
    paid_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT partner_orders_stylist_belongs_to_salon
        FOREIGN KEY (stylist_id, salon_id)
        REFERENCES stylists(id, salon_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS salon_users (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    salon_id BIGINT NOT NULL
        REFERENCES salons(id) ON DELETE RESTRICT,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    failed_login_count INTEGER NOT NULL DEFAULT 0
        CHECK (failed_login_count >= 0),
    locked_until TIMESTAMPTZ,
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS partner_orders_salon_paid_at_idx
    ON partner_orders (salon_id, paid_at DESC);

CREATE INDEX IF NOT EXISTS partner_orders_stylist_paid_at_idx
    ON partner_orders (stylist_id, paid_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS salon_users_email_lower_uidx
    ON salon_users (LOWER(email));

CREATE INDEX IF NOT EXISTS salon_users_salon_id_idx
    ON salon_users (salon_id);

CREATE TABLE IF NOT EXISTS salon_user_access (
    user_id BIGINT NOT NULL
        REFERENCES salon_users(id) ON DELETE CASCADE,
    salon_id BIGINT NOT NULL
        REFERENCES salons(id) ON DELETE RESTRICT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, salon_id)
);

CREATE INDEX IF NOT EXISTS salon_user_access_salon_id_idx
    ON salon_user_access (salon_id);

-- Preserve every existing login's current salon access. DO NOTHING is
-- intentional: rerunning the schema must never reactivate revoked access.
INSERT INTO salon_user_access (
    user_id,
    salon_id
)
SELECT
    id,
    salon_id
FROM salon_users
ON CONFLICT (user_id, salon_id) DO NOTHING;

-- Current pilot partner. These statements never overwrite existing records.
INSERT INTO salons (
    salon_code,
    name,
    location_label,
    salon_share_cents
)
VALUES (
    'supercuts_orange_ct',
    'Supercuts',
    'Orange, CT',
    500
)
ON CONFLICT (salon_code) DO NOTHING;

UPDATE salons
SET
    location_label = 'Orange, CT',
    updated_at = NOW()
WHERE salon_code = 'supercuts_orange_ct'
  AND COALESCE(location_label, '') = '';

INSERT INTO stylists (
    salon_id,
    stylist_code,
    display_name,
    stylist_credit_cents
)
SELECT
    id,
    'rachel',
    'Rachel',
    2000
FROM salons
WHERE salon_code = 'supercuts_orange_ct'
ON CONFLICT (salon_id, stylist_code) DO NOTHING;
