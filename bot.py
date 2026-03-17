import datetime
import logging
import random
import re
from collections import deque
from typing import Any, Dict, Optional, Tuple

try:
    from google import genai
except Exception:  # pragma: no cover
    genai = None

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from calendar_api import create_event, daily_agenda_text, delete_event_by_name, list_events
from config import GEMINI_API_KEY, GEMINI_MAX_RPM, ID_SEGUNDO_CALENDARIO, MI_CHAT_ID, TELEGRAM_TOKEN, TIMEZONE, UNI_REMINDER_DAYS, USE_GEMINI_ASSISTANT
from database import (
    agregar_objetivo_proyecto,
    agregar_tarea_suelta,
    agregar_universidad,
    cambiar_estado_objetivo,
    cambiar_estado_tarea_suelta,
    cambiar_estado_universidad,
    init_db,
    listar_objetivos_proyectos,
    listar_tareas_sueltas,
    listar_universidad,
    snapshot_pendientes,
    universidad_random_activa,
    vincular_objetivo_proyecto,
    vincular_tarea_suelta,
    vincular_universidad,
    universidad_vencida_o_proxima,
)
from ia_router import build_priority_instruction, build_router_instruction

COACH_WEEKDAYS = {0, 3}  # Monday and Thursday
AFTERNOON_START_HOUR = 11
AFTERNOON_END_HOUR = 17
GEMINI_DISABLED_FOR_SESSION = False
GEMINI_CALLS_MINUTE = deque(maxlen=max(GEMINI_MAX_RPM, 1) * 2)

HELP_ACTIONS = {
    "help_tarea": "crear tarea comprar pan",
    "help_tarea_fecha": "crear tarea entregar informe | 20/03/26 18:00",
    "help_tarea_done": "completar tarea 1",
    "help_objetivo": "nuevo objetivo terminar la app",
    "help_objetivo_done": "completar objetivo 1",
    "help_uni": "nueva uni parcial algebra | algebra | examen | 25/03/26 08:00",
    "help_final": "nueva final fisica 2 | fisica 2 | final | 10/07/26 08:00",
    "help_uni_done": "completar uni 1",
    "help_leer": "leer db",
    "help_leer_uni": "leer uni",
    "help_evento": "crear evento reunion | 17/03/26 10:00 | 17/03/26 11:00",
    "help_borrar": "borrar evento reunion",
}


def _extraer_campos(texto: str, clave: str) -> Optional[str]:
    match = re.search(rf"{clave}:\s*(.+)", texto, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _strip_prefix(texto: str, prefixes: tuple[str, ...]) -> str:
    pattern = r"^(%s)\s*[:\-]?\s*" % "|".join(re.escape(p) for p in prefixes)
    return re.sub(pattern, "", texto.strip(), flags=re.IGNORECASE).strip()


def _parse_fecha_cruda(valor: str) -> Optional[tuple[str, bool]]:
    texto = valor.strip()
    if not texto:
        return None

    candidatos = (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M",
        "%d/%m/%Y",
        "%d/%m/%y",
    )
    for fmt in candidatos:
        try:
            dt = datetime.datetime.strptime(texto, fmt)
            if fmt == "%Y-%m-%d":
                return dt.date().isoformat(), True
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TIMEZONE)
            return dt.isoformat(), False
        except ValueError:
            continue

    try:
        dt = datetime.datetime.fromisoformat(texto.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TIMEZONE)
        return dt.isoformat(), False
    except ValueError:
        return None


def _normalizar_texto_base(texto: str) -> str:
    reemplazos = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ñ": "n",
        "¿": "",
        "?": "",
    }
    out = texto.lower()
    for a, b in reemplazos.items():
        out = out.replace(a, b)
    return out


def _resolver_fecha_relativa(texto: str) -> Optional[str]:
    normalizado = _normalizar_texto_base(texto)
    dias = {
        "lunes": 0,
        "martes": 1,
        "miercoles": 2,
        "jueves": 3,
        "viernes": 4,
        "sabado": 5,
        "domingo": 6,
    }

    if "semana que viene" in normalizado or "proxima semana" in normalizado:
        hoy = datetime.datetime.now(TIMEZONE).date()
        dias_hasta_lunes = (7 - hoy.weekday()) % 7
        if dias_hasta_lunes == 0:
            dias_hasta_lunes = 7
        inicio_semana_siguiente = hoy + datetime.timedelta(days=dias_hasta_lunes)
        for nombre, weekday in dias.items():
            m = re.search(rf"\b{nombre}\b", normalizado)
            if m:
                fecha = inicio_semana_siguiente + datetime.timedelta(days=weekday)
                return fecha.isoformat()

    for nombre, weekday in dias.items():
        if re.search(rf"\b{nombre}\b", normalizado):
            hoy = datetime.datetime.now(TIMEZONE).date()
            dias_a_sumar = (weekday - hoy.weekday()) % 7
            if dias_a_sumar == 0:
                dias_a_sumar = 7
            fecha = hoy + datetime.timedelta(days=dias_a_sumar)
            return fecha.isoformat()

    return None


def _extraer_texto_y_fecha(contenido: str) -> tuple[str, Optional[str], bool]:
    partes = [p.strip() for p in contenido.split("|") if p.strip()]
    if len(partes) >= 2:
        parsed = _parse_fecha_cruda(partes[-1])
        if parsed:
            fecha_iso, all_day = parsed
            texto = " | ".join(partes[:-1]).strip()
            return texto, fecha_iso, all_day
    return contenido.strip(), None, False


def _normalizar_evento_intervalo(inicio_raw: str, fin_raw: str) -> tuple[Optional[str], Optional[str], bool]:
    inicio_parsed = _parse_fecha_cruda(inicio_raw)
    fin_parsed = _parse_fecha_cruda(fin_raw)
    if not inicio_parsed or not fin_parsed:
        return None, None, False
    inicio_iso, inicio_all_day = inicio_parsed
    fin_iso, fin_all_day = fin_parsed
    all_day = inicio_all_day and fin_all_day
    return inicio_iso, fin_iso, all_day


def _parsear_comando_local(texto: str) -> Tuple[str, Dict[str, Any]]:
    raw = texto.strip()
    low = raw.lower()

    if any(x in low for x in ("leer lo pendiente", "leer pendientes", "leer db", "ver pendientes", "mostrar pendientes")):
        return "LEER_DB", {}

    if any(x in low for x in ("leer uni", "leer universidad", "ver uni", "ver universidad", "mostrar uni", "mostrar universidad")):
        return "LEER_UNI", {}

    if _parece_consulta_universidad(low):
        return "LEER_UNI", {}

    if any(x in low for x in ("leer ", "estudiar ", "repasar ", "hacer ", "rendir ", "preparar ")) and not any(
        x in low for x in ("entrega", "examen", "parcial", "final")
    ):
        return "NUEVA_TAREA", {"TEXTO": raw}

    if (
        re.search(r"\b(para|el|fecha)\b", low)
        and any(x in low for x in ("leer ", "estudiar ", "repasar ", "hacer ", "rendir ", "preparar "))
        and not any(x in low for x in ("entrega", "examen", "parcial", "final"))
        and not any(x in low for x in ("leer uni", "leer universidad", "ver uni", "ver universidad", "mostrar uni", "mostrar universidad"))
    ):
        return "NUEVA_TAREA", {"TEXTO": raw}

    if (
        "semana que viene" in low
        or "proxima semana" in low
        or re.search(r"\b(para|el)\s+(lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo)\b", low)
    ) and not any(x in low for x in ("entrega", "examen", "parcial", "final")) and not any(x in low for x in ("leer uni", "leer universidad", "ver uni", "ver universidad", "mostrar uni", "mostrar universidad")):
        if any(x in low for x in ("leer ", "estudiar ", "repasar ", "hacer ", "rendir ", "preparar ")):
            return "NUEVA_TAREA", {"TEXTO": raw}
        return "NUEVA_UNI", {"TEXTO": raw}

    if any(x in low for x in ("completar uni", "marcar uni", "terminar uni", "finalizar uni", "hecho uni")):
        contenido = re.sub(
            r"^(completar|marcar|terminar|finalizar)\s+uni(?:versidad)?\s*[:\-]?\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        )
        contenido = contenido.replace("hecho uni", "").replace("hecho universidad", "").strip()
        return "COMPLETAR_UNI", {"OBJETIVO": contenido or raw}

    if any(x in low for x in ("completar tarea", "marcar tarea", "terminar tarea", "finalizar tarea", "hecho tarea")):
        contenido = re.sub(
            r"^(completar|marcar|terminar|finalizar)\s+tarea\s*[:\-]?\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        )
        contenido = contenido.replace("hecho tarea", "").strip()
        return "COMPLETAR_TAREA", {"OBJETIVO": contenido or raw}

    if any(x in low for x in ("completar objetivo", "marcar objetivo", "terminar objetivo", "finalizar objetivo", "hecho objetivo")):
        contenido = re.sub(
            r"^(completar|marcar|terminar|finalizar)\s+objetivo\s*[:\-]?\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        )
        contenido = contenido.replace("hecho objetivo", "").strip()
        return "COMPLETAR_OBJETIVO", {"OBJETIVO": contenido or raw}

    if any(x in low for x in ("nueva uni", "agregar uni", "guardar uni", "anotar uni", "nuevo examen", "nueva entrega", "nuevo parcial", "nuevo final", "nueva final", "final")):
        contenido = _strip_prefix(
            raw,
            ("nueva uni", "agregar uni", "guardar uni", "anotar uni", "nuevo examen", "nueva entrega", "nuevo parcial", "nuevo final", "nueva final", "final"),
        )
        return "NUEVA_UNI", {"TEXTO": contenido or raw}

    if any(x in low for x in ("nuevo objetivo", "crear objetivo", "agregar objetivo", "anotar objetivo", "guardar objetivo")):
        contenido = _strip_prefix(
            raw,
            ("nuevo objetivo", "crear objetivo", "agregar objetivo", "anotar objetivo", "guardar objetivo"),
        )
        return "NUEVO_OBJETIVO", {"TEXTO": contenido or raw}

    if any(x in low for x in ("nueva tarea", "crear tarea", "agregar tarea", "anotar tarea", "guardar tarea", "recordar")):
        contenido = _strip_prefix(
            raw,
            ("nueva tarea", "crear tarea", "agregar tarea", "anotar tarea", "guardar tarea", "recordar"),
        )
        return "NUEVA_TAREA", {"TEXTO": contenido or raw}

    if any(x in low for x in ("crear evento", "agendar evento", "programar evento")):
        m = re.search(
            r"(?:crear|agendar|programar)\s+evento\s+(.+?)\s*\|\s*(.+?)\s*\|\s*(.+)",
            raw,
            flags=re.IGNORECASE,
        )
        if m:
            return "CREAR", {"EVENTO": m.group(1).strip(), "INICIO": m.group(2).strip(), "FIN": m.group(3).strip()}

        m = re.search(
            r"(?:crear|agendar|programar)\s+evento\s+(.+?)\s+desde\s+(.+?)\s+hasta\s+(.+)",
            raw,
            flags=re.IGNORECASE,
        )
        if m:
            return "CREAR", {"EVENTO": m.group(1).strip(), "INICIO": m.group(2).strip(), "FIN": m.group(3).strip()}

        return "CREAR", {}

    if any(x in low for x in ("borrar evento", "cancelar evento", "eliminar evento")):
        contenido = _strip_prefix(raw, ("borrar evento", "cancelar evento", "eliminar evento"))
        return "BORRAR", {"EVENTO": contenido or raw}

    if "leer eventos" in low or "ver eventos" in low:
        return "LISTAR", {}

    return "DESCONOCIDO", {}


def _parece_consulta_universidad(low: str) -> bool:
    patrones = (
        r"\bque pendientes tengo\b.*\buni\b",
        r"\bque pendientes tengo\b.*\buniversidad\b",
        r"\bque tengo\b.*\bde la uni\b",
        r"\bque tengo\b.*\bde la universidad\b",
        r"\bque me queda\b.*\bde la uni\b",
        r"\bque me queda\b.*\bde la universidad\b",
        r"\bmostrame\b.*\buni\b",
        r"\bmostrame\b.*\buniversidad\b",
        r"\bleer\b.*\buni\b",
        r"\bleer\b.*\buniversidad\b",
    )
    return any(re.search(p, low) for p in patrones)


def _gemini_client():
    if not USE_GEMINI_ASSISTANT or not GEMINI_API_KEY or genai is None:
        return None
    try:
        return genai.Client(api_key=GEMINI_API_KEY)
    except Exception as exc:
        print(f"No se pudo inicializar Gemini: {exc}")
        return None


def _gemini_puede_llamar() -> bool:
    if GEMINI_MAX_RPM <= 0:
        return False
    ahora = datetime.datetime.now().timestamp()
    ventana = 60.0
    while GEMINI_CALLS_MINUTE and ahora - GEMINI_CALLS_MINUTE[0] > ventana:
        GEMINI_CALLS_MINUTE.popleft()
    return len(GEMINI_CALLS_MINUTE) < GEMINI_MAX_RPM


def _gemini_generate(prompt: str) -> Optional[str]:
    global GEMINI_DISABLED_FOR_SESSION
    if GEMINI_DISABLED_FOR_SESSION:
        return None
    if not _gemini_puede_llamar():
        print("Gemini omitido por limite local por minuto.")
        return None
    client = _gemini_client()
    if client is None:
        return None
    GEMINI_CALLS_MINUTE.append(datetime.datetime.now().timestamp())
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = getattr(response, "text", None)
        if text:
            return text.strip()
    except Exception as exc:
        print(f"Gemini fallo: {exc}")
        if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc):
            GEMINI_DISABLED_FOR_SESSION = True
            print("Gemini deshabilitado por esta sesion debido a quota 429.")
    return None


def _parsear_bloque_gemini(texto: str) -> tuple[str, Dict[str, Any]]:
    match = re.search(r"---COMANDO---\s*(.*?)\s*---FIN---", texto, flags=re.DOTALL | re.IGNORECASE)
    if not match:
        return "DESCONOCIDO", {"RESPUESTA": texto.strip()}
    bloque = match.group(1)
    accion = (_extraer_campos(bloque, "ACCION") or "DESCONOCIDO").upper()
    datos: Dict[str, Any] = {}
    for clave in ("TEXTO", "EVENTO", "INICIO", "FIN", "TIPO", "ITEM_ID", "RAZON", "MATERIA", "DESCRIPCION"):
        valor = _extraer_campos(bloque, clave)
        if valor:
            datos[clave] = valor
    return accion, datos


def _interpretar_con_gemini(texto_usuario: str) -> tuple[str, Dict[str, Any]]:
    prompt = build_router_instruction(texto_usuario)
    salida = _gemini_generate(prompt)
    if not salida:
        return "DESCONOCIDO", {}
    accion, datos = _parsear_bloque_gemini(salida)
    if accion == "DESCONOCIDO" and "RESPUESTA" in datos:
        return "RESPUESTA", datos
    return accion, datos


def _snapshot_to_plain_text(pendientes: dict[str, list[dict[str, Any]]]) -> str:
    lineas = []
    if pendientes["universidad"]:
        lineas.append("Universidad:")
        for item in pendientes["universidad"][:6]:
            fecha = f" | {_formatear_fecha_amigable(str(item['fecha_evento']))}" if item.get("fecha_evento") else ""
            materia = f" | {_title_case_texto(item['materia'])}" if item.get("materia") else ""
            lineas.append(
                f"- {_title_case_texto(item['titulo'])}{materia} [{_title_case_texto(item['tipo'])}] ({_title_case_texto(item['estado'])}){fecha}"
            )
    if pendientes["objetivos_proyectos"]:
        lineas.append("Objetivos:")
        for item in pendientes["objetivos_proyectos"][:6]:
            fecha = f" | {_formatear_fecha_amigable(str(item['fecha_evento']))}" if item.get("fecha_evento") else ""
            lineas.append(f"- {_title_case_texto(item['descripcion'])} ({_title_case_texto(item['estado'])}){fecha}")
    if pendientes["tareas_sueltas"]:
        lineas.append("Tareas:")
        for item in pendientes["tareas_sueltas"][:6]:
            fecha = f" | {_formatear_fecha_amigable(str(item['fecha_evento']))}" if item.get("fecha_evento") else ""
            lineas.append(f"- {_title_case_texto(item['texto'])} ({_title_case_texto(item['estado'])}){fecha}")
    return "\n".join(lineas) if lineas else "Sin pendientes."


def _prioridad_score_item(tipo: str, item: dict[str, Any]) -> tuple[int, int, str]:
    fecha_dt = _parse_iso_suave(str(item.get("fecha_evento", "")))
    if fecha_dt is not None:
        now = datetime.datetime.now(TIMEZONE)
        dias = (fecha_dt.date() - now.date()).days
        if dias < 0:
            bucket = 0
            score = abs(dias)
        elif dias == 0:
            bucket = 1
            score = 0
        elif dias == 1:
            bucket = 2
            score = 0
        else:
            bucket = 3
            score = dias
    else:
        bucket = 4
        score = 999

    tipo_rank = {"UNIVERSIDAD": 0, "TAREA": 1, "OBJETIVO": 2}.get(tipo, 9)
    return bucket * 1000 + tipo_rank * 100 + score, int(item.get("id", 0) or 0), tipo


def _elegir_prioridad_local(pendientes: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    candidatos: list[dict[str, str]] = []

    for item in pendientes["universidad"]:
        candidatos.append(
            {
                "TIPO": "UNIVERSIDAD",
                "ITEM_ID": str(item["id"]),
                "TEXTO": item.get("titulo", ""),
                "RAZON": "es lo mas ligado a la facu y puede tener fecha cercana",
                "_SCORE": _prioridad_score_item("UNIVERSIDAD", item),
            }
        )

    for item in pendientes["objetivos_proyectos"]:
        candidatos.append(
            {
                "TIPO": "OBJETIVO",
                "ITEM_ID": str(item["id"]),
                "TEXTO": item.get("descripcion", ""),
                "RAZON": "mantiene tus proyectos en movimiento",
                "_SCORE": _prioridad_score_item("OBJETIVO", item),
            }
        )

    for item in pendientes["tareas_sueltas"]:
        candidatos.append(
            {
                "TIPO": "TAREA",
                "ITEM_ID": str(item["id"]),
                "TEXTO": item.get("texto", ""),
                "RAZON": "es una accion concreta que destraba avance rapido",
                "_SCORE": _prioridad_score_item("TAREA", item),
            }
        )

    if not candidatos:
        return {"TIPO": "NINGUNO", "ITEM_ID": "", "TEXTO": "", "RAZON": "no hay pendientes activas"}

    candidatos.sort(key=lambda item: item.get("_SCORE", (9999, 9999, "")))
    elegido = candidatos[0]
    elegido.pop("_SCORE", None)
    return elegido


def _elegir_prioridad_con_gemini(pendientes: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    resumen = _snapshot_to_plain_text(pendientes)
    prompt = build_priority_instruction(resumen)
    salida = _gemini_generate(prompt)
    if not salida:
        return _elegir_prioridad_local(pendientes)
    accion, datos = _parsear_bloque_gemini(salida)
    if accion != "PRIORIDAD":
        return _elegir_prioridad_local(pendientes)
    return {
        "TIPO": datos.get("TIPO", "NINGUNO"),
        "ITEM_ID": datos.get("ITEM_ID", ""),
        "TEXTO": datos.get("TEXTO", ""),
        "RAZON": datos.get("RAZON", "prioridad elegida por Gemini"),
    }


def _buscar_item_por_objetivo(items: list[dict[str, Any]], objetivo: str, campos: tuple[str, ...]) -> Optional[dict[str, Any]]:
    objetivo_l = objetivo.strip().lower()
    if not objetivo_l:
        return None
    if objetivo_l.isdigit():
        for item in items:
            if str(item.get("id")) == objetivo_l:
                return item
    for item in items:
        for campo in campos:
            valor = str(item.get(campo, "")).lower()
            if objetivo_l in valor:
                return item
    return None


def _resumen_universidad_item(item: dict[str, Any]) -> str:
    materia = f" de {item['materia']}" if item.get("materia") else ""
    fecha = f" para {_formatear_fecha_amigable(item['fecha_evento'])}" if item.get("fecha_evento") else ""
    return f"{item['titulo']}{materia}{fecha}"


def _sugerir_arranque(tipo: str, texto: str, item: Optional[dict[str, Any]] = None) -> str:
    tipo = (tipo or "").upper()
    texto = _title_case_texto(texto)
    if tipo == "UNIVERSIDAD":
        titulo = _title_case_texto((item or {}).get("titulo", texto))
        materia = _title_case_texto((item or {}).get("materia", ""))
        base = f"Arranc? revisando la consigna de {titulo}"
        if materia:
            base += f" de {materia}"
        return base + " y separando qu? te piden entregar primero."
    if tipo == "TAREA":
        return f"Arranc? haciendo solo el primer paso de {texto} y no intentes resolver todo junto."
    if tipo == "OBJETIVO":
        return f"Arranc? dividiendo {texto} en 3 pasos chiquitos y eleg? el primero para hoy."
    return f"Arranc? por el paso mas chico de {texto}."


async def _marcar_como_hecho(tipo: str, objetivo: str) -> tuple[bool, str]:
    if tipo == "tarea":
        items = await listar_tareas_sueltas(solo_activas=True)
        item = _buscar_item_por_objetivo(items, objetivo, ("texto",))
        if item:
            await cambiar_estado_tarea_suelta(int(item["id"]), "Completada")
            return True, item["texto"]
    if tipo == "objetivo":
        items = await listar_objetivos_proyectos(solo_activos=True)
        item = _buscar_item_por_objetivo(items, objetivo, ("descripcion",))
        if item:
            await cambiar_estado_objetivo(int(item["id"]), "Logrado")
            return True, item["descripcion"]
    if tipo == "uni":
        items = await listar_universidad(solo_activas=True)
        item = _buscar_item_por_objetivo(items, objetivo, ("titulo", "materia", "descripcion"))
        if item:
            await cambiar_estado_universidad(int(item["id"]), "Realizado")
            return True, _resumen_universidad_item(item)
    return False, objetivo


def _help_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Tarea", callback_data="help_tarea"),
            InlineKeyboardButton("Tarea + fecha", callback_data="help_tarea_fecha"),
            InlineKeyboardButton("Hecho tarea", callback_data="help_tarea_done"),
        ],
        [
            InlineKeyboardButton("Objetivo", callback_data="help_objetivo"),
            InlineKeyboardButton("Hecho obj", callback_data="help_objetivo_done"),
            InlineKeyboardButton("Universidad", callback_data="help_uni"),
            InlineKeyboardButton("Final", callback_data="help_final"),
            InlineKeyboardButton("Hecho uni", callback_data="help_uni_done"),
        ],
        [
            InlineKeyboardButton("Leer DB", callback_data="help_leer"),
            InlineKeyboardButton("Leer Uni", callback_data="help_leer_uni"),
        ],
        [
            InlineKeyboardButton("Evento", callback_data="help_evento"),
            InlineKeyboardButton("Borrar", callback_data="help_borrar"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def _help_text() -> str:
    return (
        "Elegi un atajo y te devuelvo el comando listo para copiar o mandar.\n\n"
        "Si queres escribirlo a mano, tambien sirve."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_text(), reply_markup=_help_keyboard())


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    action = query.data or ""
    comando = HELP_ACTIONS.get(action)
    if not comando:
        await query.edit_message_text("No pude preparar ese atajo.")
        return
    await query.edit_message_text(
        f"Comando listo:\n\n`{comando}`\n\nCopialo y mandamelo cuando quieras.",
        reply_markup=_help_keyboard(),
        parse_mode="Markdown",
    )


def _formatear_snapshot(pendientes: dict[str, list[dict[str, Any]]]) -> str:
    tareas = pendientes["tareas_sueltas"]
    objetivos = pendientes["objetivos_proyectos"]
    universidad = pendientes["universidad"]

    if not tareas and not objetivos and not universidad:
        return "Tu Life OS esta vacio."

    lines = ["Pendientes actuales:", ""]
    if objetivos:
        lines.append("Objetivos:")
        for o in sorted(objetivos, key=lambda item: _fecha_sort_key(item.get("fecha_evento"))):
            fecha = f" ({_formatear_fecha_amigable(str(o['fecha_evento']))})" if o.get("fecha_evento") else ""
            lines.append(f"- {_title_case_texto(o['descripcion'])} ({_title_case_texto(o['estado'])}){fecha}")
    if tareas:
        lines.append("")
        lines.append("Tareas:")
        for t in sorted(tareas, key=lambda item: _fecha_sort_key(item.get("fecha_evento"))):
            fecha = f" ({_formatear_fecha_amigable(str(t['fecha_evento']))})" if t.get("fecha_evento") else ""
            lines.append(f"- {_title_case_texto(t['texto'])} ({_title_case_texto(t['estado'])}){fecha}")
    if universidad:
        lines.append("")
        lines.append("Universidad:")
        for u in universidad[:10]:
            fecha = f" ({_formatear_fecha_amigable(str(u['fecha_evento']))})" if u.get("fecha_evento") else ""
            materia = f" - {_title_case_texto(u['materia'])}" if u.get("materia") else ""
            lines.append(f"- {_title_case_texto(u['titulo'])}{materia} [{_title_case_texto(u['tipo'])}] ({_title_case_texto(u['estado'])}){fecha}")
    return "\n".join(lines)


def _formatear_fecha_amigable(fecha_iso: str) -> str:
    if not fecha_iso:
        return "sin fecha"
    try:
        if "T" in fecha_iso:
            dt = datetime.datetime.fromisoformat(fecha_iso)
            return dt.strftime("%d/%m/%Y %H:%M")
        return datetime.datetime.fromisoformat(f"{fecha_iso}T00:00:00").strftime("%d/%m/%Y")
    except ValueError:
        return fecha_iso


def _pick_top(items: list[dict[str, Any]], key: str) -> Optional[dict[str, Any]]:
    return items[0] if items else None


def _title_case_texto(texto: str) -> str:
    texto = str(texto).strip()
    if not texto:
        return texto
    return " ".join(p[:1].upper() + p[1:].lower() if p else p for p in texto.split())


def _fecha_sort_key(fecha_raw: Any) -> tuple[int, str]:
    fecha_dt = _parse_iso_suave(str(fecha_raw))
    if fecha_dt is None:
        return (1, "")
    return (0, fecha_dt.isoformat())


def _parse_iso_suave(fecha_raw: str) -> Optional[datetime.datetime]:
    if not fecha_raw:
        return None
    try:
        fecha_dt = datetime.datetime.fromisoformat(str(fecha_raw).replace("Z", "+00:00"))
        if fecha_dt.tzinfo is None:
            fecha_dt = fecha_dt.replace(tzinfo=TIMEZONE)
        return fecha_dt
    except ValueError:
        try:
            fecha_dt = datetime.datetime.fromisoformat(f"{fecha_raw}T00:00:00").replace(tzinfo=TIMEZONE)
            return fecha_dt
        except ValueError:
            return None


def _resumen_lista(items: list[dict[str, Any]], campos: tuple[str, ...], limite: int = 3) -> list[str]:
    lineas: list[str] = []
    for item in items[:limite]:
        texto = None
        for campo in campos:
            valor = str(item.get(campo, "")).strip()
            if valor:
                texto = valor
                break
        if not texto:
            continue
        texto = _title_case_texto(texto)
        fecha = item.get("fecha_evento")
        if fecha:
            texto += f" ({_formatear_fecha_amigable(str(fecha))})"
        lineas.append(f"- {texto}")
    return lineas


def _resumen_uni_item(item: dict[str, Any]) -> str:
    partes = [_title_case_texto(item.get("titulo", ""))]
    materia = _title_case_texto(item.get("materia", ""))
    if materia:
        partes.append(f"de {materia}")
    fecha = str(item.get("fecha_evento", "")).strip()
    if fecha:
        partes.append(f"para {_formatear_fecha_amigable(fecha)}")
    return " ".join(p for p in partes if p)


async def _crear_evento_calendar(
    summary: str,
    fecha_iso: str,
    *,
    description: str = "",
    calendar_id: str = "primary",
) -> dict[str, Any]:
    if "T" in fecha_iso:
        start = datetime.datetime.fromisoformat(fecha_iso.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=TIMEZONE)
        end = start + datetime.timedelta(hours=1)
        return create_event(
            summary=summary,
            start_value=start.isoformat(),
            end_value=end.isoformat(),
            calendar_id=calendar_id,
            all_day=False,
            description=description,
        )

    end_date = (datetime.date.fromisoformat(fecha_iso) + datetime.timedelta(days=1)).isoformat()
    return create_event(
        summary=summary,
        start_value=fecha_iso,
        end_value=end_date,
        calendar_id=calendar_id,
        all_day=True,
        description=description,
    )


async def _registrar_con_calendar(
    *,
    kind: str,
    title: str,
    fecha_iso: Optional[str],
    extra_description: str = "",
    metadata: Optional[Dict[str, str]] = None,
) -> tuple[int, Optional[dict[str, Any]]]:
    metadata = metadata or {}
    calendar_event = None
    if fecha_iso:
        try:
            if kind == "tarea":
                calendar_title = f"Tarea: {title}"
            elif kind == "objetivo":
                calendar_title = f"Objetivo: {title}"
            elif kind == "uni":
                materia = metadata.get("materia", "").strip()
                tipo = metadata.get("tipo", "Entrega").strip()
                suffix = f" - {materia}" if materia else ""
                calendar_title = f"UNI {tipo}: {title}{suffix}"
            else:
                calendar_title = title
            calendar_event = await _crear_evento_calendar(
                summary=calendar_title,
                fecha_iso=fecha_iso,
                description=extra_description,
            )
        except Exception as exc:
            print(f"No se pudo crear evento de Calendar para {kind}: {exc}")

    if kind == "tarea":
        tarea_id = await agregar_tarea_suelta(
            title,
            fecha_evento=fecha_iso,
            calendar_event_id=(calendar_event or {}).get("id"),
        )
        if calendar_event:
            await vincular_tarea_suelta(
                tarea_id,
                fecha_evento=fecha_iso,
                calendar_event_id=calendar_event.get("id"),
            )
        return tarea_id, calendar_event

    if kind == "objetivo":
        objetivo_id = await agregar_objetivo_proyecto(
            title,
            fecha_evento=fecha_iso,
            calendar_event_id=(calendar_event or {}).get("id"),
        )
        if calendar_event:
            await vincular_objetivo_proyecto(
                objetivo_id,
                fecha_evento=fecha_iso,
                calendar_event_id=calendar_event.get("id"),
            )
        return objetivo_id, calendar_event

    if kind == "uni":
        universidad_id = await agregar_universidad(
            titulo=title,
            materia=metadata.get("materia", ""),
            tipo=metadata.get("tipo", "Entrega"),
            descripcion=metadata.get("descripcion", ""),
            fecha_evento=fecha_iso,
            calendar_event_id=(calendar_event or {}).get("id"),
        )
        if calendar_event:
            await vincular_universidad(
                universidad_id,
                fecha_evento=fecha_iso,
                calendar_event_id=calendar_event.get("id"),
            )
        return universidad_id, calendar_event

    raise ValueError(f"Tipo no soportado: {kind}")


def _parse_universidad_payload(contenido: str) -> dict[str, str]:
    texto = contenido.strip()
    if not texto:
        return {}

    fecha_iso = None
    partes = [p.strip() for p in texto.split("|") if p.strip()]
    if len(partes) >= 2:
        fecha_candidate = _parse_fecha_cruda(partes[-1])
        if fecha_candidate:
            fecha_iso = fecha_candidate[0]
            texto = " | ".join(partes[:-1]).strip()

    if fecha_iso is None:
        m_fecha = re.search(
            r"(?:\bpara\b|\bel\b|\bfecha\b)\s+(.+)$",
            texto,
            flags=re.IGNORECASE,
        )
        if m_fecha:
            fecha_candidate = _parse_fecha_cruda(m_fecha.group(1).strip())
            if fecha_candidate:
                fecha_iso = fecha_candidate[0]
                texto = texto[: m_fecha.start()].strip()
            else:
                fecha_relativa = _resolver_fecha_relativa(m_fecha.group(1).strip())
                if fecha_relativa:
                    fecha_iso = fecha_relativa
                    texto = texto[: m_fecha.start()].strip()

    if fecha_iso is None:
        fecha_relativa = _resolver_fecha_relativa(texto)
        if fecha_relativa:
            fecha_iso = fecha_relativa
            texto = re.sub(
                r"\b(para|el|fecha)\b\s+.*$",
                "",
                texto,
                flags=re.IGNORECASE,
            ).strip()

    tipo = "Entrega"
    for posible in ("entrega", "examen", "parcial", "final", "clase", "tp"):
        if re.search(rf"\b{re.escape(posible)}\b", texto, flags=re.IGNORECASE):
            tipo = posible.capitalize()
            break

    texto_sin_fecha = re.sub(r"\b(para|el|fecha)\b\s+.+$", "", texto, flags=re.IGNORECASE).strip()
    if "|" in texto_sin_fecha:
        partes = [p.strip() for p in texto_sin_fecha.split("|") if p.strip()]
        titulo = partes[0] if partes else texto_sin_fecha
        materia = partes[1] if len(partes) > 1 else ""
        descripcion = " | ".join(partes[2:]) if len(partes) > 2 else ""
    else:
        texto_limpio = re.sub(r"\b(?:nueva|nuevo)\s+(?:uni|universidad|final|examen|parcial|entrega)\b", "", texto_sin_fecha, flags=re.IGNORECASE).strip()
        m_de = re.search(r"(.+?)\s+de\s+(.+)", texto_limpio, flags=re.IGNORECASE)
        if m_de:
            titulo = m_de.group(1).strip()
            materia = m_de.group(2).strip()
            descripcion = ""
        else:
            titulo = texto_limpio
            materia = ""
            descripcion = ""

    titulo = titulo.strip(" -:") if "titulo" in locals() else texto.strip()
    materia = materia.strip(" -:") if "materia" in locals() else ""
    descripcion = descripcion.strip(" -:") if "descripcion" in locals() else ""

    return {
        "titulo": titulo,
        "materia": materia,
        "tipo": tipo,
        "descripcion": descripcion,
        "fecha": fecha_iso or "",
    }


async def _build_proactive_note() -> str:
    pendientes = await snapshot_pendientes()
    uni_proximas = await universidad_vencida_o_proxima(UNI_REMINDER_DAYS)
    prioridad = _elegir_prioridad_con_gemini(pendientes)
    tipo = prioridad.get("TIPO", "NINGUNO").upper()
    texto = prioridad.get("TEXTO", "").strip()
    razon = prioridad.get("RAZON", "").strip()

    if uni_proximas:
        item = uni_proximas[0]
        fecha_raw = item.get("fecha_evento", "")
        fecha_dt = _parse_iso_suave(str(fecha_raw))
        if fecha_dt is None and fecha_raw:
            try:
                fecha_dt = datetime.datetime.fromisoformat(f"{fecha_raw}T00:00:00").replace(tzinfo=TIMEZONE)
            except ValueError:
                fecha_dt = None

        titulo = _title_case_texto(item.get("titulo", ""))
        materia = f" de {_title_case_texto(item.get('materia', ''))}" if item.get("materia") else ""
        if fecha_dt is not None:
            ahora = datetime.datetime.now(TIMEZONE)
            dias = (fecha_dt.date() - ahora.date()).days
            if dias < 0:
                texto_base = f"Prioridad del dia: {titulo}{materia} ya vencio."
            elif dias == 0:
                texto_base = f"Prioridad del dia: {titulo}{materia} vence hoy."
            elif dias == 1:
                texto_base = f"Prioridad del dia: {titulo}{materia} vence manana."
            else:
                texto_base = f"Prioridad del dia: {titulo}{materia} vence en {dias} dias."
        else:
            texto_base = f"Prioridad del dia: {titulo}{materia}."

        if item.get("descripcion"):
            texto_base += f" {item['descripcion']}"
        return texto_base

    if tipo == "NINGUNO" or not texto:
        return "Hoy no veo nada activo. Buen momento para cerrar cosas chicas o respirar un poco."

    if tipo == "UNIVERSIDAD":
        uni = next((u for u in pendientes["universidad"] if str(u["id"]) == prioridad.get("ITEM_ID")), None)
        if uni is None:
            uni = await universidad_random_activa()
        if uni:
            materia = f" de {_title_case_texto(uni.get('materia', ''))}" if uni.get("materia") else ""
            fecha = f" para {_formatear_fecha_amigable(uni['fecha_evento'])}" if uni.get("fecha_evento") else ""
            mensaje = f"Prioridad del dia: {_title_case_texto(uni.get('titulo', ''))}{materia}{fecha}."
            if razon:
                mensaje += f" Gemini lo marco asi: {razon}."
            return mensaje

    if tipo == "OBJETIVO":
        mensaje = f"Prioridad del dia: {_title_case_texto(texto)}."
        if razon:
            mensaje += f" Gemini lo priorizo porque {razon}."
        return mensaje

    if tipo == "TAREA":
        mensaje = f"Prioridad del dia: {_title_case_texto(texto)}."
        if razon:
            mensaje += f" Gemini la priorizo porque {razon}."
        return mensaje

    return f"Prioridad del dia: {_title_case_texto(texto)}. {razon}".strip()


async def _dia_sin_agenda_hoy() -> bool:
    hoy = datetime.datetime.now(TIMEZONE)
    inicio = hoy.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    fin = hoy.replace(hour=23, minute=59, second=59, microsecond=0).isoformat()
    eventos = list_events(inicio, fin)
    return len(eventos) == 0


async def _build_afternoon_nudge() -> str:
    if not await _dia_sin_agenda_hoy():
        return ""

    uni_cercanas = await universidad_vencida_o_proxima(UNI_REMINDER_DAYS)
    tareas = await listar_tareas_sueltas(solo_activas=True)

    lineas = [
        "Si te queda un rato, te conviene mirar esto:",
        "",
    ]

    if uni_cercanas:
        linea_uni = _resumen_uni_item(uni_cercanas[0])
        lineas.append(f"Prioridad uni: {linea_uni}.")
    elif tareas:
        tarea = tareas[0]
        texto_tarea = _title_case_texto(tarea.get('texto', ''))
        lineas.append(f"Prioridad tarea: {texto_tarea}.")

    if len(uni_cercanas) > 1:
        lineas.append("")
        lineas.append("Otra uni cerca:")
        for item in uni_cercanas[1:3]:
            lineas.append(f"- {_resumen_uni_item(item)}")

    if tareas:
        lineas.append("")
        lineas.append("Y para destrabar:")
        lineas.extend(_resumen_lista(tareas, ("texto",), limite=3))

    if not uni_cercanas and not tareas:
        return ""

    return "\n".join(lineas)


def _next_afternoon_target(now: Optional[datetime.datetime] = None) -> datetime.datetime:
    now = now or datetime.datetime.now(TIMEZONE)
    hour = random.randint(AFTERNOON_START_HOUR, AFTERNOON_END_HOUR)
    minute = random.randint(0, 59)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = (now + datetime.timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target


def _schedule_next_afternoon_nudge(job_queue) -> None:
    if job_queue is None:
        return
    target = _next_afternoon_target()
    job_queue.run_once(coach_afternoon, when=target, name="coach_afternoon")


async def coach_afternoon(context: ContextTypes.DEFAULT_TYPE):
    if not MI_CHAT_ID:
        return
    try:
        nota = await _build_afternoon_nudge()
        if nota:
            await context.bot.send_message(chat_id=MI_CHAT_ID, text=nota)
    except Exception as e:
        print(f"Error en coach afternoon: {e}")
    finally:
        _schedule_next_afternoon_nudge(context.job_queue)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    await update.message.reply_text("Hola Augusto. El Life OS local ya esta listo.")


async def resumen_diario(context: ContextTypes.DEFAULT_TYPE):
    if not MI_CHAT_ID:
        return
    try:
        agenda = daily_agenda_text()
        proactivo = await _build_proactive_note()
        await context.bot.send_message(chat_id=MI_CHAT_ID, text=f"{agenda}\n\n{proactivo}")
    except Exception as e:
        print(f"Error en resumen diario: {e}")


async def coach_proactivo(context: ContextTypes.DEFAULT_TYPE):
    if not MI_CHAT_ID:
        return

    try:
        if datetime.datetime.now(TIMEZONE).weekday() not in COACH_WEEKDAYS:
            return
        nota = await _build_proactive_note()
        await context.bot.send_message(chat_id=MI_CHAT_ID, text=nota)
    except Exception as e:
        print(f"Error en coach proactivo: {e}")


async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_usuario = update.message.text or ""
    mensaje_espera = await update.message.reply_text("Procesando...")

    try:
        accion, datos = ("DESCONOCIDO", {})

        if _parece_consulta_universidad(texto_usuario.lower()):
            accion, datos = "LEER_UNI", {}

        if accion == "DESCONOCIDO" and USE_GEMINI_ASSISTANT:
            accion_g, datos_g = _interpretar_con_gemini(texto_usuario)
            if accion_g != "DESCONOCIDO":
                accion, datos = accion_g, datos_g
            elif "RESPUESTA" in datos_g:
                await mensaje_espera.edit_text(datos_g["RESPUESTA"])
                return

        if accion == "DESCONOCIDO":
            accion, datos = _parsear_comando_local(texto_usuario)

        if accion == "LISTAR":
            inicio = datos.get("INICIO")
            fin = datos.get("FIN")
            if not inicio or not fin:
                await mensaje_espera.edit_text(
                    "Usa: crear evento Nombre | 17/03/26 10:00 | 17/03/26 11:00"
                )
                return
            eventos = list_events(inicio, fin)
            if not eventos:
                await mensaje_espera.edit_text("No tenes eventos programados para ese periodo.")
            else:
                texto_final = "Tus eventos para esas fechas:\n\n"
                for ev in eventos:
                    inicio_ev = ev["start"].get("dateTime", ev["start"].get("date"))
                    fecha_legible = _formatear_fecha_amigable(inicio_ev)
                    prefijo = "[IUA]" if ev.get("_calendar_id") == ID_SEGUNDO_CALENDARIO else "[CAL]"
                    texto_final += f"{prefijo} {ev['summary']} ({fecha_legible})\n"
                await mensaje_espera.edit_text(texto_final)
            return

        if accion == "CREAR":
            nombre = datos.get("EVENTO")
            inicio = datos.get("INICIO")
            fin = datos.get("FIN")
            if not nombre or not inicio or not fin:
                await mensaje_espera.edit_text(
                    "Usa: crear evento Nombre | 17/03/26 10:00 | 17/03/26 11:00"
                )
                return
            inicio_norm, fin_norm, all_day = _normalizar_evento_intervalo(inicio, fin)
            if not inicio_norm or not fin_norm:
                await mensaje_espera.edit_text(
                    "No pude leer la fecha. Usa dd/mm/yy o dd/mm/yyyy, con hora opcional."
                )
                return
            create_event(nombre, inicio_norm, fin_norm, all_day=all_day)
            await mensaje_espera.edit_text("Evento agendado.")
            return

        if accion == "BORRAR":
            nombre_buscar = datos.get("EVENTO") or texto_usuario
            ahora_iso = datetime.datetime.now(TIMEZONE).isoformat()
            borrado = delete_event_by_name(nombre_buscar, ahora_iso)
            if borrado:
                await mensaje_espera.edit_text(f"El evento '{nombre_buscar}' ha sido eliminado.")
            else:
                await mensaje_espera.edit_text(f"No encontre ningun evento llamado '{nombre_buscar}'.")
            return

        if accion == "NUEVA_TAREA":
            texto, fecha_iso, _all_day = _extraer_texto_y_fecha(datos.get("TEXTO") or texto_usuario)
            if not texto:
                await mensaje_espera.edit_text("Necesito el texto de la tarea.")
                return
            tarea_id, calendar_event = await _registrar_con_calendar(
                kind="tarea",
                title=texto,
                fecha_iso=fecha_iso,
                extra_description="Tarea del Life OS",
            )
            if calendar_event:
                await mensaje_espera.edit_text("Listo, te lo anote como tarea y lo sincronice con Calendar.")
            else:
                await mensaje_espera.edit_text("Listo, te lo anote como tarea.")
            return

        if accion == "NUEVO_OBJETIVO":
            texto, fecha_iso, _all_day = _extraer_texto_y_fecha(datos.get("TEXTO") or texto_usuario)
            if not texto:
                await mensaje_espera.edit_text("Necesito el texto del objetivo.")
                return
            objetivo_id, calendar_event = await _registrar_con_calendar(
                kind="objetivo",
                title=texto,
                fecha_iso=fecha_iso,
                extra_description="Objetivo del Life OS",
            )
            if calendar_event:
                await mensaje_espera.edit_text("Objetivo guardado y sincronizado con Calendar. Guardado como objetivo.")
            else:
                await mensaje_espera.edit_text("Objetivo guardado en Life OS. Guardado como objetivo.")
            return

        if accion == "NUEVA_UNI":
            payload = _parse_universidad_payload(datos.get("TEXTO") or texto_usuario)
            titulo = payload.get("titulo", "").strip()
            if not titulo:
                await mensaje_espera.edit_text("Necesito un titulo para la materia/examen/entrega.")
                return
            uni_id, calendar_event = await _registrar_con_calendar(
                kind="uni",
                title=titulo,
                fecha_iso=payload.get("fecha") or None,
                extra_description=payload.get("descripcion", ""),
                metadata={"materia": payload.get("materia", ""), "tipo": payload.get("tipo", "Entrega"), "descripcion": payload.get("descripcion", "")},
            )
            if calendar_event:
                await mensaje_espera.edit_text("Listo, te lo anote como item de universidad y lo sincronice con Calendar.")
            else:
                await mensaje_espera.edit_text("Listo, te lo anote como item de universidad.")
            return

        if accion == "LEER_DB":
            pendientes = await snapshot_pendientes()
            await mensaje_espera.edit_text(_formatear_snapshot(pendientes))
            return

        if accion == "LEER_UNI":
            universidad = await listar_universidad(solo_activas=True)
            if not universidad:
                await mensaje_espera.edit_text("No hay items activos de universidad.")
                return
            orden = ["Entrega", "Examen", "Parcial", "Final", "Clase", "Tp"]
            grupos: dict[str, list[dict[str, Any]]] = {tipo: [] for tipo in orden}
            grupos["Otros"] = []
            for item in universidad:
                tipo = _title_case_texto(item.get("tipo", "Otros")) or "Otros"
                if tipo not in grupos or tipo == "Otros":
                    grupos["Otros"].append(item)
                else:
                    grupos[tipo].append(item)
            lineas = ["Universidad activa:", ""]
            for tipo in orden + ["Otros"]:
                items_tipo = sorted(grupos.get(tipo, []), key=lambda item: _fecha_sort_key(item.get("fecha_evento")))
                if not items_tipo:
                    continue
                lineas.append(f"{tipo}:")
                for item in items_tipo[:15]:
                    fecha = f" ({_formatear_fecha_amigable(str(item['fecha_evento']))})" if item.get("fecha_evento") else ""
                    materia = f" de {_title_case_texto(item['materia'])}" if item.get("materia") else ""
                    lineas.append(
                        f"- {_title_case_texto(item['titulo'])}{materia} ({_title_case_texto(item['estado'])}){fecha}"
                    )
                lineas.append("")
            await mensaje_espera.edit_text("\n".join(lineas).strip())
            return

        if accion == "COMPLETAR_TAREA":
            objetivo = datos.get("OBJETIVO") or texto_usuario
            ok, texto_item = await _marcar_como_hecho("tarea", objetivo)
            if ok:
                await mensaje_espera.edit_text(f"Tarea marcada como completada: {texto_item}")
            else:
                await mensaje_espera.edit_text("No encontré esa tarea para marcarla como hecha.")
            return

        if accion == "COMPLETAR_OBJETIVO":
            objetivo = datos.get("OBJETIVO") or texto_usuario
            ok, texto_item = await _marcar_como_hecho("objetivo", objetivo)
            if ok:
                await mensaje_espera.edit_text(f"Objetivo marcado como logrado: {texto_item}")
            else:
                await mensaje_espera.edit_text("No encontré ese objetivo para marcarlo como logrado.")
            return

        if accion == "COMPLETAR_UNI":
            objetivo = datos.get("OBJETIVO") or texto_usuario
            ok, texto_item = await _marcar_como_hecho("uni", objetivo)
            if ok:
                await mensaje_espera.edit_text(f"Universidad marcada como realizada: {texto_item}")
            else:
                await mensaje_espera.edit_text("No encontré ese item de universidad para marcarlo como realizado.")
            return

        if "tarea" in texto_usuario.lower():
            texto = re.sub(
                r"^(crear|agregar|anotar|guardar)\s+(una\s+)?tarea\s*",
                "",
                texto_usuario.strip(),
                flags=re.IGNORECASE,
            ).strip()
            if texto:
                texto, fecha_iso, _all_day = _extraer_texto_y_fecha(texto)
                tarea_id, calendar_event = await _registrar_con_calendar(
                    kind="tarea",
                    title=texto,
                    fecha_iso=fecha_iso,
                    extra_description="Tarea del Life OS",
                )
                if calendar_event:
                    await mensaje_espera.edit_text("Listo, te lo anote como tarea y lo sincronice con Calendar.")
                else:
                    await mensaje_espera.edit_text("Listo, te lo anote como tarea.")
                return

        if "objetivo" in texto_usuario.lower() or "proyecto" in texto_usuario.lower():
            texto = re.sub(
                r"^(crear|agregar|anotar|guardar)\s+(un\s+)?(objetivo|proyecto)\s*",
                "",
                texto_usuario.strip(),
                flags=re.IGNORECASE,
            ).strip()
            if texto:
                texto, fecha_iso, _all_day = _extraer_texto_y_fecha(texto)
                objetivo_id, calendar_event = await _registrar_con_calendar(
                    kind="objetivo",
                    title=texto,
                    fecha_iso=fecha_iso,
                    extra_description="Objetivo del Life OS",
                )
                if calendar_event:
                    await mensaje_espera.edit_text("Objetivo guardado y sincronizado con Calendar. Guardado como objetivo.")
                else:
                    await mensaje_espera.edit_text("Objetivo guardado en Life OS. Guardado como objetivo.")
                return

        if re.search(r"\b(que tengo|qué tengo|pendiente|pendientes|leer uni|leer universidad|ver uni|ver universidad|mostrar uni|mostrar universidad)\b", texto_usuario.lower()):
            universidad = await listar_universidad(solo_activas=True)
            if universidad:
                item = universidad[0]
                fecha = f" ({_formatear_fecha_amigable(item['fecha_evento'])})" if item.get("fecha_evento") else ""
                materia = f" de {item['materia']}" if item.get("materia") else ""
                await mensaje_espera.edit_text(
                    f"Tenes pendiente {item['titulo']}{materia}{fecha}. Si queres, lo guardo mejor con fecha y materia para que te avise solo."
                )
                return

        if "pendiente" in texto_usuario.lower():
            pendientes = await snapshot_pendientes()
            await mensaje_espera.edit_text(_formatear_snapshot(pendientes))
            return

        await mensaje_espera.edit_text(
            "No lo pude entender bien. Reformulalo un poquito o usa /help."
        )
    except Exception as e:
        await mensaje_espera.edit_text(f"Error de sistema: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Excepcion no manejada", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message(
                "Ocurrio un error de red o timeout. Reintentarlo en unos segundos."
            )
        except Exception:
            pass


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .pool_timeout(5)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(help_callback, pattern=r"^help_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje))
    app.add_error_handler(error_handler)

    hora_alarma = datetime.time(hour=10, minute=0, tzinfo=TIMEZONE)
    app.job_queue.run_daily(resumen_diario, time=hora_alarma)
    app.job_queue.run_daily(coach_proactivo, time=datetime.time(hour=10, minute=20, tzinfo=TIMEZONE))
    _schedule_next_afternoon_nudge(app.job_queue)

    print("=======================================")
    print(" SECRETARIA VIRTUAL v6.0 (Life OS)    ")
    print("=======================================")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
