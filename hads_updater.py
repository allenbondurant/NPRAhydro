#!/usr/bin/env python3
"""
Hydromet Station Data Downloader  (IEM backend)
================================================
Downloads hydromet data from the Iowa Environmental Mesonet (IEM) HADS/DCP
archive for specified NWSLI station IDs and appends new records to per-station
CSV files.

Why IEM instead of hads.ncep.noaa.gov:
  The NOAA HADS decoded-data display only ever returns ~6 hours of data per
  request, regardless of the time window asked for. IEM ingests the exact same
  GOES/DCP transmissions but keeps a multi-year archive and supports arbitrary
  date-range queries, so a single run can capture a wide window with no gaps.

API endpoint:
  https://mesonet.agron.iastate.edu/cgi-bin/request/hads.py
  Returns clean wide-format CSV: station, utc_valid, then one column per
  full SHEF physical-element code (e.g. HGIRGZZ, HGIR2ZZ, TAIRGZZ, ...).

Stations: IKPA2, NUIA2, UBLA2, JDYA2

Usage:
    python hads_updater.py                          # last 7 days (default)
    python hads_updater.py --days 30                # last 30 days
    python hads_updater.py --start 2024-01-01       # from a date until now
    python hads_updater.py --start 2024-01-01 --end 2024-06-01
    python hads_updater.py --output-dir C:\\data     # custom output folder
    python hads_updater.py --dry-run                # fetch + parse, no writing

Notes:
  - The deduplication makes overlapping runs safe: re-pulling the same window
    never creates duplicate rows. Because of this, timing no longer matters and
    a once-daily run with a 7-day lookback will never leave a gap.
  - For an initial historical backfill, use --start with an early date. IEM
    allows multi-year ranges for single-station requests (this script fetches
    one station at a time).
"""

import argparse
import csv
import io
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATION_IDS = ["IKPA2", "NUIA2", "UBLA2", "JDYA2"]

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/hads.py"

DEFAULT_OUTPUT_DIR = Path(__file__).parent / "hydromet_data"

REQUEST_DELAY = 2.0    # seconds between station requests (be polite)
TIMEOUT      = 180     # IEM can be slow for large ranges
RETRIES      = 3       # retry attempts on timeout/connection error

# Fixed leading columns; everything after these is a SHEF data column.
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
# IEM fetch
# ---------------------------------------------------------------------------

def fetch_iem(station: str, start: datetime, end: datetime,
              session: requests.Session) -> str:
    """Download wide-format CSV text from IEM for one station and time window."""
    params = {
        "stations": station,
        "sts":      start.strftime("%Y-%m-%dT%H:%MZ"),
        "ets":      end.strftime("%Y-%m-%dT%H:%MZ"),
        "what":     "txt",   # returns comma-separated text inline
    }
    for attempt in range(1, RETRIES + 1):
        try:
            log.info("%s: requesting %s → %s (attempt %d)…",
                     station,
                     start.strftime("%Y-%m-%d"),
                     end.strftime("%Y-%m-%d"),
                     attempt)
            resp = session.get(IEM_URL, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.Timeout:
            log.warning("%s: timed out — retrying in 5s…", station)
            time.sleep(5)
        except requests.RequestException as exc:
            log.error("%s: request failed — %s", station, exc)
            break
    return ""


def parse_iem_csv(station: str, csv_text: str) -> tuple[list[str], list[dict]]:
    """
    Parse the IEM wide-format CSV.

    Returns:
        data_cols — list of SHEF code column names present (e.g. ["HGIRGZZ", ...])
        records   — list of row dicts keyed by station, datetime_utc, <SHEF codes>
    """
    text = csv_text.strip()
    if not text:
        return [], []

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], []

    # IEM uses 'utc_valid' for the timestamp column; everything else after
    # 'station' is a SHEF data column.
    data_cols = [c for c in reader.fieldnames
                 if c not in ("station", "utc_valid")]

    records = []
    for row in reader:
        ts_raw = (row.get("utc_valid") or "").strip()
        if not ts_raw:
            continue
        # Normalize "2026-06-17 00:00:00" → "2026-06-17 00:00"
        try:
            ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M")
            except ValueError:
                continue

        rec = {
            "station":      (row.get("station") or station).strip().upper(),
            "datetime_utc": ts.strftime("%Y-%m-%d %H:%M"),
        }
        for col in data_cols:
            val = (row.get(col) or "").strip()
            rec[col] = val   # keep empty strings as empty
        records.append(rec)

    return data_cols, records


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_existing_keys(csv_path: Path) -> set[str]:
    """Return a set of 'datetime_utc' keys already in the per-station CSV."""
    keys: set[str] = set()
    if not csv_path.exists():
        return keys
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            keys.add(row.get("datetime_utc", ""))
    return keys


def append_records(csv_path: Path, data_cols: list[str],
                   records: list[dict], existing_keys: set[str]) -> int:
    """Append only new rows (by datetime_utc) to the per-station CSV.
    Adds any new SHEF columns that appear over time. Returns rows written."""
    if not records:
        return 0

    new_rows = [r for r in records if r["datetime_utc"] not in existing_keys]
    for r in new_rows:
        existing_keys.add(r["datetime_utc"])
    if not new_rows:
        return 0

    write_header = not csv_path.exists()

    if write_header:
        columns = FIXED_COLS + data_cols
    else:
        # Merge existing columns with any newly-seen SHEF codes
        with csv_path.open(newline="", encoding="utf-8") as f:
            existing_cols = csv.DictReader(f).fieldnames or (FIXED_COLS + data_cols)
        columns = list(existing_cols)
        for c in data_cols:
            if c not in columns:
                columns.append(c)
        # If the column set grew, rewrite the file with the new header
        if columns != list(existing_cols):
            _rewrite_with_columns(csv_path, columns)

    with csv_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for r in new_rows:
            for c in columns:
                r.setdefault(c, "")
            writer.writerow(r)

    return len(new_rows)


def _rewrite_with_columns(csv_path: Path, columns: list[str]) -> None:
    """Rewrite an existing CSV under a superset of columns (for new SHEF codes)."""
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            for c in columns:
                r.setdefault(c, "")
            writer.writerow(r)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Download hydromet data from IEM and save per-station CSVs."
    )
    p.add_argument("--days", type=int, default=7,
                   help="Days back from now to request (default: 7).")
    p.add_argument("--start", type=str, default=None,
                   help="Start date YYYY-MM-DD (overrides --days). For backfill.")
    p.add_argument("--end", type=str, default=None,
                   help="End date YYYY-MM-DD (default: now).")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                   help=f"Folder for per-station CSVs (default: {DEFAULT_OUTPUT_DIR}).")
    p.add_argument("--stations", nargs="+", default=STATION_IDS,
                   help="NWSLI IDs (default: IKPA2 NUIA2 UBLA2 JDYA2).")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and parse but do not write anything.")
    return p.parse_args()


def resolve_window(args) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start = now - timedelta(days=args.days)
    if args.end:
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end = now
    return start, end


def main() -> None:
    args = build_args()
    start, end = resolve_window(args)
    output_dir: Path = args.output_dir

    log.info("=" * 64)
    log.info("Hydromet Updater (IEM)  —  %s UTC",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
    log.info("Source    : %s", IEM_URL)
    log.info("Stations  : %s", ", ".join(args.stations))
    log.info("Window    : %s  →  %s UTC",
             start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"))
    log.info("Output dir: %s", output_dir.resolve())
    log.info("=" * 64)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    total_new = 0
    session = requests.Session()
    session.headers.update({
        "User-Agent": "hydromet_updater/4.0 (NPRA hydromet data collection)"
    })

    for station in args.stations:
        csv_path = output_dir / f"{station.upper()}.csv"
        log.info("--- %s ---", station)

        raw = fetch_iem(station, start, end, session)
        if not raw.strip():
            log.warning("%s: empty response — skipping.", station)
            time.sleep(REQUEST_DELAY)
            continue

        data_cols, records = parse_iem_csv(station, raw)
        if not records:
            log.warning("%s: no rows parsed — skipping.", station)
            time.sleep(REQUEST_DELAY)
            continue

        log.info("%s: parsed %d timestamps, columns: %s",
                 station, len(records), ", ".join(data_cols))

        if args.dry_run:
            for rec in records[:2]:
                log.info("  [preview] %s", rec)
            log.info("  [preview] ... earliest %s, latest %s",
                     records[0]["datetime_utc"], records[-1]["datetime_utc"])
        else:
            existing_keys = load_existing_keys(csv_path)
            n = append_records(csv_path, data_cols, records, existing_keys)
            log.info("%s: wrote %d new rows → %s", station, n, csv_path.name)
            total_new += n

        time.sleep(REQUEST_DELAY)

    if args.dry_run:
        log.info("Dry-run complete. No files written.")
    else:
        log.info("Done. Total new rows appended: %d", total_new)


if __name__ == "__main__":
    main()
