#!/usr/bin/env python3
"""
main.py - Orquestador del Pipeline ETL Robusto para Transacciones Bancarias.

Flujo:  leer -> limpiar (Int.1) -> integrar (Int.2) -> validar/cuarentena (Int.3)
        -> conciliar conteos -> guardar atómicamente en SQLite (Int.4) -> reportar.

Uso rápido:
    python main.py --demo                   # genera datos SINTÉTICOS y ejecuta todo
    python main.py                          # usa data/transacciones_atm.csv y data/transferencias_digitales.json
    python main.py --help

Códigos de salida: 0 = completado, 1 = completado de forma parcial (alguna fuente omitida),
2 = error fatal (nada se guardó).
"""
from __future__ import annotations

import argparse
import csv
import importlib
import io
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

RAIZ = Path(__file__).resolve().parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

import base_datos  # noqa: E402

log = logging.getLogger("banco_etl")

RUTA_ATM_DEFECTO = RAIZ / "data" / "transacciones_atm.csv"
RUTA_DIGITAL_DEFECTO = RAIZ / "data" / "transferencias_digitales.json"
RUTA_CATALOGO_DEFECTO = RAIZ / "data" / "catalogo_cuentas.csv"
RUTA_DB_DEFECTO = RAIZ / base_datos.NOMBRE_DB_POR_DEFECTO
DIR_REPORTES_DEFECTO = RAIZ / "reports"
DIR_DEMO = RAIZ / "data" / "demo"

# Contrato que deben cumplir los módulos de los integrantes 1-3.
CONTRATO = {
    "limpieza_datos": ("limpiar_registros_atm", "limpiar_registros_digitales"),
    "integracion_datos": ("integrar_fuentes",),
    "validacion_cuarentena": ("validar_transacciones",),
}


# --------------------------------------------------------------------------- #
# Excepciones
# --------------------------------------------------------------------------- #
class PipelineError(Exception):
    """Error fatal del pipeline (no se guardó nada)."""


class ArchivoDefectuosoError(PipelineError):
    """El archivo de entrada no se puede interpretar (vacío, binario, JSON truncado...)."""


class ContratoIncumplidoError(PipelineError):
    """Un módulo no expone las funciones del contrato o devolvió un formato inesperado."""


class ConciliacionError(PipelineError):
    """Los conteos no cuadran (p. ej. un módulo perdió registros)."""


# --------------------------------------------------------------------------- #
# Carga de módulos (reales primero, referencia como respaldo)
# --------------------------------------------------------------------------- #
def cargar_modulos(forzar_referencia: bool = False) -> SimpleNamespace:
    """
    Importa limpieza_datos, integracion_datos y validacion_cuarentena.
    Prefiere los módulos REALES de la raíz del proyecto; si no existen usa
    ``modulos_referencia`` y lo deja registrado en ``origen``.
    """
    modulos: dict = {}
    origen: dict = {}
    for nombre, funciones in CONTRATO.items():
        mod = None
        if not forzar_referencia:
            try:
                mod = importlib.import_module(nombre)
                origen[nombre] = "real"
            except ModuleNotFoundError as exc:
                if exc.name != nombre:   # falla una dependencia interna del módulo real: mostrarlo
                    raise
        if mod is None:
            mod = importlib.import_module(f"modulos_referencia.{nombre}")
            origen[nombre] = "referencia"
        faltan = [f for f in funciones if not callable(getattr(mod, f, None))]
        if faltan:
            raise ContratoIncumplidoError(
                f"El módulo {nombre} ({origen[nombre]}) no expone {faltan}. "
                f"Ver la sección 'Contrato de integración' del README."
            )
        modulos[nombre] = mod
    return SimpleNamespace(
        limpieza=modulos["limpieza_datos"],
        integracion=modulos["integracion_datos"],
        validacion=modulos["validacion_cuarentena"],
        origen=origen,
    )


def _a_lista(obj, contexto: str) -> list[dict]:
    """Acepta list[dict] o un DataFrame de pandas (convertido a registros, NaN -> None)."""
    if hasattr(obj, "to_dict") and hasattr(obj, "columns"):
        obj = obj.to_dict("records")
        obj = [{k: (None if isinstance(v, float) and v != v else v) for k, v in r.items()} for r in obj]
    if not isinstance(obj, list) or any(not isinstance(r, dict) for r in obj):
        raise ContratoIncumplidoError(f"{contexto} debe devolver list[dict] (o DataFrame); recibió {type(obj).__name__}")
    return obj


# --------------------------------------------------------------------------- #
# Lectura defensiva de archivos
# --------------------------------------------------------------------------- #
def _rechazo(fuente: str, original, motivo: str, id_origen=None) -> dict:
    return {"fuente_origen": fuente, "id_origen": id_origen,
            "motivo_rechazo_cuarentena": motivo, "registro_original": original}


def _decodificar(datos: bytes, ruta: Path, advertencias: list[str]) -> str:
    if not datos.strip():
        raise ArchivoDefectuosoError(f"{ruta.name}: el archivo está vacío")
    if b"\x00" in datos:
        raise ArchivoDefectuosoError(f"{ruta.name}: contiene bytes nulos (¿archivo binario o corrupto?)")
    try:
        return datos.decode("utf-8-sig")
    except UnicodeDecodeError:
        advertencias.append(f"{ruta.name}: no es UTF-8; se interpretó como latin-1")
        return datos.decode("latin-1")


def leer_csv(ruta: Path, fuente: str) -> dict:
    """Lee un CSV. Las filas con número de columnas incorrecto van directo a cuarentena."""
    advertencias: list[str] = []
    texto = _decodificar(ruta.read_bytes(), ruta, advertencias)
    lineas = [l for l in texto.splitlines() if not l.lstrip().startswith("#")]
    try:
        lector = csv.DictReader(io.StringIO("\n".join(lineas)))
        if not lector.fieldnames:
            raise ArchivoDefectuosoError(f"{ruta.name}: no tiene encabezado")
        registros, rechazados = [], []
        for fila in lector:
            if None in fila:
                rechazados.append(_rechazo(fuente, {"campos": [fila.get(k) for k in lector.fieldnames if k in fila],
                                                    "extras": fila[None]},
                                           "fila_malformada: más columnas que el encabezado"))
            elif any(v is None for v in fila.values()):
                rechazados.append(_rechazo(fuente, dict(fila),
                                           "fila_malformada: menos columnas que el encabezado"))
            else:
                registros.append(dict(fila))
    except csv.Error as exc:
        raise ArchivoDefectuosoError(f"{ruta.name}: CSV ilegible ({exc})") from exc
    return {"registros": registros, "rechazados": rechazados, "advertencias": advertencias}


def leer_json(ruta: Path, fuente: str) -> dict:
    """Lee un JSON (lista de objetos, o un objeto que contiene una lista)."""
    advertencias: list[str] = []
    texto = _decodificar(ruta.read_bytes(), ruta, advertencias)
    try:
        datos = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ArchivoDefectuosoError(f"{ruta.name}: JSON inválido o truncado ({exc})") from exc
    if isinstance(datos, dict):
        listas = [v for v in datos.values() if isinstance(v, list)]
        if len(listas) != 1:
            raise ArchivoDefectuosoError(f"{ruta.name}: se esperaba una lista de transacciones")
        datos = listas[0]
    if not isinstance(datos, list):
        raise ArchivoDefectuosoError(f"{ruta.name}: se esperaba una lista de transacciones")
    registros, rechazados = [], []
    for elemento in datos:
        if isinstance(elemento, dict):
            registros.append(elemento)
        else:
            rechazados.append(_rechazo(fuente, {"valor": repr(elemento)},
                                       f"fila_malformada: el elemento JSON no es un objeto ({type(elemento).__name__})"))
    if not datos:
        advertencias.append(f"{ruta.name}: la lista está vacía")
    return {"registros": registros, "rechazados": rechazados, "advertencias": advertencias}


def cargar_catalogo(ruta: Path) -> set[str]:
    """Lee el catálogo de cuentas (columna 'cuenta'/'numero_cuenta'/... o la primera columna)."""
    adv: list[str] = []
    texto = _decodificar(ruta.read_bytes(), ruta, adv)
    lineas = [l for l in texto.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    filas = list(csv.reader(lineas))
    if len(filas) < 2:
        raise ArchivoDefectuosoError(f"{ruta.name}: el catálogo no contiene cuentas")
    encabezado = [h.strip().lower() for h in filas[0]]
    idx = next((i for i, h in enumerate(encabezado)
                if h in ("cuenta", "numero_cuenta", "num_cuenta", "id_cuenta", "cuenta_id")), 0)
    cuentas = {f[idx].strip().upper() for f in filas[1:] if len(f) > idx and f[idx].strip()}
    if not cuentas:
        raise ArchivoDefectuosoError(f"{ruta.name}: el catálogo no contiene cuentas")
    return cuentas


def resolver_catalogo(ruta: Path | None, permitir_sintetico: bool, catalogo_es_sintetico: bool = False):
    """Devuelve (catalogo|None, origen, advertencias). Nunca presenta uno sintético como original."""
    adv: list[str] = []
    if ruta is not None and Path(ruta).is_file():
        try:
            cat = cargar_catalogo(Path(ruta))
            if catalogo_es_sintetico:
                adv.append("Se usó un catálogo SINTÉTICO: solo válido para pruebas; no verifica cuentas reales.")
                return cat, "SINTETICO", adv
            return cat, "ARCHIVO", adv
        except ArchivoDefectuosoError as exc:
            adv.append(f"Catálogo defectuoso ({exc}).")
    elif ruta is not None:
        adv.append(f"No se encontró el catálogo de cuentas ({Path(ruta).name}).")
    if permitir_sintetico:
        from datos_sinteticos import generar_catalogo
        adv.append("Se usó un catálogo SINTÉTICO: solo válido para pruebas; no verifica cuentas reales.")
        return set(generar_catalogo()), "SINTETICO", adv
    adv.append("LIMITACIÓN: sin catálogo de cuentas no se verifica la existencia de cuentas; "
               "los resultados de validación son incompletos.")
    return None, "NO_DISPONIBLE", adv


# --------------------------------------------------------------------------- #
# Etapas con aislamiento de fallos
# --------------------------------------------------------------------------- #
def _limpiar_aislando(fn, registros: list[dict], fuente: str):
    """
    Llama a la limpieza del lote. Si el módulo lanza una excepción, reintenta registro por
    registro para aislar al culpable (va a cuarentena) sin perder el resto.
    """
    if not registros:
        return [], []
    try:
        salida = _a_lista(fn(registros), f"limpieza ({fuente})")
        if len(salida) != len(registros):
            raise ConciliacionError(
                f"La limpieza de {fuente} devolvió {len(salida)} filas para {len(registros)} de entrada: "
                f"el contrato exige una fila por registro (no descartar).")
        return salida, []
    except ConciliacionError:
        raise
    except Exception as exc:  # módulo upstream falló: aislar
        log.warning("La limpieza de %s lanzó %s; se aísla registro por registro.", fuente, type(exc).__name__)
    limpios, rechazos = [], []
    for reg in registros:
        try:
            out = _a_lista(fn([reg]), f"limpieza ({fuente})")
            if len(out) != 1:
                raise ContratoIncumplidoError("devolvió un número distinto de filas")
            limpios.extend(out)
        except Exception as exc:
            rechazos.append(_rechazo(fuente, reg, f"excepción en limpieza: {type(exc).__name__}: {exc}"))
    return limpios, rechazos


def _completar_rechazo(rej: dict) -> dict:
    rej = dict(rej)
    if not str(rej.get("motivo_rechazo_cuarentena") or "").strip():
        rej["motivo_rechazo_cuarentena"] = "motivo no especificado por el validador"
    if rej.get("registro_original") is None:
        rej["registro_original"] = {k: v for k, v in rej.items() if k != "motivo_rechazo_cuarentena"}
    return rej


def _contar_motivos(rechazados: list[dict]) -> dict:
    c: Counter = Counter()
    for r in rechazados:
        for m in str(r["motivo_rechazo_cuarentena"]).split(";"):
            m = m.strip()
            if m:
                c[m.split(":")[0] if m.startswith(("fila_malformada", "excepción en limpieza")) else m] += 1
    return dict(c.most_common())


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def ejecutar_pipeline(
    ruta_atm=RUTA_ATM_DEFECTO,
    ruta_digital=RUTA_DIGITAL_DEFECTO,
    ruta_catalogo=RUTA_CATALOGO_DEFECTO,
    ruta_db=RUTA_DB_DEFECTO,
    dir_reportes=DIR_REPORTES_DEFECTO,
    usar_catalogo_sintetico: bool = False,
    catalogo_es_sintetico: bool = False,
    estricto: bool = False,
    modulos: SimpleNamespace | None = None,
) -> dict:
    """Ejecuta el pipeline completo y devuelve el resumen (dict). Ver README."""
    t0 = time.perf_counter()
    modulos = modulos or cargar_modulos()
    advertencias: list[str] = []
    fuentes: dict = {}
    rechazados_pre: list[dict] = []   # lectura + limpieza
    crudos: dict = {}

    # 1) Lectura -------------------------------------------------------------
    for nombre, ruta, lector in (("ATM", ruta_atm, leer_csv), ("DIGITAL", ruta_digital, leer_json)):
        info = {"ruta": str(ruta), "estado": "OK", "leidos": 0, "rechazados_lectura": 0, "error": None}
        try:
            p = Path(ruta)
            if not p.is_file():
                raise ArchivoDefectuosoError(f"{p.name}: archivo no encontrado")
            res = lector(p, nombre)
            crudos[nombre] = res["registros"]
            rechazados_pre.extend(res["rechazados"])
            advertencias.extend(res["advertencias"])
            info["leidos"] = len(res["registros"]) + len(res["rechazados"])
            info["rechazados_lectura"] = len(res["rechazados"])
        except ArchivoDefectuosoError as exc:
            info.update(estado="OMITIDA", error=str(exc))
            crudos[nombre] = []
            msg = f"Fuente {nombre} omitida: {exc}"
            if estricto:
                raise PipelineError(msg) from exc
            advertencias.append(msg)
            log.error(msg)
        fuentes[nombre] = info

    if all(f["estado"] == "OMITIDA" for f in fuentes.values()):
        raise PipelineError("Ninguna fuente de entrada pudo leerse: " +
                            "; ".join(f["error"] for f in fuentes.values()))
    parcial = any(f["estado"] == "OMITIDA" for f in fuentes.values())

    # 2) Catálogo ------------------------------------------------------------
    catalogo, origen_catalogo, adv_cat = resolver_catalogo(
        Path(ruta_catalogo) if ruta_catalogo else None, usar_catalogo_sintetico, catalogo_es_sintetico)
    advertencias.extend(adv_cat)

    # 3) Limpieza -> integración -> validación --------------------------------
    atm_l, rech = _limpiar_aislando(modulos.limpieza.limpiar_registros_atm, crudos["ATM"], "ATM")
    rechazados_pre.extend(rech)
    dig_l, rech = _limpiar_aislando(modulos.limpieza.limpiar_registros_digitales, crudos["DIGITAL"], "DIGITAL")
    rechazados_pre.extend(rech)

    try:
        integrados = _a_lista(modulos.integracion.integrar_fuentes(atm_l, dig_l), "integración")
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Falló integracion_datos: {type(exc).__name__}: {exc}") from exc
    if len(integrados) != len(atm_l) + len(dig_l):
        raise ConciliacionError(
            f"La integración devolvió {len(integrados)} filas para {len(atm_l) + len(dig_l)} de entrada.")

    try:
        validos, rech_val = modulos.validacion.validar_transacciones(integrados, catalogo)
        validos, rech_val = _a_lista(validos, "validación (válidos)"), _a_lista(rech_val, "validación (rechazados)")
    except PipelineError:
        raise
    except Exception as exc:
        raise PipelineError(f"Falló validacion_cuarentena: {type(exc).__name__}: {exc}") from exc
    if len(validos) + len(rech_val) != len(integrados):
        raise ConciliacionError(
            f"La validación devolvió {len(validos)} válidos + {len(rech_val)} rechazados "
            f"para {len(integrados)} de entrada.")
    rechazados = rechazados_pre + [_completar_rechazo(r) for r in rech_val]

    # 4) Filtro de persistibilidad (segunda línea de defensa) -----------------
    validos_ok = []
    for reg in validos:
        try:
            base_datos.normalizar_fila_limpia(reg)
            validos_ok.append(reg)
        except base_datos.RegistroInvalidoError as exc:
            rechazados.append(_rechazo(reg.get("fuente_origen"), reg.get("registro_original", reg),
                                       f"no persistible: {exc}", reg.get("id_origen")))
    validos = validos_ok

    # 5) Conciliación previa a guardar ---------------------------------------
    leidos = sum(f["leidos"] for f in fuentes.values())
    if leidos != len(validos) + len(rechazados):
        raise ConciliacionError(
            f"Conteos no cuadran: leídos={leidos}, válidos={len(validos)}, rechazados={len(rechazados)}.")

    # 6) Persistencia atómica ------------------------------------------------
    ruta_db = Path(ruta_db)
    ruta_db.parent.mkdir(parents=True, exist_ok=True)
    conn = base_datos.conectar(ruta_db)
    try:
        base_datos.inicializar_db(conn)
        id_corrida = base_datos.iniciar_corrida(conn, origen_catalogo)
        base = {"leidos": leidos, "validos": len(validos), "rechazados": len(rechazados)}
        try:
            cont = base_datos.guardar_lote(conn, id_corrida, validos, rechazados)
        except base_datos.ErrorPersistencia as exc:
            base_datos.cerrar_corrida(conn, id_corrida, "FALLIDA", base, str(exc))
            raise PipelineError(f"Error al guardar; el lote fue revertido (rollback): {exc}") from exc
        base_datos.cerrar_corrida(conn, id_corrida, "COMPLETADA", {**base, **cont})
        totales = base_datos.totales_db(conn)
    finally:
        conn.close()

    resumen = {
        "id_corrida": id_corrida,
        "fecha_ejecucion": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duracion_segundos": round(time.perf_counter() - t0, 3),
        "estado": "COMPLETADA_PARCIAL" if parcial else "COMPLETADA",
        "fuentes": fuentes,
        "leidos": leidos,
        "validos": len(validos),
        "rechazados": len(rechazados),
        "rechazados_en_lectura_y_limpieza": len(rechazados_pre),
        "almacenados_limpias": cont["insertados_limpias"],
        "duplicados_omitidos": cont["duplicados_limpias"],
        "conflictos": cont["conflictos_limpias"],
        "almacenados_cuarentena": cont["insertados_cuarentena"],
        "duplicados_cuarentena_omitidos": cont["duplicados_cuarentena"],
        "totales_en_bd": totales,
        "motivos_rechazo": _contar_motivos(rechazados),
        "catalogo": {"origen": origen_catalogo, "cuentas": None if catalogo is None else len(catalogo)},
        "modulos": modulos.origen,
        "base_de_datos": str(ruta_db),
        "advertencias": advertencias,
    }
    if any(v == "referencia" for v in modulos.origen.values()):
        resumen["advertencias"].append(
            "Se usaron módulos de REFERENCIA para: " +
            ", ".join(k for k, v in modulos.origen.items() if v == "referencia") +
            ". Sustituir por los módulos reales de los integrantes 1-3.")
    resumen["archivos_reporte"] = escribir_reportes(resumen, Path(dir_reportes))
    return resumen


# --------------------------------------------------------------------------- #
# Reportes
# --------------------------------------------------------------------------- #
def renderizar_markdown(r: dict) -> str:
    l = [
        f"# Reporte del pipeline ETL - corrida {r['id_corrida']}", "",
        f"- Fecha (UTC): {r['fecha_ejecucion']}",
        f"- Estado: **{r['estado']}**  |  Duración: {r['duracion_segundos']} s",
        f"- Base de datos: `{r['base_de_datos']}`",
        f"- Catálogo de cuentas: **{r['catalogo']['origen']}**"
        + (f" ({r['catalogo']['cuentas']} cuentas)" if r['catalogo']['cuentas'] else ""),
        "- Módulos: " + ", ".join(f"{k}={v}" for k, v in r["modulos"].items()), "",
        "## Resumen de conteos", "",
        "| Concepto | Cantidad |", "|---|---:|",
        f"| Registros leídos | {r['leidos']} |",
        f"| Válidos | {r['validos']} |",
        f"| Rechazados (cuarentena) | {r['rechazados']} |",
        f"| Almacenados en `transacciones_limpias` (nuevos) | {r['almacenados_limpias']} |",
        f"| Válidos omitidos por ya existir (idempotencia) | {r['duplicados_omitidos']} |",
        f"| Conflictos (mismo id, contenido distinto) | {r['conflictos']} |",
        f"| Almacenados en `cuarentena_errores` (nuevos) | {r['almacenados_cuarentena']} |",
        f"| Rechazados omitidos por ya existir | {r['duplicados_cuarentena_omitidos']} |", "",
        "Control: leídos = válidos + rechazados -> "
        f"{r['leidos']} = {r['validos']} + {r['rechazados']}", "",
        "## Total acumulado en la base de datos", "",
        "| Tabla | Filas |", "|---|---:|",
    ]
    l += [f"| `{t}` | {n} |" for t, n in r["totales_en_bd"].items()]
    l += ["", "## Fuentes", "", "| Fuente | Estado | Leídos | Rechazados en lectura |", "|---|---|---:|---:|"]
    l += [f"| {n} | {f['estado']} | {f['leidos']} | {f['rechazados_lectura']} |" for n, f in r["fuentes"].items()]
    l += ["", "## Motivos de rechazo", ""]
    l += [f"- {m}: {n}" for m, n in r["motivos_rechazo"].items()] or ["- (ninguno)"]
    l += ["", "## Advertencias y limitaciones", ""]
    l += [f"- {a}" for a in r["advertencias"]] or ["- (ninguna)"]
    return "\n".join(l) + "\n"


def escribir_reportes(resumen: dict, directorio: Path) -> list[str]:
    directorio.mkdir(parents=True, exist_ok=True)
    rutas = []
    base = directorio / f"resumen_corrida_{resumen['id_corrida']}"
    rutas.append(base.with_suffix(".json"))
    rutas.append(base.with_suffix(".md"))
    # json primero sin la clave de rutas (se agrega después en memoria)
    rutas[0].write_text(json.dumps({k: v for k, v in resumen.items() if k != "archivos_reporte"},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    rutas[1].write_text(renderizar_markdown(resumen), encoding="utf-8")
    return [str(r) for r in rutas]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Pipeline ETL bancario con zona de cuarentena (SQLite).")
    p.add_argument("--atm", default=str(RUTA_ATM_DEFECTO), help="CSV de transacciones ATM")
    p.add_argument("--digital", default=str(RUTA_DIGITAL_DEFECTO), help="JSON de transferencias digitales")
    p.add_argument("--catalogo", default=str(RUTA_CATALOGO_DEFECTO), help="CSV del catálogo de cuentas (original)")
    p.add_argument("--db", default=str(RUTA_DB_DEFECTO), help="ruta de la base SQLite")
    p.add_argument("--reportes", default=str(DIR_REPORTES_DEFECTO), help="carpeta de reportes")
    p.add_argument("--catalogo-sintetico", action="store_true",
                   help="SOLO PRUEBAS: usa un catálogo sintético si no hay catálogo original")
    p.add_argument("--demo", action="store_true",
                   help="genera datos SINTÉTICOS en data/demo/ y ejecuta el pipeline con ellos")
    p.add_argument("--estricto", action="store_true", help="aborta si alguna fuente no se puede leer")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    kwargs = dict(ruta_atm=args.atm, ruta_digital=args.digital, ruta_catalogo=args.catalogo,
                  ruta_db=args.db, dir_reportes=args.reportes,
                  usar_catalogo_sintetico=args.catalogo_sintetico, estricto=args.estricto)
    if args.demo:
        from datos_sinteticos import AVISO_SINTETICO, crear_dataset
        ds = crear_dataset(DIR_DEMO)
        log.warning(AVISO_SINTETICO)
        kwargs.update(ruta_atm=ds["ruta_atm"], ruta_digital=ds["ruta_digital"],
                      ruta_catalogo=ds["ruta_catalogo"], catalogo_es_sintetico=True)
    try:
        r = ejecutar_pipeline(**kwargs)
    except PipelineError as exc:
        log.error("PIPELINE ABORTADO: %s", exc)
        return 2
    except Exception:
        log.exception("Error inesperado")
        return 2
    print(renderizar_markdown(r))
    print("Reportes:", ", ".join(r["archivos_reporte"]))
    return 1 if r["estado"] == "COMPLETADA_PARCIAL" else 0


if __name__ == "__main__":
    sys.exit(main())
