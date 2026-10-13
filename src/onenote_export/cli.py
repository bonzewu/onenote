"""命令列介面。

    onenote-export [輸入路徑 ...] [-o 輸出資料夾] [選項]

未指定輸入路徑時，使用 Mac 版 OneNote 的預設備份資料夾。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .binary import FormatError
from .exporter import (
    ExportOptions,
    ExportReport,
    OutputFormat,
    default_backup_dir,
    discover,
    export,
    load_section,
    select_sources,
)

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_USAGE = 2

log = logging.getLogger("onenote_export")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onenote-export",
        description="把 OneNote（Mac 版）備份的 .one 節檔案匯出成 Markdown 或 JSON。",
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="一或多個 .one 檔案或資料夾（預設：自動尋找 Mac 版 OneNote 的備份資料夾）",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("onenote-export"), help="輸出資料夾（預設：./onenote-export）"
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=[item.value for item in OutputFormat],
        default=OutputFormat.MARKDOWN.value,
        help="輸出格式（預設：markdown）",
    )
    parser.add_argument("--all-versions", action="store_true", help="匯出同一節的所有備份版本（預設只匯出最新一份）")
    parser.add_argument(
        "--include-recycle-bin", action="store_true", help="一併匯出 OneNote_RecycleBin 中已刪除的頁面與節"
    )
    parser.add_argument("--list", action="store_true", help="只列出找到的節與頁面標題，不寫入任何檔案")
    parser.add_argument("--force", action="store_true", help="輸出資料夾已有內容時仍然寫入（同名檔案會被覆寫）")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="顯示除錯訊息")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="只顯示錯誤")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _configure_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)


def _print_listing(sources: Sequence, out=sys.stdout) -> int:
    failures = 0
    for source in sources:
        try:
            section = load_section(source)
        except (FormatError, OSError) as error:
            print(f"✗ {source.display_name}：{error}", file=out)
            failures += 1
            continue
        date_text = f"（{source.backup_date.isoformat()}）" if source.backup_date else ""
        print(f"{source.display_name}{date_text}：{len(section.pages)} 頁", file=out)
        for page in section.pages:
            print(f"  {'  ' * (page.level - 1)}- {page.title}", file=out)
    return EXIT_PARTIAL_FAILURE if failures else EXIT_OK


def _print_summary(report: ExportReport, output: Path, out=sys.stdout) -> None:
    print(f"\n匯出完成：{output.resolve()}", file=out)
    print(f"  節：{len(report.results) - len(report.failures)}／{len(report.results)}", file=out)
    print(f"  頁面：{report.total('pages')}", file=out)
    print(f"  圖片：匯出 {report.total('images_exported')}，備份檔未包含 {report.total('images_missing')}", file=out)
    print(
        f"  附件：匯出 {report.total('attachments_exported')}，備份檔未包含 {report.total('attachments_missing')}",
        file=out,
    )
    if report.total("pages_without_content"):
        print(f"  只有標題（內容不在備份檔中）的頁面：{report.total('pages_without_content')}", file=out)
    for failure in report.failures:
        print(f"  ✗ {failure.source.path}：{failure.error}", file=out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose, args.quiet)

    inputs = args.inputs or [default_backup_dir()]
    options = ExportOptions(
        output=args.output.expanduser(),
        format=OutputFormat(args.format),
        all_versions=args.all_versions,
        include_recycle_bin=args.include_recycle_bin,
    )

    sources = select_sources(discover(inputs), options)
    if not sources:
        log.error("找不到任何 .one 檔案：%s", "、".join(str(path) for path in inputs))
        return EXIT_USAGE

    if args.list:
        return _print_listing(sources)

    if options.output.exists() and any(options.output.iterdir()) and not args.force:
        log.error("輸出資料夾 %s 已有內容；請換一個資料夾，或加上 --force 覆寫", options.output)
        return EXIT_USAGE

    report = export(sources, options)
    if not args.quiet:
        _print_summary(report, options.output)
    return EXIT_PARTIAL_FAILURE if report.failures else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
