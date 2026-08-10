CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id     TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    repo_full_name  TEXT    NOT NULL,
    payload         TEXT    NOT NULL,
    signature       TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'received'
                    CHECK (status IN ('received','processing','completed','failed')),
    retry_count     INTEGER NOT NULL DEFAULT 0,
    claim_token     TEXT,
    agent_id        TEXT,
    error_message   TEXT,
    received_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    claimed_at      TEXT,
    completed_at    TEXT
);

-- GitHub redelivery deduplication
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_delivery
    ON events(delivery_id);

-- claim scan: order by id for FIFO
CREATE INDEX IF NOT EXISTS ix_events_status_received
    ON events(id) WHERE status = 'received';

-- TTL requeue scan
CREATE INDEX IF NOT EXISTS ix_events_status_processing
    ON events(claimed_at) WHERE status = 'processing';
