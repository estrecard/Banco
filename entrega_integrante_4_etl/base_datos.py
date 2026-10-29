"""
Módulo: base_datos.py
Responsabilidad: Integrante 4 - Persistencia, DDL, Idempotencia y Transaccionalidad ACID en SQLite.
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Generator, Dict, Any
from contextlib import contextmanager
from datetime import datetime
import pandas as pd

logger = logging.getLogger("ETL_BANCO.Persistencia")
DEFAULT_DB_PATH = Path("banco_etl.db")

def obtener_conexion(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def transaccion_alcance(conn: sqlite3.Connection) -> Generator[sqlite3.Cursor, None, None]:
    cursor = conn.cursor()
    try:
        cursor.execute("BEGIN TRANSACTION;")
        yield cursor
        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.error(f"Fallo en transacción SQLite. Se ejecutó ROLLBACK: {exc}")
        raise exc
    finally:
        cursor.close()

def inicializar_bd(db_path: Path = DEFAULT_DB_PATH) -> None:
    conn = obtener_conexion(db_path)
    with transaccion_alcance(conn) as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS transacciones_limpias (
            id_transaccion_etl TEXT PRIMARY KEY,
            id_origen TEXT NOT NULL,
            canal_origen TEXT NOT NULL CHECK(canal_origen IN ('ATM', 'TRANSFERENCIA_DIGITAL')),
            fecha_transaccion TEXT NOT NULL,
            cuenta_origen TEXT NOT NULL,
            cuenta_destino TEXT,
            tipo_transaccion TEXT NOT NULL,
            monto REAL NOT NULL CHECK(monto > 0),
            moneda TEXT NOT NULL CHECK(moneda IN ('MXN', 'USD')),
            estado TEXT NOT NULL,
            hash_integridad TEXT NOT NULL UNIQUE,
            fecha_carga_etl TEXT NOT NULL
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS cuarentena_errores (
            id_cuarentena INTEGER PRIMARY KEY AUTOINCREMENT,
            id_transaccion_etl TEXT,
            id_origen TEXT,
            canal_origen TEXT,
            datos_crudos_json TEXT NOT NULL,
            motivo_rechazo_cuarentena TEXT NOT NULL,
            codigo_regla TEXT,
            severidad TEXT DEFAULT 'ERROR',
            fecha_ingreso_cuarentena TEXT NOT NULL,
            hash_error TEXT NOT NULL UNIQUE
        );
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS control_ejecuciones_etl (
            id_ejecucion INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_inicio TEXT NOT NULL,
            timestamp_fin TEXT,
            total_leidos INTEGER NOT NULL,
            total_validos INTEGER NOT NULL,
            total_rechazados INTEGER NOT NULL,
            total_almacenados INTEGER NOT NULL,
            estado TEXT NOT NULL
        );
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_tl_cuenta_origen ON transacciones_limpias(cuenta_origen);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tl_fecha ON transacciones_limpias(fecha_transaccion);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cuarentena_canal ON cuarentena_errores(canal_origen);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cuarentena_regla ON cuarentena_errores(codigo_regla);")
    conn.close()

def persistir_transacciones_limpias(df_validas: pd.DataFrame, db_path: Path = DEFAULT_DB_PATH) -> int:
    if df_validas.empty:
        return 0

    columnas_esperadas = [
        "id_transaccion_etl", "id_origen", "canal_origen", "fecha_transaccion",
        "cuenta_origen", "cuenta_destino", "tipo_transaccion", "monto",
        "moneda", "estado", "hash_integridad", "fecha_carga_etl"
    ]

    df_proc = df_validas.copy()
    if "fecha_carga_etl" not in df_proc.columns:
        df_proc["fecha_carga_etl"] = datetime.utcnow().isoformat()

    for col in columnas_esperadas:
        if col not in df_proc.columns:
            df_proc[col] = None

    registros = df_proc[columnas_esperadas].to_dict(orient="records")

    sentencia_sql = """
    INSERT INTO transacciones_limpias (
        id_transaccion_etl, id_origen, canal_origen, fecha_transaccion,
        cuenta_origen, cuenta_destino, tipo_transaccion, monto,
        moneda, estado, hash_integridad, fecha_carga_etl
    ) VALUES (
        :id_transaccion_etl, :id_origen, :canal_origen, :fecha_transaccion,
        :cuenta_origen, :cuenta_destino, :tipo_transaccion, :monto,
        :moneda, :estado, :hash_integridad, :fecha_carga_etl
    )
    ON CONFLICT(id_transaccion_etl) DO UPDATE SET
        id_origen = excluded.id_origen,
        canal_origen = excluded.canal_origen,
        fecha_transaccion = excluded.fecha_transaccion,
        cuenta_origen = excluded.cuenta_origen,
        cuenta_destino = excluded.cuenta_destino,
        tipo_transaccion = excluded.tipo_transaccion,
        monto = excluded.monto,
        moneda = excluded.moneda,
        estado = excluded.estado,
        hash_integridad = excluded.hash_integridad,
        fecha_carga_etl = excluded.fecha_carga_etl;
    """

    conn = obtener_conexion(db_path)
    with transaccion_alcance(conn) as cur:
        cur.executemany(sentencia_sql, registros)
    conn.close()
    return len(registros)

def persistir_cuarentena(df_cuarentena: pd.DataFrame, db_path: Path = DEFAULT_DB_PATH) -> int:
    if df_cuarentena.empty:
        return 0

    registros = []
    fecha_ahora = datetime.utcnow().isoformat()

    for _, row in df_cuarentena.iterrows():
        id_tx = str(row.get("id_transaccion_etl", "")) if pd.notnull(row.get("id_transaccion_etl")) else None
        id_orig = str(row.get("id_origen", "")) if pd.notnull(row.get("id_origen")) else None
        canal = str(row.get("canal_origen", "")) if pd.notnull(row.get("canal_origen")) else None
        motivo = str(row.get("motivo_rechazo_cuarentena", "ERROR_NO_IDENTIFICADO"))
        cod_regla = str(row.get("codigo_regla", "GENERAL"))
        severidad = str(row.get("severidad", "ERROR"))

        datos_dict = {k: (None if pd.isna(v) else v) for k, v in row.to_dict().items()}
        raw_json = json.dumps(datos_dict, default=str)
        hash_err = f"{id_orig}_{id_tx}_{canal}_{cod_regla}_{hash(motivo)}"

        registros.append({
            "id_transaccion_etl": id_tx,
            "id_origen": id_orig,
            "canal_origen": canal,
            "datos_crudos_json": raw_json,
            "motivo_rechazo_cuarentena": motivo,
            "codigo_regla": cod_regla,
            "severidad": severidad,
            "fecha_ingreso_cuarentena": fecha_ahora,
            "hash_error": hash_err
        })

    sentencia_sql = """
    INSERT INTO cuarentena_errores (
        id_transaccion_etl, id_origen, canal_origen, datos_crudos_json,
        motivo_rechazo_cuarentena, codigo_regla, severidad,
        fecha_ingreso_cuarentena, hash_error
    ) VALUES (
        :id_transaccion_etl, :id_origen, :canal_origen, :datos_crudos_json,
        :motivo_rechazo_cuarentena, :codigo_regla, :severidad,
        :fecha_ingreso_cuarentena, :hash_error
    )
    ON CONFLICT(hash_error) DO UPDATE SET
        motivo_rechazo_cuarentena = excluded.motivo_rechazo_cuarentena,
        severidad = excluded.severidad,
        datos_crudos_json = excluded.datos_crudos_json,
        fecha_ingreso_cuarentena = excluded.fecha_ingreso_cuarentena;
    """

    conn = obtener_conexion(db_path)
    with transaccion_alcance(conn) as cur:
        cur.executemany(sentencia_sql, registros)
    conn.close()
    return len(registros)

def registrar_auditoria_ejecucion(resumen: Dict[str, Any], db_path: Path = DEFAULT_DB_PATH) -> int:
    conn = obtener_conexion(db_path)
    sql = """
    INSERT INTO control_ejecuciones_etl (
        timestamp_inicio, timestamp_fin, total_leidos,
        total_validos, total_rechazados, total_almacenados, estado
    ) VALUES (?, ?, ?, ?, ?, ?, ?);
    """
    with transaccion_alcance(conn) as cur:
        cur.execute(sql, (
            resumen.get("timestamp_inicio"),
            resumen.get("timestamp_fin"),
            resumen.get("total_leidos", 0),
            resumen.get("total_validos", 0),
            resumen.get("total_rechazados", 0),
            resumen.get("total_almacenados", 0),
            resumen.get("estado", "COMPLETADO")
        ))
        id_ejecucion = cur.lastrowid
    conn.close()
    return id_ejecucion
