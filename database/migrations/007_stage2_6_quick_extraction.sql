CREATE TABLE quick_extraction_units (
    quick_extraction_unit_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    scope TEXT NOT NULL CHECK (
        scope IN ('image', 'global_thumbnail', 'tile')
    ),
    unit_index INTEGER,
    source_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    working_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    alpha_asset_id TEXT REFERENCES derived_assets(derived_asset_id),
    long_image_layout_id TEXT REFERENCES long_image_layouts(long_image_layout_id),
    asset_sha256 TEXT,
    asset_to_image_transform_json TEXT NOT NULL,
    cache_key TEXT,
    model_run_id TEXT REFERENCES model_runs(model_run_id),
    unit_status TEXT NOT NULL CHECK (
        unit_status IN (
            'planned',
            'prepared',
            'cache_hit',
            'succeeded',
            'failed',
            'skipped',
            'budget_exhausted'
        )
    ),
    provider_attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (provider_attempt_count >= 0),
    failure_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (scope = 'tile' AND unit_index IS NOT NULL AND unit_index >= 0)
        OR (scope <> 'tile' AND unit_index IS NULL)
    )
);

CREATE UNIQUE INDEX idx_quick_units_identity
    ON quick_extraction_units(
        run_id,
        image_id,
        scope,
        COALESCE(unit_index, -1)
    );
CREATE INDEX idx_quick_units_run_status
    ON quick_extraction_units(run_id, unit_status);
CREATE INDEX idx_quick_units_cache
    ON quick_extraction_units(cache_key, unit_status);

CREATE TABLE quick_image_extractions (
    quick_image_extraction_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    output_semantics TEXT NOT NULL CHECK (
        output_semantics = 'image_observed_color_candidate'
    ),
    status TEXT NOT NULL CHECK (
        status IN ('success', 'partial', 'failed', 'skipped')
    ),
    primary_role TEXT CHECK (
        primary_role IS NULL OR primary_role IN (
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
    role_confidence REAL CHECK (
        role_confidence IS NULL
        OR (role_confidence >= 0.0 AND role_confidence <= 1.0)
    ),
    layout_type TEXT,
    layout_summary TEXT,
    representative_color_eligible INTEGER CHECK (
        representative_color_eligible IS NULL
        OR representative_color_eligible IN (0, 1)
    ),
    eligibility_confidence REAL CHECK (
        eligibility_confidence IS NULL
        OR (eligibility_confidence >= 0.0 AND eligibility_confidence <= 1.0)
    ),
    eligibility_reasons_json TEXT NOT NULL,
    summary TEXT,
    quality_risks_json TEXT NOT NULL,
    aggregation_method TEXT NOT NULL,
    successful_unit_ids_json TEXT NOT NULL,
    failed_unit_ids_json TEXT NOT NULL,
    skipped_unit_ids_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, image_id)
);

CREATE INDEX idx_quick_images_run_status
    ON quick_image_extractions(run_id, status);

CREATE TABLE quick_text_items (
    quick_text_item_id TEXT PRIMARY KEY,
    quick_image_extraction_id TEXT NOT NULL
        REFERENCES quick_image_extractions(quick_image_extraction_id),
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    text_item_id TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    text_type TEXT NOT NULL,
    bbox_image_json TEXT,
    confidence REAL NOT NULL CHECK (
        confidence >= 0.0 AND confidence <= 1.0
    ),
    source_observations_json TEXT NOT NULL,
    deduplication_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (quick_image_extraction_id, text_item_id)
);

CREATE INDEX idx_quick_text_run_image
    ON quick_text_items(run_id, image_id);
CREATE INDEX idx_quick_text_normalized
    ON quick_text_items(normalized_text);

CREATE TABLE quick_color_regions (
    quick_color_region_id TEXT PRIMARY KEY,
    quick_image_extraction_id TEXT NOT NULL
        REFERENCES quick_image_extractions(quick_image_extraction_id),
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    region_id TEXT NOT NULL,
    region_type TEXT NOT NULL,
    bbox_image_json TEXT NOT NULL,
    model_confidence REAL NOT NULL CHECK (
        model_confidence >= 0.0 AND model_confidence <= 1.0
    ),
    shade_code_text TEXT,
    shade_name_text TEXT,
    visual_color_name TEXT,
    linked_text_item_ids_json TEXT NOT NULL,
    association_confidence REAL CHECK (
        association_confidence IS NULL
        OR (association_confidence >= 0.0 AND association_confidence <= 1.0)
    ),
    extraction_eligible INTEGER NOT NULL CHECK (
        extraction_eligible IN (0, 1)
    ),
    extraction_status TEXT NOT NULL CHECK (
        extraction_status IN (
            'succeeded',
            'skipped_ineligible',
            'insufficient_pixels',
            'failed'
        )
    ),
    output_semantics TEXT NOT NULL CHECK (
        output_semantics = 'image_observed_color_candidate'
    ),
    color_hex TEXT,
    rgb_json TEXT,
    lab_json TEXT,
    valid_pixel_count INTEGER CHECK (
        valid_pixel_count IS NULL OR valid_pixel_count >= 0
    ),
    valid_pixel_ratio REAL CHECK (
        valid_pixel_ratio IS NULL
        OR (valid_pixel_ratio >= 0.0 AND valid_pixel_ratio <= 1.0)
    ),
    cluster_proportion REAL CHECK (
        cluster_proportion IS NULL
        OR (cluster_proportion >= 0.0 AND cluster_proportion <= 1.0)
    ),
    dispersion REAL CHECK (dispersion IS NULL OR dispersion >= 0.0),
    color_confidence TEXT CHECK (
        color_confidence IS NULL
        OR color_confidence IN ('high', 'medium', 'low')
    ),
    risks_json TEXT NOT NULL,
    source_observations_json TEXT NOT NULL,
    deduplication_json TEXT NOT NULL,
    algorithm_diagnostics_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (quick_image_extraction_id, region_id)
);

CREATE INDEX idx_quick_regions_run_image
    ON quick_color_regions(run_id, image_id);
CREATE INDEX idx_quick_regions_status
    ON quick_color_regions(run_id, extraction_status);
