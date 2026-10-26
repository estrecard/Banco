"""
base_datos.py - Capa de persistencia SQLite del pipeline ETL bancario.

Responsabilidades (Integrante 4):
  * Crear ``banco_etl.db`` con las tablas obligatorias ``transacciones_limpias``
    y ``cuarentena_errores`` (más ``corridas_pipeline`` para trazabilidad).
  * Guardar lotes de forma ATÓMICA: o se confirma todo (commit) o nada (rollback).
  * Ser IDEMPOTENTE: reejecutar el pipeline con los mismos datos no duplica filas.

Decisiones de diseño (justificación en REGLAS_DE_NEGOCIO.md y README.md):
  * Dinero como ``INTEGER`` en centavos (``monto_centavos``): evita errores de
    punto flotante (0.1 + 0.2 != 0.3).
  * Identificador determinista ``id_transaccion = "<FUENTE>:<id_origen>"``:
    la misma transacción de origen produce siempre la misma clave primaria.
  * Restricciones ``CHECK`` en la BD como SEGUNDA línea de defensa: aunque un
    módulo upstream falle, la BD no acepta montos <= 0, fuentes desconocidas, etc.
  * Control manual de transacciones (``isolation_level=None`` + ``BEGIN IMMEDIATE``)
    para que el comportamiento de commit/rollback sea explícito y verificable.

Solo usa la biblioteca estándar. Requiere SQLite >= 3.24 (``ON CONFLICT DO NOTHING``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger("banco_etl.db")

NOMBRE_DB_POR_DEFECTO = "banco_etl.db"
FUENTES_VALIDAS = ("ATM", "DIGITAL")
FUENTES_CUARENTENA = FUENTES_VALIDAS + ("DESCONOCIDA",)
TABLAS_CONSULTABLES = ("transacciones_limpias", "cuarentena_errores", "corridas_pipeline")


# --------------------------------------------------------------------------- #
# Excepciones
# --------------------------------------------------------------------------- #
class RegistroInvalidoError(ValueError):
    """El registro no puede representarse en la BD (tipo/valor inválido)."""


class ErrorPersistencia(Exception):
    """Falló el guardado de un lote. El lote completo fue revertido (rollback)."""


# --------------------------------------------------------------------------- #
# Esquema
# --------------------------------------------------------------------------- #
DDL: tuple[str, ...] = (
    # Bitácora de ejecuciones: permite rastrear qué corrida insertó cada fila.
    """
    CREATE TABLE IF NOT EXISTS corridas_pipeline (
        id_corrida            INTEGER PRIMARY KEY AUTOINCREMENT,
        iniciada_en           TEXT    NOT NULL,
        finalizada_en         TEXT,
        estado                TEXT    NOT NULL DEFAULT 'EN_CURSO'
                              CHECK (estado IN ('EN_CURSO','COMPLETADA','FALLIDA')),
        origen_catalogo       TEXT,
        leidos                INTEGER,
        validos               INTEGER,
        rechazados            INTEGER,
        insertados_limpias    INTEGER,
        duplicados_limpias    INTEGER,
        conflictos_limpias    INTEGER,
        insertados_cuarentena INTEGER,
        duplicados_cuarentena INTEGER,
        detalle_error         TEXT
    )
    """,
    # Transacciones que pasaron limpieza, integración y validación.
    """
    CREATE TABLE IF NOT EXISTS transacciones_limpias (
        id_transaccion   TEXT    PRIMARY KEY,
        fuente_origen    TEXT    NOT NULL CHECK (fuente_origen IN ('ATM','DIGITAL')),
        id_origen        TEXT    NOT NULL CHECK (length(trim(id_origen)) > 0),
        fecha_hora       TEXT    NOT NULL CHECK (length(fecha_hora) >= 10),
        cuenta_origen    TEXT    NOT NULL CHECK (length(trim(cuenta_origen)) > 0),
        cuenta_destino   TEXT,
        monto_centavos   INTEGER NOT NULL CHECK (monto_centavos > 0),
        moneda           TEXT    NOT NULL CHECK (length(moneda) = 3),
        tipo_transaccion TEXT    NOT NULL CHECK (length(trim(tipo_transaccion)) > 0),
        estado           TEXT,
        hash_contenido   TEXT    NOT NULL,
        id_corrida       INTEGER NOT NULL REFERENCES corridas_pipeline(id_corrida),
        registrado_en    TEXT    NOT NULL,
        UNIQUE (fuente_origen, id_origen),
        CHECK (id_transaccion = fuente_origen || ':' || id_origen)
    )
    """,
    # Registros rechazados, con el motivo y el registro original completo (JSON).
    """
    CREATE TABLE IF NOT EXISTS cuarentena_errores (
        id_cuarentena             INTEGER PRIMARY KEY AUTOINCREMENT,
        hash_registro             TEXT    NOT NULL UNIQUE,
        fuente_origen             TEXT    NOT NULL
                                  CHECK (fuente_origen IN ('ATM','DIGITAL','DESCONOCIDA')),
        id_origen                 TEXT,
        motivo_rechazo_cuarentena TEXT    NOT NULL
                                  CHECK (length(trim(motivo_rechazo_cuarentena)) > 0),
        registro_original         TEXT    NOT NULL,
        id_corrida                INTEGER NOT NULL REFERENCES corridas_pipeline(id_corrida),
        registrado_en             TEXT    NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_limpias_cuenta ON transacciones_limpias(cuenta_origen)",
    "CREATE INDEX IF NOT EXISTS idx_limpias_corrida ON transacciones_limpias(id_corrida)",
    "CREATE INDEX IF NOT EXISTS idx_cuarentena_origen ON cuarentena_errores(fuente_origen, id_origen)",
)


# --------------------------------------------------------------------------- #
# Conexión y transacciones
# --------------------------------------------------------------------------- #
def conectar(ruta: str | Path = NOMBRE_DB_POR_DEFECTO, timeout: float = 30.0) -> sqlite3.Connection:
    """Abre la BD con claves foráneas activas y control manual de transacciones."""
    conn = sqlite3.connect(str(ruta), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaccion(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """
    Bloque transaccional: COMMIT si termina bien, ROLLBACK ante cualquier excepción.

    ``BEGIN IMMEDIATE`` toma el bloqueo de escritura desde el inicio, de modo que
    dos procesos no se pisan a mitad de un lote.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def inicializar_db(conn: sqlite3.Connection) -> None:
    """Crea las tablas si no existen (idempotente)."""
    with transaccion(conn):
        for sentencia in DDL:
            conn.execute(sentencia)


# --------------------------------------------------------------------------- #
# Utilidades de normalización
# --------------------------------------------------------------------------- #
def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _txt(valor: Any) -> str | None:
    """Texto recortado o None si es nulo/vacío."""
    if valor is None:
        return None
    s = str(valor).strip()
    return s or None


def a_centavos(monto: Any) -> int:
    """
    Convierte un monto a centavos enteros sin pasar por float binario.

    Acepta int, Decimal, str y float. Rechaza None, bool, NaN/inf y montos con
    más de 2 decimales (redondear en silencio ocultaría datos sospechosos).
    """
    if monto is None or isinstance(monto, bool):
        raise RegistroInvalidoError(f"monto no convertible: {monto!r}")
    try:
        d = monto if isinstance(monto, Decimal) else Decimal(str(monto).strip())
    except (InvalidOperation, ValueError) as exc:
        raise RegistroInvalidoError(f"monto no numérico: {monto!r}") from exc
    if not d.is_finite():
        raise RegistroInvalidoError(f"monto no finito: {monto!r}")
    centavos = d * 100
    if centavos != centavos.to_integral_value():
        raise RegistroInvalidoError(f"monto con más de 2 decimales: {monto!r}")
    return int(centavos)


def construir_id_transaccion(fuente: str, id_origen: str) -> str:
    return f"{fuente}:{id_origen}"


def _hash(*partes: Any) -> str:
    return hashlib.sha256("|".join("" if p is None else str(p) for p in partes).encode("utf-8")).hexdigest()


def normalizar_fila_limpia(reg: dict) -> dict:
    """
    Valida que un registro "válido" sea persistible y lo convierte al formato de la tabla.
    Lanza ``RegistroInvalidoError`` si no lo es. Se usa como filtro previo (en main) y
    dentro del guardado (donde un error revierte todo el lote).
    """
    fuente = (_txt(reg.get("fuente_origen")) or "").upper()
    if fuente not in FUENTES_VALIDAS:
        raise RegistroInvalidoError(f"fuente_origen inválida: {reg.get('fuente_origen')!r}")
    id_origen = _txt(reg.get("id_origen"))
    if not id_origen:
        raise RegistroInvalidoError("id_origen ausente")
    fecha = _txt(reg.get("fecha_hora"))
    if not fecha or len(fecha) < 10:
        raise RegistroInvalidoError(f"fecha_hora inválida: {reg.get('fecha_hora')!r}")
    cuenta_origen = _txt(reg.get("cuenta_origen"))
    if not cuenta_origen:
        raise RegistroInvalidoError("cuenta_origen ausente")
    centavos = a_centavos(reg.get("monto"))
    if centavos <= 0:
        raise RegistroInvalidoError(f"monto no positivo: {reg.get('monto')!r}")
    moneda = (_txt(reg.get("moneda")) or "").upper()
    if len(moneda) != 3:
        raise RegistroInvalidoError(f"moneda inválida: {reg.get('moneda')!r}")
    tipo = (_txt(reg.get("tipo_transaccion")) or "").upper()
    if not tipo:
        raise RegistroInvalidoError("tipo_transaccion ausente")
    fila = {
        "id_transaccion": construir_id_transaccion(fuente, id_origen),
        "fuente_origen": fuente,
        "id_origen": id_origen,
        "fecha_hora": fecha,
        "cuenta_origen": cuenta_origen,
        "cuenta_destino": _txt(reg.get("cuenta_destino")),
        "monto_centavos": centavos,
        "moneda": moneda,
        "tipo_transaccion": tipo,
        "estado": _txt(reg.get("estado")),
    }
    fila["hash_contenido"] = _hash(
        fila["fecha_hora"], fila["cuenta_origen"], fila["cuenta_destino"],
        fila["monto_centavos"], fila["moneda"], fila["tipo_transaccion"], fila["estado"],
    )
    return fila


def hash_registro_original(fuente: str, original: Any) -> str:
    """Hash estable (independiente del orden de claves) del registro rechazado."""
    canonico = json.dumps(original, sort_keys=True, ensure_ascii=False, default=str)
    return _hash(fuente, canonico)


def normalizar_fila_cuarentena(rej: dict) -> dict:
    """Prepara un registro rechazado para ``cuarentena_errores``."""
    motivo = _txt(rej.get("motivo_rechazo_cuarentena"))
    if not motivo:
        raise RegistroInvalidoError("motivo_rechazo_cuarentena es obligatorio")
    fuente = (_txt(rej.get("fuente_origen")) or "DESCONOCIDA").upper()
    if fuente not in FUENTES_CUARENTENA:
        fuente = "DESCONOCIDA"
    original = rej.get("registro_original")
    if original is None:
        original = {k: v for k, v in rej.items() if k != "motivo_rechazo_cuarentena"}
    return {
        "hash_registro": hash_registro_original(fuente, original),
        "fuente_origen": fuente,
        "id_origen": _txt(rej.get("id_origen")),
        "motivo_rechazo_cuarentena": motivo,
        "registro_original": json.dumps(original, ensure_ascii=False, sort_keys=True, default=str),
    }


# --------------------------------------------------------------------------- #
# Bitácora de corridas
# --------------------------------------------------------------------------- #
def iniciar_corrida(conn: sqlite3.Connection, origen_catalogo: str | None = None) -> int:
    """Registra el inicio de una corrida (transacción propia, ya confirmada)."""
    with transaccion(conn):
        cur = conn.execute(
            "INSERT INTO corridas_pipeline (iniciada_en, estado, origen_catalogo) VALUES (?, 'EN_CURSO', ?)",
            (_ahora(), origen_catalogo),
        )
        return int(cur.lastrowid)


def cerrar_corrida(
    conn: sqlite3.Connection,
    id_corrida: int,
    estado: str,
    resumen: dict | None = None,
    detalle_error: str | None = None,
) -> None:
    resumen = resumen or {}
    with transaccion(conn):
        conn.execute(
            """UPDATE corridas_pipeline SET finalizada_en=?, estado=?, leidos=?, validos=?, rechazados=?,
                      insertados_limpias=?, duplicados_limpias=?, conflictos_limpias=?,
                      insertados_cuarentena=?, duplicados_cuarentena=?, detalle_error=?
               WHERE id_corrida=?""",
            (
                _ahora(), estado, resumen.get("leidos"), resumen.get("validos"), resumen.get("rechazados"),
                resumen.get("insertados_limpias"), resumen.get("duplicados_limpias"),
                resumen.get("conflictos_limpias"), resumen.get("insertados_cuarentena"),
                resumen.get("duplicados_cuarentena"), detalle_error, id_corrida,
            ),
        )


# --------------------------------------------------------------------------- #
# Guardado atómico e idempotente
# --------------------------------------------------------------------------- #
_SQL_INSERT_LIMPIA = """
    INSERT INTO transacciones_limpias
        (id_transaccion, fuente_origen, id_origen, fecha_hora, cuenta_origen, cuenta_destino,
         monto_centavos, moneda, tipo_transaccion, estado, hash_contenido, id_corrida, registrado_en)
    VALUES
        (:id_transaccion, :fuente_origen, :id_origen, :fecha_hora, :cuenta_origen, :cuenta_destino,
         :monto_centavos, :moneda, :tipo_transaccion, :estado, :hash_contenido, :id_corrida, :registrado_en)
    ON CONFLICT DO NOTHING
"""

_SQL_INSERT_CUARENTENA = """
    INSERT INTO cuarentena_errores
        (hash_registro, fuente_origen, id_origen, motivo_rechazo_cuarentena,
         registro_original, id_corrida, registrado_en)
    VALUES
        (:hash_registro, :fuente_origen, :id_origen, :motivo_rechazo_cuarentena,
         :registro_original, :id_corrida, :registrado_en)
    ON CONFLICT (hash_registro) DO NOTHING
"""


def _insertar_cuarentena(conn: sqlite3.Connection, fila: dict) -> bool:
    """Inserta una fila en cuarentena. Devuelve True si se insertó, False si ya existía."""
    return conn.execute(_SQL_INSERT_CUARENTENA, fila).rowcount == 1


def guardar_lote(
    conn: sqlite3.Connection,
    id_corrida: int,
    validos: Iterable[dict],
    rechazados: Iterable[dict],
) -> dict:
    """
    Guarda válidos y rechazados en UNA sola transacción.

    * Si cualquier registro falla (tipo inválido, CHECK, FK, error de disco...), se hace
      ROLLBACK de todo el lote (ambas tablas) y se lanza ``ErrorPersistencia``.
    * Idempotencia: una fila cuyo ``(fuente_origen, id_origen)`` ya existe NO se vuelve a
      insertar; se cuenta como duplicado. Si el contenido difiere del ya guardado se cuenta
      además como conflicto (la fila original no se modifica).
    * Cuarentena: se deduplica por hash del registro original.

    Devuelve un dict con los contadores del lote.
    """
    ahora = _ahora()
    cont = {
        "insertados_limpias": 0, "duplicados_limpias": 0, "conflictos_limpias": 0,
        "insertados_cuarentena": 0, "duplicados_cuarentena": 0,
    }
    try:
        with transaccion(conn):
            for reg in validos:
                fila = normalizar_fila_limpia(reg)
                fila.update(id_corrida=id_corrida, registrado_en=ahora)
                if conn.execute(_SQL_INSERT_LIMPIA, fila).rowcount == 1:
                    cont["insertados_limpias"] += 1
                else:
                    cont["duplicados_limpias"] += 1
                    previo = conn.execute(
                        "SELECT hash_contenido FROM transacciones_limpias WHERE id_transaccion = ?",
                        (fila["id_transaccion"],),
                    ).fetchone()
                    if previo is not None and previo["hash_contenido"] != fila["hash_contenido"]:
                        cont["conflictos_limpias"] += 1
                        log.warning("Conflicto: %s ya existe con contenido distinto; se conserva el original.",
                                    fila["id_transaccion"])
            for rej in rechazados:
                fila = normalizar_fila_cuarentena(rej)
                fila.update(id_corrida=id_corrida, registrado_en=ahora)
                if _insertar_cuarentena(conn, fila):
                    cont["insertados_cuarentena"] += 1
                else:
                    cont["duplicados_cuarentena"] += 1
    except Exception as exc:
        log.error("Rollback del lote por error: %s: %s", type(exc).__name__, exc)
        raise ErrorPersistencia(f"{type(exc).__name__}: {exc}") from exc
    return cont


# --------------------------------------------------------------------------- #
# Consultas de apoyo
# --------------------------------------------------------------------------- #
def contar_filas(conn: sqlite3.Connection, tabla: str) -> int:
    if tabla not in TABLAS_CONSULTABLES:
        raise ValueError(f"tabla no permitida: {tabla}")
    return int(conn.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0])  # nosec: tabla validada


def totales_db(conn: sqlite3.Connection) -> dict:
    return {
        "transacciones_limpias": contar_filas(conn, "transacciones_limpias"),
        "cuarentena_errores": contar_filas(conn, "cuarentena_errores"),
        "corridas_pipeline": contar_filas(conn, "corridas_pipeline"),
    }
