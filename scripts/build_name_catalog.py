"""Build a small, queryable name dictionary without materializing name pairs.

The source directory can contain very large files.  This tool always streams files
and writes a machine-readable inspection report before unfamiliar formats are
accepted into the catalogue.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pypinyin import Style, lazy_pinyin


MAX_SAMPLE_BYTES = 64 * 1024
MAX_SAMPLE_ROWS = 200
VALID_NAME = re.compile(r"^[a-z]{2,40}$")
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16", "cp932")

# Sort longest-first so compound surnames are never split as one-character names.
CHINESE_SURNAMES = tuple(sorted((
    "欧阳", "司马", "上官", "诸葛", "司徒", "夏侯", "东方", "皇甫", "尉迟", "公孙", "慕容", "令狐",
    "宇文", "长孙", "司空", "东郭", "南宫", "独孤", "西门", "百里", "闻人", "呼延", "羊舌", "微生",
    "万俟", "澹台", "公冶", "宗政", "濮阳", "淳于", "单于", "太叔", "申屠", "公羊", "赫连", "轩辕",
    "王", "李", "张", "刘", "陈", "杨", "黄", "赵", "吴", "周", "徐", "孙", "马", "朱", "胡", "郭",
    "何", "高", "林", "罗", "郑", "梁", "谢", "宋", "唐", "许", "韩", "冯", "邓", "曹", "彭", "曾",
    "肖", "田", "董", "袁", "潘", "于", "蒋", "蔡", "余", "杜", "叶", "程", "苏", "魏", "吕", "丁",
    "任", "卢", "姚", "沈", "钟", "姜", "崔", "谭", "陆", "范", "汪", "廖", "石", "金", "韦", "贾",
    "夏", "付", "方", "邹", "熊", "白", "孟", "秦", "邱", "江", "尹", "薛", "闫", "段", "雷", "侯",
    "龙", "史", "陶", "黎", "贺", "顾", "毛", "郝", "龚", "邵", "万", "钱", "严", "赖", "覃", "洪",
    "武", "莫", "孔", "向", "汤", "常", "温", "康", "施", "文", "牛", "樊", "葛", "邢", "安", "齐",
    "易", "乔", "伍", "庞", "颜", "倪", "庄", "聂", "章", "鲁", "岳", "翟", "殷", "詹", "申", "欧",
    "耿", "关", "兰", "焦", "俞", "左", "柳", "甘", "祝", "包", "宁", "尚", "符", "舒", "阮", "柯",
    "纪", "梅", "童", "凌", "毕", "单", "季", "裴", "霍", "涂", "成", "苗", "谷", "盛", "曲", "翁",
    "冉", "骆", "蓝", "路", "游", "辛", "欧", "管", "柴", "蒙", "鲍", "华", "喻", "祁", "蒲", "房",
    "屈", "时", "傅", "皮", "卞", "冀", "平", "谈", "宗", "牟", "戴", "原", "乐", "夏", "井", "邬",
    "花", "缪", "戚", "项", "祝", "卓", "戈", "阳", "郁", "车", "杭", "滕", "皮", "全", "班", "仇",
), key=len, reverse=True))
CHINESE_SURNAME_SET = frozenset(CHINESE_SURNAMES)


def is_han_name(value: str) -> bool:
    return bool(value) and all("\u4e00" <= char <= "\u9fff" for char in value)


def chinese_pinyin(value: str) -> str | None:
    if not is_han_name(value):
        return None
    return romanize("".join(lazy_pinyin(value, style=Style.NORMAL, errors="")))


def romanize(value: str) -> str | None:
    """Accept only an existing Latin spelling suitable for an email local part."""
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    ascii_name = "".join(char for char in normalized if not unicodedata.combining(char))
    compact = re.sub(r"[^a-z]", "", ascii_name)
    return compact if VALID_NAME.fullmatch(compact) else None


def detect_encoding(path: Path) -> str | None:
    # Never load a country-wide source file just to inspect its encoding.
    with path.open("rb") as handle:
        raw = handle.read(MAX_SAMPLE_BYTES)
    for encoding in ENCODINGS:
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return None


def inspect_delimited_file(path: Path) -> dict[str, object]:
    encoding = detect_encoding(path)
    record: dict[str, object] = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "encoding": encoding,
    }
    if encoding is None:
        record["status"] = "unreadable_encoding"
        return record

    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        sample = handle.read(MAX_SAMPLE_BYTES)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = None
    record["delimiter"] = delimiter
    if delimiter is None:
        record["status"] = "not_a_delimited_table"
        return record

    rows: list[list[str]] = []
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for _, row in zip(range(MAX_SAMPLE_ROWS), reader):
            rows.append(row)
    record["sample_rows"] = len(rows)
    record["first_row"] = rows[0] if rows else []
    record["column_count"] = len(rows[0]) if rows else 0
    record["status"] = "inspected"
    return record


def iter_source_files(source: Path, countries: set[str]) -> Iterable[tuple[str, Path]]:
    american = source / "American"
    if "US" in countries and american.is_dir():
        yield "american_ssa", american

    for path in sorted(source.glob("*.txt")):
        yield "corpus", path

    world = source / "World Name"
    description = world / "description.txt"
    if description.is_file():
        yield "world_description", description
    data = world / "data"
    for country in sorted(countries):
        path = data / f"{country}.csv"
        if path.is_file():
            yield "world_csv", path


def inspect_sources(source: Path, countries: set[str]) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for source_type, path in iter_source_files(source, countries):
        if source_type == "american_ssa":
            files = sorted(path.glob("yob*.txt"))
            entries.append({
                "source_type": source_type,
                "path": str(path),
                "files": len(files),
                "first_file": inspect_delimited_file(files[0]) if files else None,
                "last_file": inspect_delimited_file(files[-1]) if files else None,
                "status": "known_schema_name_sex_count",
            })
        else:
            record = inspect_delimited_file(path)
            record["source_type"] = source_type
            entries.append(record)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "countries": sorted(countries),
        "sources": entries,
        "note": (
            "Only the American SSA name,sex,count source is imported automatically. "
            "Other files are inspected first so that non-Roman names are never guessed "
            "or assigned to a first-name/surname column incorrectly."
        ),
    }


def open_catalogue(path: Path, replace: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not replace:
        raise FileExistsError(
            f"Catalogue already exists: {path}. Use --replace to rebuild this derived file."
        )
    if path.exists() and replace:
        path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS name_entries (
            country TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('given', 'surname')),
            romanized TEXT NOT NULL,
            gender TEXT,
            weight INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(country, kind, romanized)
        );
        CREATE INDEX IF NOT EXISTS idx_name_entries_lookup
            ON name_entries(country, kind, weight DESC, romanized);
        CREATE TABLE IF NOT EXISTS catalogue_sources (
            source TEXT PRIMARY KEY,
            imported_at TEXT NOT NULL,
            rows_seen INTEGER NOT NULL,
            rows_accepted INTEGER NOT NULL,
            rows_rejected INTEGER NOT NULL
        );
        """
    )
    return connection


UPSERT_NAME = """
    INSERT INTO name_entries(country, kind, romanized, gender, weight)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(country, kind, romanized) DO UPDATE SET
        weight = name_entries.weight + excluded.weight,
        gender = CASE
            WHEN name_entries.gender = excluded.gender THEN name_entries.gender
            WHEN name_entries.gender IS NULL THEN excluded.gender
            ELSE 'U'
        END
"""


def flush_names(connection: sqlite3.Connection, batch: list[tuple[str, str, str, str, int]]) -> None:
    if batch:
        connection.executemany(UPSERT_NAME, batch)
        batch.clear()


def import_american_ssa(connection: sqlite3.Connection, directory: Path) -> dict[str, int]:
    totals = Counter()
    batch: list[tuple[str, str, str, str, int]] = []
    for path in sorted(directory.glob("yob*.txt")):
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.reader(handle):
                totals["seen"] += 1
                if len(row) != 3:
                    totals["rejected"] += 1
                    continue
                name, sex, count = row
                normalized = romanize(name)
                if normalized is None or sex not in {"M", "F"}:
                    totals["rejected"] += 1
                    continue
                try:
                    weight = int(count)
                except ValueError:
                    totals["rejected"] += 1
                    continue
                batch.append(("US", "given", normalized, sex, weight))
                totals["accepted"] += 1
                if len(batch) >= 5_000:
                    flush_names(connection, batch)
    flush_names(connection, batch)
    return dict(totals)


def import_world_names(
    connection: sqlite3.Connection,
    directory: Path,
    countries: set[str],
    max_rows_per_file: int,
) -> dict[str, dict[str, int]]:
    """Import only documented four-column World Name files, one line at a time."""
    results: dict[str, dict[str, int]] = {}
    for country in sorted(countries):
        if country == "CN":
            # CN.csv contains people merely attributed to China. It includes
            # foreign and corrupted values, so it is not a Chinese name source.
            results[country] = {"skipped_untrusted_country_source": 1}
            continue
        path = directory / f"{country}.csv"
        if not path.is_file():
            continue
        encoding = detect_encoding(path)
        totals = Counter()
        if encoding is None:
            # Some country files contain a small number of damaged rows. The
            # CSV contract is still known; replacement characters cause only
            # those non-Roman rows to be rejected by romanize().
            encoding = "utf-8"
            totals["lossy_utf8_fallback"] += 1
        batch: list[tuple[str, str, str, str, int]] = []
        with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
            for row in csv.reader(handle):
                if max_rows_per_file and totals["seen"] >= max_rows_per_file:
                    totals["stopped_at_row_limit"] += 1
                    break
                totals["seen"] += 1
                if len(row) != 4:
                    totals["rejected"] += 1
                    continue
                given, surname, gender, row_country = row
                normalized_given = romanize(given)
                normalized_surname = romanize(surname)
                if row_country.upper() != country or not normalized_given or not normalized_surname:
                    totals["rejected"] += 1
                    continue
                normalized_gender = gender.upper() if gender.upper() in {"M", "F"} else "U"
                batch.extend((
                    (country, "given", normalized_given, normalized_gender, 1),
                    (country, "surname", normalized_surname, "U", 1),
                ))
                totals["accepted_rows"] += 1
                if len(batch) >= 5_000:
                    flush_names(connection, batch)
        flush_names(connection, batch)
        results[country] = dict(totals)
    return results


def import_chinese_name_corpus(connection: sqlite3.Connection, source: Path) -> dict[str, int]:
    """Build a clean pinyin CN dictionary from actual Chinese full names only."""
    totals = Counter()
    batch: list[tuple[str, str, str, str, int]] = []
    with source.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            totals["seen"] += 1
            full_name = line.strip()
            if not (2 <= len(full_name) <= 4 and is_han_name(full_name)):
                totals["rejected"] += 1
                continue
            surname = next((item for item in CHINESE_SURNAMES if full_name.startswith(item)), None)
            if surname is None:
                totals["rejected"] += 1
                continue
            given = full_name[len(surname):]
            if not (1 <= len(given) <= 2 and is_han_name(given)):
                totals["rejected"] += 1
                continue
            romanized_surname = chinese_pinyin(surname)
            romanized_given = chinese_pinyin(given)
            if not romanized_surname or not romanized_given:
                totals["rejected"] += 1
                continue
            batch.extend((
                ("CN", "given", romanized_given, "U", 1),
                ("CN", "surname", romanized_surname, "U", 1),
            ))
            totals["accepted"] += 1
            if len(batch) >= 5_000:
                flush_names(connection, batch)
    flush_names(connection, batch)
    return dict(totals)


def write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("Name"))
    parser.add_argument("--output", type=Path, default=Path("data/name_catalog.db"))
    parser.add_argument("--report", type=Path, default=Path("data/name-catalog-report.json"))
    parser.add_argument("--countries", default="US,DE,CN,JP")
    parser.add_argument(
        "--include-world",
        action="store_true",
        help="Import documented World Name CSV files for the selected countries.",
    )
    parser.add_argument(
        "--max-world-rows",
        type=int,
        default=50_000,
        help="Per-country safety limit for World Name files; use 0 only for an explicit full import.",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing derived catalogue rather than appending duplicate weights.",
    )
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    countries = {country.strip().upper() for country in args.countries.split(",") if country.strip()}
    if not args.source.is_dir():
        parser.error(f"source directory does not exist: {args.source}")

    report = inspect_sources(args.source, countries)
    if args.report_only:
        write_report(args.report, report)
        print(f"Inspection report written to {args.report}")
        return 0

    try:
        connection = open_catalogue(args.output, args.replace)
    except FileExistsError as exc:
        parser.error(str(exc))
    try:
        imports: dict[str, object] = {}
        american = args.source / "American"
        if "US" in countries and american.is_dir():
            stats = import_american_ssa(connection, american)
            connection.execute(
                """
                INSERT INTO catalogue_sources(source, imported_at, rows_seen, rows_accepted, rows_rejected)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    imported_at = excluded.imported_at,
                    rows_seen = excluded.rows_seen,
                    rows_accepted = excluded.rows_accepted,
                    rows_rejected = excluded.rows_rejected
                """,
                ("american_ssa", datetime.now(timezone.utc).isoformat(), stats.get("seen", 0),
                 stats.get("accepted", 0), stats.get("rejected", 0)),
            )
            imports["american_ssa"] = stats
        if args.include_world:
            imports["world_name"] = import_world_names(
                connection,
                args.source / "World Name" / "data",
                countries,
                args.max_world_rows,
            )
        if "CN" in countries:
            chinese_sources = sorted(args.source.glob("Chinese_Names_Corpus*.txt"))
            if chinese_sources:
                imports["chinese_name_corpus"] = import_chinese_name_corpus(
                    connection, chinese_sources[0]
                )
        connection.commit()
        report["imports"] = imports
        report["catalogue"] = {
            "path": str(args.output),
            "entries": connection.execute("SELECT COUNT(*) FROM name_entries").fetchone()[0],
        }
    finally:
        connection.close()
    write_report(args.report, report)
    print(f"Catalogue written to {args.output}; report written to {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
