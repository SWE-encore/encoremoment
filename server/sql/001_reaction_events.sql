CREATE TABLE IF NOT EXISTS reaction_events (
    session_id text NOT NULL,
    event_id text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    event jsonb NOT NULL CHECK (
        event ?& ARRAY['event_id', 'phase']
        AND event->>'event_id' = event_id AND event->>'phase' = 'FINAL'
    ),
    PRIMARY KEY (session_id, event_id)
);
