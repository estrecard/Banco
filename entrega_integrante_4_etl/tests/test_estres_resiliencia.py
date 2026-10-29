import random
import pytest
import pandas as pd
from pathlib import Path
import base_datos

@pytest.fixture
def temp_db(tmp_path):
    db_file = tmp_path / "estres_banco.db"
    base_datos.inicializar_bd(db_file)
    return db_file

def test_estres_volumen_y_resiliencia(temp_db):
    n = 1000
    datos = []
    for i in range(n):
        datos.append({
            "id_transaccion_etl": f"TX_STRESS_{i}",
            "id_origen": f"ORIG_{i}",
            "canal_origen": "TRANSFERENCIA_DIGITAL" if i % 2 == 0 else "ATM",
            "fecha_transaccion": "2026-10-29T12:00:00",
            "cuenta_origen": f"CTA_{random.randint(1000, 9999)}",
            "cuenta_destino": f"CTA_{random.randint(1000, 9999)}",
            "tipo_transaccion": "TRANSFERENCIA",
            "monto": round(random.uniform(10.0, 50000.0), 2),
            "moneda": "MXN",
            "estado": "APROBADA",
            "hash_integridad": f"HASH_{i}"
        })
    df_volumen = pd.DataFrame(datos)

    almacenados = base_datos.persistir_transacciones_limpias(df_volumen, db_path=temp_db)
    assert almacenados == n

    conn = base_datos.obtener_conexion(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM transacciones_limpias;")
    assert cursor.fetchone()[0] == n
    conn.close()

def test_cuarentena_ante_registros_malformados(temp_db):
    df_malformados = pd.DataFrame([
        {
            "id_transaccion_etl": "ERR_01",
            "id_origen": "BAD_1",
            "canal_origen": "ATM",
            "monto": -500.0,
            "motivo_rechazo_cuarentena": "MONTO_NEGATIVO",
            "codigo_regla": "RN_MONTO_01"
        },
        {
            "id_transaccion_etl": "ERR_02",
            "id_origen": "BAD_2",
            "canal_origen": "TRANSFERENCIA_DIGITAL",
            "cuenta_origen": None,
            "motivo_rechazo_cuarentena": "CUENTA_ORIGEN_NULA",
            "codigo_regla": "RN_CTA_01"
        }
    ])

    conteo = base_datos.persistir_cuarentena(df_malformados, db_path=temp_db)
    assert conteo == 2

    conn = base_datos.obtener_conexion(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT motivo_rechazo_cuarentena FROM cuarentena_errores WHERE id_origen='BAD_1';")
    motivo = cursor.fetchone()[0]
    conn.close()
    assert motivo == "MONTO_NEGATIVO"
