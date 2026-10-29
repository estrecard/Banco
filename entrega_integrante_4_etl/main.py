"""
Módulo: main.py
Responsabilidad: Integración de módulos 1, 2, 3 y 4.
Orquesta: Extracción -> Limpieza -> Integración -> Validación/Cuarentena -> Persistencia Transaccional.
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional
import pandas as pd

import base_datos

try:
    import validacion_cuarentena
except ImportError:
    validacion_cuarentena = None

try:
    import integracion_datos
except ImportError:
    integracion_datos = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline_ejecucion.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("ETL_BANCO.Main")

def verificar_catalogo_cuentas(ruta_catalogo: Path = Path("data/raw/catalogo_cuentas.csv")) -> Optional[pd.DataFrame]:
    if ruta_catalogo.exists():
        logger.info(f"Catálogo de cuentas localizado en: {ruta_catalogo}")
        return pd.read_csv(ruta_catalogo)
    
    logger.warning(
        "[LIMITACIÓN DE DATOS]: 'catalogo_cuentas.csv' NO está presente en el repositorio original. "
        "El pipeline continuará ejecutando validaciones intrínsecas sin inventar datos no disponibles."
    )
    return None

def ejecutar_pipeline(
    ruta_atm: Path = Path("data/raw/transacciones_atm.csv"),
    ruta_digital: Path = Path("data/raw/transferencias_digitales.json"),
    db_path: Path = Path("banco_etl.db"),
    reportes_dir: Path = Path("reports")
) -> Dict[str, Any]:
    t_inicio = datetime.utcnow().isoformat()
    logger.info("=== INICIANDO PIPELINE ETL BANCARIO ===")

    base_datos.inicializar_bd(db_path=db_path)
    catalogo_df = verificar_catalogo_cuentas()

    df_integrado = pd.DataFrame()
    if integracion_datos and hasattr(integracion_datos, "procesar_integracion"):
        logger.info("Invocando integracion_datos...")
        df_integrado = integracion_datos.procesar_integracion(ruta_atm=ruta_atm, ruta_digital=ruta_digital)
    else:
        logger.info("Ejecutando unificación directa de datos...")
        dfs = []
        if ruta_atm.exists():
            df_atm = pd.read_csv(ruta_atm)
            df_atm["canal_origen"] = "ATM"
            df_atm["id_origen"] = df_atm.get("id_transaccion", df_atm.index).astype(str)
            dfs.append(df_atm)
        if ruta_digital.exists():
            df_dig = pd.read_json(ruta_digital)
            df_dig["canal_origen"] = "TRANSFERENCIA_DIGITAL"
            df_dig["id_origen"] = df_dig.get("transferencia_id", df_dig.index).astype(str)
            dfs.append(df_dig)

        if dfs:
            df_integrado = pd.concat(dfs, ignore_index=True)
            if "id_transaccion_etl" not in df_integrado.columns:
                df_integrado["id_transaccion_etl"] = df_integrado["canal_origen"] + "_" + df_integrado["id_origen"]
            if "monto" not in df_integrado.columns and "cantidad" in df_integrado.columns:
                df_integrado["monto"] = df_integrado["cantidad"]
            if "moneda" not in df_integrado.columns:
                df_integrado["moneda"] = "MXN"
            if "estado" not in df_integrado.columns:
                df_integrado["estado"] = "PENDIENTE"
            if "hash_integridad" not in df_integrado.columns:
                df_integrado["hash_integridad"] = df_integrado["id_transaccion_etl"].apply(lambda x: str(hash(x)))

    total_leidos = len(df_integrado)
    logger.info(f"Registros totales leídos y unificados: {total_leidos}")

    df_validas = pd.DataFrame()
    df_cuarentena = pd.DataFrame()

    if validacion_cuarentena and hasattr(validacion_cuarentena, "validar_transacciones"):
        logger.info("Invocando validacion_cuarentena...")
        df_validas, df_cuarentena = validacion_cuarentena.validar_transacciones(
            df_integrado, catalogo_cuentas=catalogo_df
        )
    else:
        logger.info("Aplicando reglas de validación base...")
        mascara_valida = (
            df_integrado["monto"].notnull() &
            (df_integrado["monto"] > 0) &
            df_integrado.get("cuenta_origen", pd.Series(dtype=object)).notnull()
        )
        df_validas = df_integrado[mascara_valida].copy()
        df_cuarentena = df_integrado[~mascara_valida].copy()
        df_cuarentena["motivo_rechazo_cuarentena"] = "MONTO_INVALIDO_O_CUENTA_ORIGEN_NULA"
        df_cuarentena["codigo_regla"] = "RN_GEN_001"

    total_validas = len(df_validas)
    total_rechazadas = len(df_cuarentena)

    almacenados_validos = base_datos.persistir_transacciones_limpias(df_validas, db_path=db_path)
    almacenados_cuarentena = base_datos.persistir_cuarentena(df_cuarentena, db_path=db_path)
    total_almacenados = almacenados_validos + almacenados_cuarentena

    t_fin = datetime.utcnow().isoformat()
    resumen = {
        "timestamp_inicio": t_inicio,
        "timestamp_fin": t_fin,
        "total_leidos": total_leidos,
        "total_validos": total_validas,
        "total_rechazados": total_rechazadas,
        "total_almacenados": total_almacenados,
        "estado": "EXITOSO"
    }

    base_datos.registrar_auditoria_ejecucion(resumen, db_path=db_path)
    
    reportes_dir.mkdir(parents=True, exist_ok=True)
    with open(reportes_dir / "resumen_ejecucion_final.json", "w", encoding="utf-8") as f:
        json.dump(resumen, f, indent=4, ensure_ascii=False)

    return resumen

if __name__ == "__main__":
    res = ejecutar_pipeline()
    print("\n" + "="*50)
    print("RESUMEN DE EJECUCIÓN ETL")
    print("="*50)
    for k, v in res.items():
        print(f"  {k}: {v}")
    print("="*50 + "\n")
