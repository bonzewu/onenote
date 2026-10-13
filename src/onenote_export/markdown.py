"""把 `model.Page` 轉成 Markdown（GitHub Flavored Markdown）。

設計為純函式：輸入頁面資料，輸出 Markdown 字串與需要寫入的附件清單，
不碰檔案系統，方便測試。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from .model import (
    Attachment,
    Block,
    Image,
    Outline,
    OutlineItem,
    Page,
    PageElement,
    Paragraph,
    Table,
    TableCell,
    TextRun,
    Unsupported,
)
from .naming import Asset, AssetAllocator

_URL = re.compile(r"(?:https?|ftp)://[^\s<>\"]+|www\.[^\s<>\"]+", re.IGNORECASE)
# `<` 只在可能被當成 HTML 標籤時才跳脫；`>` 只在行首有意義，交給 escape_line_start
_INLINE_SPECIAL = re.compile(r"([\\`*_\[\]~]|<(?=[A-Za-z/!?]))")
_LINE_START_SYMBOL = re.compile(r"^(\s*)([#>+=-])(?=\s|$)")
_LINE_START_NUMBER = re.compile(r"^(\s*\d+)([.)])(?=\s|$)")
_HEADING_STYLE = re.compile(r"^h([1-6])$")
_LEADING_WHITESPACE = re.compile(r"^[ \t]+")


@dataclass(frozen=True, slots=True)
class RenderedPage:
    markdown: str
    assets: tuple[Asset, ...]


# ---------------------------------------------------------------------------
# 行內文字
# ---------------------------------------------------------------------------


def escape_text(text: str) -> str:
    """跳脫 Markdown 特殊字元，但保留網址原樣，讓 GFM 自動轉成連結。"""
    pieces: list[str] = []
    position = 0
    for found in _URL.finditer(text):
        pieces.append(_INLINE_SPECIAL.sub(r"\\\1", text[position : found.start()]))
        pieces.append(found.group(0))
        position = found.end()
    pieces.append(_INLINE_SPECIAL.sub(r"\\\1", text[position:]))
    return "".join(pieces)


def escape_line_start(line: str) -> str:
    """避免一般段落開頭被誤判為標題、清單或引言。"""
    # 「# 」「- 」「> 」等符號直接在前面加反斜線；「1. 」則要跳脫標點，寫成「1\. 」
    line = _LINE_START_SYMBOL.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", line)
    return _LINE_START_NUMBER.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", line)


def preserve_indentation(line: str) -> str:
    """行首空白改成 &nbsp;，避免 4 個以上空白被 Markdown 當成程式碼區塊，同時保留縮排外觀。"""
    return _LEADING_WHITESPACE.sub(lambda m: "&nbsp;" * len(m.group(0).replace("\t", "    ")), line)


def _format_link_destination(url: str) -> str:
    if re.search(r"[\s()<>]", url):
        return f"<{url.replace('>', '%3E')}>"
    return url


def _wrap(text: str, opening: str, closing: str) -> str:
    """加上格式標記，並把前後空白移到標記外（`** 粗體**` 在 Markdown 中無效）。"""
    stripped = text.strip()
    if not stripped:
        return text
    leading = text[: len(text) - len(text.lstrip())]
    trailing = text[len(text.rstrip()) :]
    return f"{leading}{opening}{stripped}{closing}{trailing}"


def render_run(run: TextRun) -> str:
    text = escape_text(run.text)
    if run.bold:
        text = _wrap(text, "**", "**")
    if run.italic:
        text = _wrap(text, "*", "*")
    if run.strikethrough:
        text = _wrap(text, "~~", "~~")
    if run.underline and not run.hyperlink:
        text = _wrap(text, "<u>", "</u>")
    if run.superscript:
        text = _wrap(text, "<sup>", "</sup>")
    if run.subscript:
        text = _wrap(text, "<sub>", "</sub>")

    url = run.hyperlink
    if url and not url.startswith("#"):
        if not run.text.strip():
            # 沒有可見文字的連結（常見於網頁剪輯中圖片上的連結），改以網址本身呈現
            return f"{run.text}<{url}>" if re.match(r"[a-z][a-z0-9+.-]*:", url, re.IGNORECASE) else run.text
        if run.text.strip() == url and _URL.fullmatch(url):
            return text  # 顯示文字就是網址，GFM 會自動連結
        return _wrap(text, "[", f"]({_format_link_destination(url)})")
    return text


def render_inline(runs: Iterable[TextRun]) -> str:
    return "".join(render_run(run) for run in runs)


def render_paragraph_lines(paragraph: Paragraph) -> list[str]:
    """把段落轉成多行（OneNote 的軟換行在 Markdown 中以行尾反斜線表示強制換行）。"""
    text = render_inline(paragraph.runs)
    lines: list[str] = []
    for line in (raw.rstrip() for raw in text.split("\n")):
        # 去掉開頭的空行，並把段落中連續的空行合併成一行
        if line or (lines and lines[-1]):
            lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return []
    lines = [preserve_indentation(escape_line_start(line)) for line in lines]

    match paragraph.style:
        case "code":
            return [f"`{line}`" if line else "" for line in lines]
        case "quote":
            return [f"> {line}" for line in lines]
        case "cite":
            return [_wrap(line, "*", "*") for line in lines]
        case _:
            pass
    # 只有下一行還有文字時才需要強制換行；空行本身就會分段
    return [
        line + "\\" if line and index + 1 < len(lines) and lines[index + 1] else line
        for index, line in enumerate(lines)
    ]


def _heading_level(paragraph: Paragraph) -> int | None:
    found = _HEADING_STYLE.match(paragraph.style or "")
    # 頁面標題已使用 H1，所以 OneNote 的「標題 1」對應到 H2
    return min(int(found.group(1)) + 1, 6) if found else None


# ---------------------------------------------------------------------------
# 區塊
# ---------------------------------------------------------------------------


class _Renderer:
    def __init__(self, allocator: AssetAllocator) -> None:
        self.allocator = allocator

    # ---- 圖片、附件 ------------------------------------------------------

    def image(self, image: Image) -> str:
        name = image.filename or f"image{image.extension}"
        if image.data is None:
            return f"*［圖片未包含在備份檔中：{escape_text(name)}］*"
        path = self.allocator.add(image.data, image.filename, "image", image.extension)
        alt = escape_text(image.alt_text or image.filename or "圖片").replace("\n", " ").strip()
        return f"![{alt}]({_format_link_destination(path)})"

    def attachment(self, attachment: Attachment) -> str:
        if attachment.data is None:
            return f"*［附件未包含在備份檔中：{escape_text(attachment.filename)}］*"
        path = self.allocator.add(attachment.data, attachment.filename, "attachment", "")
        return f"附件：[{escape_text(attachment.filename)}]({_format_link_destination(path)})"

    @staticmethod
    def unsupported(item: Unsupported) -> str:
        return f"*［未匯出的內容：{escape_text(item.kind)}］*"

    # ---- 表格 ------------------------------------------------------------

    def _cell_text(self, cell: TableCell) -> str:
        parts: list[str] = []

        def visit(items: tuple[OutlineItem, ...], depth: int) -> None:
            for item in items:
                prefix = "　" * depth + ("• " if item.list_marker or depth else "")
                for block in item.content:
                    text = self._inline_block(block)
                    if text:
                        parts.append(prefix + text)
                        prefix = "　" * depth
                visit(item.children, depth + 1)

        visit(cell.items, 0)
        return "<br>".join(part.replace("|", "\\|") for part in parts)

    def _inline_block(self, block: Block) -> str:
        match block:
            case Paragraph():
                return render_inline(block.runs).strip().replace("\n", "<br>")
            case Image():
                return self.image(block)
            case Attachment():
                return self.attachment(block)
            case Table():
                # 巢狀表格無法放進 GFM 表格的儲存格，攤平成文字：列以換行分隔、欄以「／」分隔
                rows = (" ／ ".join(filter(None, (self._cell_text(cell) for cell in row))) for row in block.rows)
                return "<br>".join(filter(None, rows))
            case Unsupported():
                return self.unsupported(block)

    def table(self, table: Table) -> list[str]:
        rows = [[self._cell_text(cell) for cell in row] for row in table.rows if row]
        if not rows:
            return []
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return lines

    # ---- 大綱 ------------------------------------------------------------

    def block_lines(self, block: Block, top_level: bool) -> list[str]:
        match block:
            case Paragraph():
                lines = render_paragraph_lines(block)
                level = _heading_level(block)
                if lines and level is not None:
                    heading = " ".join(line.removesuffix("\\") for line in lines)
                    return [f"{'#' * level} {heading}"] if top_level else [_wrap(heading, "**", "**")]
                return lines
            case Image():
                return [self.image(block)]
            case Attachment():
                return [self.attachment(block)]
            case Table():
                return self.table(block)
            case Unsupported():
                return [self.unsupported(block)]

    def items(self, items: tuple[OutlineItem, ...], indent: str, nested: bool) -> list[tuple[str, list[str]]]:
        """回傳 [(類型, 行)]；類型為 "list" 的相鄰區塊之間不空行。"""
        chunks: list[tuple[str, list[str]]] = []
        number = 0
        for item in items:
            marker = self._marker(item, nested, number + 1)
            number = number + 1 if item.list_marker and item.list_marker.ordered else 0

            block_lines = [
                lines for block in item.content if (lines := self.block_lines(block, not marker and not indent))
            ]
            if marker:
                continuation = indent + " " * len(marker)
                lines: list[str] = []
                for index, block in enumerate(block_lines):
                    if index == 0:
                        if _is_table(block):
                            # GFM 表格不能和清單符號同一行，讓清單項目以空行開頭再接表格
                            lines.append(indent + marker.rstrip())
                            lines.extend(continuation + line for line in block)
                            continue
                        lines.append(indent + marker + block[0])
                        lines.extend(continuation + line if line else "" for line in block[1:])
                    else:
                        lines.append("")
                        lines.extend(continuation + line if line else "" for line in block)
                if not lines:
                    if not item.children:
                        continue
                    lines.append(indent + marker.rstrip())
                chunks.append(("list", lines))
                child_indent = continuation
            else:
                for block in block_lines:
                    chunks.append(("block", [indent + line if line else "" for line in block]))
                child_indent = indent

            if item.children:
                child_chunks = self.items(item.children, child_indent, nested=True)
                chunks.extend(("list" if kind == "list" or marker else kind, lines) for kind, lines in child_chunks)
        return chunks

    @staticmethod
    def _marker(item: OutlineItem, nested: bool, number: int) -> str:
        checkbox = next((tag for tag in item.tags if tag.is_checkbox), None)
        if checkbox is not None:
            return "- [x] " if checkbox.completed else "- [ ] "
        if item.list_marker is not None:
            return f"{number}. " if item.list_marker.ordered else "- "
        # OneNote 允許沒有清單符號的縮排段落；Markdown 無法縮排一般段落，改用清單保留層級
        return "- " if nested else ""

    def element(self, element: PageElement) -> list[tuple[str, list[str]]]:
        match element:
            case Outline():
                return self.items(element.items, "", nested=False)
            case Image():
                return [("block", [self.image(element)])]
            case Attachment():
                return [("block", [self.attachment(element)])]
            case Unsupported():
                return [("block", [self.unsupported(element)])]


def _is_table(lines: list[str]) -> bool:
    """判斷一組行是否為 `_Renderer.table` 產生的 GFM 表格（第二行為分隔列）。"""
    return len(lines) >= 2 and lines[0].startswith("|") and lines[1].startswith("| --- |")


def _join_chunks(chunks: list[tuple[str, list[str]]]) -> str:
    output: list[str] = []
    previous_kind: str | None = None
    for kind, lines in chunks:
        if not any(line.strip() for line in lines):
            continue
        if output:
            output.append("\n" if kind == "list" and previous_kind == "list" else "\n\n")
        output.append("\n".join(lines))
        previous_kind = kind
    return "".join(output)


# ---------------------------------------------------------------------------
# 頁面
# ---------------------------------------------------------------------------


def _format_datetime(value: datetime | None) -> str | None:
    return value.astimezone().isoformat(timespec="seconds") if value is not None else None


def front_matter(page: Page, section: str | None, notebook: str | None) -> str:
    fields: dict[str, str | int | None] = {
        "title": page.title,
        "notebook": notebook,
        "section": section,
        "created": _format_datetime(page.created),
        "modified": _format_datetime(page.modified),
        "author": page.author,
        "level": page.level if page.level > 1 else None,
    }
    # JSON 字串同時也是合法的 YAML 雙引號字串，可安全處理引號與特殊字元
    lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items() if value is not None]
    return "---\n" + "\n".join(lines) + "\n---\n"


def render_page(
    page: Page,
    asset_directory: str,
    section: str | None = None,
    notebook: str | None = None,
) -> RenderedPage:
    allocator = AssetAllocator(asset_directory)
    renderer = _Renderer(allocator)

    chunks: list[tuple[str, list[str]]] = [("block", [f"# {escape_text(page.title)}"])]
    if any("頁面內容不在備份檔中" in warning for warning in page.warnings):
        chunks.append(("block", ["> 這一頁的內容不在備份檔中，只保留了標題。"]))
    for element in page.elements:
        chunks.extend(renderer.element(element))

    body = _join_chunks(chunks)
    markdown = front_matter(page, section, notebook) + "\n" + body.rstrip() + "\n"
    return RenderedPage(markdown=markdown, assets=tuple(allocator.assets))
