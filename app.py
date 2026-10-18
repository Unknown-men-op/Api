"""People search API — single-file tool.

Put a CSV named database.csv next to this file (any columns you like, as
long as one of them holds a mobile number), run ./run.sh, and search by
mobile number. The CSV is loaded automatically and re-read whenever the
file changes — there is no separate build step.

Endpoints:
    GET /                                  minimal HTML search page
    GET /search?mobile=<number>            JSON, exact match (normalized)
    GET /search?mobile=<number>&partial=1  substring match (slower)
    GET /health                            service + dataset info

Usage:
    ./run.sh [--csv FILE] [--mobile-column "Exact Header"]
             [--host 0.0.0.0] [--port 5000]
"""

import argparse
import csv
import re
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CSV = BASE_DIR / "database.csv"
SAMPLE_SIZE = 50  # values sampled per column when auto-detecting the mobile column

app = Flask(__name__)
app.json.sort_keys = False  # keep the column order of your CSV in responses

MOBILE_HEADER_ALIASES = [
    "mobile_number", "mobile_no", "mobileno", "mobile", "mob", "mob_no",
    "phone_number", "phone_no", "phoneno", "phone", "contact_number",
    "contact_no", "contact", "cell", "cell_number", "whatsapp",
    "whatsapp_number", "number",
]

_DIGITS = re.compile(r"\D")
_MOBILE_RE = re.compile(r"[6-9]\d{9}")


class DataError(Exception):
    """The CSV cannot be loaded or has no usable mobile-number column."""


def clean_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def normalize_mobile(raw) -> str:
    """Digits-only form; drops spaces/dashes and a +91 / 0 / 091 prefix."""
    text = str(raw or "").strip()
    spreadsheet = re.fullmatch(r"(\d+)\.0+", text)  # "9000000001.0" from Excel
    if spreadsheet:
        text = spreadsheet.group(1)
    digits = _DIGITS.sub("", text)
    if len(digits) == 13 and digits.startswith("091"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def looks_like_mobile(raw) -> bool:
    return bool(_MOBILE_RE.fullmatch(normalize_mobile(raw)))


def detect_mobile_column(fieldnames, sample_rows, explicit=None) -> str:
    cleaned = {clean_header(name): name for name in fieldnames}

    if explicit:
        wanted = clean_header(explicit)
        if wanted in cleaned:
            return cleaned[wanted]
        raise DataError(
            f"Column {explicit!r} not found.\n"
            f"  columns in the file: {', '.join(fieldnames)}"
        )

    for alias in MOBILE_HEADER_ALIASES:
        if alias in cleaned:
            return cleaned[alias]

    # No familiar header — look at the values instead.
    best_name, best_score = None, 0.0
    for name in fieldnames:
        values = [row.get(name, "") for row in sample_rows]
        values = [v for v in values if str(v).strip()]
        if not values:
            continue
        score = sum(looks_like_mobile(v) for v in values) / len(values)
        if score > best_score:
            best_name, best_score = name, score

    if best_name and best_score >= 0.6:
        return best_name

    raise DataError(
        "Could not find a mobile-number column.\n"
        f"  columns in the file: {', '.join(fieldnames)}\n"
        "Rename one column to something like 'mobile_number', or pass "
        '--mobile-column "Exact Header".'
    )


def load_csv(path: Path, explicit_mobile_col=None) -> dict:
    if not path.exists():
        raise DataError(f"CSV not found: {path}")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle, restval="")
        if not reader.fieldnames:
            raise DataError("The CSV has no header row.")
        raw_rows, sample = [], []
        for row in reader:
            if len(sample) < SAMPLE_SIZE:
                sample.append(row)
            raw_rows.append(row)
        fieldnames = list(reader.fieldnames)

    mobile_col = detect_mobile_column(fieldnames, sample, explicit_mobile_col)

    rows, by_mobile = [], {}
    skipped = 0
    for row in raw_rows:
        normalized = normalize_mobile(row.get(mobile_col))
        if not normalized:
            skipped += 1
            continue
        record = {key: value for key, value in row.items() if key is not None}
        rows.append((normalized, record))
        by_mobile.setdefault(normalized, []).append(record)

    return {
        "rows": rows,
        "by_mobile": by_mobile,
        "columns": fieldnames,
        "mobile_col": mobile_col,
        "records": len(rows),
        "skipped": skipped,
    }


# --- dataset state (loaded once, re-read when the file changes) ---------------

_lock = threading.Lock()
_state = {
    "rows": None, "by_mobile": {}, "columns": [], "mobile_col": None,
    "records": 0, "skipped": 0, "filekey": None, "source": None,
}


def refresh_state(force: bool = False) -> dict:
    path = Path(app.config["CSV_PATH"])
    try:
        stat = path.stat()
        filekey = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        filekey = None

    with _lock:
        if not force and _state["rows"] is not None and _state["filekey"] == filekey:
            return _state
        try:
            loaded = load_csv(path, app.config["MOBILE_COLUMN"])
        except DataError as err:
            if _state["rows"] is None:
                raise
            app.logger.warning("CSV reload failed, keeping previous data: %s", err)
            return _state
        loaded["filekey"] = filekey
        loaded["source"] = str(path)
        _state.update(loaded)
        return _state


# --- HTTP API -----------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>People Search (demo)</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 40rem;
         padding: 0 1rem; }
  input[type=text] { font-size: 1.1rem; padding: .5rem; width: 55%; }
  pre { background: #f4f4f4; padding: 1rem; overflow-x: auto; border-radius: .4rem; }
</style>
</head>
<body>
<h1>People Search &mdash; demo</h1>
<p>Searches the loaded CSV by mobile number.</p>
<form id="search-form">
  <input id="query" type="text" inputmode="tel" placeholder="e.g. 9000000001" required>
  <label><input id="partial" type="checkbox"> partial</label>
  <button>Search</button>
</form>
<pre id="out">Results appear here&hellip;</pre>
<script>
document.getElementById("search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = document.getElementById("query").value.trim();
  const partial = document.getElementById("partial").checked ? "&partial=1" : "";
  const response = await fetch(`/search?mobile=${encodeURIComponent(query)}${partial}`);
  const body = await response.json();
  document.getElementById("out").textContent = JSON.stringify(body, null, 2);
});
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return INDEX_HTML


@app.get("/health")
def health():
    state = refresh_state()
    return jsonify(
        status="ok",
        records=state["records"],
        skipped=state["skipped"],
        source=state["source"],
        mobile_column=state["mobile_col"],
        columns=state["columns"],
    )


@app.get("/search")
def search():
    raw = (request.args.get("mobile") or "").strip()
    if not raw:
        return jsonify(error="Pass ?mobile=<number>, e.g. /search?mobile=9000000001"), 400

    normalized = normalize_mobile(raw)
    partial = request.args.get("partial", "").lower() in {"1", "true", "yes"}
    minimum = 3 if partial else 6
    if len(normalized) < minimum:
        return jsonify(error=f"{raw!r} is too short to search (need at least {minimum} digits)"), 400

    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 500)
    except ValueError:
        return jsonify(error="limit must be a number"), 400

    state = refresh_state()
    if partial:
        matches = [record for norm, record in state["rows"] if normalized in norm][:limit]
    else:
        matches = state["by_mobile"].get(normalized, [])[:limit]

    return jsonify(
        query={"raw": raw, "normalized": normalized, "partial": partial},
        count=len(matches),
        results=matches,
    )


# --- startup ------------------------------------------------------------------

def resolve_csv(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise DataError(f"CSV not found: {path}")
        return path
    if DEFAULT_CSV.exists():
        return DEFAULT_CSV
    candidates = sorted(BASE_DIR.glob("*.csv"))
    if len(candidates) == 1:
        print(f"Using {candidates[0].name} (the only CSV in this folder)")
        return candidates[0]
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise DataError(
            f"Several CSVs found: {names}\n"
            "Rename the one you want to 'database.csv', or pass --csv <file>."
        )
    raise DataError(
        f"No CSV found. Put your file at {DEFAULT_CSV}\n"
        "It must have a header row and contain a mobile number column."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the people search API from a CSV file.")
    parser.add_argument("--csv", help="path to the CSV (default: database.csv next to this script)")
    parser.add_argument("--mobile-column", help="name of the mobile-number column (auto-detected by default)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="use 0.0.0.0 to expose on your LAN (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    try:
        csv_path = resolve_csv(args.csv)
        app.config["CSV_PATH"] = str(csv_path)
        app.config["MOBILE_COLUMN"] = args.mobile_column
        state = refresh_state(force=True)
    except DataError as err:
        sys.exit(f"ERROR: {err}")

    print(f"Loaded {state['records']} records from {state['source']}")
    print(f"  columns       : {', '.join(state['columns'])}")
    print(f"  mobile column : {state['mobile_col']}")
    if state["skipped"]:
        print(f"  skipped rows without a mobile number: {state['skipped']}")
    print("  (the CSV is re-read automatically whenever the file changes)")
    print(f"Serving search API on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
