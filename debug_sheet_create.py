"""
Runs the same spreadsheet creation path as the bot (Drive create + Sheets setup).
Usage: python debug_sheet_create.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
KEY = os.path.join(ROOT, "google-service-account.json")

if __name__ == "__main__":
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)

    try:
        import sheet_sync
        from googleapiclient.errors import HttpError
    except ImportError as e:
        print("Import error:", e)
        print("pip install google-auth google-api-python-client")
        sys.exit(1)

    if not os.path.isfile(KEY):
        print(f"Missing {KEY}")
        sys.exit(1)

    with open(KEY, encoding="utf-8") as f:
        meta = json.load(f)
    print("project_id:", meta.get("project_id"))
    print("client_email:", meta.get("client_email"))
    print("---")

    try:
        sid, url = sheet_sync.create_user_spreadsheet("DEBUG")
        print("SUCCESS")
        print("spreadsheet_id:", sid)
        print("url:", url)
    except HttpError as e:
        print("HTTP", e.resp.status, e.resp.reason)
        try:
            print(e.content.decode())
        except Exception:
            print(e)
        sys.exit(1)
    except RuntimeError as e:
        cause = e.__cause__
        if isinstance(cause, HttpError):
            print("HTTP", cause.resp.status, cause.resp.reason)
            try:
                print(cause.content.decode())
            except Exception:
                pass
            print("---")
        print(e)
        sys.exit(1)
    except Exception as e:
        print(e)
        sys.exit(1)
