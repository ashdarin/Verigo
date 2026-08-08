"""Input loading and normalization for the legacy verifier entry point."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from app.core.smtp_verifier import check_email_characters


class VerificationInput:
    """Keep file parsing and pre-verification cleanup independent of execution."""

    _ENCODINGS = ("utf-8", "gbk")

    @classmethod
    def load_emails_from_file(cls, filepath: str | Path) -> list[str]:
        path = Path(filepath)
        try:
            suffix = path.suffix.lower()
            if suffix == ".csv":
                return cls._load_csv(path)
            if suffix == ".txt":
                return cls._load_text(path)
            if suffix == ".json":
                return cls._load_json(path)
            print(f"❌ 不支持的文件格式: {path.suffix}")
            return []
        except Exception as exc:
            print(f"❌ 加载文件失败: {exc}")
            return []

    @classmethod
    def _read_with_fallback(cls, path: Path, reader):
        for encoding in cls._ENCODINGS:
            try:
                with path.open("r", encoding=encoding) as handle:
                    return reader(handle)
            except UnicodeDecodeError:
                continue
        raise UnicodeDecodeError("fallback", b"", 0, 1, "unable to decode input")

    @classmethod
    def _load_csv(cls, path: Path) -> list[str]:
        def read(handle):
            emails: list[str] = []
            for row in csv.reader(handle):
                for cell in row:
                    if cell and "@" in cell and "." in cell:
                        emails.append(cell.strip())
                        break
            return emails

        return cls._read_with_fallback(path, read)

    @classmethod
    def _load_text(cls, path: Path) -> list[str]:
        def read(handle):
            return [
                line
                for raw_line in handle
                if (line := raw_line.strip()) and "@" in line and "." in line
            ]

        return cls._read_with_fallback(path, read)

    @classmethod
    def _load_json(cls, path: Path) -> list[str]:
        def read(handle):
            data = json.load(handle)
            if isinstance(data, list):
                return [str(item).strip() for item in data if "@" in str(item)]
            if isinstance(data, dict) and "emails" in data:
                return [
                    str(item).strip()
                    for item in data["emails"]
                    if "@" in str(item)
                ]
            return []

        return cls._read_with_fallback(path, read)

    @staticmethod
    def clean_email_list(emails: Iterable[object]) -> list[str]:
        """Remove malformed and case-insensitive duplicate addresses."""
        values = list(emails)
        cleaned: list[str] = []
        seen: set[str] = set()
        removed_bad: list[tuple[str, str]] = []
        removed_dup: list[str] = []

        for raw in values:
            email = str(raw).strip()
            if not email:
                continue
            is_clean, issue = check_email_characters(email)
            if not is_clean:
                removed_bad.append((email, issue))
                continue
            key = email.lower()
            if key in seen:
                removed_dup.append(email)
                continue
            seen.add(key)
            cleaned.append(email)

        if removed_dup or removed_bad:
            print("🧹 验证前清洗:")
            print(f"   原始: {len(values)} 个 → 保留: {len(cleaned)} 个")
            if removed_dup:
                print(f"   🗑️ 删除重复: {len(removed_dup)} 个")
                for email in removed_dup[:10]:
                    print(f"      - {email}")
                if len(removed_dup) > 10:
                    print(f"      ... 其余 {len(removed_dup) - 10} 个略")
            if removed_bad:
                print(f"   ⚠️ 删除含空格/非法字符: {len(removed_bad)} 个")
                for email, issue in removed_bad[:10]:
                    print(f"      - {repr(email)} ({issue})")
                if len(removed_bad) > 10:
                    print(f"      ... 其余 {len(removed_bad) - 10} 个略")

        return cleaned
