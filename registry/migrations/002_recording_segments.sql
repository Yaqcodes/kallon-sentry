-- Recording segment metadata (S3 object registry) + platform defaults.
-- Applied after 001_initial.sql on init_schema().

CREATE TABLE IF NOT EXISTS platform_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO platform_config (key, value)
VALUES ('recording_retention_days', '30')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS recording_segments (
    segment_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    device_id TEXT NOT NULL REFERENCES towers(device_id),
    camera INTEGER NOT NULL,
    filename TEXT NOT NULL,
    s3_bucket TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256_hex TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_sec INTEGER,
    UNIQUE (device_id, camera, filename)
);

CREATE INDEX IF NOT EXISTS idx_recording_segments_customer_started
    ON recording_segments (customer_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_recording_segments_device_cam_started
    ON recording_segments (device_id, camera, started_at DESC);
