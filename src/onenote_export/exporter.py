"""匯出流程：找出備份檔 → 挑選版本 → 解析 → 寫出 Markdown／JSON 與附件。"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .binary import FormatError
from .document import build_section
from .markdown import escape_text, render_page
from .model import Attachment, Image, Page, Section
from .naming import AssetAllocator, UniqueNamer, parse_backup_name, safe_filename
from .onestore import parse_onestore

log = logging.getLogger(__name__)

RECYCLE_BIN_DIR = "OneNote_RecycleBin"
ONENOTE_MAC_DATA_DIR = (
    Path.home()
    / "Library/Containers/com.microsoft.onenote.mac/Data/Library/Application Support"
    / "Microsoft User Data/OneNote/15.0"
)
# 備份資料夾名稱會跟著 OneNote 的介面語言改變
BACKUP_DIR_NAMES = ("備份", "Backup", "备份", "バックアップ", "백업")


def default_backup_dir(base: Path = ONENOTE_MAC_DATA_DIR) -> Path:
    """找出 Mac 版 OneNote 的備份資料夾；找不到時回傳最常見的路徑（供錯誤訊息使用）。"""
    for name in BACKUP_DIR_NAMES:
        if (base / name).is_dir():
            return base / name
    if base.is_dir():
        # 其他語言：取第一個含有 .one 檔、且不是快取或索引的子資料夾
        for child in sorted(base.iterdir()):
            if child.is_dir() and child.name not in ("cache", "FullTextSearchIndex") and any(child.rglob("*.one")):
                return child
    return base / BACKUP_DIR_NAMES[0]


class OutputFormat(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class SourceFile:
    """一個要匯出的 `.one` 備份檔。"""

    path: Path
    notebook: str
    section: str
    backup_date: date | None
    in_recycle_bin: bool

    @property
    def display_name(self) -> str:
        return f"{self.notebook}/{self.section}" if self.notebook else self.section


@dataclass(frozen=True, slots=True)
class ExportOptions:
    output: Path
    format: OutputFormat = OutputFormat.MARKDOWN
    all_versions: bool = False
    include_recycle_bin: bool = False


@dataclass(slots=True)
class SectionResult:
    source: SourceFile
    output_dir: Path | None = None
    pages: int = 0
    images_exported: int = 0
    images_missing: int = 0
    attachments_exported: int = 0
    attachments_missing: int = 0
    pages_without_content: int = 0
    error: str | None = None


@dataclass(slots=True)
class ExportReport:
    results: list[SectionResult] = field(default_factory=list)

    @property
    def failures(self) -> list[SectionResult]:
        return [result for result in self.results if result.error]

    def total(self, attribute: str) -> int:
        return sum(getattr(result, attribute) for result in self.results)


# ---------------------------------------------------------------------------
# 找出備份檔
# ---------------------------------------------------------------------------


def _source_from_path(path: Path, root: Path | None) -> SourceFile:
    parsed = parse_backup_name(path.name)
    parent_parts = path.parent.relative_to(root).parts if root is not None else (path.parent.name,)
    in_recycle_bin = RECYCLE_BIN_DIR in parent_parts
    notebook = "/".join(parent_parts)
    return SourceFile(path, notebook, parsed.section, parsed.backup_date, in_recycle_bin)


def discover(inputs: Iterable[Path]) -> list[SourceFile]:
    """從檔案或資料夾（遞迴）找出所有 `.one` 檔。"""
    sources: list[SourceFile] = []
    for given in inputs:
        path = given.expanduser()
        if path.is_dir():
            sources.extend(
                _source_from_path(found, path)
                for found in sorted(path.rglob("*.one"))
                if found.is_file() and not found.name.startswith("._")
            )
        elif path.is_file():
            sources.append(_source_from_path(path, None))
        else:
            log.warning("找不到輸入路徑：%s", path)
    return sources


def select_sources(sources: Sequence[SourceFile], options: ExportOptions) -> list[SourceFile]:
    """依選項過濾回收筒，並在預設情況下只保留每個節最新的備份。"""
    candidates = [s for s in sources if options.include_recycle_bin or not s.in_recycle_bin]
    if options.all_versions:
        return candidates

    latest: dict[tuple[str, str], SourceFile] = {}
    for source in candidates:
        key = (source.notebook, source.section)
        current = latest.get(key)
        if current is None or (source.backup_date or date.min) > (current.backup_date or date.min):
            latest[key] = source
    return sorted(latest.values(), key=lambda s: (s.notebook, s.section))


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def load_section(source: SourceFile) -> Section:
    data = source.path.read_bytes()
    return build_section(parse_onestore(data), source.section)


# ---------------------------------------------------------------------------
# JSON 輸出
# ---------------------------------------------------------------------------


def _to_jsonable(value: Any, on_binary: Callable[[Image | Attachment], str | None]) -> Any:
    if isinstance(value, (Image, Attachment)):
        result = {"type": type(value).__name__.lower(), "path": on_binary(value)}
        result.update({f.name: getattr(value, f.name) for f in fields(value) if f.name != "data"})
        return result
    if is_dataclass(value) and not isinstance(value, type):
        result = {"type": type(value).__name__.lower()}
        result.update({f.name: _to_jsonable(getattr(value, f.name), on_binary) for f in fields(value)})
        return result
    if isinstance(value, (tuple, list)):
        return [_to_jsonable(item, on_binary) for item in value]
    if isinstance(value, datetime):
        return value.astimezone().isoformat(timespec="seconds")
    return value


def page_to_json(page: Page, allocator: AssetAllocator) -> dict[str, Any]:
    def save(item: Image | Attachment) -> str | None:
        if item.data is None:
            return None
        if isinstance(item, Image):
            return allocator.add(item.data, item.filename, "image", item.extension)
        return allocator.add(item.data, item.filename, "attachment", "")

    return _to_jsonable(page, save)


# ---------------------------------------------------------------------------
# 寫檔
# ---------------------------------------------------------------------------


def _count_media(page: Page, result: SectionResult) -> None:
    """統計頁面中的圖片與附件（含表格內）。"""

    def visit(value: Any) -> None:
        match value:
            case Image():
                if value.data is None:
                    result.images_missing += 1
                else:
                    result.images_exported += 1
            case Attachment():
                if value.data is None:
                    result.attachments_missing += 1
                else:
                    result.attachments_exported += 1
            case tuple() | list():
                for item in value:
                    visit(item)
            case _ if is_dataclass(value):
                for f in fields(value):
                    if f.name != "data":
                        visit(getattr(value, f.name))
            case _:
                pass

    visit(page.elements)


def _section_dir_name(source: SourceFile, options: ExportOptions) -> str:
    name = safe_filename(source.section)
    if options.all_versions and source.backup_date is not None:
        name = f"{name} ({source.backup_date.isoformat()})"
    return name


def write_section(section: Section, source: SourceFile, options: ExportOptions) -> SectionResult:
    notebook_dir = options.output.joinpath(*(safe_filename(part) for part in source.notebook.split("/") if part))
    section_dir = notebook_dir / _section_dir_name(source, options)
    section_dir.mkdir(parents=True, exist_ok=True)
    result = SectionResult(source=source, output_dir=section_dir, pages=len(section.pages))

    namer = UniqueNamer()
    # 編號位數依頁數決定，確保檔案總管依名稱排序時與 OneNote 中的順序一致
    width = max(3, len(str(len(section.pages))))
    index_entries: list[tuple[Page, str]] = []
    json_pages: list[dict[str, Any]] = []
    for number, page in enumerate(section.pages, start=1):
        stem = namer.claim(f"{number:0{width}d} {safe_filename(page.title, max_length=60)}")
        asset_dir = f"assets/{stem}"
        _count_media(page, result)
        if any("頁面內容不在備份檔中" in warning for warning in page.warnings):
            result.pages_without_content += 1

        if options.format is OutputFormat.JSON:
            allocator = AssetAllocator(asset_dir)
            json_pages.append(page_to_json(page, allocator))
            assets = allocator.assets
        else:
            rendered = render_page(page, asset_dir, section=section.name, notebook=source.notebook or None)
            (section_dir / f"{stem}.md").write_text(rendered.markdown, encoding="utf-8")
            assets = list(rendered.assets)
            index_entries.append((page, f"{stem}.md"))

        for asset in assets:
            target = section_dir / asset.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(asset.data)

    if options.format is OutputFormat.JSON:
        document = {
            "notebook": source.notebook,
            "section": section.name,
            "source": source.path.name,
            "backup_date": source.backup_date.isoformat() if source.backup_date else None,
            "pages": json_pages,
        }
        (section_dir / "section.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        (section_dir / "index.md").write_text(_section_index(section, source, index_entries), encoding="utf-8")
    return result


def _link(path: str) -> str:
    return f"<{path}>" if any(ch in path for ch in " ()<>") else path


def _section_index(section: Section, source: SourceFile, entries: list[tuple[Page, str]]) -> str:
    lines = [f"# {escape_text(section.name)}", ""]
    meta = f"來源：`{source.path.name}`"
    if source.backup_date:
        meta += f"（備份日期 {source.backup_date.isoformat()}）"
    lines += [meta, "", f"共 {len(entries)} 頁。", ""]
    for page, filename in entries:
        indent = "  " * (max(page.level, 1) - 1)
        lines.append(f"{indent}- [{escape_text(page.title)}]({_link(filename)})")
    return "\n".join(lines) + "\n"


def write_root_index(report: ExportReport, options: ExportOptions) -> None:
    lines = ["# OneNote 匯出", "", f"匯出時間：{datetime.now().astimezone().isoformat(timespec='seconds')}"]
    current_notebook: str | None = None
    for result in sorted(report.results, key=lambda r: (r.source.notebook, r.source.section)):
        if result.output_dir is None:
            continue
        if result.source.notebook != current_notebook:
            current_notebook = result.source.notebook
            lines += ["", f"## {escape_text(current_notebook or '（未分類）')}", ""]
        relative = result.output_dir.relative_to(options.output).as_posix()
        target = f"{relative}/index.md" if options.format is OutputFormat.MARKDOWN else f"{relative}/section.json"
        lines.append(f"- [{escape_text(result.source.section)}]({_link(target)})：{result.pages} 頁")
    (options.output / "index.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def export(sources: Sequence[SourceFile], options: ExportOptions) -> ExportReport:
    """匯出所有選取的備份檔；單一節失敗不會中斷其他節。"""
    report = ExportReport()
    options.output.mkdir(parents=True, exist_ok=True)
    for position, source in enumerate(sources, start=1):
        log.info("[%d/%d] %s", position, len(sources), source.display_name)
        try:
            section = load_section(source)
            result = write_section(section, source, options)
        except (FormatError, OSError) as error:
            log.error("無法匯出 %s：%s", source.path, error)
            result = SectionResult(source=source, error=str(error))
        except Exception as error:
            log.exception("匯出 %s 時發生未預期的錯誤", source.path)
            result = SectionResult(source=source, error=f"{type(error).__name__}: {error}")
        report.results.append(result)
    write_root_index(report, options)
    return report
