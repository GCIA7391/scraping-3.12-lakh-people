"""Streaming ingestion of prospect workbooks.

Reads every file, every worksheet, every row — without ever holding a whole file
in memory, because the production input is 312,160 rows across four ~24 MB files
and the pipeline must stay flat in RAM.

Supports:
* ``.csv`` / ``.tsv`` — including the UTF-8 BOM that the real export carries
  (the first header is ``\\ufeffName``, which silently breaks naive readers).
* ``.xlsx`` / ``.xlsm`` / ``.xltx`` — all worksheets, via openpyxl's read-only
  streaming mode.

Column names are matched case- and punctuation-insensitively so that variations
like "Private Circle URL", "privatecircle_url" or "LinkedIn URL" all land on the
right field without per-file configuration.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

logger = logging.getLogger(__name__)

# Raise the field-size ceiling: some PrivateCircle URLs are very long base64 blobs.
csv.field_size_limit(10_000_000)

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}

# Canonical field -> accepted header spellings (normalised: lowercase, alnum only).
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "fullname", "personname", "directorname", "contactname"),
    "company": ("company", "companyname", "organisation", "organization", "entity", "entityname"),
    "industry": ("industry", "sector", "industryname"),
    "designation": ("designation", "title", "jobtitle", "role", "position"),
    "location": ("location", "city", "place", "region"),
    "revenue": ("revenue", "turnover"),
    "employees": ("employees", "headcount", "employeecount", "staff"),
    "website": ("website", "url", "companywebsite", "domain"),
    "pc_url": ("privatecircleurl", "privatecircle", "pcurl", "profileurl", "sourceurl"),
    "page_number": ("pagenumber", "page", "pageno"),
}

_NORMALISE_HEADER_RE = re.compile(r"[^a-z0-9]+")


def _normalise_header(header: str) -> str:
    return _NORMALISE_HEADER_RE.sub("", (header or "").strip().lower())


def build_column_map(headers: Sequence[str]) -> dict[str, str]:
    """Map canonical field names to the actual header strings present in a sheet.

    Unrecognised headers are simply not mapped; they are still preserved verbatim
    in the output because the writer copies the original row wholesale.
    """
    normalised = {_normalise_header(h): h for h in headers if h is not None}
    mapping: dict[str, str] = {}
    for canonical, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                mapping[canonical] = normalised[alias]
                break
    return mapping


@dataclass
class InputRow:
    """One prospect row, carrying both parsed fields and the untouched original.

    ``raw`` is preserved byte-for-byte so the output writer can re-emit the input
    columns exactly as they arrived — the requirement is to *append* columns and
    never modify existing ones.
    """

    row_uid: str
    source_file: str
    sheet_name: str
    row_index: int
    raw: dict[str, str] = field(default_factory=dict)
    headers: tuple[str, ...] = ()

    name: str = ""
    company: str = ""
    industry: str = ""
    designation: str = ""
    location: str = ""
    pc_url: str = ""

    @property
    def has_minimum_fields(self) -> bool:
        """A row is only actionable with both a person and a company."""
        return bool(self.name.strip()) and bool(self.company.strip())


def make_row_uid(source_file: str, sheet_name: str, row_index: int) -> str:
    """Stable identity for a row across re-runs.

    Deliberately derived from position rather than content: content-hashing would
    collapse the 3,660 legitimate duplicate (name, company) rows in the real file
    into single records and silently drop output lines. Duplicate *work* is
    avoided separately by the prefilter, which links duplicates to a canonical row
    while keeping every original row addressable.
    """
    digest = hashlib.sha1(
        f"{Path(source_file).name}\x00{sheet_name}\x00{row_index}".encode("utf-8")
    )
    return digest.hexdigest()[:20]


def _row_from_mapping(
    values: dict[str, str],
    headers: Sequence[str],
    column_map: dict[str, str],
    source_file: str,
    sheet_name: str,
    row_index: int,
) -> InputRow:
    def get(field_name: str) -> str:
        header = column_map.get(field_name)
        if not header:
            return ""
        return (values.get(header) or "").strip()

    return InputRow(
        row_uid=make_row_uid(source_file, sheet_name, row_index),
        source_file=source_file,
        sheet_name=sheet_name,
        row_index=row_index,
        raw=values,
        headers=tuple(headers),
        name=get("name"),
        company=get("company"),
        industry=get("industry"),
        designation=get("designation"),
        location=get("location"),
        pc_url=get("pc_url"),
    )


def read_csv(path: Path) -> Iterator[InputRow]:
    """Stream a CSV/TSV file. ``utf-8-sig`` transparently strips the BOM."""
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        headers = reader.fieldnames or []
        if not headers:
            logger.warning("no header row in %s; skipping", path)
            return
        column_map = build_column_map(headers)
        if "name" not in column_map or "company" not in column_map:
            logger.warning(
                "%s lacks a recognisable Name/Company column (headers=%s); skipping",
                path, headers,
            )
            return
        for index, values in enumerate(reader, start=1):
            # DictReader emits None keys for ragged rows; drop them so the writer
            # never has to reason about a null column.
            clean = {k: (v if v is not None else "") for k, v in values.items() if k is not None}
            yield _row_from_mapping(clean, headers, column_map, str(path), "", index)


def read_excel(path: Path) -> Iterator[InputRow]:
    """Stream every worksheet of an Excel workbook."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError("openpyxl is required to read Excel workbooks") from exc

    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            try:
                header_row = next(rows)
            except StopIteration:
                continue
            headers = [str(h).strip() if h is not None else "" for h in header_row]
            column_map = build_column_map(headers)
            if "name" not in column_map or "company" not in column_map:
                logger.warning(
                    "worksheet %s!%s lacks Name/Company columns; skipping",
                    path.name, sheet.title,
                )
                continue
            for index, values in enumerate(rows, start=1):
                if values is None or all(v is None for v in values):
                    continue
                record = {
                    headers[i]: ("" if v is None else str(v).strip())
                    for i, v in enumerate(values)
                    if i < len(headers)
                }
                yield _row_from_mapping(
                    record, headers, column_map, str(path), sheet.title, index
                )
    finally:
        workbook.close()


def read_file(path: str | Path) -> Iterator[InputRow]:
    """Dispatch to the right reader by file extension."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")
    suffix = path.suffix.lower()
    if suffix in CSV_SUFFIXES:
        yield from read_csv(path)
    elif suffix in EXCEL_SUFFIXES:
        yield from read_excel(path)
    else:
        raise ValueError(f"unsupported input format {suffix!r} for {path}")


def read_all(paths: Sequence[str | Path]) -> Iterator[InputRow]:
    """Stream every row of every supplied workbook, in order."""
    for path in paths:
        logger.info("reading %s", path)
        count = 0
        for row in read_file(path):
            count += 1
            yield row
        logger.info("read %s rows from %s", f"{count:,}", path)
