"""Paced, stratified sampling for the Company Finder vitality index."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
from datetime import timedelta

import duckdb

from company_vitality import VitalityStore, normalize_domain, utc_now


CATALOG_PATH = os.getenv(
    "COMPANY_FINDER_DATABASE_PATH",
    "/opt/verigo-company-finder/data/company_catalog.duckdb",
)
VITALITY_PATH = os.getenv(
    "COMPANY_FINDER_VITALITY_DATABASE_PATH",
    "/opt/verigo-company-finder/data/company_vitality.sqlite",
)


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


HARD_LIMIT = env_int("COMPANY_FINDER_SAMPLE_HARD_LIMIT", 15_000, 1, 15_000)
DAILY_TARGET = env_int("COMPANY_FINDER_DAILY_SAMPLE_TARGET", 10_000, 1, HARD_LIMIT)
BURNIN_TARGET = env_int("COMPANY_FINDER_SAMPLE_BURNIN_TARGET", 5_000, 1, DAILY_TARGET)
INTERVAL_SECONDS = env_int("COMPANY_FINDER_SAMPLE_INTERVAL_SECONDS", 900, 300, 3600)
QUEUE_LIMIT = env_int("COMPANY_FINDER_SAMPLE_QUEUE_LIMIT", 500, 25, 2_000)
HEALTHY_DURATION_MS = env_int(
    "COMPANY_FINDER_SAMPLE_HEALTHY_DURATION_MS", 8_000, 1_000, 30_000,
)


def _seed(day: str) -> int:
    slot = int(utc_now().timestamp()) // INTERVAL_SECONDS
    digest = hashlib.sha256(f"{day}:{slot}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big")


def _stratified_order(
    rows: list[dict[str, object]], limit: int, rng: random.Random,
) -> list[dict[str, object]]:
    rng.shuffle(rows)
    coverage_limit = min(limit, math.ceil(limit * 0.20))
    coverage: list[dict[str, object]] = []
    remainder: list[dict[str, object]] = []
    countries: set[str] = set()
    industries: set[str] = set()
    sizes: set[str] = set()
    for row in rows:
        country = str(row.get("country") or "").strip().lower()
        industry = str(row.get("industry") or "").strip().lower()
        size = str(row.get("size") or "").strip().lower()
        adds_coverage = (
            (country and country not in countries)
            or (industry and industry not in industries)
            or (size and size not in sizes)
        )
        if len(coverage) < coverage_limit and adds_coverage:
            coverage.append(row)
            countries.add(country)
            industries.add(industry)
            sizes.add(size)
        else:
            remainder.append(row)
    return (coverage + remainder)[:limit]


def _raw_candidates(limit: int, seed: int) -> list[dict[str, object]]:
    if limit <= 0:
        return []
    rng = random.Random(seed)
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    windows = 6
    per_window = max(48, math.ceil(limit * 4 / windows))
    with duckdb.connect(CATALOG_PATH, read_only=True) as connection:
        connection.execute("SET memory_limit='256MB'")
        connection.execute("SET threads=1")
        total = int(connection.execute("SELECT count(*) FROM companies").fetchone()[0])
        for _ in range(windows):
            offset = rng.randrange(max(1, total - per_window))
            records = connection.execute("""
                SELECT id, name, website, country, industry, size
                FROM companies
                WHERE rowid >= ? AND website <> ''
                LIMIT ?
            """, [offset, per_window]).fetchall()
            for company_id, name, website, country, industry, size in records:
                identity = str(company_id or "").strip()
                if not identity or identity in seen or not normalize_domain(website):
                    continue
                seen.add(identity)
                rows.append({
                    "id": identity,
                    "name": str(name or ""),
                    "website": str(website or ""),
                    "country": str(country or ""),
                    "industry": str(industry or ""),
                    "size": str(size or ""),
                })
    return _stratified_order(rows, limit, rng)


def run() -> dict[str, object]:
    store = VitalityStore(VITALITY_PATH)
    now = utc_now()
    day = now.date().isoformat()
    started_at = store.sampler_started_at()
    elapsed = now - started_at
    stats = store.stats()
    sample_quality = store.report(1)["totals"]["sources"]["daily_sample"]
    average_duration = int(sample_quality["review_duration"]["average_ms"])
    healthy = int(stats["queued"]) < QUEUE_LIMIT and (
        average_duration == 0 or average_duration <= HEALTHY_DURATION_MS
    )
    target = DAILY_TARGET if elapsed >= timedelta(hours=24) and healthy else BURNIN_TARGET
    mode = "stable" if target == DAILY_TARGET and elapsed >= timedelta(hours=24) else "burnin"
    runs_per_day = max(1, math.ceil(86_400 / INTERVAL_SECONDS))
    batch = max(1, math.ceil(target / runs_per_day))
    scheduled = store.sample_day_scheduled(day)
    remaining = max(0, target - scheduled)
    batch = min(batch, remaining)
    if not batch or int(stats["queued"]) >= QUEUE_LIMIT:
        store.record_sampler_run(target, "paused", mode)
        return {
            "status": "paused", "target": target, "scheduled": scheduled,
            "queued": int(stats["queued"]), "average_duration_ms": average_duration,
        }

    uncertain_limit = math.floor(batch * 0.15)
    public_limit = math.floor(batch * 0.10)
    inactive_limit = math.floor(batch * 0.05)
    candidates: list[dict[str, object]] = []
    candidates.extend(store.sample_cohort(
        ("uncertain",), uncertain_limit, timedelta(days=3),
    ))
    candidates.extend(store.sample_cohort(
        ("active_verified", "recently_observed"), public_limit, timedelta(days=7),
    ))
    candidates.extend(store.sample_cohort(
        ("inactive",), inactive_limit, timedelta(days=14), include_legacy=True,
    ))
    raw_needed = batch - len(candidates)
    candidates.extend(_raw_candidates(raw_needed, _seed(day)))
    result = store.enqueue_samples(
        candidates,
        daily_limit=target,
        max_batch=batch,
        queue_limit=QUEUE_LIMIT,
    )
    store.record_sampler_run(target, str(result["reason"]), mode)
    return {
        "status": result["reason"], "target": target, "batch": batch,
        "inserted": result["inserted"], "scheduled": result["scheduled"],
        "queued": result["queued"], "sample_queued": result["sample_queued"],
        "average_duration_ms": average_duration,
    }


if __name__ == "__main__":
    print(json.dumps(run(), separators=(",", ":")), flush=True)
