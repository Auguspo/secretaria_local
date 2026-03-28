import datetime
import logging
import random
from typing import Any, Dict, List, Optional

from ollama import AsyncClient
from telegram.ext import ContextTypes

from calendar_api import list_events
from config import ID_SEGUNDO_CALENDARIO, MI_CHAT_ID, OLLAMA_MODEL, TIMEZONE
from database import listar_objetivos_proyectos, listar_tareas_sueltas, listar_universidad

ollama_client = AsyncClient()

_ENTREGABLE_KEYS = ("examen", "parcial", "entrega", "tp")
_CLASE_KEYS = ("clase", "teoria", "practica")


def _target_chat_id() -> Optional[int]:
    raw = str(MI_CHAT_ID or "").strip()
    if not raw:
        logging.warning("[scheduler] MI_CHAT_ID vacio: no se enviaran mensajes proactivos")
        return None
    try:
        return int(raw)
    except ValueError:
        logging.error("[scheduler] MI_CHAT_ID invalido (debe ser numerico): %r", raw)
        return None


def _parse_iso_suave(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        return dt.astimezone(TIMEZONE)
    except ValueError:
        pass

    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%y %H:%M", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            dt = datetime.datetime.strptime(raw, fmt)
            if " %H:%M" not in fmt:
                dt = dt.replace(hour=0, minute=0)
            return dt.replace(tzinfo=TIMEZONE)
        except ValueError:
            continue
    return None


def _to_lower_text(*parts: Any) -> str:
    joined = " ".join(str(p or "") for p in parts)
    return joined.lower()


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


def _is_entregable_text(*parts: Any) -> bool:
    return _contains_any(_to_lower_text(*parts), _ENTREGABLE_KEYS)


def _is_clase_text(*parts: Any) -> bool:
    return _contains_any(_to_lower_text(*parts), _CLASE_KEYS)


def _dias_hasta(target: datetime.datetime, now: datetime.datetime) -> int:
    return (target.date() - now.date()).days


def _fmt_fecha(dt: Optional[datetime.datetime]) -> str:
    if dt is None:
        return "sin fecha"
    return dt.strftime("%d/%m/%Y %H:%M")


def _event_start_dt(ev: Dict[str, Any]) -> Optional[datetime.datetime]:
    start = ev.get("start", {})
    value = start.get("dateTime") or start.get("date")
    return _parse_iso_suave(value)


def _event_summary(ev: Dict[str, Any]) -> str:
    return str(ev.get("summary", "")).strip()


def _event_is_universidad(ev: Dict[str, Any]) -> bool:
    summary = _event_summary(ev)
    return ev.get("_calendar_id") == ID_SEGUNDO_CALENDARIO or _is_entregable_text(summary) or _is_clase_text(summary)


def _event_is_clase_regular(ev: Dict[str, Any]) -> bool:
    summary = _event_summary(ev)
    low = summary.lower()
    # Heuristica: todo evento del calendario IUA del dia se considera clase regular,
    # salvo que sea entregable academico explicito.
    if ev.get("_calendar_id") == ID_SEGUNDO_CALENDARIO and not _is_entregable_text(low):
        return True
    return _is_clase_text(low)


async def _obtener_eventos_hoy() -> List[Dict[str, Any]]:
    now = datetime.datetime.now(TIMEZONE)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end = now.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    return list_events(start, end)


async def _obtener_eventos_calendario_alerta_7d() -> List[str]:
    now = datetime.datetime.now(TIMEZONE)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end = (now + datetime.timedelta(days=8)).replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    events = list_events(start, end)

    alerts: List[str] = []
    for ev in events:
        summary = _event_summary(ev)
        if not summary or not _event_is_universidad(ev):
            continue
        if not _is_entregable_text(summary):
            continue

        dt = _event_start_dt(ev)
        if dt is None:
            continue
        if _dias_hasta(dt, now) == 7:
            alerts.append(f"- {summary} ({_fmt_fecha(dt)})")
    return alerts


async def _obtener_tareas_pendientes() -> List[Dict[str, Any]]:
    return await listar_tareas_sueltas(solo_activas=True)


async def _obtener_universidad_activa() -> List[Dict[str, Any]]:
    return await listar_universidad(solo_activas=True)


def _extraer_clases_hoy(uni_items: List[Dict[str, Any]]) -> List[str]:
    now = datetime.datetime.now(TIMEZONE)
    out: List[str] = []
    for item in uni_items:
        titulo = str(item.get("titulo", "")).strip()
        materia = str(item.get("materia", "")).strip()
        tipo = str(item.get("tipo", "")).strip()
        descripcion = str(item.get("descripcion", "")).strip()
        if not _is_clase_text(titulo, tipo, descripcion):
            continue
        dt = _parse_iso_suave(item.get("fecha_evento"))
        if dt is None:
            continue
        if _dias_hasta(dt, now) == 0:
            materia_txt = f" - {materia}" if materia else ""
            out.append(f"- {titulo}{materia_txt} ({_fmt_fecha(dt)})")
    return out


def _extraer_clases_hoy_calendario(eventos_hoy: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for ev in eventos_hoy:
        if not _event_is_clase_regular(ev):
            continue
        summary = _event_summary(ev)
        if not summary:
            continue
        dt = _event_start_dt(ev)
        out.append(f"- {summary} ({_fmt_fecha(dt)})")
    return out


def _extraer_alertas_universidad_7d(uni_items: List[Dict[str, Any]]) -> List[str]:
    now = datetime.datetime.now(TIMEZONE)
    out: List[str] = []
    for item in uni_items:
        titulo = str(item.get("titulo", "")).strip()
        materia = str(item.get("materia", "")).strip()
        tipo = str(item.get("tipo", "")).strip()
        descripcion = str(item.get("descripcion", "")).strip()
        if not _is_entregable_text(titulo, tipo, descripcion):
            continue
        dt = _parse_iso_suave(item.get("fecha_evento"))
        if dt is None:
            continue
        if _dias_hasta(dt, now) == 7:
            materia_txt = f" - {materia}" if materia else ""
            out.append(f"- {titulo}{materia_txt} ({_fmt_fecha(dt)})")
    return out


def _extraer_alertas_tareas_7d(tareas: List[Dict[str, Any]]) -> List[str]:
    now = datetime.datetime.now(TIMEZONE)
    out: List[str] = []
    for t in tareas:
        texto = str(t.get("texto", "")).strip()
        if not _is_entregable_text(texto):
            continue
        dt = _parse_iso_suave(t.get("fecha_evento"))
        if dt is None:
            continue
        if _dias_hasta(dt, now) == 7:
            out.append(f"- {texto} ({_fmt_fecha(dt)})")
    return out


def _armar_reporte_diario(
    eventos_hoy: List[Dict[str, Any]],
    tareas_pendientes: List[Dict[str, Any]],
    clases_hoy: List[str],
    alertas_7d: List[str],
) -> str:
    lines: List[str] = ["Reporte diario", ""]

    lines.append("Agenda de hoy:")
    agenda_eventos = [ev for ev in eventos_hoy if not _event_is_clase_regular(ev)]
    if agenda_eventos:
        for ev in agenda_eventos:
            summary = _event_summary(ev) or "Sin titulo"
            dt = _event_start_dt(ev)
            prefix = "[IUA]" if ev.get("_calendar_id") == ID_SEGUNDO_CALENDARIO else "[CAL]"
            lines.append(f"- {prefix} {summary} ({_fmt_fecha(dt)})")
    else:
        lines.append("- Sin eventos en Calendar para hoy.")

    lines.append("")
    lines.append("Tareas pendientes:")
    tareas_con_fecha = [t for t in tareas_pendientes if t.get("fecha_evento")]
    tareas_sin_fecha = [t for t in tareas_pendientes if not t.get("fecha_evento")]
    if tareas_con_fecha:
        for t in tareas_con_fecha[:10]:
            texto = str(t.get("texto", "")).strip() or "Sin descripcion"
            dt = _parse_iso_suave(t.get("fecha_evento"))
            fecha_txt = f" | {_fmt_fecha(dt)}" if dt else ""
            lines.append(f"- {texto}{fecha_txt}")
    else:
        lines.append("- Sin tareas con fecha.")

    if tareas_sin_fecha:
        lines.append("")
        lines.append("Backlog sin fecha:")
        for t in tareas_sin_fecha[:5]:
            texto = str(t.get("texto", "")).strip() or "Sin descripcion"
            lines.append(f"- {texto}")

    lines.append("")
    lines.append("Clases de hoy:")
    if clases_hoy:
        lines.extend(clases_hoy[:10])
    else:
        lines.append("- No hay clases para hoy.")

    if alertas_7d:
        lines.append("")
        lines.append("Alerta universidad (faltan 7 dias):")
        lines.extend(alertas_7d[:10])

    return "\n".join(lines).strip()


async def _generar_nudge_ollama(objetivos: List[Dict[str, Any]]) -> str:
    if not objetivos:
        return ""

    sample_size = 2 if len(objetivos) >= 2 else 1
    seleccion = random.sample(objetivos, k=sample_size)
    payload = "\n".join(
        f"- {str(o.get('descripcion', '')).strip()} | fecha={str(o.get('fecha_evento') or 'sin fecha')}"
        for o in seleccion
    )

    now_iso = datetime.datetime.now(TIMEZONE).isoformat()
    system_prompt = (
        "Eres un coach proactivo y cercano. "
        "Redacta un unico mensaje breve (maximo 3 lineas) en espanol rioplatense, "
        "amigable y motivador, preguntando avance de estos objetivos activos. "
        "No uses listas, no uses markdown, no inventes datos.\n"
        f"Fecha actual: {now_iso}"
    )
    user_prompt = f"Objetivos activos seleccionados:\n{payload}"

    try:
        resp = await ollama_client.chat(
            model=OLLAMA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            options={"temperature": 0.2, "top_p": 0.9},
        )
        text = ((resp.get("message") or {}).get("content") or "").strip()
        if text:
            return text
    except Exception:
        pass

    # Fallback local simple
    elegido = seleccion[0]
    desc = str(elegido.get("descripcion", "")).strip() or "tu objetivo activo"
    return f"Check rapido: como venis con '{desc}'? Si hoy avanzas un poco, ya suma."


async def job_reporte_diario(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _target_chat_id()
    if chat_id is None:
        return
    try:
        eventos_hoy = await _obtener_eventos_hoy()
        tareas_pendientes = await _obtener_tareas_pendientes()
        uni_items = await _obtener_universidad_activa()

        clases_hoy = _extraer_clases_hoy(uni_items)
        clases_hoy.extend(_extraer_clases_hoy_calendario(eventos_hoy))
        # dedupe conservando orden
        seen_clases = set()
        clases_unique: List[str] = []
        for c in clases_hoy:
            key = c.lower().strip()
            if key and key not in seen_clases:
                seen_clases.add(key)
                clases_unique.append(c)
        alertas_7d: List[str] = []
        alertas_7d.extend(_extraer_alertas_universidad_7d(uni_items))
        alertas_7d.extend(_extraer_alertas_tareas_7d(tareas_pendientes))
        alertas_7d.extend(await _obtener_eventos_calendario_alerta_7d())

        # Unifica sin duplicar.
        seen = set()
        alertas_unique: List[str] = []
        for item in alertas_7d:
            key = item.lower().strip()
            if key and key not in seen:
                seen.add(key)
                alertas_unique.append(item)

        reporte = _armar_reporte_diario(
            eventos_hoy=eventos_hoy,
            tareas_pendientes=tareas_pendientes,
            clases_hoy=clases_unique,
            alertas_7d=alertas_unique,
        )
        sent = await context.bot.send_message(chat_id=chat_id, text=reporte)
        logging.info("[scheduler] reporte diario enviado chat_id=%s message_id=%s", chat_id, getattr(sent, "message_id", None))
    except Exception as exc:
        logging.exception("[scheduler] error en reporte diario")
        await context.bot.send_message(chat_id=chat_id, text=f"Error en reporte diario: {exc}")


async def job_nudge_objetivos(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _target_chat_id()
    if chat_id is None:
        return

    # Martes(1) y Viernes(4), salvo ejecucion de test forzada con job.data={"force": True}.
    force = False
    if getattr(context, "job", None) is not None:
        data = getattr(context.job, "data", None)
        if isinstance(data, dict):
            force = bool(data.get("force"))
    weekday = datetime.datetime.now(TIMEZONE).weekday()
    if not force and weekday not in (1, 4):
        return

    try:
        objetivos = await listar_objetivos_proyectos(solo_activos=True)
        if not objetivos:
            logging.info("[scheduler] nudge omitido: sin objetivos activos")
            return
        msg = await _generar_nudge_ollama(objetivos)
        if msg.strip():
            sent = await context.bot.send_message(chat_id=chat_id, text=msg.strip())
            logging.info("[scheduler] nudge enviado chat_id=%s message_id=%s", chat_id, getattr(sent, "message_id", None))
    except Exception as exc:
        logging.exception("[scheduler] error en nudge objetivos")
        await context.bot.send_message(chat_id=chat_id, text=f"Error en nudge de objetivos: {exc}")
