import csv
import io
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://withjoy.com/login"
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
    except Exception as exc:
        print(f"failed to dump debug output: {exc}", file=sys.stderr)


def download_csv(username: str, password: str, guest_list_url: str) -> bytes:
    debug = bool(os.environ.get("DEBUG"))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            page.goto(LOGIN_URL, wait_until="domcontentloaded")

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

            page.goto(guest_list_url, wait_until="networkidle", timeout=60_000)

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


def _write_rows(spreadsheet: gspread.Spreadsheet, tab_name: str, rows: list[list[str]]) -> None:
    try:
        worksheet = spreadsheet.worksheet(tab_name)
        worksheet.clear()
    except gspread.WorksheetNotFound:
        cols = max((len(r) for r in rows), default=1)
        worksheet = spreadsheet.add_worksheet(title=tab_name, rows=max(len(rows), 1), cols=max(cols, 1))
    if rows:
        worksheet.update(values=rows, range_name="A1")


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
) -> tuple[int, str, int]:
    rows = _parse_csv(csv_bytes)
    guest_count = max(len(rows) - 1, 0)

    client = gspread.service_account(filename=credentials_path)
    spreadsheet = client.open_by_key(sheet_id)

    today = datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")

    _write_rows(spreadsheet, latest_tab, rows)
    _write_rows(spreadsheet, today, rows)
    pruned = _prune_history(spreadsheet, keep_days)

    return guest_count, today, pruned


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

        csv_bytes = download_csv(username, password, guest_list_url)
        guest_count, today, pruned = upload_to_sheets(
            csv_bytes, sheet_id, credentials_path, latest_tab, timezone, keep_days
        )
        print(
            f"Exported {guest_count} guests → tab '{latest_tab}' + tab '{today}'. "
            f"Pruned {pruned} old tabs."
        )
        return 0
    except ExporterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
