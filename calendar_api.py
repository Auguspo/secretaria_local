import datetime
import os
from typing import List, Dict, Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import (
    SCOPES,
    CALENDAR_TOKEN_PATH,
    CREDENTIALS_FILE,
    ID_SEGUNDO_CALENDARIO,
    TIMEZONE,
)

_calendar_service = None


def _ensure_credentials() -> Credentials:
    creds = None
    if os.path.exists(CALENDAR_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(CALENDAR_TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=8080, open_browser=False)
        with open(CALENDAR_TOKEN_PATH, "w") as token:
            token.write(creds.to_json())
    return creds


def get_calendar_service():
    global _calendar_service
    if _calendar_service is None:
        creds = _ensure_credentials()
        _calendar_service = build("calendar", "v3", credentials=creds)
    return _calendar_service


def list_events(start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
    service = get_calendar_service()
    events = []
    for calendar_id in ("primary", ID_SEGUNDO_CALENDARIO):
        res = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start_iso,
                timeMax=end_iso,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        items = res.get("items", [])
        for ev in items:
            ev["_calendar_id"] = calendar_id
        events.extend(items)

    events.sort(key=lambda x: x["start"].get("dateTime", x["start"].get("date")))
    return events


def create_event(summary: str, start_iso: str, end_iso: str):
    service = get_calendar_service()
    body = {
        "summary": summary,
        "start": {"dateTime": start_iso, "timeZone": str(TIMEZONE)},
        "end": {"dateTime": end_iso, "timeZone": str(TIMEZONE)},
    }
    return service.events().insert(calendarId="primary", body=body).execute()


def delete_event_by_name(name: str, start_from_iso: str) -> bool:
    service = get_calendar_service()
    res = (
        service.events()
        .list(calendarId="primary", timeMin=start_from_iso, singleEvents=True)
        .execute()
    )
    events = res.get("items", [])
    for ev in events:
        if name.lower() in ev.get("summary", "").lower():
            service.events().delete(calendarId="primary", eventId=ev["id"]).execute()
            return True
    return False


def daily_agenda_text() -> str:
    today = datetime.datetime.now(TIMEZONE)
    start_day = today.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end_day = today.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()

    events = list_events(start_day, end_day)
    if not events:
        return "☕ ¡Buen día! Tienes la agenda de hoy libre."

    lines = ["☕ ¡Buen día! Tu agenda para hoy:", ""]
    for ev in events:
        inicio = ev["start"].get("dateTime", ev["start"].get("date"))
        hora = inicio[11:16] if "T" in inicio else "Todo el día"
        prefix = "🟡 [IUA]" if ev.get("_calendar_id") == ID_SEGUNDO_CALENDARIO else "🔵"
        lines.append(f"{prefix} {ev['summary']} ({hora})")
    return "\n".join(lines)
