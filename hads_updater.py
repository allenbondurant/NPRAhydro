#!/usr/bin/env python3
"""
HADS Hydromet Station Data Downloader
======================================
Downloads daily data from NOAA HADS for specified NWSLI station IDs
and appends new records to per-station CSV files.

Data source: https://hads.ncep.noaa.gov/nexhads2/servlet/DecodedData
(The old interestingData.pl endpoint no longer exists.)

Stations: IKPA2, NUIA2, UBLA2, JDYA2

Usage:
    python hads_updater.py                      # Download last 2 days
    python hads_updater.py --days 3             # Download last N days
    python hads_updater.py --backfill 7         # Backfill last 7 days
    python hads_updater.py --output-dir C:\\data # Custom output folder
    python hads_updater.py --dry-run            # Parse only, no writing

Schedule (cron or Windows Task Scheduler):
    Daily at 06:00 — python C:\\path\\to\\hads_updater.py
"""

import argparse
import csv
import logging
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATION_IDS = ["IKPA2", "NUIA2", "UBLA2", "JDYA2"]

HADS_URL = "https://hads.ncep.noaa.gov/nexhads2/servlet/DecodedData"

DEFAULT_OUTPUT_DIR = Path(__file__).parent / "hydromet_data"

REQUEST_DELAY = 2.0  # seconds between station fetches

# Output CSV columns — wide format: one row per timestamp, PE codes as columns
# The PE columns are dynamic per station; fixed columns come first.
FIXED_COLS = ["station", "datetime_utc"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hads_updater")


# ---------------------------------------------------------------------------
# HTML table parser
# ---------------------------------------------------------------------------

class TableParser(HTMLParser):
    """Minimal HTML parser that extracts all <table> rows as lists of strings."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []   # tables → rows → cells
        self._in_table = False
        self._current_table: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell = ""
        self._in_cell = False
        self._depth = 0   # nested table depth

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._in_table = True
                self._current_table = []
        elif tag in ("tr",) and self._depth == 1:
            self._current_row = []
        elif tag in ("td", "th") and self._depth == 1:
            self._in_cell = True
            self._current_cell = ""

    def handle_endtag(self, tag):
        if tag == "table":
            if self._depth == 1:
                self.tables.append(self._current_table)
                self._in_table = False
            self._depth -= 1
        elif tag == "tr" and self._depth == 1:
            if self._current_row:
                self._current_table.append(self._current_row)
        elif tag in ("td", "th") and self._depth == 1:
            self._in_cell = False
            self._current_row.append(self._current_cell.strip())
            self._current_cell = ""

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell += data


def parse_hads_html(station: str, html: str) -> tuple[list[str], list[dict]]:
    """
    Parse the HADS DecodedData HTML response.

    Returns:
        pe_codes  — list of PE code strings found in the header (e.g. ["HG","TW","TA"])
        records   — list of dicts with keys: station, datetime_utc, <pe_code>, ...
    """
    parser = TableParser()
    parser.feed(html)

    pe_codes: list[str] = []
    records: list[dict] = []

    for table in parser.tables:
        if len(table) < 2:
            continue

        header = table[0]
        # First cell is "Observation Time"; the rest are PE codes like "HG (IKPA2)"
        if not header or "time" not in header[0].lower():
            continue

        # Extract PE code from header cells like "HG (IKPA2) Graph" → "HG"
        # or "HG(IKPA2)Graph" → "HG"
        cols = []
        for cell in header[1:]:
            pe = cell.strip()
            pe = pe.split("(")[0].strip()   # drop everything from "(" onward
            pe = pe.replace("Graph", "").strip()
            if pe:
                cols.append(pe)

        if not cols:
            continue

        pe_codes = cols

        for row in table[1:]:
            if not row or len(row) < 2:
                continue

            ts_raw = row[0].strip()
            if not ts_raw or ts_raw.lower() in ("", "n/a"):
                continue

            rec: dict = {
                "station":      station.upper(),
                "datetime_utc": ts_raw,
            }

            for i, pe in enumerate(cols, start=1):
                if i < len(row):
                    val = row[i].strip()
                    rec[pe] = val if val not in ("", "--", "N/A", "n/a") else ""
                else:
                    rec[pe] = ""

            records.append(rec)

        break   # only need the first matching table

    return pe_codes, records


# ---------------------------------------------------------------------------
# HADS fetch
# ---------------------------------------------------------------------------

def fetch_hads(station: str, days_back: int,
               session: requests.Session,
               timeout: int = 30,
               retries: int = 2) -> str:
    """Download the decoded data HTML page from HADS for one station.
    Retries on timeout up to `retries` times before giving up."""
    params = {
        "nwslis":   station,
        "sinceday": -abs(days_back),  # HADS uses negative values: -2 = last 2 days
        "hsa":      "nil",
        "state":    "nil",
        "of":       "0",              # 0 = HTML table output
    }
    for attempt in range(1, retries + 2):
        try:
            log.info("%s: connecting to HADS (attempt %d, timeout=%ds)…",
                     station, attempt, timeout)
            resp = session.get(HADS_URL, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.Timeout:
            if attempt <= retries:
                log.warning("%s: request timed out — retrying in 5s…", station)
                time.sleep(5)
            else:
                log.error("%s: timed out after %d attempts — skipping.", station, attempt)
        except requests.RequestException as exc:
            log.error("%s: request failed — %s", station, exc)
            break
    return ""


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_existing_keys(csv_path: Path) -> set[str]:
    """Return a set of 'station|datetime_utc' keys already in the CSV."""
    keys: set[str] = set()
    if not csv_path.exists():
        return keys
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            keys.add(f"{row['station']}|{row['datetime_utc']}")
    return keys


def append_records(csv_path: Path, pe_codes: list[str],
                   records: list[dict], existing_keys: set[str]) -> int:
    """
    Append only new rows to the per-station CSV.
    Columns: station, datetime_utc, then one column per PE code.
    If the file already exists with fewer PE columns, new columns are added.
    Returns count of new rows written.
    """
    if not records:
        return 0

    columns = FIXED_COLS + pe_codes
    new_rows = []

    for rec in records:
        key = f"{rec['station']}|{rec['datetime_utc']}"
        if key not in existing_keys:
            new_rows.append(rec)
            existing_keys.add(key)

    if not new_rows:
        return 0

    write_header = not csv_path.exists()

    # If file exists, check whether we need to add columns
    if not write_header:
        with csv_path.open(newline="", encoding="utf-8") as f:
            existing_cols = csv.DictReader(f).fieldnames or []
        # Merge: keep existing order, append any new PE codes
        for pe in pe_codes:
            if pe not in existing_cols:
                existing_cols.append(pe)
        columns = existing_cols

    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for row in new_rows:
            # Fill any missing PE columns with empty string
            for col in columns:
                row.setdefault(col, "")
            writer.writerow(row)

    return len(new_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download HADS hydromet data and save per-station CSVs."
    )
    p.add_argument(
        "--days", type=int, default=2,
        help="Number of days back to request (default: 2)."
    )
    p.add_argument(
        "--backfill", type=int, default=None,
        help="Override --days for a longer initial backfill (max ~7 on HADS)."
    )
    p.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Folder for per-station CSVs (default: {DEFAULT_OUTPUT_DIR})."
    )
    p.add_argument(
        "--stations", nargs="+", default=STATION_IDS,
        help="NWSLI IDs to download (default: IKPA2 NUIA2 UBLA2 JDYA2)."
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Download and parse but do not write anything to disk."
    )
    return p.parse_args()


def main() -> None:
    args = build_args()
    days = args.backfill if args.backfill else args.days
    output_dir: Path = args.output_dir

    log.info("=" * 60)
    log.info("HADS Hydromet Updater  —  %s UTC",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    log.info("Source    : %s", HADS_URL)
    log.info("Stations  : %s", ", ".join(args.stations))
    log.info("Days back : %d", days)
    log.info("Output dir: %s", output_dir.resolve())
    log.info("=" * 60)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    total_new = 0
    session = requests.Session()
    session.headers.update({
        "User-Agent": "hads_updater/3.0 (hydromet data collection)"
    })

    for station in args.stations:
        csv_path = output_dir / f"{station.upper()}.csv"
        log.info("--- %s ---", station)

        html = fetch_hads(station, days, session)
        if not html.strip():
            log.warning("%s: empty response — skipping.", station)
            time.sleep(REQUEST_DELAY)
            continue

        pe_codes, records = parse_hads_html(station, html)

        if not records:
            log.warning("%s: no data rows found in response. "
                        "Station may be inactive or page format changed.", station)
            time.sleep(REQUEST_DELAY)
            continue

        log.info("%s: found %d timestamps, PE codes: %s",
                 station, len(records), ", ".join(pe_codes))

        if args.dry_run:
            for rec in records[:3]:
                log.info("  [preview] %s", rec)
            if len(records) > 3:
                log.info("  [preview] ... and %d more rows", len(records) - 3)
        else:
            existing_keys = load_existing_keys(csv_path)
            n = append_records(csv_path, pe_codes, records, existing_keys)
            log.info("%s: wrote %d new rows → %s", station, n, csv_path.name)
            total_new += n

        time.sleep(REQUEST_DELAY)

    if not args.dry_run:
        log.info("Done. Total new rows appended: %d", total_new)
    else:
        log.info("Dry-run complete. No files written.")


if __name__ == "__main__":
    main()
