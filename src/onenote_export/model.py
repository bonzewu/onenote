"""匯出用的資料模型（與檔案格式無關的純資料結構）。

Section 節
 └─ Page 頁面
     └─ PageElement：Outline（大綱容器）／Image／Attachment／Unsupported
          └─ OutlineItem（一個段落節點，可巢狀縮排、可帶清單符號）
               └─ Block：Paragraph／Image／Attachment／Table／Unsupported
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class TextRun:
    """一段格式一致的文字。"""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    superscript: bool = False
    subscript: bool = False
    hyperlink: str | None = None

    def same_format(self, other: TextRun) -> bool:
        return (
            self.bold == other.bold
            and self.italic == other.italic
            and self.underline == other.underline
            and self.strikethrough == other.strikethrough
            and self.superscript == other.superscript
            and self.subscript == other.subscript
            and self.hyperlink == other.hyperlink
        )


@dataclass(frozen=True, slots=True)
class Paragraph:
    runs: tuple[TextRun, ...]
    # OneNote 內建段落樣式：h1–h6、p、code、quote、cite…
    style: str | None = None

    @property
    def plain_text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass(frozen=True, slots=True)
class Image:
    data: bytes | None
    extension: str = ".png"
    filename: str | None = None
    alt_text: str | None = None


@dataclass(frozen=True, slots=True)
class Attachment:
    data: bytes | None
    filename: str = "attachment"


@dataclass(frozen=True, slots=True)
class Unsupported:
    """無法轉換的內容（例如手寫墨跡），保留類型名稱以便在輸出中提示。"""

    kind: str


@dataclass(frozen=True, slots=True)
class ListMarker:
    ordered: bool
    # OneNote 原始的清單格式字串（除錯用）
    format: str = ""


@dataclass(frozen=True, slots=True)
class NoteTag:
    label: str | None
    is_checkbox: bool
    completed: bool


@dataclass(frozen=True, slots=True)
class OutlineItem:
    content: tuple[Block, ...]
    children: tuple[OutlineItem, ...] = ()
    list_marker: ListMarker | None = None
    tags: tuple[NoteTag, ...] = ()


@dataclass(frozen=True, slots=True)
class TableCell:
    items: tuple[OutlineItem, ...]


@dataclass(frozen=True, slots=True)
class Table:
    rows: tuple[tuple[TableCell, ...], ...]


@dataclass(frozen=True, slots=True)
class Outline:
    items: tuple[OutlineItem, ...]


type Block = Paragraph | Image | Attachment | Table | Unsupported
type PageElement = Outline | Image | Attachment | Unsupported


@dataclass(frozen=True, slots=True)
class Page:
    title: str
    level: int = 1
    created: datetime | None = None
    modified: datetime | None = None
    author: str | None = None
    elements: tuple[PageElement, ...] = ()
    # 解析過程中遇到、但不致命的問題
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    pages: tuple[Page, ...] = field(default_factory=tuple)
