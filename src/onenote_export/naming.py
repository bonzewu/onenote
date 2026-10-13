"""檔名清理、備份檔名解析與附件路徑配置。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import PurePosixPath

# Windows / macOS 都不允許或容易出問題的字元
_INVALID_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|\ufffc\ufddf\ufeff]')
_INVISIBLE = re.compile(r"[\ufffc\ufddf\ufeff\u200b\u200c\u200d\u2060]")
_WHITESPACE = re.compile(r"\s+")
_RESERVED_WINDOWS = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
# OneNote 備份檔名：「節名.one (2026-9-27).one」
_BACKUP_NAME = re.compile(r"^(?P<name>.+?)\.one \((?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\)\.one$")


def strip_invisible(text: str) -> str:
    """移除物件取代字元、零寬字元等不可見字元（OneNote 有時會把它們放進節名或標題）。"""
    return _INVISIBLE.sub("", text)


def safe_filename(name: str, max_length: int = 80, fallback: str = "untitled") -> str:
    """把任意字串轉成可安全用於檔名的形式（保留中文等 Unicode 字元）。"""
    normalized = unicodedata.normalize("NFC", name)
    cleaned = _INVALID_CHARS.sub(" ", normalized)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" .")
    if cleaned.upper() in _RESERVED_WINDOWS:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or fallback


def split_extension(filename: str) -> tuple[str, str]:
    path = PurePosixPath(filename)
    suffix = path.suffix if 0 < len(path.suffix) <= 8 else ""
    stem = filename[: len(filename) - len(suffix)] if suffix else filename
    return stem, suffix


@dataclass(frozen=True, slots=True)
class BackupName:
    """從備份檔名解析出的節名稱與備份日期。"""

    section: str
    backup_date: date | None


def parse_backup_name(filename: str) -> BackupName:
    """解析「節名.one (YYYY-M-D).one」；不符合格式時把整個檔名（去掉 .one）當節名。"""
    filename = strip_invisible(filename)
    matched = _BACKUP_NAME.match(filename)
    if matched:
        try:
            backup_date = date(int(matched["y"]), int(matched["m"]), int(matched["d"]))
        except ValueError:
            backup_date = None
        return BackupName(matched["name"], backup_date)
    stem = filename.removesuffix(".one")
    return BackupName(stem, None)


class UniqueNamer:
    """在同一個資料夾內產生不重複的檔名（不分大小寫比對，以相容 macOS 預設檔案系統）。"""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def claim(self, stem: str, suffix: str = "") -> str:
        candidate = f"{stem}{suffix}"
        counter = 2
        while candidate.casefold() in self._used:
            candidate = f"{stem} ({counter}){suffix}"
            counter += 1
        self._used.add(candidate.casefold())
        return candidate


@dataclass(frozen=True, slots=True)
class Asset:
    """要寫到磁碟的附件檔：相對於頁面檔案的路徑 + 內容。"""

    relative_path: str
    data: bytes


@dataclass(slots=True)
class AssetAllocator:
    """替一個頁面的圖片與附件配置不重複的相對路徑。"""

    directory: str
    assets: list[Asset] = field(default_factory=list)
    _namer: UniqueNamer = field(default_factory=UniqueNamer)

    def add(self, data: bytes, suggested_name: str | None, default_stem: str, default_suffix: str) -> str:
        stem, suffix = split_extension(suggested_name or "")
        stem = safe_filename(stem, max_length=60, fallback=default_stem)
        suffix = suffix.lower() if suffix else default_suffix
        name = self._namer.claim(stem, suffix)
        relative_path = f"{self.directory}/{name}"
        self.assets.append(Asset(relative_path, data))
        return relative_path
