"""共用的 pytest fixture。"""

from __future__ import annotations

from pathlib import Path

import pytest

from onenote_export.document import build_section
from onenote_export.model import Section
from onenote_export.onestore import parse_onestore

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def load_fixture_section(name: str) -> Section:
    path = FIXTURES / name
    return build_section(parse_onestore(path.read_bytes()), path.stem)


@pytest.fixture
def section_loader():
    return load_fixture_section
