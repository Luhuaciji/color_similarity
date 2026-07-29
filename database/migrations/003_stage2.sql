CREATE TABLE image_preprocessing_observations (
    preprocess_observation_id TEXT PRIMARY KEY,
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    decode_status TEXT NOT NULL CHECK (
        decode_status IN ('ok', 'corrupt', 'policy_rejected', 'recovered')
    ),
    decode_recovered INTEGER NOT NULL CHECK (decode_recovered IN (0, 1)),
    source_format TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    frame_count INTEGER,
    selected_frame INTEGER,
    exif_orientation INTEGER,
    orientation_corrected INTEGER NOT NULL CHECK (
        orientation_corrected IN (0, 1)
    ),
    icc_status TEXT NOT NULL,
    working_color_space TEXT NOT NULL,
    converted_to_srgb INTEGER NOT NULL CHECK (converted_to_srgb IN (0, 1)),
    has_alpha INTEGER NOT NULL CHECK (has_alpha IN (0, 1)),
    transparent_pixel_ratio REAL,
    working_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    alpha_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    quality_json TEXT NOT NULL,
    transform_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (image_id, run_id)
);

CREATE TABLE preprocessing_occurrence_links (
    preprocess_occurrence_link_id TEXT PRIMARY KEY,
    preprocess_observation_id TEXT NOT NULL
        REFERENCES image_preprocessing_observations(preprocess_observation_id),
    image_occurrence_id TEXT NOT NULL
        REFERENCES image_occurrences(image_occurrence_id),
    legacy_image_id TEXT,
    legacy_metadata_row_hash TEXT,
    legacy_working_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    legacy_alpha_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    migration_status TEXT NOT NULL,
    conflict_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (preprocess_observation_id, image_occurrence_id)
);

CREATE TABLE duplicate_edges (
    duplicate_edge_id TEXT PRIMARY KEY,
    image_id_a TEXT NOT NULL REFERENCES image_contents(image_id),
    image_id_b TEXT NOT NULL REFERENCES image_contents(image_id),
    method TEXT NOT NULL,
    distance REAL NOT NULL,
    threshold_version TEXT NOT NULL,
    confidence_class TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (image_id_a < image_id_b),
    UNIQUE (run_id, image_id_a, image_id_b, method, threshold_version)
);

CREATE TABLE quality_flags (
    quality_flag_id TEXT PRIMARY KEY,
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    flag_code TEXT NOT NULL,
    metric_value REAL,
    threshold_version TEXT NOT NULL,
    severity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (image_id, run_id, flag_code, threshold_version)
);

CREATE INDEX idx_preprocessing_run
    ON image_preprocessing_observations(run_id, decode_status);
CREATE INDEX idx_preprocessing_occurrence
    ON preprocessing_occurrence_links(image_occurrence_id);
CREATE INDEX idx_duplicate_edges_images
    ON duplicate_edges(image_id_a, image_id_b);
CREATE INDEX idx_quality_flags_run
    ON quality_flags(run_id, flag_code);
