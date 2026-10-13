"""針對本機真實 OneNote 備份的煙霧測試（沒有備份資料夾時自動略過）。

只檢查「能完整匯出、沒有失敗」，不在測試中印出或比對任何筆記內容。
"""

from __future__ import annotations

import pytest

from onenote_export.exporter import ExportOptions, default_backup_dir, discover, export, select_sources

BACKUP_DIR = default_backup_dir()

pytestmark = [
    pytest.mark.local_backups,
    pytest.mark.skipif(not BACKUP_DIR.is_dir(), reason="本機沒有 OneNote 備份資料夾"),
]


def test_every_local_backup_exports_without_failures(tmp_path):
    options = ExportOptions(output=tmp_path / "out", all_versions=True, include_recycle_bin=True)
    sources = select_sources(discover([BACKUP_DIR]), options)
    assert sources, "備份資料夾中沒有 .one 檔"

    report = export(sources, options)

    assert report.failures == []
    assert report.total("pages") > 0
    for result in report.results:
        assert result.output_dir is not None
        assert (result.output_dir / "index.md").is_file()
        markdown_files = [path for path in result.output_dir.glob("*.md") if path.name != "index.md"]
        assert len(markdown_files) == result.pages
