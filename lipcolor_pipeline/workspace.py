"""Copied workspace database, run fingerprints, and JSONL mirrors."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .settings import PipelineSettings, canonical_json, sha256_json
from .stage1_manifest import apply_migrations, sha256_file, stable_id


WORKSPACE_SCHEMA_VERSION = "stage2-6-1"
PIPELINE_VERSION = "0.3.0"
DEPENDENCIES = (
    "fastapi",
    "numpy",
    "openai",
    "Pillow",
    "pydantic",
    "PyYAML",
    "uvicorn",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Workspace:
    repo_root: Path
    output_root: Path
    database_path: Path
    dataset_snapshot_id: str
    stage1_database_path: Path

    @property
    def runs_root(self) -> Path:
        return self.output_root / "runs"

    @property
    def assets_root(self) -> Path:
        return self.output_root / "assets"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id


def dependency_snapshot(lock_path: Path | None = None) -> dict[str, Any]:
    versions: dict[str, str] = {}
    for name in DEPENDENCIES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not-installed"
    snapshot: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "packages": versions,
    }
    if lock_path is not None and lock_path.is_file():
        snapshot["lock_file"] = {
            "path": lock_path.name,
            "sha256": sha256_file(lock_path),
        }
    return snapshot


def git_state(repo_root: Path) -> tuple[str, int]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown", 1
    return commit, int(dirty)


def source_fingerprint(repo_root: Path) -> str:
    patterns = (
        "lipcolor_pipeline/**/*.py",
        "database/migrations/*.sql",
        "configs/**/*.yaml",
        "configs/**/*.yml",
        "configs/**/*.txt",
    )
    paths: set[Path] = {
        repo_root / "pyproject.toml",
        repo_root / "requirements.lock",
    }
    for pattern in patterns:
        paths.update(repo_root.glob(pattern))
    digest = hashlib.sha256()
    for path in sorted(
        (path for path in paths if path.is_file()),
        key=lambda item: item.relative_to(repo_root).as_posix(),
    ):
        relative = path.relative_to(repo_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@contextmanager
def open_database(
    path: Path,
    *,
    readonly: bool = False,
) -> Iterator[sqlite3.Connection]:
    if readonly:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON")
    else:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        if not readonly:
            connection.commit()
    except Exception:
        if not readonly:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize_workspace(
    settings: PipelineSettings,
    *,
    through_version: int = 7,
) -> Workspace:
    stage1_db = settings.project_path("stage1_database")
    if not stage1_db.is_file():
        raise FileNotFoundError(f"Stage 1 database not found: {stage1_db}")

    with open_database(stage1_db, readonly=True) as source:
        row = source.execute(
            "SELECT dataset_snapshot_id FROM dataset_snapshots"
        ).fetchone()
        if row is None:
            raise RuntimeError("Stage 1 database has no dataset snapshot")
        dataset_snapshot_id = str(row[0])

    output_root = settings.project_path("output_root")
    workspace_dir = output_root / "workspaces" / dataset_snapshot_id
    database_path = workspace_dir / "lipcolor.sqlite"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (output_root / "runs").mkdir(parents=True, exist_ok=True)
    (output_root / "assets").mkdir(parents=True, exist_ok=True)

    source_hash_before = sha256_file(stage1_db)
    if not database_path.exists():
        fd, temporary_name = tempfile.mkstemp(
            prefix="lipcolor.",
            suffix=".sqlite.tmp",
            dir=workspace_dir,
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            with open_database(stage1_db, readonly=True) as source:
                with closing(sqlite3.connect(temporary_path)) as destination:
                    source.backup(destination)
                    destination.commit()
            os.replace(temporary_path, database_path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    with open_database(database_path) as connection:
        copied_snapshot = connection.execute(
            "SELECT dataset_snapshot_id FROM dataset_snapshots"
        ).fetchone()
        if copied_snapshot is None or copied_snapshot[0] != dataset_snapshot_id:
            raise RuntimeError("workspace database belongs to another dataset snapshot")
        apply_migrations(
            connection,
            settings.repo_root / "database" / "migrations",
            through_version=through_version,
        )
        metadata = {
            "dataset_snapshot_id": dataset_snapshot_id,
            "stage1_database_uri": _repo_uri(stage1_db, settings.repo_root),
            "stage1_database_sha256": source_hash_before,
            "workspace_schema_version": WORKSPACE_SCHEMA_VERSION,
        }
        for key, value in metadata.items():
            existing = connection.execute(
                "SELECT value FROM workspace_metadata WHERE key = ?",
                (key,),
            ).fetchone()
            if (
                existing is not None
                and existing[0] != value
                and key != "workspace_schema_version"
            ):
                raise RuntimeError(f"workspace metadata mismatch: {key}")
            connection.execute(
                """
                INSERT INTO workspace_metadata(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, utc_now()),
            )
        connection.commit()

    source_hash_after = sha256_file(stage1_db)
    if source_hash_before != source_hash_after:
        raise RuntimeError("Stage 1 database changed while initializing workspace")

    return Workspace(
        repo_root=settings.repo_root,
        output_root=output_root,
        database_path=database_path,
        dataset_snapshot_id=dataset_snapshot_id,
        stage1_database_path=stage1_db,
    )


def _repo_uri(path: Path, repo_root: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(repo_root.resolve())
    except ValueError:
        return f"external://{resolved.as_posix()}"
    return f"repo://{relative.as_posix()}"


def ensure_run_directory(
    workspace: Workspace,
    run_id: str,
    *,
    resume: bool,
) -> Path:
    run_dir = workspace.run_dir(run_id)
    if run_dir.exists() and not resume:
        raise FileExistsError(
            f"run directory already exists; use --resume: {run_dir}"
        )
    for child in ("config", "jsonl", "reports", "model/requests", "model/raw", "model/parsed"):
        (run_dir / child).mkdir(parents=True, exist_ok=True)
    return run_dir


def begin_pipeline_run(
    connection: sqlite3.Connection,
    workspace: Workspace,
    settings: PipelineSettings,
    *,
    run_id: str,
    stage: str,
    resume: bool,
    extra_config: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    config_payload: dict[str, Any] = {
        "pipeline_config": settings.values,
        "config_path": _repo_uri(settings.config_path, settings.repo_root),
        "stage": stage,
        "code_fingerprint": source_fingerprint(workspace.repo_root),
    }
    if extra_config:
        config_payload["run_overrides"] = dict(extra_config)
    config_hash = sha256_json(config_payload)
    existing = connection.execute(
        "SELECT * FROM pipeline_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if existing is not None:
        if not resume:
            raise FileExistsError(f"pipeline run already exists: {run_id}")
        if existing["stage"] != stage or existing["config_hash"] != config_hash:
            raise RuntimeError("resume rejected: stage/config fingerprint changed")
        return existing

    commit, dirty = git_state(workspace.repo_root)
    connection.execute(
        """
        INSERT INTO pipeline_runs(
            run_id, dataset_snapshot_id, stage, pipeline_version,
            schema_version, git_commit, git_dirty, config_json, config_hash,
            dependency_snapshot_json, started_at, finished_at, status,
            error_summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            run_id,
            workspace.dataset_snapshot_id,
            stage,
            PIPELINE_VERSION,
            WORKSPACE_SCHEMA_VERSION,
            commit,
            dirty,
            canonical_json(config_payload),
            config_hash,
            canonical_json(
                dependency_snapshot(workspace.repo_root / "requirements.lock")
            ),
            utc_now(),
            "running",
            "{}",
        ),
    )
    connection.commit()
    return connection.execute(
        "SELECT * FROM pipeline_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()


def finish_pipeline_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    error_summary: Mapping[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        UPDATE pipeline_runs
        SET finished_at = ?, status = ?, error_summary_json = ?
        WHERE run_id = ?
        """,
        (utc_now(), status, canonical_json(error_summary or {}), run_id),
    )
    connection.commit()


def write_json_snapshot(
    directory: Path,
    name: str,
    value: Mapping[str, Any],
) -> Path:
    """Write a content-addressed JSON report without replacing prior evidence."""

    directory.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    path = directory / f"{name}.{fingerprint[:16]}.json"
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise FileExistsError(f"snapshot hash collision: {path}")
        return path
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    os.replace(temporary, path)
    return path


def export_tables_jsonl(
    connection: sqlite3.Connection,
    output_dir: Path,
    table_names: Sequence[str],
) -> dict[str, dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, dict[str, Any]] = {}
    for table in table_names:
        columns = [
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
        ]
        if not columns:
            raise ValueError(f"unknown table: {table}")
        order_columns = [
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
            if row["pk"]
        ]
        order_sql = (
            " ORDER BY " + ", ".join(f'"{name}"' for name in order_columns)
            if order_columns
            else ""
        )
        rows = connection.execute(f'SELECT * FROM "{table}"{order_sql}')
        base_path = output_dir / f"{table}.jsonl"
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{table}.",
            suffix=".jsonl.tmp",
            dir=output_dir,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        count = 0
        try:
            with temporary_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                for row in rows:
                    payload = {column: row[column] for column in columns}
                    line = canonical_json(payload) + "\n"
                    handle.write(line)
                    digest.update(line.encode("utf-8"))
                    count += 1
            digest_value = digest.hexdigest()
            if not base_path.exists():
                path = base_path
                os.replace(temporary_path, path)
            elif sha256_file(base_path) == digest_value:
                path = base_path
                temporary_path.unlink()
            else:
                path = output_dir / f"{table}.{digest_value[:16]}.jsonl"
                if path.exists():
                    if sha256_file(path) != digest_value:
                        raise FileExistsError(
                            f"JSONL snapshot hash collision: {path}"
                        )
                    temporary_path.unlink()
                else:
                    os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        summary[table] = {
            "row_count": count,
            "sha256": digest_value,
            "relative_path": path.name,
        }
    return summary


def atomic_link_or_copy(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return "reused"
    temporary = destination.with_name(destination.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        os.link(source, temporary)
        method = "hardlink"
    except OSError:
        shutil.copy2(source, temporary)
        method = "copy"
    os.replace(temporary, destination)
    return method
