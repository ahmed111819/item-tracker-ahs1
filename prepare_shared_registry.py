import io
import json
import os
import tempfile
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

folder_id = os.environ["DRIVE_AMAZON_SA_FOLDER_ID"].strip()
info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
credentials = service_account.Credentials.from_service_account_info(
    info, scopes=["https://www.googleapis.com/auth/drive"])
drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
name = "amazon_deals_sent_registry.json"
query = f"'{folder_id}' in parents and name = '{name}' and trashed = false"
result = drive.files().list(q=query, pageSize=100, fields="files(id,name)",
                            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
files = result.get("files", [])
if len(files) != 1:
    raise RuntimeError(f"Expected exactly one existing {name} in the shared Drive folder; found {len(files)}.")
request = drive.files().get_media(fileId=files[0]["id"], supportsAllDrives=True)
stream = io.BytesIO()
downloader = MediaIoBaseDownload(stream, request)
done = False
while not done:
    _, done = downloader.next_chunk()
raw = stream.getvalue()
data = json.loads(raw)
if not isinstance(data, dict) or not isinstance(data.get("alerts", {}), dict):
    raise RuntimeError("Shared alert registry JSON is invalid.")
target = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / name
target.write_bytes(raw)
print(f"Prepared shared registry cache: {target}")
