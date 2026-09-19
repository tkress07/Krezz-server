-- Krezzcut partner reporting schema
-- Contains partner attribution and financial reporting data only.
-- Customer identity, shipping details, and STL data do not belong here.

CREATE TABLE IF NOT EXISTS salons (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    salon_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    salon_share_cents INTEGER NOT NULL DEFAULT 0
        CHECK (salon_share_cents >= 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (salon_code ~ '^[a-z0-9][a-z0-9_-]*$')
);

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

CREATE INDEX IF NOT EXISTS partner_orders_salon_paid_at_idx
    ON partner_orders (salon_id, paid_at DESC);

CREATE INDEX IF NOT EXISTS partner_orders_stylist_paid_at_idx
    ON partner_orders (stylist_id, paid_at DESC);

-- Current pilot partner. These inserts are idempotent and will not overwrite
-- later changes made to names, active status, or reporting amounts.
INSERT INTO salons (
    salon_code,
    name,
    salon_share_cents
)
VALUES (
    'supercuts_orange_ct',
    'Supercuts - Orange, CT',
    500
)
ON CONFLICT (salon_code) DO NOTHING;

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
