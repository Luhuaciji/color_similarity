ALTER TABLE derived_assets
    ADD COLUMN root_alias TEXT NOT NULL DEFAULT 'pipeline_output';
ALTER TABLE derived_assets
    ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
ALTER TABLE derived_assets
    ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE workspace_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_derived_asset_identity
    ON derived_assets(
        run_id,
        image_id,
        asset_type,
        transform_fingerprint,
        relative_path
    );

CREATE TABLE pilot_samples (
    pilot_sample_id TEXT PRIMARY KEY,
    pilot_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    selected_occurrence_ids_json TEXT NOT NULL,
    selected_source_record_ids_json TEXT NOT NULL,
    coverage_tags_json TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    human_review_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (pilot_run_id, image_id)
);

CREATE TABLE model_runs (
    model_run_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    analysis_layer TEXT NOT NULL CHECK (analysis_layer IN ('A', 'B')),
    analysis_unit_type TEXT NOT NULL,
    analysis_unit_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    base_url_alias TEXT NOT NULL,
    prompt_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    input_context_policy TEXT NOT NULL,
    generation_parameters_json TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_path TEXT NOT NULL,
    raw_response_path TEXT,
    parsed_response_path TEXT,
    response_hash TEXT,
    schema_validation_status TEXT NOT NULL,
    latency_ms INTEGER,
    token_usage_json TEXT NOT NULL,
    status TEXT NOT NULL,
    error_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX idx_model_runs_cache
    ON model_runs(cache_key, status);
CREATE INDEX idx_model_runs_run
    ON model_runs(run_id, analysis_layer);

CREATE TABLE long_image_layouts (
    long_image_layout_id TEXT PRIMARY KEY,
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    global_thumbnail_asset_id TEXT NOT NULL REFERENCES derived_assets(derived_asset_id),
    original_width INTEGER NOT NULL CHECK (original_width > 0),
    original_height INTEGER NOT NULL CHECK (original_height > 0),
    global_thumbnail_width INTEGER NOT NULL CHECK (global_thumbnail_width > 0),
    global_thumbnail_height INTEGER NOT NULL CHECK (global_thumbnail_height > 0),
    reading_axis TEXT NOT NULL CHECK (reading_axis IN ('horizontal', 'vertical')),
    layout_type TEXT NOT NULL,
    global_layout_json TEXT NOT NULL,
    image_to_thumbnail_transform_json TEXT NOT NULL,
    tiling_strategy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, image_id, tiling_strategy_version)
);

CREATE TABLE image_tiles (
    image_tile_id TEXT PRIMARY KEY,
    long_image_layout_id TEXT NOT NULL
        REFERENCES long_image_layouts(long_image_layout_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    tile_asset_id TEXT NOT NULL REFERENCES derived_assets(derived_asset_id),
    tile_index INTEGER NOT NULL CHECK (tile_index >= 0),
    bbox_image_json TEXT NOT NULL,
    overlap_before_px INTEGER NOT NULL CHECK (overlap_before_px >= 0),
    overlap_after_px INTEGER NOT NULL CHECK (overlap_after_px >= 0),
    tile_width INTEGER NOT NULL CHECK (tile_width > 0),
    tile_height INTEGER NOT NULL CHECK (tile_height > 0),
    image_to_tile_transform_json TEXT NOT NULL,
    tile_to_image_transform_json TEXT NOT NULL,
    transform_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (long_image_layout_id, tile_index)
);

CREATE TABLE content_visual_analyses (
    content_visual_analysis_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    analysis_scope TEXT NOT NULL CHECK (
        analysis_scope IN (
            'image',
            'global_thumbnail',
            'tile',
            'merged_content_summary'
        )
    ),
    analysis_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    parent_content_visual_analysis_id TEXT
        REFERENCES content_visual_analyses(content_visual_analysis_id),
    tile_index INTEGER,
    tile_bbox_image_json TEXT NOT NULL,
    primary_role TEXT NOT NULL CHECK (
        primary_role IN (
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
    secondary_roles_json TEXT NOT NULL,
    layout_type TEXT NOT NULL,
    global_layout_json TEXT NOT NULL,
    role_confidence REAL NOT NULL CHECK (
        role_confidence >= 0.0 AND role_confidence <= 1.0
    ),
    contains_text INTEGER NOT NULL CHECK (contains_text IN (0, 1)),
    contains_multiple_shades INTEGER NOT NULL CHECK (
        contains_multiple_shades IN (0, 1)
    ),
    contains_lips INTEGER NOT NULL CHECK (contains_lips IN (0, 1)),
    contains_skin_swatch INTEGER NOT NULL CHECK (
        contains_skin_swatch IN (0, 1)
    ),
    contains_product_bullet INTEGER NOT NULL CHECK (
        contains_product_bullet IN (0, 1)
    ),
    contains_packaging INTEGER NOT NULL CHECK (contains_packaging IN (0, 1)),
    depicted_shades_json TEXT NOT NULL,
    representative_color_eligible INTEGER NOT NULL CHECK (
        representative_color_eligible IN (0, 1)
    ),
    eligibility_score REAL NOT NULL CHECK (
        eligibility_score >= 0.0 AND eligibility_score <= 1.0
    ),
    recommended_strategy TEXT NOT NULL,
    rejection_reasons_json TEXT NOT NULL,
    candidate_regions_json TEXT NOT NULL,
    model_run_id TEXT NOT NULL REFERENCES model_runs(model_run_id),
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        run_id,
        image_id,
        analysis_scope,
        analysis_asset_id,
        tile_index
    )
);

CREATE INDEX idx_content_visual_image
    ON content_visual_analyses(image_id, run_id);

CREATE TABLE occurrence_context_fusions (
    occurrence_context_fusion_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_occurrence_id TEXT NOT NULL
        REFERENCES image_occurrences(image_occurrence_id),
    source_record_id TEXT NOT NULL REFERENCES source_records(source_record_id),
    source_ref_id TEXT NOT NULL REFERENCES source_image_refs(source_ref_id),
    folder_group_id TEXT NOT NULL REFERENCES folder_groups(folder_group_id),
    content_visual_analysis_id TEXT NOT NULL
        REFERENCES content_visual_analyses(content_visual_analysis_id),
    source_sku_id_raw TEXT NOT NULL,
    folder_context_json TEXT NOT NULL,
    csv_context_json TEXT NOT NULL,
    context_shade_json TEXT NOT NULL,
    depicted_shades_json TEXT NOT NULL,
    relationship_to_context TEXT NOT NULL CHECK (
        relationship_to_context IN (
            'exact_shade_match',
            'contains_context_shade',
            'same_product_unspecified_shade',
            'shade_conflict',
            'unrelated',
            'insufficient_evidence'
        )
    ),
    context_conflicts_json TEXT NOT NULL,
    fusion_method TEXT NOT NULL,
    fusion_version TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    review_status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        run_id,
        image_occurrence_id,
        source_record_id,
        source_ref_id,
        content_visual_analysis_id,
        fusion_version
    )
);

CREATE INDEX idx_context_fusions_occurrence
    ON occurrence_context_fusions(image_occurrence_id, run_id);
