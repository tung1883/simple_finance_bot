"""
Google Sheets integration (optional). Requires:
  pip install google-auth google-api-python-client

Credentials (first match wins):
  1. GOOGLE_SERVICE_ACCOUNT_FILE in .env — path to the JSON key
  2. Otherwise: google-service-account.json in the project folder (next to this file)

**Default flow:** each Telegram user creates their own Google Sheet, shares it with the service account
as **Editor**, then runs `/linksheet` with the URL or spreadsheet ID. The bot uses **Sheets API** only
(append/backfill + a named **Transactions** tab with headers).

**Optional:** set `GOOGLE_SHEETS_AUTO_CREATE=true` to attempt **Drive API** `files.create` per user (needs
non-zero Drive quota on the service account).

Sharing: Optional `GOOGLE_SHEETS_SHARE_EMAILS` / `GOOGLE_SHEETS_PUBLIC_LINK` apply **after** creation or
linking (Drive permissions API), when the service account is allowed to modify sharing.
"""
import json
import os
import re
from typing import Optional, Tuple

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SERVICE_ACCOUNT_JSON = os.path.join(_BOT_DIR, "google-service-account.json")

try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    Credentials = None  # type: ignore
    build = None  # type: ignore
    HttpError = type("HttpError", (Exception,), {})  # type: ignore

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)


def service_account_json_path():
    """Path to the service-account JSON if present, else None."""
    env = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if env:
        p = os.path.expanduser(env.strip().strip('"').strip("'"))
        if os.path.isfile(p):
            return p
    return _DEFAULT_SERVICE_ACCOUNT_JSON if os.path.isfile(_DEFAULT_SERVICE_ACCOUNT_JSON) else None


def service_account_email() -> Optional[str]:
    """client_email from the service-account JSON, if available."""
    path = service_account_json_path()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f).get("client_email") or "").strip() or None
    except (OSError, json.JSONDecodeError):
        return None


def parse_spreadsheet_id(text: str) -> Optional[str]:
    """Extract spreadsheet ID from a docs.google.com URL or a bare ID string."""
    s = (text or "").strip()
    if not s:
        return None
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", s)
    if m:
        return m.group(1)
    if re.match(r"^[a-zA-Z0-9-_]{20,}$", s):
        return s
    return None


def sheets_available() -> bool:
    return (
        Credentials is not None
        and build is not None
        and service_account_json_path() is not None
    )


def _credentials():
    path = service_account_json_path()
    if not path or not Credentials:
        return None
    return Credentials.from_service_account_file(path, scopes=SCOPES)


def _services():
    creds = _credentials()
    if not creds or build is None:
        return None, None
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets, drive


def _service_account_project_id():
    path = service_account_json_path()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("project_id")
    except (OSError, json.JSONDecodeError):
        return None


def _api_links_hint():
    pid = _service_account_project_id()
    if not pid:
        return (
            "Enable Google Sheets API and Google Drive API in Google Cloud Console "
            "(APIs & Services → Library) for the same project as your service account."
        )
    return (
        f"Enable APIs for GCP project `{pid}`:\n"
        f"• Sheets: https://console.cloud.google.com/apis/library/sheets.googleapis.com?project={pid}\n"
        f"• Drive:  https://console.cloud.google.com/apis/library/drive.googleapis.com?project={pid}\n"
        "Click Enable on each, wait 1–2 minutes, then try /sheet again."
    )


def _http_error_api_reason(e: BaseException) -> Tuple[Optional[str], Optional[str]]:
    """Parse googleapiclient HttpError JSON body → (errors[].reason, message)."""
    raw = getattr(e, "content", None)
    if not raw:
        return None, None
    try:
        data = json.loads(raw.decode())
    except (ValueError, AttributeError):
        return None, None
    err = data.get("error") or {}
    errors = err.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        return first.get("reason"), first.get("message") or err.get("message")
    return None, err.get("message")


def _env_truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _share_email_list():
    raw = os.getenv("GOOGLE_SHEETS_SHARE_EMAILS", "")
    parts = raw.replace(";", ",").split(",")
    out = []
    seen = set()
    for p in parts:
        e = p.strip()
        if not e:
            continue
        key = e.lower()
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _share_role() -> str:
    r = (os.getenv("GOOGLE_SHEETS_SHARE_ROLE") or "writer").strip().lower()
    return r if r in ("reader", "writer", "commenter") else "writer"


def _apply_drive_sharing(drive, spreadsheet_id: str) -> None:
    """
    Drive "Restricted" = no anonymous link unless GOOGLE_SHEETS_PUBLIC_LINK is set.
    Named Google accounts get GOOGLE_SHEETS_SHARE_ROLE (default writer).
    """
    emails = _share_email_list()
    allow_anyone_reader = _env_truthy("GOOGLE_SHEETS_PUBLIC_LINK")
    role = _share_role()

    if allow_anyone_reader:
        try:
            drive.permissions().create(
                fileId=spreadsheet_id,
                body={"type": "anyone", "role": "reader"},
                fields="id",
            ).execute()
        except HttpError as e:
            if getattr(e.resp, "status", None) == 403:
                raise RuntimeError(
                    "Google Drive returned 403 adding link sharing — check Drive API, IAM, "
                    "and whether your organization allows 'anyone with the link'.\n\n"
                    + _api_links_hint()
                ) from e
            raise

    for email in emails:
        try:
            drive.permissions().create(
                fileId=spreadsheet_id,
                body={"type": "user", "role": role, "emailAddress": email},
                fields="id",
                sendNotificationEmail=False,
            ).execute()
        except HttpError as e:
            if getattr(e.resp, "status", None) == 403:
                raise RuntimeError(
                    f"Google Drive returned 403 sharing with `{email}` — the account may need "
                    "to be in your Workspace domain, or an admin may block external sharing. "
                    "Try GOOGLE_SHEETS_PUBLIC_LINK=true for read-only link access, or use a "
                    "Workspace-managed identity.\n\n"
                    + _api_links_hint()
                ) from e
            raise


TRANSACTION_TAB = "Transactions"


def _ensure_transactions_tab(sheets, spreadsheet_id: str) -> None:
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    for sh in meta.get("sheets") or []:
        props = sh.get("properties") or {}
        if props.get("title") == TRANSACTION_TAB:
            return
    sheets_list = meta.get("sheets") or []
    if not sheets_list:
        raise RuntimeError("Spreadsheet has no tabs")
    first = sheets_list[0]["properties"]["sheetId"]
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={
            "requests": [
                {
                    "updateSheetProperties": {
                        "properties": {"sheetId": first, "title": TRANSACTION_TAB},
                        "fields": "title",
                    }
                }
            ]
        },
    ).execute()


def create_user_spreadsheet(user_label: str) -> Tuple[str, str]:
    sheets, drive = _services()
    if not sheets or not drive:
        raise RuntimeError("Google API not configured")

    title = f"CashButler — {user_label}"
    file_body = {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
    }

    try:
        created = (
            drive.files()
            .create(body=file_body, fields="id")
            .execute()
        )
    except HttpError as e:
        if getattr(e.resp, "status", None) == 403:
            reason, msg = _http_error_api_reason(e)
            if reason == "storageQuotaExceeded":
                raise RuntimeError(
                    "Google Drive returned **storageQuotaExceeded** for this **service account**. "
                    "Many service accounts now have **0 bytes** of Drive quota (see Drive "
                    "`about.get`, field `storageQuota.limit`); that is separate from your Gmail storage. "
                    "You cannot increase that quota from Cloud Console like Gmail storage. "
                    "Practical options: authenticate as **your Google user** (OAuth) for `files.create`, "
                    "have a user-owned spreadsheet shared with this service account as **Editor** (then use "
                    "its ID with the Sheets API only), or use a **Workspace shared drive** where this "
                    "service account is a member.\n\n"
                    f"API message: {msg or reason}"
                ) from e
            raise RuntimeError(
                "Google Drive returned 403 when creating the spreadsheet file — enable "
                "**Google Drive API**, link billing, and grant the service account IAM on the project "
                "(e.g. Service Usage Consumer) if required.\n\n"
                + _api_links_hint()
            ) from e
        raise

    spreadsheet_id = created["id"]

    try:
        _ensure_transactions_tab(sheets, spreadsheet_id)
    except HttpError as e:
        if getattr(e.resp, "status", None) == 403:
            raise RuntimeError(
                "Google Sheets returned 403 when configuring the new spreadsheet — enable "
                "**Google Sheets API** for this service account's project.\n\n"
                + _api_links_hint()
            ) from e
        raise

    _apply_drive_sharing(drive, spreadsheet_id)

    rng = f"{TRANSACTION_TAB}!A1:D1"
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=rng,
        valueInputOption="RAW",
        body={"values": [["Time", "Type", "Amount", "Category"]]},
    ).execute()

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    return spreadsheet_id, url


def prepare_linked_spreadsheet(spreadsheet_id: str) -> Tuple[str, str]:
    """
    User-owned spreadsheet already shared with the service account (Editor).
    Ensures Transactions tab + header row; optional Drive sharing env vars.
    """
    sheets, drive = _services()
    if not sheets or not drive:
        raise RuntimeError("Google API not configured")

    spreadsheet_id = spreadsheet_id.strip()
    try:
        sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    except HttpError as e:
        status = getattr(e.resp, "status", None)
        if status == 404:
            raise RuntimeError(
                "Spreadsheet not found. Check the ID/URL and that the file exists."
            ) from e
        if status == 403:
            raise RuntimeError(
                "No access (403). Share the spreadsheet with the service account email "
                f"({service_account_email() or 'see google-service-account.json → client_email'}) "
                "as Editor, then try again."
            ) from e
        raise

    try:
        _ensure_transactions_tab(sheets, spreadsheet_id)
    except HttpError as e:
        if getattr(e.resp, "status", None) == 403:
            raise RuntimeError(
                "Google Sheets returned 403 configuring the spreadsheet — enable "
                "**Google Sheets API** and ensure the service account has Editor access.\n\n"
                + _api_links_hint()
            ) from e
        raise

    _apply_drive_sharing(drive, spreadsheet_id)

    rng = f"{TRANSACTION_TAB}!A1:D1"
    sheets.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=rng,
        valueInputOption="RAW",
        body={"values": [["Time", "Type", "Amount", "Category"]]},
    ).execute()

    url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
    return spreadsheet_id, url


def append_transaction(spreadsheet_id: str, time_str: str, tx_type: str, amount: float, category: str) -> None:
    sheets, _ = _services()
    if not sheets:
        raise RuntimeError("Google API not configured")
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{TRANSACTION_TAB}!A:D",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [[time_str, tx_type, amount, category]]},
    ).execute()


def backfill_transactions(spreadsheet_id: str, rows: list) -> None:
    """rows: list of (time, type, amount, category)"""
    if not rows:
        return
    sheets, _ = _services()
    if not sheets:
        raise RuntimeError("Google API not configured")
    values = [[t, ty, amt, cat] for t, ty, amt, cat in rows]
    sheets.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{TRANSACTION_TAB}!A:D",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()
