CREATE TABLE IF NOT EXISTS assignments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS checklist_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assignment_id BIGINT NOT NULL REFERENCES assignments (id) ON DELETE CASCADE,
    title VARCHAR(200) NOT NULL,
    evidence TEXT NOT NULL,
    completed BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS checklist_items_assignment_idx
    ON checklist_items (assignment_id, id);

ALTER TABLE assignments ADD COLUMN IF NOT EXISTS deadline_evidence TEXT NOT NULL DEFAULT '';
ALTER TABLE assignments ADD COLUMN IF NOT EXISTS extraction_reviewed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE checklist_items ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'requirement'
    CHECK (kind IN ('requirement', 'suggestion'));
ALTER TABLE checklist_items ADD COLUMN IF NOT EXISTS extraction_engine TEXT NOT NULL DEFAULT 'manual'
    CHECK (extraction_engine IN ('manual', 'basic', 'ai'));

CREATE TABLE IF NOT EXISTS extraction_drafts (
    id UUID PRIMARY KEY,
    assignment_id BIGINT NOT NULL REFERENCES assignments (id) ON DELETE CASCADE,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    saved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS extraction_drafts_assignment_idx
    ON extraction_drafts (assignment_id);
