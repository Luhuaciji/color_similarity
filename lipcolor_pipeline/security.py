"""Redacting secret scanner for the worktree, Git index, and reachable history."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class Finding:
    scope: str
    location: str
    line: int
    rule: str


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "dashscope_api_key",
        re.compile(r"\bsk-(?:ws-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "alibaba_access_key_id",
        re.compile(r"\bLTAI[A-Za-z0-9]{12,}\b"),
    ),
    (
        "generic_secret_assignment",
        re.compile(
            r"""(?ix)
            \b(?:api[_-]?key|access[_-]?key|secret|token|authorization)\b
            \s*[:=]\s*
            ["'](?P<value>[A-Za-z0-9_./+=:-]{20,})["']
            """
        ),
    ),
)

_PLACEHOLDER_MARKERS = (
    "example",
    "placeholder",
    "replace_me",
    "replace-me",
    "your_",
    "your-",
    "dummy",
    "redacted",
    "<",
    ">",
    "${",
)


def _is_placeholder(value: str) -> bool:
    lowered = value.casefold()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def scan_bytes(data: bytes, *, scope: str, location: str) -> list[Finding]:
    """Scan text bytes and return redacted locations only."""

    if b"\x00" in data[:8192]:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return []

    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule_name, pattern in _RULES:
            matched_rule = False
            for match in pattern.finditer(line):
                candidate = match.groupdict().get("value") or match.group(0)
                if not _is_placeholder(candidate):
                    findings.append(
                        Finding(
                            scope=scope,
                            location=location,
                            line=line_number,
                            rule=rule_name,
                        )
                    )
                    matched_rule = True
                    break
            if matched_rule:
                # Prefer the first (most specific) rule and avoid duplicate
                # reports for one credential assignment.
                break
    return findings


def build_self_test_secret() -> str:
    """Build a synthetic detector fixture without storing it contiguously."""

    return "".join(("sk", "-", "A7b9C2d4E6f8G1h3J5k7M9n2P4r6"))


def self_test() -> bool:
    fake_line = f'credential = "{build_self_test_secret()}"\n'.encode()
    findings = scan_bytes(fake_line, scope="self_test", location="synthetic_fixture")
    return len(findings) == 1 and findings[0].rule == "dashscope_api_key"


def _git(repo: Path, args: Sequence[str], *, text: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        errors="surrogateescape" if text else None,
    )
    return completed.stdout


def _scan_worktree(repo: Path) -> list[Finding]:
    raw = _git(repo, ["ls-files", "-co", "--exclude-standard", "-z"])
    assert isinstance(raw, bytes)
    findings: list[Finding] = []
    for encoded_path in raw.split(b"\0"):
        if not encoded_path:
            continue
        relative_path = encoded_path.decode("utf-8", errors="surrogateescape")
        path = repo / relative_path
        if path.is_file() and not path.is_symlink():
            findings.extend(
                scan_bytes(
                    path.read_bytes(),
                    scope="worktree",
                    location=Path(relative_path).as_posix(),
                )
            )
    return findings


def _scan_index(repo: Path) -> list[Finding]:
    raw = _git(repo, ["ls-files", "-s", "-z"])
    assert isinstance(raw, bytes)
    findings: list[Finding] = []
    scanned_blobs: set[str] = set()
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, encoded_path = entry.split(b"\t", 1)
        _mode, encoded_oid, stage = metadata.split()
        if stage != b"0":
            continue
        oid = encoded_oid.decode("ascii")
        if oid in scanned_blobs:
            continue
        scanned_blobs.add(oid)
        data = _git(repo, ["cat-file", "blob", oid])
        assert isinstance(data, bytes)
        path = encoded_path.decode("utf-8", errors="surrogateescape")
        findings.extend(
            scan_bytes(data, scope="index", location=Path(path).as_posix())
        )
    return findings


def _reachable_blob_paths(repo: Path) -> Iterable[tuple[str, str]]:
    output = _git(repo, ["rev-list", "--objects", "--all"], text=True)
    assert isinstance(output, str)
    seen: set[str] = set()
    for line in output.splitlines():
        oid, separator, path = line.partition(" ")
        if not separator or not path or oid in seen:
            continue
        seen.add(oid)
        yield oid, path


def _scan_history(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for oid, path in _reachable_blob_paths(repo):
        object_type = _git(repo, ["cat-file", "-t", oid], text=True)
        assert isinstance(object_type, str)
        if object_type.strip() != "blob":
            continue
        data = _git(repo, ["cat-file", "blob", oid])
        assert isinstance(data, bytes)
        findings.extend(
            scan_bytes(
                data,
                scope="reachable_history",
                location=f"{Path(path).as_posix()}@blob:{oid[:12]}",
            )
        )
    return findings


def _remote_refs(repo: Path) -> list[str]:
    output = _git(
        repo,
        ["for-each-ref", "--format=%(refname)", "refs/remotes"],
        text=True,
    )
    assert isinstance(output, str)
    return sorted(line for line in output.splitlines() if line)


def scan_repository(repo: Path, scopes: Sequence[str]) -> dict[str, object]:
    repo = repo.resolve()
    selected = list(dict.fromkeys(scopes))
    invalid = sorted(set(selected) - {"worktree", "index", "history"})
    if invalid:
        raise ValueError(f"unknown scan scopes: {', '.join(invalid)}")

    scanners = {
        "worktree": _scan_worktree,
        "index": _scan_index,
        "history": _scan_history,
    }
    findings: list[Finding] = []
    counts: dict[str, int] = {}
    for scope in selected:
        scope_findings = scanners[scope](repo)
        findings.extend(scope_findings)
        counts[scope] = len(scope_findings)

    fixture_ok = self_test()
    return {
        "schema_version": "security-scan-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repository": ".",
        "scopes": selected,
        "scope_finding_counts": counts,
        "reachable_remote_refs": _remote_refs(repo),
        "self_test": {
            "status": "passed" if fixture_ok else "failed",
            "finding_count": 1 if fixture_ok else 0,
        },
        "status": "passed" if fixture_ok and not findings else "failed",
        "findings": [asdict(item) for item in findings],
        "redaction": "secret values and secret fingerprints are intentionally omitted",
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--scope",
        action="append",
        choices=("worktree", "index", "history"),
        help="repeat to select scopes; defaults to all",
    )
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--self-test-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test_only:
        passed = self_test()
        print(json.dumps({"self_test": "passed" if passed else "failed"}))
        return 0 if passed else 1

    report = scan_repository(
        args.repo,
        args.scope or ("worktree", "index", "history"),
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(serialized, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "scope_finding_counts": report["scope_finding_counts"],
                "self_test": report["self_test"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
