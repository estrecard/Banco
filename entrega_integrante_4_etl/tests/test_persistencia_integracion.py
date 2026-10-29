import sqlite3
import pytest
import pandas as pd
from pathlib import Path
import base_datos

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "test_banco.db"
    base_datos.inicializar_bd(db_file)
    return db_file

def test_inicializacion_tablas(temp_db):
    conn = base_datos.obtener_conexion(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tablas = [row[0] for row in cursor.fetchall()]
    assert "transacciones_limpias" in tablas
    assert "cuarentena_errores" in tablas
    assert "control_ejecuciones_etl" in tablas
    conn.close()

def test_atomicidad_rollback_ante_error(temp_db):
    conn = base_datos.obtener_conexion(temp_db)
    with pytest.raises(Exception):
        with base_datos.transaccion_alcance(conn) as cur:
            cur.execute("""
                INSERT INTO transacciones_limpias (
                    id_transaccion_etl, id_origen, canal_origen, fecha_transaccion,
                    cuenta_origen, tipo_transaccion, monto, moneda, estado, hash_integridad, fecha_carga_etl
                ) VALUES ('TX_01', 'O_01', 'ATM', '2026-10-01', 'CTA-1', 'RETIRO', 500.0, 'MXN', 'EXITOSO', 'H1', '2026-10-01');
            """)
            # Forzar error violando restricción CHECK (monto > 0)
            cur.execute("""
                INSERT INTO transacciones_limpias (
                    id_transaccion_etl, id_origen, canal_origen, fecha_transaccion,
                    cuenta_origen, tipo_transaccion, monto, moneda, estado, hash_integridad, fecha_carga_etl
                ) VALUES ('TX_02', 'O_02', 'ATM', '2026-10-01', 'CTA-2', 'RETIRO', -100.0, 'MXN', 'EXITOSO', 'H2', '2026-10-01');
            """)

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM transacciones_limpias;")
    conteo = cursor.fetchone()[0]
    conn.close()
    assert conteo == 0

def test_idempotencia_re_ejecucion(temp_db):
    df_test = pd.DataFrame([{
        "id_transaccion_etl": "ATM_1001",
        "id_origen": "1001",
        "canal_origen": "ATM",
        "fecha_transaccion": "2026-10-25T10:00:00",
        "cuenta_origen": "12345678",
        "cuenta_destino": None,
        "tipo_transaccion": "RETIRO",
        "monto": 1200.0,
        "moneda": "MXN",
        "estado": "COMPLETADA",
        "hash_integridad": "HASH_IDEMPOTENCIA_1"
    }])

    base_datos.persistir_transacciones_limpias(df_test, db_path=temp_db)
    base_datos.persistir_transacciones_limpias(df_test, db_path=temp_db)

    conn = base_datos.obtener_conexion(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM transacciones_limpias WHERE id_transaccion_etl='ATM_1001';")
    conteo = cursor.fetchone()[0]
    conn.close()
    assert conteo == 1
