CREATE TABLE owner_review_delegations (
    owner_review_delegation_id TEXT PRIMARY KEY,
    pilot_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    scope TEXT NOT NULL,
    instruction_text TEXT NOT NULL,
    delegated_agent TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE pilot_sample_additions (
    pilot_sample_addition_id TEXT PRIMARY KEY,
    pilot_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    selection_method TEXT NOT NULL,
    selection_version TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    candidate_evidence_json TEXT NOT NULL,
    owner_review_delegation_id TEXT NOT NULL
        REFERENCES owner_review_delegations(owner_review_delegation_id),
    created_at TEXT NOT NULL,
    UNIQUE (pilot_run_id, image_id),
    FOREIGN KEY (pilot_run_id, image_id)
        REFERENCES pilot_samples(pilot_run_id, image_id)
);

ALTER TABLE annotation_events
    ADD COLUMN review_provenance TEXT NOT NULL DEFAULT 'human'
    CHECK (
        review_provenance IN ('human', 'owner_delegated_agent')
    );

ALTER TABLE annotation_events
    ADD COLUMN owner_review_delegation_id TEXT
    REFERENCES owner_review_delegations(owner_review_delegation_id);

ALTER TABLE pilot_samples
    ADD COLUMN review_provenance TEXT NOT NULL DEFAULT 'human'
    CHECK (
        review_provenance IN ('human', 'owner_delegated_agent')
    );

CREATE TRIGGER owner_review_delegations_no_update
BEFORE UPDATE ON owner_review_delegations
BEGIN
    SELECT RAISE(ABORT, 'owner_review_delegations are immutable');
END;

CREATE TRIGGER owner_review_delegations_no_delete
BEFORE DELETE ON owner_review_delegations
BEGIN
    SELECT RAISE(ABORT, 'owner_review_delegations are immutable');
END;

CREATE TRIGGER pilot_sample_additions_no_update
BEFORE UPDATE ON pilot_sample_additions
BEGIN
    SELECT RAISE(ABORT, 'pilot_sample_additions are immutable');
END;

CREATE TRIGGER pilot_sample_additions_no_delete
BEFORE DELETE ON pilot_sample_additions
BEGIN
    SELECT RAISE(ABORT, 'pilot_sample_additions are immutable');
END;

CREATE TRIGGER annotation_events_delegation_required
BEFORE INSERT ON annotation_events
WHEN NEW.review_provenance = 'owner_delegated_agent'
     AND (
         NEW.owner_review_delegation_id IS NULL
         OR NOT EXISTS (
             SELECT 1
             FROM owner_review_delegations AS delegation
             JOIN annotation_items AS item
               ON item.annotation_item_id = NEW.annotation_item_id
             JOIN annotation_sets AS annotation_set
               ON annotation_set.annotation_set_id =
                  item.annotation_set_id
             WHERE delegation.owner_review_delegation_id =
                   NEW.owner_review_delegation_id
               AND delegation.pilot_run_id = annotation_set.run_id
         )
     )
BEGIN
    SELECT RAISE(
        ABORT,
        'owner-delegated event requires a matching immutable delegation'
    );
END;

CREATE TRIGGER annotation_events_human_delegation_forbidden
BEFORE INSERT ON annotation_events
WHEN NEW.review_provenance = 'human'
     AND NEW.owner_review_delegation_id IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'human event cannot reference an owner review delegation'
    );
END;

CREATE INDEX idx_owner_review_delegations_run
    ON owner_review_delegations(pilot_run_id, created_at);
CREATE INDEX idx_pilot_sample_additions_run
    ON pilot_sample_additions(pilot_run_id, created_at);
CREATE INDEX idx_annotation_events_provenance
    ON annotation_events(review_provenance, owner_review_delegation_id);
