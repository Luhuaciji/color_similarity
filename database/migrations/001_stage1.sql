CREATE TABLE dataset_snapshots (
    dataset_snapshot_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    column_schema_json TEXT NOT NULL,
    naming_rule_version TEXT NOT NULL,
    root_aliases_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE pipeline_runs (
    run_id TEXT PRIMARY KEY,
    dataset_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(dataset_snapshot_id),
    stage TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    git_dirty INTEGER NOT NULL CHECK (git_dirty IN (0, 1)),
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    dependency_snapshot_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    error_summary_json TEXT NOT NULL
);

CREATE TABLE folder_groups (
    folder_group_id TEXT PRIMARY KEY,
    dataset_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(dataset_snapshot_id),
    brand_folder_raw TEXT NOT NULL,
    product_folder_raw TEXT NOT NULL,
    relative_folder_path TEXT NOT NULL,
    source_record_count INTEGER NOT NULL CHECK (source_record_count >= 0),
    image_occurrence_count INTEGER NOT NULL CHECK (image_occurrence_count >= 0),
    collision_status TEXT NOT NULL,
    UNIQUE (dataset_snapshot_id, relative_folder_path)
);

CREATE TABLE source_records (
    source_record_id TEXT PRIMARY KEY,
    dataset_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(dataset_snapshot_id),
    folder_group_id TEXT NOT NULL REFERENCES folder_groups(folder_group_id),
    row_number INTEGER NOT NULL CHECK (row_number >= 2),
    row_hash TEXT NOT NULL,
    asset_id_raw TEXT NOT NULL,
    sku_id_raw TEXT NOT NULL,
    goods_id_raw TEXT NOT NULL,
    brand_id_raw TEXT NOT NULL,
    brand_name_raw TEXT NOT NULL,
    sku_name_raw TEXT NOT NULL,
    sku_concat_name_raw TEXT NOT NULL,
    sku_color_no_raw TEXT NOT NULL,
    raw_record_json TEXT NOT NULL,
    UNIQUE (dataset_snapshot_id, row_number)
);

CREATE TABLE source_image_refs (
    source_ref_id TEXT PRIMARY KEY,
    source_record_id TEXT NOT NULL REFERENCES source_records(source_record_id),
    source_field TEXT NOT NULL,
    image_index INTEGER NOT NULL CHECK (image_index >= 1),
    source_url TEXT NOT NULL,
    source_url_hash TEXT NOT NULL,
    declared_extension TEXT NOT NULL,
    expected_relative_path TEXT NOT NULL,
    download_status TEXT NOT NULL,
    unmatched_reason TEXT,
    http_metadata_json TEXT NOT NULL,
    UNIQUE (source_record_id, source_field, image_index)
);

CREATE TABLE image_contents (
    image_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    detected_format TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    CHECK (image_id = sha256)
);

CREATE TABLE image_occurrences (
    image_occurrence_id TEXT PRIMARY KEY,
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    folder_group_id TEXT NOT NULL REFERENCES folder_groups(folder_group_id),
    root_alias TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    extension_mismatch INTEGER NOT NULL CHECK (extension_mismatch IN (0, 1)),
    brand_folder_raw TEXT NOT NULL,
    product_folder_raw TEXT NOT NULL,
    legacy_image_id TEXT,
    source_exists INTEGER NOT NULL CHECK (source_exists IN (0, 1)),
    source_mtime_ns INTEGER NOT NULL,
    UNIQUE (root_alias, relative_path)
);

CREATE TABLE source_ref_occurrences (
    source_ref_id TEXT NOT NULL REFERENCES source_image_refs(source_ref_id),
    image_occurrence_id TEXT NOT NULL REFERENCES image_occurrences(image_occurrence_id),
    match_method TEXT NOT NULL,
    match_confidence REAL NOT NULL CHECK (
        match_confidence >= 0.0 AND match_confidence <= 1.0
    ),
    PRIMARY KEY (source_ref_id, image_occurrence_id)
);

CREATE TABLE brand_alias_candidates (
    alias_candidate_id TEXT PRIMARY KEY,
    dataset_snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(dataset_snapshot_id),
    alias_group_id TEXT NOT NULL,
    brand_id_raw TEXT NOT NULL,
    brand_folder_raw TEXT NOT NULL,
    canonical_brand_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE (dataset_snapshot_id, alias_group_id, brand_folder_raw)
);

CREATE TABLE legacy_id_mappings (
    legacy_image_id TEXT NOT NULL,
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    image_occurrence_id TEXT NOT NULL REFERENCES image_occurrences(image_occurrence_id),
    metadata_source_path TEXT NOT NULL,
    metadata_sha256 TEXT NOT NULL,
    PRIMARY KEY (legacy_image_id, image_occurrence_id)
);

CREATE TABLE derived_assets (
    derived_asset_id TEXT PRIMARY KEY,
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    image_occurrence_id TEXT REFERENCES image_occurrences(image_occurrence_id),
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    asset_type TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    format TEXT,
    transform_name TEXT NOT NULL,
    transform_version TEXT NOT NULL,
    transform_fingerprint TEXT NOT NULL
);

CREATE TABLE pipeline_errors (
    error_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_id TEXT REFERENCES image_contents(image_id),
    image_occurrence_id TEXT REFERENCES image_occurrences(image_occurrence_id),
    stage TEXT NOT NULL,
    error_code TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    retryable INTEGER NOT NULL CHECK (retryable IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_source_records_folder_group
    ON source_records(folder_group_id);
CREATE INDEX idx_source_refs_source_record
    ON source_image_refs(source_record_id);
CREATE INDEX idx_occurrences_image_id
    ON image_occurrences(image_id);
CREATE INDEX idx_occurrences_folder_group
    ON image_occurrences(folder_group_id);
CREATE INDEX idx_ref_occurrences_occurrence
    ON source_ref_occurrences(image_occurrence_id);
CREATE INDEX idx_brand_alias_group
    ON brand_alias_candidates(alias_group_id);
