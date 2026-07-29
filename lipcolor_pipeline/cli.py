"""Unified command-line interface for stages 1.5 through 2.6."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

from .annotations import (
    annotation_progress,
    apply_owner_delegated_pilot_review,
    compute_pilot_baseline,
    create_pilot_review_set,
    create_stage2_5_annotation_set,
    supersede_stage2_5_annotation_set,
    validate_and_freeze_stage2_5,
)
from .pilot import (
    add_pilot_topup_sample,
    create_context_review_sample,
    fuse_pilot_context,
    prepare_pilot_assets,
    select_pilot_samples,
    validate_pilot,
)
from .preprocessing import (
    migrate_legacy_preprocessing,
    run_stage2_preprocessing,
    validate_stage2,
)
from .quick_extract import (
    export_quick_extraction,
    plan_quick_extraction,
    recover_quick_run_artifacts,
    run_quick_extraction,
)
from .settings import PipelineSettings, load_settings
from .vlm_client import repair_failed_pilot_analyses, run_pilot_vlm
from .workspace import Workspace, dependency_snapshot, initialize_workspace


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print_result(value: Any) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
    )


def _safe_error_message(error: BaseException) -> str:
    message = str(error)
    for variable in (
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
    ):
        secret = os.environ.get(variable)
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return re.sub(
        r"\bsk-(?:ws-)?[A-Za-z0-9_-]{12,}\b",
        "[REDACTED]",
        message,
    )


def _load_context(args: argparse.Namespace) -> tuple[PipelineSettings, Workspace]:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config).resolve() if args.config else None
    settings = load_settings(repo_root, config_path)
    workspace = initialize_workspace(settings)
    return settings, workspace


def _dry_run(args: argparse.Namespace, operation: str) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "operation": operation,
        "run_id": getattr(args, "run_id", None),
        "limit": getattr(args, "limit", None),
        "image_id": getattr(args, "image_id", None),
        "message": "No operation-specific records or assets were written.",
    }


def _cmd_workspace_init(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config).resolve() if args.config else None
    settings = load_settings(repo_root, config_path)
    workspace = initialize_workspace(
        settings,
        through_version=args.through_version,
    )
    return {
        "status": "ready",
        "dataset_snapshot_id": workspace.dataset_snapshot_id,
        "database_path": workspace.database_path,
        "source_stage1_database": workspace.stage1_database_path,
        "workspace_schema_through_version": args.through_version,
        "dependency_snapshot": dependency_snapshot(),
    }


def _cmd_pilot_select(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.select")
    settings, workspace = _load_context(args)
    return select_pilot_samples(
        workspace,
        settings,
        run_id=args.run_id,
        resume=args.resume,
        count=args.limit,
    )


def _cmd_pilot_topup(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.topup")
    settings, workspace = _load_context(args)
    if args.candidate_evidence_json:
        evidence = json.loads(args.candidate_evidence_json)
        if not isinstance(evidence, dict):
            raise ValueError("--candidate-evidence-json must be a JSON object")
    else:
        if not all(
            (
                args.candidate_retrieval_method,
                args.candidate_retrieval_version,
                args.candidate_visual_basis,
            )
        ):
            raise ValueError(
                "provide --candidate-evidence-json or all candidate "
                "retrieval/visual evidence fields"
            )
        evidence = {
            "retrieval_method": args.candidate_retrieval_method,
            "retrieval_version": args.candidate_retrieval_version,
            "score": args.candidate_score,
            "gallery_index": args.candidate_gallery_index,
            "visual_basis": args.candidate_visual_basis,
            "source_sha256": args.image_id,
        }
    return add_pilot_topup_sample(
        workspace,
        settings,
        run_id=args.run_id,
        image_id=args.image_id,
        delegation_scope=args.delegation_scope,
        delegated_agent=args.delegated_agent,
        owner_instruction=args.owner_instruction,
        selection_method=args.selection_method,
        selection_version=args.selection_version,
        selection_reason=args.selection_reason,
        candidate_evidence=evidence,
    )


def _cmd_pilot_delegated_review(
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.delegated-review")
    _settings, workspace = _load_context(args)
    return apply_owner_delegated_pilot_review(
        workspace,
        pilot_run_id=args.run_id,
        image_id=args.image_id,
        annotator_id=args.delegated_agent,
        owner_review_delegation_id=args.owner_review_delegation_id,
        role_code=args.role,
        eligibility_label=bool(args.eligible),
        eligibility_reason_codes=args.eligibility_reason,
    )


def _cmd_pilot_prepare(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.prepare")
    settings, workspace = _load_context(args)
    return prepare_pilot_assets(
        workspace,
        settings,
        run_id=args.run_id,
        resume=True,
        image_id=args.image_id,
        limit=args.limit,
    )


def _cmd_pilot_run(args: argparse.Namespace) -> dict[str, Any]:
    settings, workspace = _load_context(args)
    if args.dry_run and args.execute_online:
        raise ValueError("--dry-run and --execute-online are mutually exclusive")
    return run_pilot_vlm(
        workspace,
        settings,
        run_id=args.run_id,
        execute_online=bool(args.execute_online and not args.dry_run),
        max_calls=args.max_calls,
        image_id=args.image_id,
        limit=args.limit,
    )


def _cmd_quick_plan(args: argparse.Namespace) -> dict[str, Any]:
    settings, workspace = _load_context(args)
    return plan_quick_extraction(
        workspace,
        settings,
        run_id=args.run_id,
        image_id=args.image_id,
        selection_manifest=args.selection_manifest,
        folder_group_id=args.folder_group_id,
        limit=args.limit,
    )


def _cmd_quick_run(args: argparse.Namespace) -> dict[str, Any]:
    settings, workspace = _load_context(args)
    return run_quick_extraction(
        workspace,
        settings,
        run_id=args.run_id,
        execute_online=args.execute_online,
        max_calls=args.max_calls,
        resume=args.resume,
        image_id=args.image_id,
        selection_manifest=args.selection_manifest,
        folder_group_id=args.folder_group_id,
        limit=args.limit,
    )


def _cmd_quick_export(args: argparse.Namespace) -> dict[str, Any]:
    _settings, workspace = _load_context(args)
    return export_quick_extraction(
        workspace,
        run_id=args.run_id,
        output_dir=args.output_dir,
    )


def _cmd_quick_recover(args: argparse.Namespace) -> dict[str, Any]:
    settings, workspace = _load_context(args)
    return recover_quick_run_artifacts(
        workspace,
        settings,
        run_id=args.run_id,
        finalize=True,
    )


def _cmd_pilot_fuse(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.fuse")
    _settings, workspace = _load_context(args)
    return fuse_pilot_context(
        workspace,
        run_id=args.run_id,
        image_id=args.image_id,
        limit=args.limit,
    )


def _cmd_pilot_repair(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.repair")
    settings, workspace = _load_context(args)
    return repair_failed_pilot_analyses(
        workspace,
        settings,
        run_id=args.run_id,
        image_id=args.image_id,
        limit=args.limit,
    )


def _cmd_pilot_review_init(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.review-init")
    _settings, workspace = _load_context(args)
    return create_pilot_review_set(
        workspace,
        pilot_run_id=args.run_id,
    )


def _cmd_pilot_context_sample(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.context-sample")
    _settings, workspace = _load_context(args)
    return create_context_review_sample(
        workspace,
        run_id=args.run_id,
    )


def _cmd_pilot_validate(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "pilot.validate")
    _settings, workspace = _load_context(args)
    return validate_pilot(
        workspace,
        run_id=args.run_id,
        finalize=args.finalize,
        approved_by=args.approved_by,
        decision=args.decision,
    )


def _cmd_preprocess_migrate(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "preprocess.migrate")
    settings, workspace = _load_context(args)
    return migrate_legacy_preprocessing(
        workspace,
        settings,
        run_id=args.run_id,
        pilot_run_id=args.pilot_run_id,
        resume=args.resume,
    )


def _cmd_preprocess_run(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "preprocess.run")
    settings, workspace = _load_context(args)
    return run_stage2_preprocessing(
        workspace,
        settings,
        run_id=args.run_id,
        pilot_run_id=args.pilot_run_id,
        resume=args.resume,
        limit=args.limit,
        image_id=args.image_id,
    )


def _cmd_preprocess_validate(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "preprocess.validate")
    settings, workspace = _load_context(args)
    return validate_stage2(
        workspace,
        settings,
        run_id=args.run_id,
        full_source_hash_check=not args.skip_full_source_hash,
        finalize=args.finalize,
    )


def _cmd_annotate_init_pilot(args: argparse.Namespace) -> dict[str, Any]:
    _settings, workspace = _load_context(args)
    return create_pilot_review_set(
        workspace,
        pilot_run_id=args.pilot_run_id,
    )


def _cmd_annotate_init(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "annotate.init")
    settings, workspace = _load_context(args)
    return create_stage2_5_annotation_set(
        workspace,
        settings,
        run_id=args.run_id,
        stage2_run_id=args.stage2_run_id,
        pilot_run_id=args.pilot_run_id,
        resume=args.resume,
    )


def _cmd_annotate_progress(args: argparse.Namespace) -> dict[str, Any]:
    _settings, workspace = _load_context(args)
    return annotation_progress(
        workspace,
        annotation_set_id=args.annotation_set_id,
    )


def _cmd_annotate_supersede(args: argparse.Namespace) -> dict[str, Any]:
    if args.dry_run:
        return _dry_run(args, "annotate.supersede")
    _settings, workspace = _load_context(args)
    return supersede_stage2_5_annotation_set(
        workspace,
        annotation_set_id=args.annotation_set_id,
        replacement_run_id=args.replacement_run_id,
        reason=args.reason,
    )


def _cmd_annotate_freeze(args: argparse.Namespace) -> dict[str, Any]:
    settings, workspace = _load_context(args)
    return validate_and_freeze_stage2_5(
        workspace,
        settings,
        annotation_set_id=args.annotation_set_id,
        approved_by=args.approved_by,
    )


def _cmd_annotate_serve(args: argparse.Namespace) -> None:
    if args.host != "127.0.0.1":
        raise ValueError("annotation server may only listen on 127.0.0.1")
    _settings, workspace = _load_context(args)
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required for annotate serve") from exc
    from .review_app import create_app

    uvicorn.run(
        create_app(workspace),
        host="127.0.0.1",
        port=args.port,
        log_level="info",
    )
    return None


def _cmd_evaluate_baseline(args: argparse.Namespace) -> dict[str, Any]:
    _settings, workspace = _load_context(args)
    return compute_pilot_baseline(
        workspace,
        evaluation_set_id=args.evaluation_set_id,
        pilot_run_id=args.pilot_run_id,
    )


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-root",
        default=str(Path.cwd()),
        help="Repository root (default: current directory).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Pipeline YAML path (default: configs/pipeline.yaml).",
    )


def _add_batch_flags(
    parser: argparse.ArgumentParser,
    *,
    run_id: bool = True,
    resume: bool = True,
    dry_run: bool = True,
    limit: bool = True,
    image_id: bool = True,
) -> None:
    if run_id:
        parser.add_argument("--run-id", required=True)
    if resume:
        parser.add_argument("--resume", action="store_true")
    if dry_run:
        parser.add_argument("--dry-run", action="store_true")
    if limit:
        parser.add_argument("--limit", type=int, default=None)
    if image_id:
        parser.add_argument("--image-id", default=None)


def _leaf(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    handler: Callable[[argparse.Namespace], Any],
    *,
    help_text: str,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name, help=help_text)
    _add_common_paths(parser)
    parser.set_defaults(handler=handler)
    return parser


def _add_quick_selector(parser: argparse.ArgumentParser) -> None:
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--image-id", default=None)
    selector.add_argument(
        "--selection-manifest",
        type=Path,
        default=None,
    )
    selector.add_argument("--folder-group-id", default=None)
    selector.add_argument("--limit", type=int, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lipcolor_pipeline.cli",
        description="Stages 1.5–2.6 lip-color pipeline.",
    )
    groups = parser.add_subparsers(dest="group", required=True)

    workspace = groups.add_parser("workspace")
    workspace_commands = workspace.add_subparsers(dest="command", required=True)
    workspace_init = _leaf(
        workspace_commands,
        "init",
        _cmd_workspace_init,
        help_text="Copy Stage 1 SQLite and apply workspace migrations.",
    )
    workspace_init.add_argument(
        "--through-version",
        type=int,
        choices=(2, 3, 4, 5, 6, 7),
        default=7,
    )

    pilot = groups.add_parser("pilot")
    pilot_commands = pilot.add_subparsers(dest="command", required=True)
    pilot_select = _leaf(
        pilot_commands,
        "select",
        _cmd_pilot_select,
        help_text="Select the deterministic image-only Pilot.",
    )
    _add_batch_flags(pilot_select)
    pilot_topup = _leaf(
        pilot_commands,
        "topup",
        _cmd_pilot_topup,
        help_text="Append an owner-authorized image-only Pilot top-up.",
    )
    _add_batch_flags(
        pilot_topup,
        resume=False,
        limit=False,
    )
    pilot_topup.add_argument(
        "--delegation-scope",
        required=True,
        choices=("stage1_5_color_card_topup",),
    )
    pilot_topup.add_argument("--delegated-agent", required=True)
    pilot_topup.add_argument("--owner-instruction", required=True)
    pilot_topup.add_argument("--selection-method", required=True)
    pilot_topup.add_argument("--selection-version", required=True)
    pilot_topup.add_argument("--selection-reason", required=True)
    pilot_topup.add_argument("--candidate-evidence-json", default=None)
    pilot_topup.add_argument("--candidate-retrieval-method", default=None)
    pilot_topup.add_argument("--candidate-retrieval-version", default=None)
    pilot_topup.add_argument("--candidate-score", type=float, default=None)
    pilot_topup.add_argument("--candidate-gallery-index", type=int, default=None)
    pilot_topup.add_argument("--candidate-visual-basis", default=None)
    pilot_delegated_review = _leaf(
        pilot_commands,
        "delegated-review",
        _cmd_pilot_delegated_review,
        help_text="Record the authorized agent review for a Pilot top-up.",
    )
    _add_batch_flags(
        pilot_delegated_review,
        resume=False,
        limit=False,
    )
    pilot_delegated_review.add_argument("--delegated-agent", required=True)
    pilot_delegated_review.add_argument(
        "--owner-review-delegation-id",
        required=True,
    )
    pilot_delegated_review.add_argument(
        "--role",
        required=True,
        choices=("color_card",),
    )
    eligibility = pilot_delegated_review.add_mutually_exclusive_group(
        required=True
    )
    eligibility.add_argument(
        "--eligible",
        action="store_true",
        dest="eligible",
    )
    eligibility.add_argument(
        "--ineligible",
        action="store_false",
        dest="eligible",
    )
    pilot_delegated_review.add_argument(
        "--eligibility-reason",
        action="append",
        default=[],
    )
    pilot_prepare = _leaf(
        pilot_commands,
        "prepare",
        _cmd_pilot_prepare,
        help_text="Decode Pilot images and create analysis assets.",
    )
    _add_batch_flags(pilot_prepare)
    pilot_run = _leaf(
        pilot_commands,
        "run",
        _cmd_pilot_run,
        help_text="Plan or explicitly execute online VLM calls.",
    )
    _add_batch_flags(pilot_run)
    pilot_run.add_argument("--execute-online", action="store_true")
    pilot_run.add_argument("--max-calls", type=int, default=None)
    pilot_fuse = _leaf(
        pilot_commands,
        "fuse",
        _cmd_pilot_fuse,
        help_text="Create occurrence-context B-layer rows.",
    )
    _add_batch_flags(pilot_fuse)
    pilot_repair = _leaf(
        pilot_commands,
        "repair",
        _cmd_pilot_repair,
        help_text="Replay failed raw responses through deterministic repair.",
    )
    _add_batch_flags(pilot_repair)
    pilot_review = _leaf(
        pilot_commands,
        "review-init",
        _cmd_pilot_review_init,
        help_text="Create the minimal Pilot review set.",
    )
    _add_batch_flags(
        pilot_review,
        resume=False,
        limit=False,
        image_id=False,
    )
    pilot_context_sample = _leaf(
        pilot_commands,
        "context-sample",
        _cmd_pilot_context_sample,
        help_text="Freeze the stratified occurrence-context audit sample.",
    )
    _add_batch_flags(
        pilot_context_sample,
        resume=False,
        limit=False,
        image_id=False,
    )
    pilot_validate = _leaf(
        pilot_commands,
        "validate",
        _cmd_pilot_validate,
        help_text="Validate Pilot artifacts and human gate.",
    )
    _add_batch_flags(
        pilot_validate,
        resume=False,
        limit=False,
        image_id=False,
    )
    pilot_validate.add_argument("--finalize", action="store_true")
    pilot_validate.add_argument(
        "--decision",
        choices=("go", "no_go"),
        default=None,
    )
    pilot_validate.add_argument("--approved-by", default=None)

    preprocess = groups.add_parser("preprocess")
    preprocess_commands = preprocess.add_subparsers(
        dest="command", required=True
    )
    preprocess_migrate = _leaf(
        preprocess_commands,
        "migrate",
        _cmd_preprocess_migrate,
        help_text="Verify and register legacy Stage 2 assets.",
    )
    _add_batch_flags(
        preprocess_migrate,
        limit=False,
        image_id=False,
    )
    preprocess_migrate.add_argument("--pilot-run-id", required=True)
    preprocess_run = _leaf(
        preprocess_commands,
        "run",
        _cmd_preprocess_run,
        help_text="Run strict unique-content preprocessing.",
    )
    _add_batch_flags(preprocess_run)
    preprocess_run.add_argument("--pilot-run-id", required=True)
    preprocess_validate = _leaf(
        preprocess_commands,
        "validate",
        _cmd_preprocess_validate,
        help_text="Validate Stage 2 counts, assets, and source invariants.",
    )
    _add_batch_flags(
        preprocess_validate,
        resume=False,
        limit=False,
        image_id=False,
    )
    preprocess_validate.add_argument(
        "--skip-full-source-hash",
        action="store_true",
    )
    preprocess_validate.add_argument("--finalize", action="store_true")

    quick = groups.add_parser("quick-extract")
    quick_commands = quick.add_subparsers(dest="command", required=True)
    quick_plan = _leaf(
        quick_commands,
        "plan",
        _cmd_quick_plan,
        help_text=(
            "Plan Stage 2.6 selection, assets, cache, and provider budget."
        ),
    )
    quick_plan.add_argument("--run-id", required=True)
    _add_quick_selector(quick_plan)
    quick_run = _leaf(
        quick_commands,
        "run",
        _cmd_quick_run,
        help_text=(
            "Prepare assets, call/cache Qwen, merge, extract colour, and persist."
        ),
    )
    quick_run.add_argument("--run-id", required=True)
    quick_run.add_argument("--resume", action="store_true")
    quick_run.add_argument("--execute-online", action="store_true")
    quick_run.add_argument("--max-calls", type=int, default=None)
    _add_quick_selector(quick_run)
    quick_export = _leaf(
        quick_commands,
        "export",
        _cmd_quick_export,
        help_text="Export Stage 2.6 canonical image and occurrence results.",
    )
    quick_export.add_argument("--run-id", required=True)
    quick_export.add_argument("--output-dir", type=Path, default=None)
    quick_recover = _leaf(
        quick_commands,
        "recover",
        _cmd_quick_recover,
        help_text=(
            "Recover interrupted request/raw/parsed artifacts into SQLite."
        ),
    )
    quick_recover.add_argument("--run-id", required=True)

    annotate = groups.add_parser("annotate")
    annotate_commands = annotate.add_subparsers(dest="command", required=True)
    annotate_pilot = _leaf(
        annotate_commands,
        "init-pilot",
        _cmd_annotate_init_pilot,
        help_text="Create or inspect the Pilot review set.",
    )
    annotate_pilot.add_argument("--pilot-run-id", required=True)
    annotate_init = _leaf(
        annotate_commands,
        "init",
        _cmd_annotate_init,
        help_text="Create the 480-image Stage 2.5 annotation set.",
    )
    _add_batch_flags(
        annotate_init,
        limit=False,
        image_id=False,
    )
    annotate_init.add_argument("--stage2-run-id", required=True)
    annotate_init.add_argument("--pilot-run-id", required=True)
    annotate_serve = _leaf(
        annotate_commands,
        "serve",
        _cmd_annotate_serve,
        help_text="Serve the local FastAPI/Canvas annotation tool.",
    )
    annotate_serve.add_argument("--host", default="127.0.0.1")
    annotate_serve.add_argument("--port", type=int, default=8765)
    annotate_progress_parser = _leaf(
        annotate_commands,
        "progress",
        _cmd_annotate_progress,
        help_text="Report annotation progress.",
    )
    annotate_progress_parser.add_argument("--annotation-set-id", required=True)
    annotate_supersede = _leaf(
        annotate_commands,
        "supersede",
        _cmd_annotate_supersede,
        help_text="Close an unused Stage 2.5 candidate selection.",
    )
    _add_batch_flags(
        annotate_supersede,
        run_id=False,
        resume=False,
        limit=False,
        image_id=False,
    )
    annotate_supersede.add_argument("--annotation-set-id", required=True)
    annotate_supersede.add_argument("--replacement-run-id", required=True)
    annotate_supersede.add_argument("--reason", required=True)
    annotate_freeze = _leaf(
        annotate_commands,
        "freeze",
        _cmd_annotate_freeze,
        help_text="Validate and explicitly freeze Stage 2.5 ground truth.",
    )
    annotate_freeze.add_argument("--annotation-set-id", required=True)
    annotate_freeze.add_argument("--approved-by", default=None)

    evaluate = groups.add_parser("evaluate")
    evaluate_commands = evaluate.add_subparsers(dest="command", required=True)
    evaluate_baseline = _leaf(
        evaluate_commands,
        "baseline",
        _cmd_evaluate_baseline,
        help_text="Compute Pilot metrics against a frozen evaluation set.",
    )
    evaluate_baseline.add_argument("--evaluation-set-id", required=True)
    evaluate_baseline.add_argument("--pilot-run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
        if result is not None:
            _print_result(result)
        return 0
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as error:
        payload = {
            "status": "error",
            "error_type": type(error).__name__,
            "message": _safe_error_message(error),
        }
        print(
            json.dumps(payload, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
