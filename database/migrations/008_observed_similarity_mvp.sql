CREATE TABLE shade_similarity_inputs (
    shade_similarity_input_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    source_quick_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    quick_image_extraction_id TEXT NOT NULL
        REFERENCES quick_image_extractions(quick_image_extraction_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    input_status TEXT NOT NULL CHECK (
        input_status IN ('selected', 'excluded')
    ),
    source_manifest_path TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL,
    selection_reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, image_id),
    UNIQUE (run_id, sequence)
);

CREATE INDEX idx_shade_similarity_inputs_source
    ON shade_similarity_inputs(source_quick_run_id, image_id);

CREATE TABLE shade_color_observations (
    shade_color_observation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    shade_similarity_input_id TEXT NOT NULL
        REFERENCES shade_similarity_inputs(shade_similarity_input_id),
    source_quick_run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    quick_color_region_id TEXT NOT NULL
        REFERENCES quick_color_regions(quick_color_region_id),
    image_id TEXT NOT NULL REFERENCES image_contents(image_id),
    quick_text_item_id TEXT
        REFERENCES quick_text_items(quick_text_item_id),
    linked_shade_text_item_ids_json TEXT NOT NULL,
    raw_shade_texts_json TEXT NOT NULL,
    normalized_shade_code TEXT,
    shade_id TEXT,
    identity_status TEXT NOT NULL CHECK (
        identity_status IN (
            'business_resolved',
            'image_local_unmatched',
            'image_local_ambiguous',
            'excluded'
        )
    ),
    source_sku_id_raw TEXT,
    candidate_sku_ids_json TEXT NOT NULL,
    source_record_ids_json TEXT NOT NULL,
    brand_id_raw TEXT,
    brand_name_raw TEXT,
    product_name_raw TEXT,
    shade_name_raw TEXT,
    region_type TEXT NOT NULL,
    representation_profile TEXT NOT NULL,
    bbox_image_json TEXT NOT NULL,
    extraction_status TEXT NOT NULL,
    output_semantics TEXT NOT NULL CHECK (
        output_semantics = 'image_observed_color_candidate'
    ),
    color_hex TEXT,
    rgb_json TEXT,
    lab_json TEXT,
    model_confidence REAL NOT NULL CHECK (
        model_confidence >= 0.0 AND model_confidence <= 1.0
    ),
    association_confidence REAL CHECK (
        association_confidence IS NULL
        OR (
            association_confidence >= 0.0
            AND association_confidence <= 1.0
        )
    ),
    color_confidence TEXT CHECK (
        color_confidence IS NULL
        OR color_confidence IN ('high', 'medium', 'low')
    ),
    valid_pixel_count INTEGER CHECK (
        valid_pixel_count IS NULL OR valid_pixel_count >= 0
    ),
    valid_pixel_ratio REAL CHECK (
        valid_pixel_ratio IS NULL
        OR (
            valid_pixel_ratio >= 0.0
            AND valid_pixel_ratio <= 1.0
        )
    ),
    cluster_proportion REAL CHECK (
        cluster_proportion IS NULL
        OR (
            cluster_proportion >= 0.0
            AND cluster_proportion <= 1.0
        )
    ),
    dispersion REAL CHECK (
        dispersion IS NULL OR dispersion >= 0.0
    ),
    formal_eligible INTEGER NOT NULL CHECK (
        formal_eligible IN (0, 1)
    ),
    exclusion_reasons_json TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, quick_color_region_id),
    CHECK (
        formal_eligible = 0
        OR (
            shade_id IS NOT NULL
            AND normalized_shade_code IS NOT NULL
            AND identity_status IN (
                'business_resolved',
                'image_local_unmatched',
                'image_local_ambiguous'
            )
            AND region_type = 'swatch'
            AND representation_profile = 'swatch'
            AND extraction_status = 'succeeded'
            AND color_confidence IN ('high', 'medium')
            AND color_hex IS NOT NULL
            AND rgb_json IS NOT NULL
            AND lab_json IS NOT NULL
            AND association_confidence IS NOT NULL
        )
    )
);

CREATE INDEX idx_shade_observations_run_eligibility
    ON shade_color_observations(
        run_id,
        formal_eligible,
        representation_profile
    );
CREATE INDEX idx_shade_observations_shade
    ON shade_color_observations(run_id, shade_id);

CREATE TABLE shade_color_profiles (
    shade_color_profile_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    shade_id TEXT NOT NULL,
    identity_status TEXT NOT NULL CHECK (
        identity_status IN (
            'business_resolved',
            'image_local_unmatched',
            'image_local_ambiguous'
        )
    ),
    source_sku_id_raw TEXT,
    normalized_shade_code TEXT NOT NULL,
    shade_code_aliases_json TEXT NOT NULL,
    brand_id_raw TEXT,
    brand_name_raw TEXT,
    product_name_raw TEXT,
    shade_name_raw TEXT,
    representation_profile TEXT NOT NULL,
    representative_observation_id TEXT NOT NULL
        REFERENCES shade_color_observations(shade_color_observation_id),
    representative_hex TEXT NOT NULL,
    representative_rgb_json TEXT NOT NULL,
    representative_lab_json TEXT NOT NULL,
    lab_l REAL NOT NULL,
    lab_a REAL NOT NULL,
    lab_b REAL NOT NULL,
    lch_c REAL NOT NULL CHECK (lch_c >= 0.0),
    lch_h_deg REAL NOT NULL CHECK (
        lch_h_deg >= 0.0 AND lch_h_deg < 360.0
    ),
    color_confidence TEXT NOT NULL CHECK (
        color_confidence IN ('high', 'medium')
    ),
    profile_status TEXT NOT NULL CHECK (
        profile_status IN (
            'single_observation_provisional',
            'multi_observation_provisional'
        )
    ),
    accepted_observation_count INTEGER NOT NULL CHECK (
        accepted_observation_count >= 1
    ),
    evidence_image_count INTEGER NOT NULL CHECK (
        evidence_image_count >= 1
    ),
    within_profile_pair_count INTEGER NOT NULL CHECK (
        within_profile_pair_count >= 0
    ),
    within_profile_delta_e00_p50 REAL CHECK (
        within_profile_delta_e00_p50 IS NULL
        OR within_profile_delta_e00_p50 >= 0.0
    ),
    within_profile_delta_e00_max REAL CHECK (
        within_profile_delta_e00_max IS NULL
        OR within_profile_delta_e00_max >= 0.0
    ),
    output_semantics TEXT NOT NULL CHECK (
        output_semantics =
            'image_observed_color_similarity_baseline'
    ),
    algorithm_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, shade_id, representation_profile)
);

CREATE INDEX idx_shade_profiles_run_profile
    ON shade_color_profiles(run_id, representation_profile);

CREATE TABLE shade_similarity_pairs (
    shade_similarity_pair_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    shade_color_profile_id_a TEXT NOT NULL
        REFERENCES shade_color_profiles(shade_color_profile_id),
    shade_color_profile_id_b TEXT NOT NULL
        REFERENCES shade_color_profiles(shade_color_profile_id),
    shade_id_a TEXT NOT NULL,
    shade_id_b TEXT NOT NULL,
    representation_profile TEXT NOT NULL,
    delta_e00 REAL NOT NULL CHECK (delta_e00 >= 0.0),
    display_score REAL NOT NULL CHECK (
        display_score > 0.0 AND display_score <= 100.0
    ),
    display_score_version TEXT NOT NULL,
    distance_band TEXT NOT NULL CHECK (
        distance_band IN (
            'de00_le_2',
            'de00_gt_2_le_5',
            'de00_gt_5_le_10',
            'de00_gt_10_le_20',
            'de00_gt_20'
        )
    ),
    delta_l REAL NOT NULL CHECK (delta_l >= 0.0),
    delta_c REAL NOT NULL CHECK (delta_c >= 0.0),
    delta_h_deg REAL NOT NULL CHECK (
        delta_h_deg >= 0.0 AND delta_h_deg <= 180.0
    ),
    pair_quality_tier TEXT NOT NULL CHECK (
        pair_quality_tier IN ('high', 'medium')
    ),
    output_semantics TEXT NOT NULL CHECK (
        output_semantics =
            'image_observed_color_similarity_baseline'
    ),
    algorithm_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (
        run_id,
        shade_color_profile_id_a,
        shade_color_profile_id_b
    ),
    CHECK (
        shade_color_profile_id_a < shade_color_profile_id_b
        AND shade_id_a <> shade_id_b
    )
);

CREATE INDEX idx_shade_pairs_run_distance
    ON shade_similarity_pairs(
        run_id,
        representation_profile,
        delta_e00
    );

CREATE TABLE shade_similarity_topk (
    shade_similarity_topk_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id),
    shade_similarity_pair_id TEXT NOT NULL
        REFERENCES shade_similarity_pairs(shade_similarity_pair_id),
    query_profile_id TEXT NOT NULL
        REFERENCES shade_color_profiles(shade_color_profile_id),
    candidate_profile_id TEXT NOT NULL
        REFERENCES shade_color_profiles(shade_color_profile_id),
    query_shade_id TEXT NOT NULL,
    candidate_shade_id TEXT NOT NULL,
    representation_profile TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    delta_e00 REAL NOT NULL CHECK (delta_e00 >= 0.0),
    display_score REAL NOT NULL CHECK (
        display_score > 0.0 AND display_score <= 100.0
    ),
    display_score_version TEXT NOT NULL,
    distance_band TEXT NOT NULL,
    delta_l REAL NOT NULL CHECK (delta_l >= 0.0),
    delta_c REAL NOT NULL CHECK (delta_c >= 0.0),
    delta_h_deg REAL NOT NULL CHECK (
        delta_h_deg >= 0.0 AND delta_h_deg <= 180.0
    ),
    pair_quality_tier TEXT NOT NULL CHECK (
        pair_quality_tier IN ('high', 'medium')
    ),
    output_semantics TEXT NOT NULL CHECK (
        output_semantics =
            'image_observed_color_similarity_baseline'
    ),
    algorithm_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, query_profile_id, candidate_profile_id),
    UNIQUE (run_id, query_profile_id, rank),
    CHECK (
        query_profile_id <> candidate_profile_id
        AND query_shade_id <> candidate_shade_id
    )
);

CREATE INDEX idx_shade_topk_run_query
    ON shade_similarity_topk(run_id, query_profile_id, rank);
