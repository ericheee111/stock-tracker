#!/usr/bin/env python3
"""Self-check for the Stage 2A auxiliary synthetic fixture corpus.

Stdlib-only. Verifies that every fixture under this directory is:
  - well-formed JSON;
  - explicitly marked synthetic-only (no real strategy claim);
  - free of any verified/complete/T2/T3 truth claim;
  - referencing only synthetic A-share symbols (no real security implied).

Run from the repo root:
    python tests_quant/fixtures/stage2_aux/selfcheck.py

Exit code is non-zero if any fixture violates the synthetic-fixture contract.
This script does NOT import the project and never touches the production database.
"""

from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Synthetic prefixes only. Real A-share prefixes (600/601/603/604/605/688,
# 000/001/002/003/300/301) are rejected to guarantee no real security is implied.
SYNTHETIC_CODE_RE = re.compile(r"^(607|009)\d{3}\.(SH|SZ)$")
REAL_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ)$")
SYMBOL_SCAN_RE = re.compile(r"\b(\d{6}\.(?:SH|SZ))\b")

TRUST_CLAIM_RE = re.compile(r"T[23]|RESEARCH_GRADE|OPERATIONAL_VERIFIED", re.IGNORECASE)


def fail(path: str, msg: str) -> str:
    return f"FAIL  {path}: {msg}"


def walk_fixtures() -> list[str]:
    found: list[str] = []
    for root, _dirs, files in os.walk(HERE):
        for name in sorted(files):
            if name.endswith(".json"):
                found.append(os.path.join(root, name))
    return found


def check_doc(path: str, doc: dict, errors: list[str]) -> None:
    if doc.get("synthetic_fixture_only") is not True:
        errors.append(fail(path, "missing synthetic_fixture_only=true"))
    if doc.get("no_real_strategy_claim") is not True:
        errors.append(fail(path, "missing no_real_strategy_claim=true"))

    # Trust-tier must never be claimed as T2/T3.
    tier = doc.get("trust_tier")
    if isinstance(tier, str) and TRUST_CLAIM_RE.search(tier):
        errors.append(fail(path, f"trust_tier claims a real tier: {tier!r}"))

    # Top-level verified/complete must be false (these are source facts, not claims).
    for key in ("verified", "complete"):
        if doc.get(key) is True:
            errors.append(fail(path, f"top-level {key}=true is a forbidden claim"))

    # Any nested verified/complete=true inside expected_facts is also forbidden.
    for fact in doc.get("expected_facts", []) or []:
        if not isinstance(fact, dict):
            continue
        for key in ("verified", "complete"):
            if fact.get(key) is True:
                errors.append(
                    fail(path, f"expected_facts has {key}=true (forbidden claim)")
                )

    # Symbols must be synthetic only.
    text = json.dumps(doc)
    for sym in SYMBOL_SCAN_RE.findall(text):
        if REAL_CODE_RE.match(sym) and not SYNTHETIC_CODE_RE.match(sym):
            errors.append(fail(path, f"non-synthetic symbol implies real data: {sym}"))

    # Boundary fixtures (everything but the manifest) must declare a boundary.
    base = os.path.basename(path)
    if base != "manifest.json":
        if not doc.get("boundary"):
            errors.append(fail(path, "boundary fixture missing 'boundary'"))


def check_manifest(path: str, doc: dict, errors: list[str]) -> None:
    matrix = doc.get("coverage_matrix") or []
    if len(matrix) != 21:
        errors.append(fail(path, f"coverage_matrix expects 21 entries, got {len(matrix)}"))
    seen_paths: set[str] = set()
    for row in matrix:
        fp = row.get("fixture")
        if not fp:
            errors.append(fail(path, "coverage_matrix row missing 'fixture'"))
            continue
        seen_paths.add(fp)
        # Fixture paths are relative to this stage2_aux directory.
        candidate = os.path.join(HERE, fp)
        if not os.path.exists(candidate):
            errors.append(fail(path, f"manifest references missing fixture: {fp}"))
    if len(seen_paths) != len(matrix):
        errors.append(fail(path, "coverage_matrix has duplicate fixture paths"))


def main() -> int:
    errors: list[str] = []
    fixtures = walk_fixtures()
    manifest_path = os.path.join(HERE, "manifest.json")
    count = 0
    for path in fixtures:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(fail(path, f"JSON parse error: {exc}"))
            continue
        if not isinstance(doc, dict):
            errors.append(fail(path, "top-level JSON is not an object"))
            continue
        count += 1
        if os.path.abspath(path) == os.path.abspath(manifest_path):
            check_manifest(path, doc, errors)
        else:
            check_doc(path, doc, errors)

    print(f"Scanned {count} fixture JSON files under {HERE}")
    if errors:
        print("\n".join(errors))
        print(f"\n{len(errors)} problem(s) found. Fixtures are NOT compliant.")
        return 1
    print("All fixtures compliant: SYNTHETIC_FIXTURE_ONLY, no T2/T3 claims, synthetic symbols only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
