CREATE TABLE annotation_sets (
    annotation_set_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    purpose TEXT NOT NULL,
    label_schema_version TEXT NOT NULL,
    selection_rules_json TEXT NOT NULL,
    content_grouping_method TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE (name, version)
);

CREATE TABLE annotation_items (
    annotation_item_id TEXT PRIMARY KEY,
    annotation_set_id TEXT NOT NULL
        REFERENCES annotation_sets(annotation_set_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    image_occurrence_id TEXT REFERENCES image_occurrences(image_occurrence_id),
    global_thumbnail_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    task_types_json TEXT NOT NULL,
    content_context_visibility TEXT NOT NULL CHECK (
        content_context_visibility IN ('image_only', 'occurrence_context')
    ),
    coverage_tags_json TEXT NOT NULL,
    group_id TEXT NOT NULL,
    split TEXT CHECK (split IN ('train', 'validation', 'test')),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (annotation_set_id, image_id, image_occurrence_id)
);

CREATE TABLE annotation_events (
    annotation_event_id TEXT PRIMARY KEY,
    annotation_item_id TEXT NOT NULL
        REFERENCES annotation_items(annotation_item_id),
    annotator_id TEXT NOT NULL,
    annotation_type TEXT NOT NULL CHECK (
        annotation_type IN (
            'role',
            'eligibility',
            'region',
            'mask',
            'multi_shade',
            'occurrence_relation',
            'revoke',
            'adjudication'
        )
    ),
    role_code TEXT CHECK (
        role_code IS NULL OR role_code IN (
            'single_bullet',
            'single_swatch',
            'lip_effect',
            'multi_shade_comparison',
            'color_card',
            'packaging',
            'text_promo',
            'invalid'
        )
    ),
    eligibility_label INTEGER CHECK (
        eligibility_label IS NULL OR eligibility_label IN (0, 1)
    ),
    eligibility_reason_codes_json TEXT NOT NULL,
    region_type TEXT,
    bbox_image_json TEXT,
    polygon_image_json TEXT,
    mask_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    multi_shade_annotation_json TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    supersedes_event_id TEXT REFERENCES annotation_events(annotation_event_id),
    created_at TEXT NOT NULL
);

CREATE TRIGGER annotation_events_no_update
BEFORE UPDATE ON annotation_events
BEGIN
    SELECT RAISE(ABORT, 'annotation_events are append-only');
END;

CREATE TRIGGER annotation_events_no_delete
BEFORE DELETE ON annotation_events
BEGIN
    SELECT RAISE(ABORT, 'annotation_events are append-only');
END;

CREATE TABLE evaluation_sets (
    evaluation_set_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    source_annotation_set_ids_json TEXT NOT NULL,
    selection_rules_json TEXT NOT NULL,
    content_grouping_method TEXT NOT NULL,
    split_policy_json TEXT NOT NULL,
    metric_schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    UNIQUE (name, version)
);

CREATE TABLE evaluation_set_items (
    evaluation_set_item_id TEXT PRIMARY KEY,
    evaluation_set_id TEXT NOT NULL REFERENCES evaluation_sets(evaluation_set_id),
    annotation_item_id TEXT NOT NULL REFERENCES annotation_items(annotation_item_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    image_occurrence_id TEXT REFERENCES image_occurrences(image_occurrence_id),
    group_id TEXT NOT NULL,
    split TEXT NOT NULL CHECK (split IN ('train', 'validation', 'test')),
    slice_tags_json TEXT NOT NULL,
    ground_truth_version TEXT NOT NULL,
    UNIQUE (evaluation_set_id, annotation_item_id)
);

CREATE TABLE evaluation_runs (
    evaluation_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    evaluation_set_id TEXT NOT NULL REFERENCES evaluation_sets(evaluation_set_id),
    prediction_run_id TEXT REFERENCES pipeline_runs(run_id),
    metric_schema_version TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary_json TEXT NOT NULL
);

CREATE TABLE evaluation_metrics (
    evaluation_metric_id TEXT PRIMARY KEY,
    evaluation_run_id TEXT NOT NULL REFERENCES evaluation_runs(evaluation_run_id),
    metric_name TEXT NOT NULL,
    slice_name TEXT NOT NULL,
    metric_value REAL,
    evaluation_status TEXT NOT NULL,
    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
    details_json TEXT NOT NULL,
    UNIQUE (evaluation_run_id, metric_name, slice_name)
);

CREATE TABLE performance_threshold_versions (
    threshold_version TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_definition_version TEXT NOT NULL,
    slice_name TEXT NOT NULL,
    operator TEXT NOT NULL,
    target_value REAL,
    status TEXT NOT NULL CHECK (status IN ('provisional_target', 'frozen')),
    pilot_run_id TEXT REFERENCES pipeline_runs(run_id),
    annotation_set_id TEXT REFERENCES annotation_sets(annotation_set_id),
    baseline_value REAL,
    sample_count INTEGER NOT NULL CHECK (sample_count >= 0),
    rationale TEXT NOT NULL,
    approved_by TEXT,
    created_at TEXT NOT NULL,
    frozen_at TEXT,
    PRIMARY KEY (threshold_version, metric_name, slice_name)
);

CREATE INDEX idx_annotation_items_set
    ON annotation_items(annotation_set_id, status);
CREATE INDEX idx_annotation_events_item
    ON annotation_events(annotation_item_id, created_at);
CREATE INDEX idx_evaluation_items_set
    ON evaluation_set_items(evaluation_set_id, split);
