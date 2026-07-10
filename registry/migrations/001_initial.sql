-- registry/migrations/001_initial.sql
-- Kallon fleet registry — initial schema.
--
-- Target: PostgreSQL 16 on the Terra physical server (production).
-- The SQLite test provider (registry/sqlite_provider.py) applies a translated
-- subset of this schema in-memory for unit tests.
--
-- Identity formats (see docs/identity-and-secrets.md):
--   customer_id  cust_<slug>
--   device_id    kln_<slug>_<6-digit serial>
--   gateway_id   gw_<slug>
--   group_id     grp_<slug>_<site>
--   claim_code   clm_<base64url 16 bytes>

CREATE TABLE IF NOT EXISTS customers (
    customer_id         TEXT PRIMARY KEY,
    display_name        TEXT        NOT NULL,
    vpn_subnet          TEXT        NOT NULL UNIQUE,         -- e.g. 10.50.0.0/24
    gateway_id          TEXT,
    gateway_endpoint    TEXT,                                -- host:51820
    gateway_public_key  TEXT,
    hub_alert_url       TEXT,                                -- http://10.50.0.1:8080/alerts
    hub_provider        TEXT        NOT NULL DEFAULT 'manual'
                        CHECK (hub_provider IN ('lightsail','hetzner','ovh','proxmox','manual')),
    hub_host_id         TEXT,                                -- provider instance id or manual host
    status              TEXT        NOT NULL DEFAULT 'pending_hub'
                        CHECK (status IN ('pending_hub','active','suspended')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS towers (
    device_id           TEXT PRIMARY KEY,
    customer_id         TEXT        NOT NULL REFERENCES customers(customer_id) ON DELETE RESTRICT,
    group_id            TEXT,
    vpn_ip              TEXT        UNIQUE,                  -- 10.50.0.x (allocated)
    wg_public_key       TEXT,                               -- filled at provision/enroll
    claim_code          TEXT        UNIQUE,
    enrollment_token_hash TEXT,                             -- sha256 of one-time token
    manufactured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    enrolled_at         TIMESTAMPTZ,                        -- null until first boot
    acceptance_status   TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (acceptance_status IN ('pending','pass','fail')),
    status              TEXT        NOT NULL DEFAULT 'manufactured'
                        CHECK (status IN ('manufactured','enrolled','active','suspended')),
    shipped_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_towers_customer ON towers(customer_id);

-- Per-customer monotonic host-octet allocator (the .2-.99 tower range).
CREATE TABLE IF NOT EXISTS ip_allocations (
    customer_id         TEXT PRIMARY KEY REFERENCES customers(customer_id) ON DELETE CASCADE,
    next_host_octet     INTEGER     NOT NULL DEFAULT 2
);

CREATE TABLE IF NOT EXISTS audit_events (
    id                  BIGSERIAL PRIMARY KEY,
    event_type          TEXT        NOT NULL,               -- customer_created, tower_registered, ...
    entity_id           TEXT,                               -- customer_id / device_id
    actor               TEXT,                               -- ops user / 'enrollment-api'
    payload_json        TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_id);
