import datetime
import os
from typing import Any, Dict, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from config import (
    CALENDAR_TOKEN_PATH,
    CREDENTIALS_FILE,
    ID_SEGUNDO_CALENDARIO,
    SCOPES,
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
        with open(CALENDAR_TOKEN_PATH, "w", encoding="utf-8") as token:
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
    events: List[Dict[str, Any]] = []
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


def create_event(
    summary: str,
    start_value: str,
    end_value: str,
    *,
    calendar_id: str = "primary",
    all_day: bool = False,
    description: str = "",
):
    service = get_calendar_service()
    body: Dict[str, Any] = {"summary": summary}
    if description:
        body["description"] = description
    if all_day:
        body["start"] = {"date": start_value[:10]}
        body["end"] = {"date": end_value[:10]}
    else:
        body["start"] = {"dateTime": start_value, "timeZone": str(TIMEZONE)}
        body["end"] = {"dateTime": end_value, "timeZone": str(TIMEZONE)}
    return service.events().insert(calendarId=calendar_id, body=body).execute()


def create_all_day_event(summary: str, date_iso: str, *, calendar_id: str = "primary", description: str = ""):
    """Crea un evento de dia completo en Calendar."""
    date_value = date_iso[:10]
    next_day = (datetime.datetime.fromisoformat(f"{date_value}T00:00:00") + datetime.timedelta(days=1)).date().isoformat()
    return create_event(
        summary=summary,
        start_value=date_value,
        end_value=next_day,
        calendar_id=calendar_id,
        all_day=True,
        description=description,
    )


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
        return "Cafe y foco: hoy tenes la agenda libre."

    lines = ["Buen dia. Tu agenda para hoy:", ""]
    for ev in events:
        inicio = ev["start"].get("dateTime", ev["start"].get("date"))
        hora = inicio[11:16] if "T" in inicio else "Todo el dia"
        prefix = "[IUA]" if ev.get("_calendar_id") == ID_SEGUNDO_CALENDARIO else "[CAL]"
        lines.append(f"{prefix} {ev['summary']} ({hora})")
    return "\n".join(lines)
