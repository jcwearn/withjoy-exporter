import csv
import io
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from gspread.utils import fill_gaps
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://withjoy.com/login"
LOGIN_NAV_TIMEOUT_MS = 60_000
LOGIN_NAV_ATTEMPTS = 3
LOGIN_NAV_BACKOFF_SECONDS = 5
DATE_TAB_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ExporterError(RuntimeError):
    pass


class LoginFailed(ExporterError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ExporterError(f"Missing required env var: {name}")
    return value


def _dump_debug(page, label: str) -> None:
    debug_dir = Path(os.environ.get("DEBUG_OUTPUT_DIR", "/tmp/debug"))
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(debug_dir / f"{label}.png"), full_page=True)
        (debug_dir / f"{label}.html").write_text(page.content(), encoding="utf-8")
        print(f"wrote debug artifacts to {debug_dir}/{label}.{{png,html}}", file=sys.stderr)
    # The blanket catch below is deliberate, not a dodge. This helper runs only
    # while something has already gone wrong, and its whole job is to capture
    # evidence about that. A screenshot or a page.content() call that raises
    # here -- a closed page, a full disk, a read-only DEBUG_OUTPUT_DIR -- must
    # not replace the real failure with a second one from the diagnostics.
    # Narrowing it means guessing which of playwright's and the filesystem's
    # exception types can surface, and being wrong loses the original error.
    except Exception as exc:  # noqa: BLE001
        print(f"failed to dump debug output: {exc}", file=sys.stderr)


def _goto_login(page, sleep=time.sleep, attempts: int = LOGIN_NAV_ATTEMPTS) -> None:
    """Navigate to the login page, retrying transient navigation timeouts.

    This is the first thing the run does, so a blip reaching withjoy.com used
    to kill the whole export on Playwright's 30s default. Every later step
    already tolerates a slow page, so give the entry point the same slack.
    """
    for attempt in range(1, attempts + 1):
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=LOGIN_NAV_TIMEOUT_MS)
            return
        except PlaywrightTimeout:
            if attempt == attempts:
                raise
            print(
                f"login page navigation timed out (attempt {attempt}/{attempts}); retrying",
                file=sys.stderr,
            )
            sleep(LOGIN_NAV_BACKOFF_SECONDS)


def download_csv(username: str, password: str, guest_list_url: str) -> bytes:
    debug = bool(os.environ.get("DEBUG"))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            _goto_login(page)

            email_input = page.locator(
                'input[type="email"], input[name="email"], input[name="username"]'
            ).first
            email_input.wait_for(state="visible", timeout=15_000)
            email_input.fill(username)

            password_input = page.locator('input[type="password"]').first
            password_input.wait_for(state="visible", timeout=10_000)
            password_input.fill(password)

            try:
                with page.expect_navigation(timeout=25_000, wait_until="networkidle"):
                    password_input.press("Enter")
            except PlaywrightTimeout:
                pass

            if "auth0" in page.url or "/login" in page.url:
                _dump_debug(page, "login_failed")
                raise LoginFailed(
                    f"Login did not leave the auth flow (url={page.url}). "
                    "Check WITHJOY_USERNAME / WITHJOY_PASSWORD. "
                    "If MFA is enabled, disable it for this script. "
                    "If Auth0 is flagging the headless run, see the debug screenshot."
                )

            try:
                page.goto(guest_list_url, wait_until="networkidle", timeout=60_000)
            except PlaywrightTimeout:
                pass

            if "auth0" in page.url or "/login" in page.url:
                _dump_debug(page, "session_lost")
                raise LoginFailed(
                    f"Navigating to the guest list bounced back to login (url={page.url})."
                )

            if debug:
                _dump_debug(page, "guest_list_loaded")

            export_re = re.compile(r"Export\s+All\s+Guests", re.IGNORECASE)
            export_button = page.get_by_text(export_re).first
            try:
                export_button.wait_for(state="visible", timeout=30_000)
            except PlaywrightTimeout:
                _dump_debug(page, "export_button_not_found")
                raise

            with page.expect_download(timeout=30_000) as download_info:
                export_button.click()
            download = download_info.value

            tmp_path = Path(download.path())
            return tmp_path.read_bytes()
        finally:
            browser.close()


def _parse_csv(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def _label_plus_ones(rows: list[list[str]]) -> list[list[str]]:
    if len(rows) < 2:
        return rows
    last_first_name = ""
    for row in rows[1:]:
        first = row[0].strip() if len(row) > 0 and row[0] else ""
        last = row[1].strip() if len(row) > 1 and row[1] else ""
        if first:
            last_first_name = first
        elif not last and last_first_name:
            row[0] = f"{last_first_name}'s Guest"
    return rows


def _expand_tags(rows: list[list[str]]) -> list[list[str]]:
    if len(rows) < 2:
        return rows
    header = rows[0]
    tags_idx = next(
        (i for i, name in enumerate(header) if name.strip().lower() == "tags"), None
    )
    if tags_idx is None:
        return rows

    row_tags: list[set[str]] = []
    all_tags: set[str] = set()
    for row in rows[1:]:
        cell = row[tags_idx] if len(row) > tags_idx else ""
        tags = {t.strip() for t in cell.split(",") if t.strip()}
        row_tags.append(tags)
        all_tags |= tags

    if not all_tags:
        return rows

    sorted_tags = sorted(all_tags, key=str.lower)
    base_cols = max(len(r) for r in rows)
    header += [""] * (base_cols - len(header))
    header += [f"{tag} (tag)" for tag in sorted_tags]
    for row, tags in zip(rows[1:], row_tags):
        row += [""] * (base_cols - len(row))
        row += [1 if tag in tags else 0 for tag in sorted_tags]
    return rows


def _parse_columns(value: str) -> list[str]:
    return [name.strip() for name in re.split(r"[,\n]", value) if name.strip()]


def _select_columns(rows: list[list[str]], columns: list[str]) -> list[list[str]]:
    if not columns or not rows:
        return rows
    header = rows[0]

    by_name: dict[str, int] = {}
    for i, name in enumerate(header):
        by_name.setdefault(name.strip().lower(), i)

    wanted = {name.strip().lower() for name in columns}
    indices: list[int | None] = [by_name.get(name.strip().lower()) for name in columns]

    missing = [name for name, idx in zip(columns, indices) if idx is None]
    if missing:
        print(f"columns not found in export: {', '.join(missing)}", file=sys.stderr)

    dropped = [n for n in header if n.strip() and n.strip().lower() not in wanted]
    if dropped:
        print(f"ignoring undeclared export columns: {', '.join(dropped)}", file=sys.stderr)

    header[:] = list(columns)
    for row in rows[1:]:
        row[:] = [
            row[idx] if idx is not None and idx < len(row) else "" for idx in indices
        ]
    return rows


def _rows_equal(a: list[list[str]], b: list[list[str]]) -> bool:
    if len(a) != len(b):
        return False
    cols = max(
        max((len(r) for r in a), default=0),
        max((len(r) for r in b), default=0),
    )
    for ra, rb in zip(a, b):
        pa = [str(c) for c in ra] + [""] * (cols - len(ra))
        pb = [str(c) for c in rb] + [""] * (cols - len(rb))
        if pa != pb:
            return False
    return True


def _write_rows(spreadsheet: gspread.Spreadsheet, tab_name: str, rows: list[list[str]]) -> None:
    n_cols = max((len(r) for r in rows), default=1)
    n_rows = max(len(rows), 1)
    try:
        worksheet = spreadsheet.worksheet(tab_name)
        worksheet.clear()
        worksheet.resize(rows=n_rows, cols=n_cols)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=tab_name, rows=n_rows, cols=n_cols)
    if rows:
        padded = fill_gaps(rows, rows=n_rows, cols=n_cols)
        worksheet.update(values=padded, range_name="A1")


def _prune_history(spreadsheet: gspread.Spreadsheet, keep: int) -> int:
    dated = [ws for ws in spreadsheet.worksheets() if DATE_TAB_RE.match(ws.title)]
    dated.sort(key=lambda ws: ws.title, reverse=True)
    to_delete = dated[keep:]
    for ws in to_delete:
        spreadsheet.del_worksheet(ws)
    return len(to_delete)


def upload_to_sheets(
    csv_bytes: bytes,
    sheet_id: str,
    credentials_path: str,
    latest_tab: str,
    timezone: str,
    keep_days: int,
    columns: list[str],
) -> tuple[int, str, int, bool]:
    rows = _parse_csv(csv_bytes)
    _label_plus_ones(rows)
    _select_columns(rows, columns)
    _expand_tags(rows)
    guest_count = max(len(rows) - 1, 0)

    client = gspread.service_account(filename=credentials_path)
    spreadsheet = client.open_by_key(sheet_id)

    today = datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")

    try:
        existing_rows = spreadsheet.worksheet(latest_tab).get_all_values()
    except gspread.WorksheetNotFound:
        existing_rows = None

    if existing_rows is not None and _rows_equal(existing_rows, rows):
        return guest_count, today, 0, False

    _write_rows(spreadsheet, latest_tab, rows)
    _write_rows(spreadsheet, today, rows)
    pruned = _prune_history(spreadsheet, keep_days)

    return guest_count, today, pruned, True


def main() -> int:
    try:
        username = _require("WITHJOY_USERNAME")
        password = _require("WITHJOY_PASSWORD")
        guest_list_url = _require("WITHJOY_GUEST_LIST_URL")
        sheet_id = _require("SHEET_ID")
        credentials_path = os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS", "/secrets/google-service-account.json"
        )
        latest_tab = os.environ.get("LATEST_TAB_NAME", "latest")
        timezone = os.environ.get("TIMEZONE", "America/New_York")
        keep_days = int(os.environ.get("HISTORY_KEEP_DAYS", "5"))
        columns = _parse_columns(os.environ.get("EXPORT_COLUMNS", ""))

        csv_bytes = download_csv(username, password, guest_list_url)
        guest_count, today, pruned, changed = upload_to_sheets(
            csv_bytes, sheet_id, credentials_path, latest_tab, timezone, keep_days, columns
        )
        if changed:
            print(
                f"Exported {guest_count} guests → tab '{latest_tab}' + tab '{today}'. "
                f"Pruned {pruned} old tabs."
            )
        else:
            print(f"No changes since last run ({guest_count} guests). Skipped sheet update.")
        return 0
    except ExporterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
