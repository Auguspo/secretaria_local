from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import aiosqlite

DB_PATH = Path(__file__).with_name("cerebro.db")

DEFAULT_TASK_STATE = "Pendiente"
DEFAULT_OBJECTIVE_STATE = "Activo"
DEFAULT_UNI_STATE = "Pendiente"


async def init_db() -> None:
    """Crea la base local si no existe."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tareas_sueltas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                texto TEXT NOT NULL,
                estado TEXT NOT NULL DEFAULT 'Pendiente',
                fecha_evento TEXT,
                calendar_event_id TEXT,
                fecha_creacion TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS objetivos_proyectos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                descripcion TEXT NOT NULL,
                estado TEXT NOT NULL DEFAULT 'Activo',
                fecha_evento TEXT,
                calendar_event_id TEXT,
                fecha_creacion TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS universidad (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                materia TEXT,
                tipo TEXT NOT NULL DEFAULT 'Entrega',
                descripcion TEXT,
                fecha_evento TEXT,
                estado TEXT NOT NULL DEFAULT 'Pendiente',
                calendar_event_id TEXT,
                fecha_creacion TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await _ensure_column(db, "tareas_sueltas", "fecha_evento", "TEXT")
        await _ensure_column(db, "tareas_sueltas", "calendar_event_id", "TEXT")
        await _ensure_column(db, "objetivos_proyectos", "fecha_evento", "TEXT")
        await _ensure_column(db, "objetivos_proyectos", "calendar_event_id", "TEXT")
        await _ensure_column(db, "universidad", "materia", "TEXT")
        await _ensure_column(db, "universidad", "tipo", "TEXT NOT NULL DEFAULT 'Entrega'")
        await _ensure_column(db, "universidad", "descripcion", "TEXT")
        await _ensure_column(db, "universidad", "fecha_evento", "TEXT")
        await _ensure_column(db, "universidad", "estado", "TEXT NOT NULL DEFAULT 'Pendiente'")
        await _ensure_column(db, "universidad", "calendar_event_id", "TEXT")
        await db.commit()


async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
    cursor = await db.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    existing = {row[1] for row in rows}
    if column not in existing:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


async def _fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def agregar_tarea_suelta(
    texto: str,
    estado: str = DEFAULT_TASK_STATE,
    fecha_evento: Optional[str] = None,
    calendar_event_id: Optional[str] = None,
) -> int:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO tareas_sueltas (texto, estado, fecha_evento, calendar_event_id)
            VALUES (?, ?, ?, ?)
            """,
            (texto.strip(), estado, fecha_evento, calendar_event_id),
        )
        await db.commit()
        return cursor.lastrowid


async def listar_tareas_sueltas(solo_activas: bool = False) -> list[dict[str, Any]]:
    query = "SELECT id, texto, estado, fecha_evento, calendar_event_id, fecha_creacion FROM tareas_sueltas"
    params: tuple[Any, ...] = ()
    if solo_activas:
        query += " WHERE estado = 'Pendiente'"
    query += " ORDER BY id DESC"
    return await _fetch_all(query, params)


async def cambiar_estado_tarea_suelta(tarea_id: int, estado: str) -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tareas_sueltas SET estado = ? WHERE id = ?",
            (estado, tarea_id),
        )
        await db.commit()


async def vincular_tarea_suelta(
    tarea_id: int,
    *,
    fecha_evento: Optional[str] = None,
    calendar_event_id: Optional[str] = None,
) -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE tareas_sueltas
            SET fecha_evento = COALESCE(?, fecha_evento),
                calendar_event_id = COALESCE(?, calendar_event_id)
            WHERE id = ?
            """,
            (fecha_evento, calendar_event_id, tarea_id),
        )
        await db.commit()


async def agregar_objetivo_proyecto(
    descripcion: str,
    estado: str = DEFAULT_OBJECTIVE_STATE,
    fecha_evento: Optional[str] = None,
    calendar_event_id: Optional[str] = None,
) -> int:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO objetivos_proyectos (descripcion, estado, fecha_evento, calendar_event_id)
            VALUES (?, ?, ?, ?)
            """,
            (descripcion.strip(), estado, fecha_evento, calendar_event_id),
        )
        await db.commit()
        return cursor.lastrowid


async def listar_objetivos_proyectos(solo_activos: bool = False) -> list[dict[str, Any]]:
    query = "SELECT id, descripcion, estado, fecha_evento, calendar_event_id, fecha_creacion FROM objetivos_proyectos"
    params: tuple[Any, ...] = ()
    if solo_activos:
        query += " WHERE estado = 'Activo'"
    query += " ORDER BY id DESC"
    return await _fetch_all(query, params)


async def cambiar_estado_objetivo(objetivo_id: int, estado: str) -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE objetivos_proyectos SET estado = ? WHERE id = ?",
            (estado, objetivo_id),
        )
        await db.commit()


async def vincular_objetivo_proyecto(
    objetivo_id: int,
    *,
    fecha_evento: Optional[str] = None,
    calendar_event_id: Optional[str] = None,
) -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE objetivos_proyectos
            SET fecha_evento = COALESCE(?, fecha_evento),
                calendar_event_id = COALESCE(?, calendar_event_id)
            WHERE id = ?
            """,
            (fecha_evento, calendar_event_id, objetivo_id),
        )
        await db.commit()


async def agregar_universidad(
    titulo: str,
    materia: str = "",
    tipo: str = "Entrega",
    descripcion: str = "",
    fecha_evento: Optional[str] = None,
    estado: str = DEFAULT_UNI_STATE,
    calendar_event_id: Optional[str] = None,
) -> int:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO universidad (titulo, materia, tipo, descripcion, fecha_evento, estado, calendar_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (titulo.strip(), materia.strip(), tipo.strip(), descripcion.strip(), fecha_evento, estado, calendar_event_id),
        )
        await db.commit()
        return cursor.lastrowid


async def vincular_universidad(
    universidad_id: int,
    *,
    fecha_evento: Optional[str] = None,
    calendar_event_id: Optional[str] = None,
) -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE universidad
            SET fecha_evento = COALESCE(?, fecha_evento),
                calendar_event_id = COALESCE(?, calendar_event_id)
            WHERE id = ?
            """,
            (fecha_evento, calendar_event_id, universidad_id),
        )
        await db.commit()


async def cambiar_estado_universidad(universidad_id: int, estado: str) -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE universidad SET estado = ? WHERE id = ?",
            (estado, universidad_id),
        )
        await db.commit()


async def listar_universidad(solo_activas: bool = False, proximas_dias: Optional[int] = None) -> list[dict[str, Any]]:
    query = "SELECT id, titulo, materia, tipo, descripcion, fecha_evento, estado, calendar_event_id, fecha_creacion FROM universidad"
    filtros = []
    params: list[Any] = []
    if solo_activas:
        filtros.append("estado = ?")
        params.append(DEFAULT_UNI_STATE)
    if proximas_dias is not None:
        filtros.append("fecha_evento IS NOT NULL")
    if filtros:
        query += " WHERE " + " AND ".join(filtros)
    query += " ORDER BY CASE WHEN fecha_evento IS NULL THEN 1 ELSE 0 END, fecha_evento ASC, id DESC"
    return await _fetch_all(query, tuple(params))


async def universidad_random_activa() -> Optional[dict[str, Any]]:
    items = await listar_universidad(solo_activas=True)
    if not items:
        return None
    import random

    return random.choice(items)


async def proximas_universidad(dias: int = 7) -> list[dict[str, Any]]:
    """Devuelve eventos universitarios cuya fecha sea hoy o dentro de los proximos dias."""
    await init_db()
    import datetime as _dt

    ahora = _dt.datetime.now().astimezone()
    inicio = ahora.replace(microsecond=0).isoformat(timespec="seconds")
    limite = (ahora + _dt.timedelta(days=dias)).replace(microsecond=0).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, titulo, materia, tipo, descripcion, fecha_evento, estado, calendar_event_id, fecha_creacion
            FROM universidad
            WHERE estado = ?
              AND fecha_evento IS NOT NULL
              AND fecha_evento >= ?
              AND fecha_evento <= ?
            ORDER BY fecha_evento ASC, id DESC
            """,
            (DEFAULT_UNI_STATE, inicio, limite),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def universidad_vencida_o_proxima(dias: int = 5) -> list[dict[str, Any]]:
    """Devuelve items con fecha ya vencida o dentro de los proximos dias."""
    await init_db()
    import datetime as _dt

    ahora = _dt.datetime.now().astimezone()
    limite = ahora + _dt.timedelta(days=dias)
    items = await listar_universidad(solo_activas=True)
    filtrados: list[dict[str, Any]] = []
    for item in items:
        fecha_raw = item.get("fecha_evento")
        if not fecha_raw:
            continue
        try:
            fecha_dt = _dt.datetime.fromisoformat(fecha_raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                fecha_dt = _dt.datetime.fromisoformat(f"{fecha_raw}T00:00:00").replace(tzinfo=ahora.tzinfo)
            except ValueError:
                continue
        if fecha_dt <= limite:
            filtrados.append(item)
    filtrados.sort(key=lambda x: x.get("fecha_evento") or "")
    return filtrados


async def snapshot_pendientes() -> dict[str, list[dict[str, Any]]]:
    """Devuelve todo lo activo para el coach proactivo."""
    return {
        "tareas_sueltas": await listar_tareas_sueltas(solo_activas=True),
        "objetivos_proyectos": await listar_objetivos_proyectos(solo_activos=True),
        "universidad": await listar_universidad(solo_activas=True),
    }
