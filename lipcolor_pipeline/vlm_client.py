"""Audited qwen3.6-plus image-only calls for the Stage 1.5 Pilot."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from .config import require_env
from .image_assets import MIME_BY_FORMAT
from .settings import PipelineSettings, canonical_json, sha256_json
from .stage1_manifest import sha256_file, stable_id
from .vlm_schemas import (
    ContentVisualAnalysis,
    parse_content_visual_analysis,
    parse_with_deterministic_repair,
)
from .workspace import Workspace, open_database, utc_now


ALLOWED_REQUEST_KEYS = {
    "schema_version",
    "analysis_layer",
    "analysis_unit_type",
    "analysis_unit_id",
    "image_id",
    "analysis_scope",
    "input_context_policy",
    "model",
    "prompt_name",
    "prompt_version",
    "prompt",
    "generation_parameters",
    "image_asset",
    "child_analysis_hashes",
}
ALLOWED_ASSET_KEYS = {
    "derived_asset_id",
    "asset_type",
    "sha256",
    "mime_type",
    "width",
    "height",
    "transform_fingerprint",
}


class CallBudgetExceeded(RuntimeError):
    pass


class CallBudget:
    def __init__(self, maximum: int) -> None:
        if maximum <= 0:
            raise ValueError("max_calls must be positive")
        self.maximum = maximum
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> int:
        with self._lock:
            if self.used >= self.maximum:
                raise CallBudgetExceeded(
                    f"online call budget exhausted: {self.maximum}"
                )
            self.used += 1
            return self.used


@dataclass(frozen=True)
class AnalysisTask:
    image_id: str
    scope: str
    unit_type: str
    unit_id: str
    asset_id: str | None
    asset_path: Path | None
    asset_sha256: str
    asset_type: str | None
    asset_format: str | None
    width: int | None
    height: int | None
    transform_fingerprint: str
    tile_index: int | None
    tile_bbox_image_json: str
    parent_analysis_id: str | None = None
    child_payloads: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class AttemptResult:
    model_run_id: str
    cache_key: str
    attempt: int
    status: str
    schema_status: str
    request_relative_path: str
    raw_relative_path: str | None
    parsed_relative_path: str | None
    response_hash: str | None
    latency_ms: int
    token_usage: dict[str, Any]
    error: dict[str, Any]
    parsed: ContentVisualAnalysis | None
    provider_model_name: str | None


def _prompt(
    settings: PipelineSettings,
    scope: str,
    *,
    child_payloads: tuple[dict[str, Any], ...] = (),
) -> str:
    base = (
        settings.repo_root / "configs" / "prompts" / "image_role_v1.txt"
    ).read_text(encoding="utf-8")
    schema = ContentVisualAnalysis.model_json_schema()
    parts = [
        base,
        f"\nanalysis_scope 必须严格输出为 {scope}。",
        "\nJSON Schema：\n",
        canonical_json(schema),
    ]
    if child_payloads:
        parts.extend(
            [
                "\n以下是同一图片的全局图和局部图分析结果。只合并这些可见内容事实，"
                "不得补充任何外部信息：\n",
                canonical_json(list(child_payloads)),
            ]
        )
    return "".join(parts)


def _generation_parameters(settings: PipelineSettings) -> dict[str, Any]:
    config = settings.section("vlm")
    return {
        "temperature": float(config["temperature"]),
        "response_format": {"type": "json_object"},
        "enable_thinking": bool(config["enable_thinking"]),
    }


def _request_manifest(
    settings: PipelineSettings,
    task: AnalysisTask,
    model: str,
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": "safe-vlm-request-1",
        "analysis_layer": "A",
        "analysis_unit_type": task.unit_type,
        "analysis_unit_id": task.unit_id,
        "image_id": task.image_id,
        "analysis_scope": task.scope,
        "input_context_policy": "image_only",
        "model": model,
        "prompt_name": settings.section("vlm")["prompt_name"],
        "prompt_version": settings.section("vlm")["prompt_version"],
        "prompt": _prompt(
            settings,
            task.scope,
            child_payloads=task.child_payloads,
        ),
        "generation_parameters": _generation_parameters(settings),
    }
    if task.asset_id:
        manifest["image_asset"] = {
            "derived_asset_id": task.asset_id,
            "asset_type": task.asset_type,
            "sha256": task.asset_sha256,
            "mime_type": MIME_BY_FORMAT.get(
                str(task.asset_format).upper(), "application/octet-stream"
            ),
            "width": task.width,
            "height": task.height,
            "transform_fingerprint": task.transform_fingerprint,
        }
    if task.child_payloads:
        manifest["child_analysis_hashes"] = [
            sha256_json(payload) for payload in task.child_payloads
        ]
    audit_image_only_manifest(manifest)
    return manifest


def audit_image_only_manifest(manifest: Mapping[str, Any]) -> None:
    extra = set(manifest) - ALLOWED_REQUEST_KEYS
    if extra:
        raise ValueError(f"A-layer request contains non-whitelisted keys: {extra}")
    asset = manifest.get("image_asset")
    if asset is not None:
        if not isinstance(asset, Mapping):
            raise ValueError("image_asset must be an object")
        extra_asset = set(asset) - ALLOWED_ASSET_KEYS
        if extra_asset:
            raise ValueError(
                f"A-layer image asset contains non-whitelisted keys: {extra_asset}"
            )
    if manifest.get("input_context_policy") != "image_only":
        raise ValueError("A-layer request must use image_only policy")


def _cache_key(manifest: Mapping[str, Any]) -> str:
    cache_payload = {
        "image_id": manifest["image_id"],
        "analysis_scope": manifest["analysis_scope"],
        "asset_sha256": (
            manifest.get("image_asset", {}).get("sha256", "")
            if isinstance(manifest.get("image_asset"), Mapping)
            else ""
        ),
        "child_analysis_hashes": manifest.get("child_analysis_hashes", []),
        "model": manifest["model"],
        "prompt_name": manifest["prompt_name"],
        "prompt_version": manifest["prompt_version"],
        "prompt_hash": hashlib.sha256(
            str(manifest["prompt"]).encode("utf-8")
        ).hexdigest(),
        "generation_parameters": manifest["generation_parameters"],
    }
    return sha256_json(cache_payload)


def _redact(value: str, api_key: str) -> str:
    redacted = value.replace(api_key, "[REDACTED]") if api_key else value
    return redacted.replace("Authorization: Bearer", "Authorization: [REDACTED]")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    if path.exists():
        if path.read_text(encoding="utf-8") == serialized:
            return
        raise FileExistsError(f"immutable run artifact already exists: {path}")
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)


def _write_report_snapshot(
    run_dir: Path,
    name: str,
    value: Mapping[str, Any],
) -> Path:
    fingerprint = sha256_json(value)[:16]
    path = run_dir / "reports" / f"{name}.{fingerprint}.json"
    _write_json(path, value)
    return path


def _data_url(path: Path, image_format: str) -> str:
    import base64

    mime = MIME_BY_FORMAT.get(image_format.upper(), "application/octet-stream")
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _call_once(
    *,
    client: Any,
    api_key: str,
    budget: CallBudget,
    settings: PipelineSettings,
    workspace: Workspace,
    run_id: str,
    task: AnalysisTask,
    manifest: dict[str, Any],
    cache_key: str,
    attempt: int,
) -> AttemptResult:
    model_run_id = stable_id(
        "model_run", run_id, task.unit_id, cache_key, attempt
    )
    run_dir = workspace.run_dir(run_id)
    request_relative = f"model/requests/{model_run_id}.json"
    raw_relative = f"model/raw/{model_run_id}.json"
    parsed_relative = f"model/parsed/{model_run_id}.json"
    _write_json(run_dir / request_relative, manifest)
    started = time.perf_counter()
    raw_path = run_dir / raw_relative
    parsed_path = run_dir / parsed_relative
    try:
        budget.take()
        content: list[dict[str, Any]] = []
        if task.asset_path is not None:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _data_url(
                            task.asset_path,
                            str(task.asset_format or "JPEG"),
                        )
                    },
                }
            )
        content.append({"type": "text", "text": manifest["prompt"]})
        generation = manifest["generation_parameters"]
        response = client.chat.completions.create(
            model=manifest["model"],
            messages=[{"role": "user", "content": content}],
            response_format=generation["response_format"],
            temperature=generation["temperature"],
            extra_body={"enable_thinking": generation["enable_thinking"]},
        )
        raw_payload = response.model_dump(mode="json")
        # Raw provider data is durable before any parsing or schema validation.
        _write_json(raw_path, raw_payload)
        response_hash = sha256_file(raw_path)
        text = response.choices[0].message.content or ""
        schema_status = "valid"
        repair_actions: tuple[str, ...] = ()
        try:
            parsed = parse_content_visual_analysis(text)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            try:
                parsed, repair_actions = parse_with_deterministic_repair(
                    text,
                    image_width=task.width,
                    image_height=task.height,
                )
                schema_status = "valid_after_deterministic_repair"
            except (
                json.JSONDecodeError,
                ValidationError,
                ValueError,
            ) as repair_error:
                return AttemptResult(
                    model_run_id=model_run_id,
                    cache_key=cache_key,
                    attempt=attempt,
                    status="schema_failed",
                    schema_status="invalid",
                    request_relative_path=request_relative,
                    raw_relative_path=raw_relative,
                    parsed_relative_path=None,
                    response_hash=response_hash,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    token_usage=(
                        response.usage.model_dump(mode="json")
                        if response.usage is not None
                        else {}
                    ),
                    error={
                        "type": type(exc).__name__,
                        "message": _redact(str(exc), api_key),
                        "repair_error": _redact(
                            str(repair_error),
                            api_key,
                        ),
                    },
                    parsed=None,
                    provider_model_name=getattr(response, "model", None),
                )
        _write_json(parsed_path, parsed.model_dump(mode="json"))
        return AttemptResult(
            model_run_id=model_run_id,
            cache_key=cache_key,
            attempt=attempt,
            status="succeeded",
            schema_status=schema_status,
            request_relative_path=request_relative,
            raw_relative_path=raw_relative,
            parsed_relative_path=parsed_relative,
            response_hash=response_hash,
            latency_ms=round((time.perf_counter() - started) * 1000),
            token_usage=(
                response.usage.model_dump(mode="json")
                if response.usage is not None
                else {}
            ),
            error=(
                {"schema_repair_actions": list(repair_actions)}
                if repair_actions
                else {}
            ),
            parsed=parsed,
            provider_model_name=getattr(response, "model", None),
        )
    except Exception as exc:
        return AttemptResult(
            model_run_id=model_run_id,
            cache_key=cache_key,
            attempt=attempt,
            status=(
                "budget_exhausted"
                if isinstance(exc, CallBudgetExceeded)
                else "request_failed"
            ),
            schema_status="not_parsed",
            request_relative_path=request_relative,
            raw_relative_path=raw_relative if raw_path.exists() else None,
            parsed_relative_path=None,
            response_hash=sha256_file(raw_path) if raw_path.exists() else None,
            latency_ms=round((time.perf_counter() - started) * 1000),
            token_usage={},
            error={
                "type": type(exc).__name__,
                "message": _redact(str(exc), api_key),
            },
            parsed=None,
            provider_model_name=None,
        )


def _execute_task(
    *,
    client: Any,
    api_key: str,
    budget: CallBudget,
    settings: PipelineSettings,
    workspace: Workspace,
    run_id: str,
    task: AnalysisTask,
    model: str,
    attempt_offset: int = 0,
) -> tuple[AnalysisTask, dict[str, Any], list[AttemptResult]]:
    manifest = _request_manifest(settings, task, model)
    cache_key = _cache_key(manifest)
    attempts: list[AttemptResult] = []
    total_attempt_limit = int(settings.section("vlm")["max_retries"]) + 1
    attempts_this_invocation = min(
        2,
        max(0, total_attempt_limit - attempt_offset),
    )
    for local_attempt in range(1, attempts_this_invocation + 1):
        attempt = attempt_offset + local_attempt
        result = _call_once(
            client=client,
            api_key=api_key,
            budget=budget,
            settings=settings,
            workspace=workspace,
            run_id=run_id,
            task=task,
            manifest=manifest,
            cache_key=cache_key,
            attempt=attempt,
        )
        attempts.append(result)
        if result.status == "succeeded" or result.status == "budget_exhausted":
            break
        if result.status == "request_failed":
            time.sleep(min(8.0, 2.0 ** (attempt - 1)))
    return task, manifest, attempts


def _attempt_offset(
    connection: sqlite3.Connection,
    settings: PipelineSettings,
    *,
    run_id: str,
    task: AnalysisTask,
    model: str,
) -> int:
    cache_key = _cache_key(_request_manifest(settings, task, model))
    return int(
        connection.execute(
            """
            SELECT COUNT(*) FROM model_runs
            WHERE run_id = ? AND cache_key = ?
              AND status NOT IN ('cache_hit', 'budget_exhausted')
            """,
            (run_id, cache_key),
        ).fetchone()[0]
    )


def _record_attempt(
    connection: sqlite3.Connection,
    settings: PipelineSettings,
    run_id: str,
    result: AttemptResult,
    manifest: Mapping[str, Any],
) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO model_runs(
            model_run_id, run_id, analysis_layer, analysis_unit_type,
            analysis_unit_id, model_name, provider, base_url_alias,
            prompt_name, prompt_version, schema_version,
            input_context_policy, generation_parameters_json, cache_key,
            request_hash, request_path, raw_response_path,
            parsed_response_path, response_hash, schema_validation_status,
            latency_ms, token_usage_json, status, error_json,
            created_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result.model_run_id,
            run_id,
            "A",
            manifest["analysis_unit_type"],
            manifest["analysis_unit_id"],
            manifest["model"],
            settings.section("vlm")["provider"],
            os.environ.get(
                str(settings.section("vlm")["base_url_env"]), ""
            ),
            manifest["prompt_name"],
            manifest["prompt_version"],
            settings.section("vlm")["schema_version"],
            "image_only",
            canonical_json(manifest["generation_parameters"]),
            result.cache_key,
            hashlib.sha256(canonical_json(manifest).encode()).hexdigest(),
            result.request_relative_path,
            result.raw_relative_path,
            result.parsed_relative_path,
            result.response_hash,
            result.schema_status,
            result.latency_ms,
            canonical_json(result.token_usage),
            result.status,
            canonical_json(
                {
                    **result.error,
                    "provider_model_name": result.provider_model_name,
                    "attempt": result.attempt,
                }
            ),
            utc_now(),
            utc_now(),
        ),
    )


def _record_analysis(
    connection: sqlite3.Connection,
    run_id: str,
    task: AnalysisTask,
    result: AttemptResult,
    schema_version: str,
) -> str:
    if result.parsed is None:
        raise ValueError("cannot record an analysis without a parsed response")
    parsed = result.parsed
    analysis_id = stable_id(
        "content_analysis",
        run_id,
        task.image_id,
        task.scope,
        task.asset_id or "",
        task.tile_index if task.tile_index is not None else "",
        result.cache_key,
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO content_visual_analyses(
            content_visual_analysis_id, run_id, image_id, analysis_scope,
            analysis_asset_id, parent_content_visual_analysis_id,
            tile_index, tile_bbox_image_json, primary_role,
            secondary_roles_json, layout_type, global_layout_json,
            role_confidence, contains_text, contains_multiple_shades,
            contains_lips, contains_skin_swatch, contains_product_bullet,
            contains_packaging, depicted_shades_json,
            representative_color_eligible, eligibility_score,
            recommended_strategy, rejection_reasons_json,
            candidate_regions_json, model_run_id, schema_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_id,
            run_id,
            task.image_id,
            task.scope,
            task.asset_id,
            task.parent_analysis_id,
            task.tile_index,
            task.tile_bbox_image_json,
            parsed.primary_role,
            canonical_json(parsed.secondary_roles),
            parsed.layout_type,
            canonical_json(parsed.global_layout),
            parsed.role_confidence,
            int(parsed.contains_text),
            int(parsed.contains_multiple_shades),
            int(parsed.contains_lips),
            int(parsed.contains_skin_swatch),
            int(parsed.contains_product_bullet),
            int(parsed.contains_packaging),
            canonical_json(parsed.depicted_shades),
            int(parsed.representative_color_eligible),
            parsed.eligibility_score,
            parsed.recommended_strategy,
            canonical_json(parsed.rejection_reasons),
            canonical_json(
                [
                    region.model_dump(mode="json")
                    for region in parsed.candidate_color_regions
                ]
            ),
            result.model_run_id,
            schema_version,
            utc_now(),
        ),
    )
    return analysis_id


def _cached_result(
    connection: sqlite3.Connection,
    settings: PipelineSettings,
    workspace: Workspace,
    *,
    run_id: str,
    task: AnalysisTask,
    model: str,
) -> tuple[dict[str, Any], AttemptResult] | None:
    """Materialize a prior successful cache entry into the current run."""

    manifest = _request_manifest(settings, task, model)
    cache_key = _cache_key(manifest)
    row = connection.execute(
        """
        SELECT model_run_id, run_id, raw_response_path,
               parsed_response_path, response_hash,
               error_json, model_name
        FROM model_runs
        WHERE cache_key = ?
          AND status IN (
              'succeeded', 'cache_hit', 'local_repair_succeeded'
          )
          AND parsed_response_path IS NOT NULL
          AND run_id <> ?
        ORDER BY finished_at DESC, model_run_id DESC
        LIMIT 1
        """,
        (cache_key, run_id),
    ).fetchone()
    if row is None:
        return None
    source_run_dir = workspace.run_dir(row["run_id"])
    source_parsed = source_run_dir / row["parsed_response_path"]
    if not row["raw_response_path"]:
        return None
    source_raw = source_run_dir / row["raw_response_path"]
    if not source_parsed.is_file() or not source_raw.is_file():
        return None
    try:
        parsed = ContentVisualAnalysis.model_validate_json(
            source_parsed.read_text(encoding="utf-8")
        )
    except (ValidationError, ValueError, json.JSONDecodeError):
        return None

    current_model_run_id = stable_id(
        "model_run", run_id, task.unit_id, cache_key, "cache"
    )
    current_run_dir = workspace.run_dir(run_id)
    request_relative = f"model/requests/{current_model_run_id}.json"
    parsed_relative = f"model/parsed/{current_model_run_id}.json"
    raw_relative: str | None = None
    _write_json(current_run_dir / request_relative, manifest)
    _write_json(
        current_run_dir / parsed_relative,
        parsed.model_dump(mode="json"),
    )
    raw_relative = f"model/raw/{current_model_run_id}.json"
    destination = current_run_dir / raw_relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source_raw, destination)
    except (FileExistsError, OSError):
        if not destination.exists():
            shutil.copy2(source_raw, destination)
    source_error = json.loads(row["error_json"] or "{}")
    return (
        manifest,
        AttemptResult(
            model_run_id=current_model_run_id,
            cache_key=cache_key,
            attempt=0,
            status="cache_hit",
            schema_status="valid",
            request_relative_path=request_relative,
            raw_relative_path=raw_relative,
            parsed_relative_path=parsed_relative,
            response_hash=(
                row["response_hash"]
                or (
                    sha256_file(current_run_dir / raw_relative)
                    if raw_relative
                    else sha256_file(current_run_dir / parsed_relative)
                )
            ),
            latency_ms=0,
            token_usage={
                "cached": True,
                "cached_from_model_run_id": row["model_run_id"],
            },
            error={"cached_from_model_run_id": row["model_run_id"]},
            parsed=parsed,
            provider_model_name=(
                source_error.get("provider_model_name") or row["model_name"]
            ),
        ),
    )


def repair_failed_pilot_analyses(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    image_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Replay raw responses through versioned deterministic schema repair."""

    model = os.environ.get(
        str(settings.section("vlm")["model_env"]),
        "qwen3.6-plus",
    )
    schema_version = str(settings.section("vlm")["schema_version"])
    repaired_count = 0
    unrepaired: list[dict[str, Any]] = []
    run_dir = workspace.run_dir(run_id)
    with open_database(workspace.database_path) as connection:
        tasks = _asset_tasks(
            connection,
            workspace,
            run_id,
            image_id=image_id,
            limit=limit,
        )
        for task in tasks:
            if connection.execute(
                """
                SELECT 1 FROM content_visual_analyses
                WHERE run_id = ? AND analysis_asset_id = ?
                """,
                (run_id, task.asset_id),
            ).fetchone():
                continue
            failures = list(
                connection.execute(
                    """
                    SELECT * FROM model_runs
                    WHERE run_id = ? AND analysis_unit_id = ?
                      AND status = 'schema_failed'
                      AND raw_response_path IS NOT NULL
                    ORDER BY created_at DESC, model_run_id DESC
                    """,
                    (run_id, task.unit_id),
                )
            )
            repair_result: tuple[
                ContentVisualAnalysis,
                tuple[str, ...],
                sqlite3.Row,
            ] | None = None
            last_error = "no schema-failed raw response"
            for source in failures:
                source_raw = run_dir / source["raw_response_path"]
                if not source_raw.is_file():
                    last_error = "source raw response is missing"
                    continue
                try:
                    raw_payload = json.loads(
                        source_raw.read_text(encoding="utf-8")
                    )
                    text = raw_payload["choices"][0]["message"]["content"]
                    parsed, actions = parse_with_deterministic_repair(
                        text,
                        image_width=task.width,
                        image_height=task.height,
                    )
                except (
                    KeyError,
                    IndexError,
                    TypeError,
                    json.JSONDecodeError,
                    ValidationError,
                    ValueError,
                ) as error:
                    last_error = f"{type(error).__name__}: {error}"
                    continue
                repair_result = parsed, actions, source
                break
            if repair_result is None:
                if failures:
                    unrepaired.append(
                        {
                            "analysis_unit_id": task.unit_id,
                            "failure_count": len(failures),
                            "reason": last_error,
                        }
                    )
                continue

            parsed, actions, source = repair_result
            manifest = _request_manifest(settings, task, model)
            cache_key = _cache_key(manifest)
            model_run_id = stable_id(
                "model_run",
                run_id,
                task.unit_id,
                cache_key,
                "deterministic-repair-1.1",
                source["model_run_id"],
            )
            request_relative = f"model/requests/{model_run_id}.json"
            raw_relative = f"model/raw/{model_run_id}.json"
            parsed_relative = f"model/parsed/{model_run_id}.json"
            _write_json(run_dir / request_relative, manifest)
            source_raw = run_dir / source["raw_response_path"]
            destination_raw = run_dir / raw_relative
            destination_raw.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source_raw, destination_raw)
            except (FileExistsError, OSError):
                if not destination_raw.exists():
                    shutil.copy2(source_raw, destination_raw)
            _write_json(
                run_dir / parsed_relative,
                parsed.model_dump(mode="json"),
            )
            source_error = json.loads(source["error_json"] or "{}")
            result = AttemptResult(
                model_run_id=model_run_id,
                cache_key=cache_key,
                attempt=0,
                status="local_repair_succeeded",
                schema_status="valid_after_deterministic_repair",
                request_relative_path=request_relative,
                raw_relative_path=raw_relative,
                parsed_relative_path=parsed_relative,
                response_hash=source["response_hash"],
                latency_ms=0,
                token_usage={
                    "local_repair": True,
                    "source_model_run_id": source["model_run_id"],
                },
                error={
                    "schema_repair_actions": list(actions),
                    "source_model_run_id": source["model_run_id"],
                },
                parsed=parsed,
                provider_model_name=source_error.get("provider_model_name"),
            )
            _record_attempt(connection, settings, run_id, result, manifest)
            _record_analysis(
                connection,
                run_id,
                task,
                result,
                schema_version,
            )
            repaired_count += 1
        connection.commit()
    summary = {
        "schema_version": "pilot-deterministic-repair-1.1",
        "run_id": run_id,
        "repaired_analyses": repaired_count,
        "unrepaired": unrepaired,
        "repair_policy": (
            "unambiguous_pixel_or_millesimal_bbox_normalization_and_"
            "known_strategy_aliases_only"
        ),
    }
    _write_report_snapshot(run_dir, "deterministic_schema_repair", summary)
    return summary


def _asset_tasks(
    connection: sqlite3.Connection,
    workspace: Workspace,
    run_id: str,
    *,
    image_id: str | None = None,
    limit: int | None = None,
) -> list[AnalysisTask]:
    rows = connection.execute(
        """
        SELECT
            asset.derived_asset_id,
            asset.image_id,
            asset.asset_type,
            asset.relative_path,
            asset.sha256,
            asset.width,
            asset.height,
            asset.format,
            asset.transform_fingerprint,
            asset.created_at,
            tile.tile_index,
            tile.bbox_image_json
        FROM derived_assets AS asset
        JOIN pilot_samples AS sample
          ON sample.image_id = asset.image_id
         AND sample.pilot_run_id = asset.run_id
        LEFT JOIN image_tiles AS tile
          ON tile.tile_asset_id = asset.derived_asset_id
        WHERE asset.run_id = ?
          AND asset.asset_type IN (
              'analysis_preview', 'vlm_input_preview',
              'global_thumbnail', 'image_tile'
          )
        ORDER BY asset.image_id,
                 CASE asset.asset_type
                     WHEN 'analysis_preview' THEN 0
                     WHEN 'global_thumbnail' THEN 1
                     ELSE 2 END,
                 tile.tile_index
        """,
        (run_id,),
    )
    selected_rows: dict[tuple[str, str, int], sqlite3.Row] = {}
    for row in rows:
        scope = {
            "analysis_preview": "image",
            "vlm_input_preview": "image",
            "global_thumbnail": "global_thumbnail",
            "image_tile": "tile",
        }[row["asset_type"]]
        key = (
            row["image_id"],
            scope,
            int(row["tile_index"] if row["tile_index"] is not None else -1),
        )
        existing = selected_rows.get(key)
        priority = 2 if row["asset_type"] == "vlm_input_preview" else 1
        existing_priority = (
            2
            if existing is not None
            and existing["asset_type"] == "vlm_input_preview"
            else 1
        )
        if existing is not None and (
            priority < existing_priority
            or (
                priority == existing_priority
                and (
                    row["created_at"],
                    row["derived_asset_id"],
                )
                <= (
                    existing["created_at"],
                    existing["derived_asset_id"],
                )
            )
        ):
            continue
        selected_rows[key] = row

    tasks: list[AnalysisTask] = []
    for key in sorted(selected_rows):
        row = selected_rows[key]
        scope = key[1]
        tasks.append(
            AnalysisTask(
                image_id=row["image_id"],
                scope=scope,
                unit_type=(
                    "image_tile" if scope == "tile" else "derived_image_asset"
                ),
                unit_id=row["derived_asset_id"],
                asset_id=row["derived_asset_id"],
                asset_path=workspace.output_root / row["relative_path"],
                asset_sha256=row["sha256"],
                asset_type=row["asset_type"],
                asset_format=row["format"],
                width=row["width"],
                height=row["height"],
                transform_fingerprint=row["transform_fingerprint"],
                tile_index=row["tile_index"],
                tile_bbox_image_json=row["bbox_image_json"] or "[]",
            )
        )
    if image_id is not None:
        tasks = [task for task in tasks if task.image_id == image_id]
        if not tasks:
            raise KeyError("requested image_id has no prepared Pilot assets")
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        selected_images = sorted({task.image_id for task in tasks})[:limit]
        selected = set(selected_images)
        tasks = [task for task in tasks if task.image_id in selected]
    return tasks


def planned_pilot_calls(
    workspace: Workspace,
    *,
    run_id: str,
    image_id: str | None = None,
    limit: int | None = None,
    total_attempt_limit: int = 4,
) -> dict[str, int]:
    with open_database(workspace.database_path, readonly=True) as connection:
        tasks = _asset_tasks(
            connection,
            workspace,
            run_id,
            image_id=image_id,
            limit=limit,
        )
        pending_tasks: list[AnalysisTask] = []
        exhausted_asset_units = 0
        for task in tasks:
            if connection.execute(
                """
                SELECT 1 FROM content_visual_analyses
                WHERE run_id = ? AND analysis_asset_id = ?
                """,
                (run_id, task.asset_id),
            ).fetchone():
                continue
            provider_attempts = connection.execute(
                """
                SELECT COUNT(*) FROM model_runs
                WHERE run_id = ? AND analysis_unit_id = ?
                  AND status NOT IN (
                      'cache_hit', 'budget_exhausted',
                      'local_repair_succeeded'
                  )
                """,
                (run_id, task.unit_id),
            ).fetchone()[0]
            if provider_attempts >= total_attempt_limit:
                exhausted_asset_units += 1
            else:
                pending_tasks.append(task)
        long_image_ids = {
            task.image_id
            for task in tasks
            if task.scope == "global_thumbnail"
        }
        pending_long_merges = 0
        blocked_long_merges = 0
        exhausted_long_merges = 0
        for candidate in long_image_ids:
            if connection.execute(
                """
                SELECT 1 FROM content_visual_analyses
                WHERE run_id = ? AND image_id = ?
                  AND analysis_scope = 'merged_content_summary'
                """,
                (run_id, candidate),
            ).fetchone():
                continue
            expected_children = connection.execute(
                """
                SELECT 1 + COUNT(tile.image_tile_id)
                FROM long_image_layouts AS layout
                JOIN image_tiles AS tile
                  ON tile.long_image_layout_id = layout.long_image_layout_id
                WHERE layout.run_id = ? AND layout.image_id = ?
                """,
                (run_id, candidate),
            ).fetchone()[0]
            available_children = connection.execute(
                """
                SELECT COUNT(*) FROM content_visual_analyses
                WHERE run_id = ? AND image_id = ?
                  AND analysis_scope IN ('global_thumbnail', 'tile')
                """,
                (run_id, candidate),
            ).fetchone()[0]
            if available_children != expected_children:
                blocked_long_merges += 1
                continue
            merge_unit_id = stable_id(
                "merged_unit",
                run_id,
                candidate,
            )
            provider_attempts = connection.execute(
                """
                SELECT COUNT(*) FROM model_runs
                WHERE run_id = ? AND analysis_unit_id = ?
                  AND status NOT IN (
                      'cache_hit', 'budget_exhausted',
                      'local_repair_succeeded'
                  )
                """,
                (run_id, merge_unit_id),
            ).fetchone()[0]
            if provider_attempts >= total_attempt_limit:
                exhausted_long_merges += 1
            else:
                pending_long_merges += 1
    base = len(pending_tasks) + pending_long_merges
    return {
        "asset_analysis_calls": len(pending_tasks),
        "long_merge_calls": pending_long_merges,
        "exhausted_asset_units": exhausted_asset_units,
        "blocked_long_merges": blocked_long_merges,
        "exhausted_long_merges": exhausted_long_merges,
        "planned_success_calls": base,
        "recommended_max_calls_with_retry_headroom": max(
            base, int(base * 1.2 + 0.999)
        ),
    }


def run_pilot_vlm(
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    execute_online: bool,
    max_calls: int | None,
    image_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    plan = planned_pilot_calls(
        workspace,
        run_id=run_id,
        image_id=image_id,
        limit=limit,
        total_attempt_limit=int(settings.section("vlm")["max_retries"]) + 1,
    )
    run_dir = workspace.run_dir(run_id)
    _write_report_snapshot(run_dir, "online_call_plan", plan)
    if not execute_online:
        return {
            **plan,
            "run_id": run_id,
            "status": "dry_run",
            "online_calls_made": 0,
        }
    if max_calls is None:
        raise ValueError("--max-calls is required with --execute-online")

    vlm = settings.section("vlm")
    api_key = require_env(str(vlm["api_key_env"]))
    base_url = require_env(str(vlm["base_url_env"]))
    model = require_env(str(vlm["model_env"]))
    if bool(vlm["enable_thinking"]):
        raise ValueError("structured Pilot requires enable_thinking=false")
    try:
        from openai import OpenAI
    except ImportError as exc:  # pragma: no cover - dependency preflight.
        raise RuntimeError("OpenAI SDK is required for online Pilot calls") from exc
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=float(vlm["timeout_seconds"]),
        max_retries=0,
    )
    budget = CallBudget(max_calls)
    schema_version = str(vlm["schema_version"])

    cache_hits = 0
    with open_database(workspace.database_path) as connection:
        selected_tasks = _asset_tasks(
            connection,
            workspace,
            run_id,
            image_id=image_id,
            limit=limit,
        )
        tasks: list[AnalysisTask] = []
        task_attempt_offsets: dict[str, int] = {}
        for task in selected_tasks:
            if connection.execute(
                """
                SELECT 1 FROM content_visual_analyses
                WHERE run_id = ? AND analysis_asset_id = ?
                """,
                (run_id, task.asset_id),
            ).fetchone():
                continue
            cached = _cached_result(
                connection,
                settings,
                workspace,
                run_id=run_id,
                task=task,
                model=model,
            )
            if cached is None:
                offset = _attempt_offset(
                    connection,
                    settings,
                    run_id=run_id,
                    task=task,
                    model=model,
                )
                if offset >= int(vlm["max_retries"]) + 1:
                    continue
                tasks.append(task)
                task_attempt_offsets[task.unit_id] = offset
                continue
            manifest, result = cached
            _record_attempt(connection, settings, run_id, result, manifest)
            _record_analysis(
                connection,
                run_id,
                task,
                result,
                schema_version,
            )
            cache_hits += 1
        connection.commit()

    task_results: list[
        tuple[AnalysisTask, dict[str, Any], list[AttemptResult]]
    ] = []
    workers = max(1, int(vlm["concurrency"]))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vlm") as pool:
        futures = [
            pool.submit(
                _execute_task,
                client=client,
                api_key=api_key,
                budget=budget,
                settings=settings,
                workspace=workspace,
                run_id=run_id,
                task=task,
                model=model,
                attempt_offset=task_attempt_offsets[task.unit_id],
            )
            for task in tasks
        ]
        for future in as_completed(futures):
            task_results.append(future.result())

    with open_database(workspace.database_path) as connection:
        for task, manifest, attempts in task_results:
            for attempt in attempts:
                _record_attempt(connection, settings, run_id, attempt, manifest)
            successful = next(
                (attempt for attempt in reversed(attempts) if attempt.parsed),
                None,
            )
            if successful:
                _record_analysis(
                    connection,
                    run_id,
                    task,
                    successful,
                    schema_version,
                )
        connection.commit()

        merge_tasks: list[AnalysisTask] = []
        selected_image_ids = {task.image_id for task in selected_tasks}
        long_image_ids = [
            row[0]
            for row in connection.execute(
                """
                SELECT image_id FROM long_image_layouts WHERE run_id = ?
                ORDER BY image_id
                """,
                (run_id,),
            )
            if row[0] in selected_image_ids
        ]
        for image_id in long_image_ids:
            if connection.execute(
                """
                SELECT 1 FROM content_visual_analyses
                WHERE run_id = ? AND image_id = ?
                  AND analysis_scope = 'merged_content_summary'
                """,
                (run_id, image_id),
            ).fetchone():
                continue
            children = list(
                connection.execute(
                    """
                    SELECT * FROM content_visual_analyses
                    WHERE run_id = ? AND image_id = ?
                      AND analysis_scope IN ('global_thumbnail', 'tile')
                    ORDER BY CASE analysis_scope
                        WHEN 'global_thumbnail' THEN 0 ELSE 1 END, tile_index
                    """,
                    (run_id, image_id),
                )
            )
            expected = connection.execute(
                """
                SELECT 1 + COUNT(tile.image_tile_id)
                FROM long_image_layouts AS layout
                JOIN image_tiles AS tile
                  ON tile.long_image_layout_id = layout.long_image_layout_id
                WHERE layout.run_id = ? AND layout.image_id = ?
                """,
                (run_id, image_id),
            ).fetchone()[0]
            if len(children) != expected:
                continue
            child_payloads = tuple(
                {
                    "analysis_scope": row["analysis_scope"],
                    "tile_index": row["tile_index"],
                    "tile_bbox_image": json.loads(row["tile_bbox_image_json"]),
                    "primary_role": row["primary_role"],
                    "secondary_roles": json.loads(row["secondary_roles_json"]),
                    "layout_type": row["layout_type"],
                    "role_confidence": row["role_confidence"],
                    "contains_text": bool(row["contains_text"]),
                    "contains_multiple_shades": bool(
                        row["contains_multiple_shades"]
                    ),
                    "contains_lips": bool(row["contains_lips"]),
                    "contains_skin_swatch": bool(row["contains_skin_swatch"]),
                    "contains_product_bullet": bool(
                        row["contains_product_bullet"]
                    ),
                    "contains_packaging": bool(row["contains_packaging"]),
                    "depicted_shades": json.loads(row["depicted_shades_json"]),
                    "representative_color_eligible": bool(
                        row["representative_color_eligible"]
                    ),
                    "eligibility_score": row["eligibility_score"],
                    "recommended_strategy": row["recommended_strategy"],
                    "rejection_reasons": json.loads(
                        row["rejection_reasons_json"]
                    ),
                    "candidate_color_regions": json.loads(
                        row["candidate_regions_json"]
                    ),
                }
                for row in children
            )
            parent = next(
                (
                    row["content_visual_analysis_id"]
                    for row in children
                    if row["analysis_scope"] == "global_thumbnail"
                ),
                None,
            )
            merge_tasks.append(
                AnalysisTask(
                    image_id=image_id,
                    scope="merged_content_summary",
                    unit_type="merged_content",
                    unit_id=stable_id("merged_unit", run_id, image_id),
                    asset_id=None,
                    asset_path=None,
                    asset_sha256="",
                    asset_type=None,
                    asset_format=None,
                    width=None,
                    height=None,
                    transform_fingerprint="",
                    tile_index=None,
                    tile_bbox_image_json="[]",
                    parent_analysis_id=parent,
                    child_payloads=child_payloads,
                )
            )

    with open_database(workspace.database_path) as connection:
        uncached_merge_tasks: list[AnalysisTask] = []
        merge_attempt_offsets: dict[str, int] = {}
        for task in merge_tasks:
            cached = _cached_result(
                connection,
                settings,
                workspace,
                run_id=run_id,
                task=task,
                model=model,
            )
            if cached is None:
                offset = _attempt_offset(
                    connection,
                    settings,
                    run_id=run_id,
                    task=task,
                    model=model,
                )
                if offset >= int(vlm["max_retries"]) + 1:
                    continue
                uncached_merge_tasks.append(task)
                merge_attempt_offsets[task.unit_id] = offset
                continue
            manifest, result = cached
            _record_attempt(connection, settings, run_id, result, manifest)
            _record_analysis(
                connection,
                run_id,
                task,
                result,
                schema_version,
            )
            cache_hits += 1
        connection.commit()

    merge_results = [
        _execute_task(
            client=client,
            api_key=api_key,
            budget=budget,
            settings=settings,
            workspace=workspace,
            run_id=run_id,
            task=task,
            model=model,
            attempt_offset=merge_attempt_offsets[task.unit_id],
        )
        for task in uncached_merge_tasks
    ]
    with open_database(workspace.database_path) as connection:
        for task, manifest, attempts in merge_results:
            for attempt in attempts:
                _record_attempt(connection, settings, run_id, attempt, manifest)
            successful = next(
                (attempt for attempt in reversed(attempts) if attempt.parsed),
                None,
            )
            if successful:
                _record_analysis(
                    connection,
                    run_id,
                    task,
                    successful,
                    schema_version,
                )
        connection.commit()
        statuses = {
            row["status"]: row["count"]
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM model_runs WHERE run_id = ? GROUP BY status
                """,
                (run_id,),
            )
        }
        token_totals: dict[str, int] = {}
        for row in connection.execute(
            "SELECT token_usage_json FROM model_runs WHERE run_id = ?",
            (run_id,),
        ):
            usage = json.loads(row[0])
            for key, value in usage.items():
                if isinstance(value, int):
                    token_totals[key] = token_totals.get(key, 0) + value
    summary = {
        **plan,
        "run_id": run_id,
        "status": "completed_with_possible_failures",
        "online_calls_made": budget.used,
        "cache_hits": cache_hits,
        "max_calls": max_calls,
        "attempt_statuses": statuses,
        "token_totals": token_totals,
    }
    _write_report_snapshot(run_dir, "vlm_execution_summary", summary)
    return summary
