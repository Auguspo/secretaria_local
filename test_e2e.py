import asyncio
import inspect
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


RouterFn = Callable[[str], Awaitable[Any]]


def _contains_date_hint(value: str) -> bool:
    text = value.lower()
    patrones = (
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b\d{1,2}:\d{2}\b",
        r"\b(hs|hora|horas)\b",
        r"\b(lunes|martes|miercoles|miércoles|jueves|viernes|sabado|sábado|domingo)\b",
    )
    return any(re.search(p, text) for p in patrones)


def _normalize_router_output(raw: Any) -> Tuple[str, Dict[str, Any]]:
    if isinstance(raw, tuple) and len(raw) == 2:
        accion = str(raw[0]).upper().strip()
        datos = raw[1] if isinstance(raw[1], dict) else {"RAW": raw[1]}
        return accion, datos

    if isinstance(raw, dict):
        if "ACCION" in raw:
            accion = str(raw.get("ACCION", "DESCONOCIDO")).upper().strip()
            data = {k: v for k, v in raw.items() if k != "ACCION"}
            return accion, data
        return "DESCONOCIDO", {"RAW": raw}

    if isinstance(raw, str):
        m = re.search(r"\bACCION\s*:\s*([A-Z_]+)\b", raw, flags=re.IGNORECASE)
        if m:
            return m.group(1).upper(), {"RAW": raw}
        return "DESCONOCIDO", {"RESPUESTA": raw}

    return "DESCONOCIDO", {"RAW": raw}


def _resolve_router_fn() -> RouterFn:
    # Opcion 1 (nombre sugerido por vos)
    try:
        from bot import obtener_comando_ia  # type: ignore

        if inspect.iscoroutinefunction(obtener_comando_ia):
            return obtener_comando_ia
    except Exception:
        pass

    # Opcion 2 (funcion real actual en este repo)
    try:
        from bot import _interpretar_con_ollama  # type: ignore

        if inspect.iscoroutinefunction(_interpretar_con_ollama):
            return _interpretar_con_ollama
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "No pude importar dependencias del proyecto al cargar bot.py "
            f"(falta modulo: {exc.name}). Activa tu entorno virtual e instala requirements."
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            "No pude resolver una funcion de router async. "
            "Esperaba `obtener_comando_ia` o `_interpretar_con_ollama` en bot.py."
        ) from exc

    raise RuntimeError("No encontre una funcion de router valida.")


def _build_cases() -> List[Dict[str, Any]]:
    return [
        {
            "name": "Conflicto: parcial + fecha",
            "input": "Anotame que el martes rindo el parcial de Sistemas y Organizaciones a las 18hs",
            "expected_actions": {"NUEVA_UNI", "CREAR"},
            "expected_notes": "Calendar / Universidad + Fecha",
            "must_have_date": True,
        },
        {
            "name": "Ambiguedad 1: objetivo mensual running",
            "input": "Mi objetivo de este mes es bajar mis tiempos para correr los 8k",
            "expected_actions": {"NUEVO_OBJETIVO"},
            "expected_notes": "Guardar en objetivos_proyectos",
            "must_have_date": False,
        },
        {
            "name": "Ambiguedad 2: tirada de running",
            "input": "Hoy toca tirada de running en el parque",
            "expected_actions": {"NUEVA_TAREA"},
            "expected_notes": "Guardar en tareas_sueltas",
            "must_have_date": False,
        },
        {
            "name": "Cancelacion hibrida",
            "input": "Cancela lo del entrenamiento de hoy y borra ese evento",
            "expected_actions": {"BORRAR"},
            "expected_notes": "Borrado universal/Calendar",
            "must_have_date": False,
        },
    ]


async def _run_case(router_fn: RouterFn, case: Dict[str, Any]) -> Dict[str, Any]:
    texto = case["input"]
    raw = await router_fn(texto)
    accion, datos = _normalize_router_output(raw)

    ok_action = accion in case["expected_actions"]
    ok_date = True
    if case.get("must_have_date"):
        ok_date = any(
            _contains_date_hint(str(datos.get(k, "")))
            for k in ("INICIO", "FIN", "TEXTO", "RAW", "RESPUESTA")
        )

    status = "PASSED" if (ok_action and ok_date) else "FAILED"
    return {
        "status": status,
        "accion": accion,
        "datos": datos,
        "ok_action": ok_action,
        "ok_date": ok_date,
        "raw": raw,
    }


async def main() -> None:
    print("=== E2E NLP Router Stress Test ===")
    router_fn = _resolve_router_fn()
    cases = _build_cases()

    passed = 0
    failed = 0

    for i, case in enumerate(cases, start=1):
        print(f"\n[{i}] {case['name']}")
        print(f"Input: {case['input']}")
        print(f"Esperado: {case['expected_notes']} | Acciones={sorted(case['expected_actions'])}")
        try:
            result = await _run_case(router_fn, case)
            print(f"Obtenido: ACCION={result['accion']} | DATOS={result['datos']}")
            if case.get("must_have_date"):
                print(f"Chequeo fecha: {'OK' if result['ok_date'] else 'MISSING'}")

            print(f"Resultado: {result['status']}")
            if result["status"] == "PASSED":
                passed += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            print(f"Resultado: FAILED")
            print(f"Error: {type(exc).__name__}: {exc}")

    print("\n=== RESUMEN ===")
    print(f"Total: {len(cases)} | PASSED: {passed} | FAILED: {failed}")


if __name__ == "__main__":
    asyncio.run(main())
