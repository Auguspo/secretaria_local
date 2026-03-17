import datetime
import logging
import re
from typing import Optional, Tuple, Dict, Any

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from calendar_api import create_event, daily_agenda_text, delete_event_by_name, list_events
from config import ID_SEGUNDO_CALENDARIO, MI_CHAT_ID, TELEGRAM_TOKEN, TIMEZONE
from database import agregar_objetivo_proyecto, agregar_tarea_suelta, init_db, snapshot_pendientes


def _extraer_campos(texto: str, clave: str) -> Optional[str]:
    match = re.search(rf"{clave}:\s*(.+)", texto)
    return match.group(1).strip() if match else None


def _parsear_comando_local(texto: str) -> Tuple[str, Dict[str, Any]]:
    t = texto.strip().lower()

    if any(x in t for x in ("leer lo pendiente", "leer pendientes", "leer db", "ver pendientes", "mostrar pendientes")):
        return "LEER_DB", {}

    if any(x in t for x in ("nuevo objetivo", "crear objetivo", "agregar objetivo", "anotar objetivo", "guardar objetivo")):
        contenido = re.sub(
            r"^(nuevo objetivo|crear objetivo|agregar objetivo|anotar objetivo|guardar objetivo)\s*[:\-]?\s*",
            "",
            texto.strip(),
            flags=re.IGNORECASE,
        )
        return "NUEVO_OBJETIVO", {"TEXTO": contenido or texto.strip()}

    if any(x in t for x in ("nueva tarea", "crear tarea", "agregar tarea", "anotar tarea", "guardar tarea", "recordar")):
        contenido = re.sub(
            r"^(nueva tarea|crear tarea|agregar tarea|anotar tarea|guardar tarea|recordar)\s*[:\-]?\s*",
            "",
            texto.strip(),
            flags=re.IGNORECASE,
        )
        return "NUEVA_TAREA", {"TEXTO": contenido or texto.strip()}

    if any(x in t for x in ("crear evento", "agendar evento", "programar evento")):
        # Formato simple: "crear evento Nombre | inicio | fin"
        m = re.search(
            r"(?:crear|agendar|programar)\s+evento\s+(.+?)\s*\|\s*(.+?)\s*\|\s*(.+)",
            texto,
            flags=re.IGNORECASE,
        )
        if m:
            return "CREAR", {"EVENTO": m.group(1).strip(), "INICIO": m.group(2).strip(), "FIN": m.group(3).strip()}

        # Formato alternativo: "crear evento Nombre desde X hasta Y"
        m = re.search(
            r"(?:crear|agendar|programar)\s+evento\s+(.+?)\s+desde\s+(.+?)\s+hasta\s+(.+)",
            texto,
            flags=re.IGNORECASE,
        )
        if m:
            return "CREAR", {"EVENTO": m.group(1).strip(), "INICIO": m.group(2).strip(), "FIN": m.group(3).strip()}

        return "CREAR", {}

    if any(x in t for x in ("borrar evento", "cancelar evento", "eliminar evento")):
        contenido = re.sub(
            r"^(borrar|cancelar|eliminar)\s+evento\s*[:\-]?\s*",
            "",
            texto.strip(),
            flags=re.IGNORECASE,
        )
        return "BORRAR", {"EVENTO": contenido or texto.strip()}

    if "leer eventos" in t or "ver eventos" in t:
        return "LISTAR", {}

    return "DESCONOCIDO", {}


def _formatear_snapshot(pendientes: dict[str, list[dict[str, Any]]]) -> str:
    tareas = pendientes["tareas_sueltas"]
    objetivos = pendientes["objetivos_proyectos"]

    if not tareas and not objetivos:
        return "Tu Life OS esta vacio."

    lines = ["Pendientes actuales:", ""]
    if objetivos:
        lines.append("Objetivos:")
        for o in objetivos[:10]:
            lines.append(f"- [{o['id']}] {o['descripcion']} ({o['estado']})")
    if tareas:
        lines.append("")
        lines.append("Tareas:")
        for t in tareas[:10]:
            lines.append(f"- [{t['id']}] {t['texto']} ({t['estado']})")
    return "\n".join(lines)


def _coach_local(pendientes: dict[str, list[dict[str, Any]]]) -> str:
    tareas = pendientes["tareas_sueltas"]
    objetivos = pendientes["objetivos_proyectos"]

    if not tareas and not objetivos:
        return "Hoy no tenes pendientes visibles. Aprovecha para cerrar el dia en limpio."

    partes = ["Recordatorio rapido: no dejes enfriar tus proyectos."]
    if objetivos:
        top_obj = objetivos[0]["descripcion"]
        partes.append(f"Tu foco principal deberia ser: {top_obj}.")
    if tareas:
        top_tarea = tareas[0]["texto"]
        partes.append(f"Si queres avanzar sin pensar demasiado, empezaria por: {top_tarea}.")
    partes.append("Con 20 minutos de foco ya moves la aguja.")
    return " ".join(partes)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    await update.message.reply_text("Hola Augusto. El Life OS local ya esta listo.")


async def resumen_diario(context: ContextTypes.DEFAULT_TYPE):
    if not MI_CHAT_ID:
        return
    try:
        texto = daily_agenda_text()
        await context.bot.send_message(chat_id=MI_CHAT_ID, text=texto)
    except Exception as e:
        print(f"Error en resumen diario: {e}")


async def coach_proactivo(context: ContextTypes.DEFAULT_TYPE):
    if not MI_CHAT_ID:
        return

    try:
        pendientes = await snapshot_pendientes()
        tareas = pendientes["tareas_sueltas"]
        objetivos = pendientes["objetivos_proyectos"]
        if not tareas and not objetivos:
            return
        await context.bot.send_message(chat_id=MI_CHAT_ID, text=_coach_local(pendientes))
    except Exception as e:
        print(f"Error en coach proactivo: {e}")


async def procesar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto_usuario = update.message.text or ""
    mensaje_espera = await update.message.reply_text("Procesando...")

    try:
        accion, datos = _parsear_comando_local(texto_usuario)

        if accion == "LISTAR":
            inicio = datos.get("INICIO")
            fin = datos.get("FIN")
            if not inicio or not fin:
                await mensaje_espera.edit_text(
                    "Usa: crear evento Nombre | 2026-03-17T10:00:00-03:00 | 2026-03-17T11:00:00-03:00"
                )
                return
            eventos = list_events(inicio, fin)
            if not eventos:
                await mensaje_espera.edit_text("No tenes eventos programados para ese periodo.")
            else:
                texto_final = "Tus eventos para esas fechas:\n\n"
                for ev in eventos:
                    inicio_ev = ev["start"].get("dateTime", ev["start"].get("date"))
                    fecha_legible = f"{inicio_ev[:10]} a las {inicio_ev[11:16]}" if "T" in inicio_ev else inicio_ev
                    prefijo = "🟡 [IUA]" if ev.get("_calendar_id") == ID_SEGUNDO_CALENDARIO else "🔵"
                    texto_final += f"{prefijo} {ev['summary']} ({fecha_legible})\n"
                await mensaje_espera.edit_text(texto_final)
            return

        if accion == "CREAR":
            nombre = datos.get("EVENTO")
            inicio = datos.get("INICIO")
            fin = datos.get("FIN")
            if not nombre or not inicio or not fin:
                await mensaje_espera.edit_text(
                    "Usa: crear evento Nombre | 2026-03-17T10:00:00-03:00 | 2026-03-17T11:00:00-03:00"
                )
                return
            create_event(nombre, inicio, fin)
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
            texto = datos.get("TEXTO") or texto_usuario
            tarea_id = await agregar_tarea_suelta(texto)
            await mensaje_espera.edit_text(f"Tarea guardada en Life OS. ID: {tarea_id}")
            return

        if accion == "NUEVO_OBJETIVO":
            texto = datos.get("TEXTO") or texto_usuario
            objetivo_id = await agregar_objetivo_proyecto(texto)
            await mensaje_espera.edit_text(f"Objetivo guardado en Life OS. ID: {objetivo_id}")
            return

        if accion == "LEER_DB":
            pendientes = await snapshot_pendientes()
            await mensaje_espera.edit_text(_formatear_snapshot(pendientes))
            return

        # Fallback conversacional sin IA
        if "tarea" in texto_usuario.lower():
            texto = re.sub(
                r"^(crear|agregar|anotar|guardar)\s+(una\s+)?tarea\s*",
                "",
                texto_usuario.strip(),
                flags=re.IGNORECASE,
            ).strip()
            if texto:
                tarea_id = await agregar_tarea_suelta(texto)
                await mensaje_espera.edit_text(f"Tarea guardada en Life OS. ID: {tarea_id}")
                return

        if "objetivo" in texto_usuario.lower() or "proyecto" in texto_usuario.lower():
            texto = re.sub(
                r"^(crear|agregar|anotar|guardar)\s+(un\s+)?(objetivo|proyecto)\s*",
                "",
                texto_usuario.strip(),
                flags=re.IGNORECASE,
            ).strip()
            if texto:
                objetivo_id = await agregar_objetivo_proyecto(texto)
                await mensaje_espera.edit_text(f"Objetivo guardado en Life OS. ID: {objetivo_id}")
                return

        if "pendiente" in texto_usuario.lower():
            pendientes = await snapshot_pendientes()
            await mensaje_espera.edit_text(_formatear_snapshot(pendientes))
            return

        await mensaje_espera.edit_text(
            "Sin IA puedo entender: crear tarea, crear objetivo, leer pendientes, crear evento, borrar evento."
        )
    except Exception as e:
        await mensaje_espera.edit_text(f"Error de sistema: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Excepcion no manejada", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        try:
            await update.effective_chat.send_message("Ocurrio un error de red o timeout. Reintentalo en unos segundos.")
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_mensaje))
    app.add_error_handler(error_handler)

    hora_alarma = datetime.time(hour=10, minute=0, tzinfo=TIMEZONE)
    app.job_queue.run_daily(resumen_diario, time=hora_alarma)
    app.job_queue.run_daily(coach_proactivo, time=datetime.time(hour=10, minute=0, tzinfo=TIMEZONE), days=(0, 3))

    print("=======================================")
    print(" SECRETARIA VIRTUAL v6.0 (Life OS)    ")
    print("=======================================")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
