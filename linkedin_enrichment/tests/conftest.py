"""Shared fixtures. Everything here runs offline."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from linkedin_enrichment.config.settings import Settings
from linkedin_enrichment.database.store import Store
from linkedin_enrichment.providers.cassette import CassetteProvider

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SERP_DIR = FIXTURE_DIR / "serp"

# Columns exactly as they appear in the real PrivateCircle export.
INPUT_HEADERS = [
    "Name", "Company", "Industry", "Designation", "Location",
    "Revenue", "Employees", "Website", "PrivateCircle URL", "Page Number",
]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Offline settings pointed at a temp database and the cassette provider."""
    config = Settings.load()
    config.db_path = str(tmp_path / "test.db")
    config.output_dir = str(tmp_path / "out")
    config.log_dir = str(tmp_path / "logs")
    config.provider.name = "cassette"
    config.provider.cassette_dir = str(SERP_DIR)
    config.workers = 2
    config.batch_size = 10
    config.dashboard = False
    config.rate_limit.requests_per_second = 1000.0  # no artificial delay in tests
    return config


@pytest.fixture
def store(settings: Settings):
    with Store(settings.db_path) as db:
        yield db


@pytest.fixture
def cassette(settings: Settings) -> CassetteProvider:
    return CassetteProvider(settings.provider)


def write_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    """Write a CSV with a UTF-8 BOM, matching the real export's encoding."""
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INPUT_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: row.get(header, "") for header in INPUT_HEADERS})
    return path


def make_row(name: str, company: str, **extra: str) -> dict[str, str]:
    """One input row with the real file's shape.

    Revenue, Employees and Website default to empty because they are 100% empty
    in the production data — tests must never rely on signals that do not exist.
    """
    row = {
        "Name": name,
        "Company": company,
        "Industry": extra.get("industry", "Technology"),
        "Designation": extra.get("designation", "Director"),
        "Location": extra.get("location", "Bangalore"),
        "Revenue": "",
        "Employees": "",
        "Website": "",
        "PrivateCircle URL": extra.get("pc_url", f"https://privatecircle.co/p/{abs(hash(name+company))}"),
        "Page Number": extra.get("page", "1"),
    }
    return row


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    """A small file exercising the real data's awkward shapes."""
    rows = [
        make_row("Girish Rowjee", "Greytip Software Private Limited", designation="CEO"),
        make_row("Debasheesh Bagchi", "Ubiqtech Software Private Limited"),
        make_row("Melukote Shivaramu Lokesh", "Synthesis Winding Technologies Private Limited",
                 designation="Wholetime Director"),
        make_row("Habiba Zackria", "Zainab Fatima Fast Foods Private Limited"),
        make_row("Dharmesh Premchand Madhwani", "Natesh Impex Private Limited"),
        # Tier 0 rejects: an organisation in the Name column, and a one-word name.
        make_row("Electronic Manufacturers", "Essae -Teraoka Private Limited"),
        make_row("Naganna", "Hitesh Agri Solutions Private Limited"),
        # Exact duplicate of row 1 — must appear in the output, cost no queries.
        make_row("Girish Rowjee", "Greytip Software Private Limited", designation="CEO"),
    ]
    return write_csv(tmp_path / "sample.csv", rows)
