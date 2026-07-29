CREATE TABLE context_review_sampling_policies (
    context_review_sampling_policy_id TEXT PRIMARY KEY,
    pilot_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    annotation_set_id TEXT NOT NULL
        REFERENCES annotation_sets(annotation_set_id),
    policy_version TEXT NOT NULL,
    selection_seed TEXT NOT NULL,
    target_count INTEGER NOT NULL CHECK (target_count > 0),
    quotas_json TEXT NOT NULL,
    source_relation_counts_json TEXT NOT NULL,
    source_population_sha256 TEXT NOT NULL,
    selection_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (pilot_run_id, policy_version)
);

CREATE TABLE context_review_sample_items (
    context_review_sampling_policy_id TEXT NOT NULL
        REFERENCES context_review_sampling_policies(
            context_review_sampling_policy_id
        ),
    annotation_item_id TEXT NOT NULL
        REFERENCES annotation_items(annotation_item_id),
    model_relationship TEXT NOT NULL CHECK (
        model_relationship IN (
            'exact_shade_match',
            'contains_context_shade',
            'same_product_unspecified_shade',
            'shade_conflict',
            'unrelated',
            'insufficient_evidence'
        )
    ),
    model_confidence REAL NOT NULL CHECK (
        model_confidence >= 0.0 AND model_confidence <= 1.0
    ),
    selected_reason TEXT NOT NULL,
    deterministic_rank TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        context_review_sampling_policy_id,
        annotation_item_id
    )
);

CREATE TABLE pilot_gate_decisions (
    pilot_gate_decision_id TEXT PRIMARY KEY,
    pilot_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    context_review_sampling_policy_id TEXT NOT NULL
        REFERENCES context_review_sampling_policies(
            context_review_sampling_policy_id
        ),
    decision TEXT NOT NULL CHECK (decision IN ('go', 'no_go')),
    approved_by TEXT NOT NULL,
    evidence_summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER context_review_sampling_policies_no_update
BEFORE UPDATE ON context_review_sampling_policies
BEGIN
    SELECT RAISE(
        ABORT,
        'context_review_sampling_policies are immutable'
    );
END;

CREATE TRIGGER context_review_sampling_policies_no_delete
BEFORE DELETE ON context_review_sampling_policies
BEGIN
    SELECT RAISE(
        ABORT,
        'context_review_sampling_policies are immutable'
    );
END;

CREATE TRIGGER context_review_sample_items_no_update
BEFORE UPDATE ON context_review_sample_items
BEGIN
    SELECT RAISE(ABORT, 'context_review_sample_items are immutable');
END;

CREATE TRIGGER context_review_sample_items_no_delete
BEFORE DELETE ON context_review_sample_items
BEGIN
    SELECT RAISE(ABORT, 'context_review_sample_items are immutable');
END;

CREATE TRIGGER pilot_gate_decisions_no_update
BEFORE UPDATE ON pilot_gate_decisions
BEGIN
    SELECT RAISE(ABORT, 'pilot_gate_decisions are append-only');
END;

CREATE TRIGGER pilot_gate_decisions_no_delete
BEFORE DELETE ON pilot_gate_decisions
BEGIN
    SELECT RAISE(ABORT, 'pilot_gate_decisions are append-only');
END;

CREATE INDEX idx_context_review_sample_items_annotation
    ON context_review_sample_items(annotation_item_id);
CREATE INDEX idx_pilot_gate_decisions_run
    ON pilot_gate_decisions(pilot_run_id, created_at);
