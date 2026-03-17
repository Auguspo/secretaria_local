from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import aiosqlite

DB_PATH = Path(__file__).with_name("cerebro.db")

EstadoTarea = Literal["Pendiente", "Completada"]
EstadoObjetivo = Literal["Activo", "Pausado", "Logrado"]


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
                fecha_creacion TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.commit()


async def agregar_tarea_suelta(texto: str, estado: EstadoTarea = "Pendiente") -> int:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO tareas_sueltas (texto, estado)
            VALUES (?, ?)
            """,
            (texto.strip(), estado),
        )
        await db.commit()
        return cursor.lastrowid


async def listar_tareas_sueltas(solo_activas: bool = False) -> list[dict[str, Any]]:
    await init_db()
    query = "SELECT id, texto, estado, fecha_creacion FROM tareas_sueltas"
    params: tuple[Any, ...] = ()
    if solo_activas:
        query += " WHERE estado = 'Pendiente'"
    query += " ORDER BY id DESC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def cambiar_estado_tarea_suelta(tarea_id: int, estado: EstadoTarea) -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE tareas_sueltas SET estado = ? WHERE id = ?",
            (estado, tarea_id),
        )
        await db.commit()


async def agregar_objetivo_proyecto(descripcion: str, estado: EstadoObjetivo = "Activo") -> int:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO objetivos_proyectos (descripcion, estado)
            VALUES (?, ?)
            """,
            (descripcion.strip(), estado),
        )
        await db.commit()
        return cursor.lastrowid


async def listar_objetivos_proyectos(solo_activos: bool = False) -> list[dict[str, Any]]:
    await init_db()
    query = "SELECT id, descripcion, estado, fecha_creacion FROM objetivos_proyectos"
    params: tuple[Any, ...] = ()
    if solo_activos:
        query += " WHERE estado = 'Activo'"
    query += " ORDER BY id DESC"

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def cambiar_estado_objetivo(objetivo_id: int, estado: EstadoObjetivo) -> None:
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE objetivos_proyectos SET estado = ? WHERE id = ?",
            (estado, objetivo_id),
        )
        await db.commit()


async def snapshot_pendientes() -> dict[str, list[dict[str, Any]]]:
    """Devuelve todo lo activo para el coach proactivo."""
    return {
        "tareas_sueltas": await listar_tareas_sueltas(solo_activas=True),
        "objetivos_proyectos": await listar_objetivos_proyectos(solo_activos=True),
    }
