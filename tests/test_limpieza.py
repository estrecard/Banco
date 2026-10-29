"""Pruebas unitarias del modulo de limpieza (Integrante 1).

Ejecutar desde la raiz del repositorio:
    python -m pytest tests/test_limpieza.py -v

Los DataFrames pequenos de las primeras pruebas son datos de prueba (fixtures) escritos a mano para verificar
cada regla de forma aislada; no representan datos reales. La clase TestArchivosReales usa los archivos de
data/raw y se omite (skip) si no existen.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from src.limpieza_datos import (  # noqa: E402
    ErrorCargaDatos,
    ErrorConfiguracion,
    calcular_limites_iqr,
    cargar_archivo,
    clasificar_montos,
    detectar_atipicos_iqr,
    imputar_mediana,
    limpiar_datos,
    parsear_fecha,
)


def hacer_atm(n_por_tipo: int = 12) -> pd.DataFrame:
    """DataFrame de prueba con la forma de transacciones_atm.csv (3 tipos de operacion, montos variados)."""
    filas, k = [], 0
    for tipo, base in (("RETIRO", 2000), ("DEPOSITO", 4000), ("PAGO_SERVICIOS", 4000)):
        for i in range(n_por_tipo):
            k += 1
            filas.append({"id_transaccion": f"ATM-TX-{k:05d}", "fecha_hora": "2026/09/15 08:45:00",
                          "id_cuenta": "CTA-0001", "tipo_operacion": tipo, "monto_mxn": float(base + 100 * i),
                          "id_cajero": "CAJ-01", "resultado": "EXITOSA", "comision_aplicada": 0.0})
    return pd.DataFrame(filas)


# ----------------------------------------------------------------------------------------------- fechas
class TestFechas:
    @pytest.mark.parametrize("valor, esperado", [
        ("2026/09/15 08:45:00", "2026-09-15T08:45:00-06:00"),
        ("2026-10-12 22:22:00", "2026-10-12T22:22:00-06:00"),
        ("12/09/2026 13:26", "2026-09-12T13:26:00-06:00"),          # DD/MM/YYYY
        ("2026-09-12T19:49:00-06:00", "2026-09-12T19:49:00-06:00"),  # ISO con offset
        (1788816180, "2026-09-07T15:23:00-06:00"),                   # epoch en segundos (UTC)
    ])
    def test_formatos_reconocidos(self, valor, esperado):
        ts, _ = parsear_fecha(valor)
        assert ts.isoformat() == esperado

    @pytest.mark.parametrize("valor", ["31/02/2026 10:00", "ayer", "", None, np.nan, True, -5, 99999999999999999])
    def test_fechas_invalidas_no_lanzan_excepcion(self, valor):
        ts, formato = parsear_fecha(valor)
        assert pd.isna(ts)
        assert formato in ("NO_RECONOCIDO", "FALTANTE")


# ----------------------------------------------------------------------------------------------- IQR
class TestIQR:
    def test_limites_calculados(self):
        lim = calcular_limites_iqr(pd.Series([10, 20, 30, 40, 50]), factor=1.5, factor_extremo=3.0)
        assert (lim["q1"], lim["q3"], lim["iqr"]) == (20.0, 40.0, 20.0)
        assert lim["lim_sup"] == 70.0 and lim["lim_inf"] == -10.0
        assert lim["ext_sup"] == 100.0

    def test_extremo_se_etiqueta_y_los_normales_no(self):
        monto = pd.Series([100.0 + i for i in range(20)] + [100000.0])
        etiqueta, limites = detectar_atipicos_iqr(monto, pd.Series("A", index=monto.index), set(), 1.5, 3.0, 8)
        assert etiqueta.iloc[-1] == "EXTREMO_ALTO"
        assert (etiqueta.iloc[:-1] == "NO").all()
        assert limites["A"]["evaluado"] is True

    def test_segmento_con_pocas_observaciones_no_se_evalua(self):
        monto = pd.Series([1.0, 2.0, 3.0])
        etiqueta, limites = detectar_atipicos_iqr(monto, pd.Series("A", index=monto.index), set(), 1.5, 3.0, 8)
        assert limites["A"]["evaluado"] is False
        assert (etiqueta == "NO_EVALUADO").all()


# ----------------------------------------------------------------------------------------------- imputacion
class TestImputacion:
    def test_mediana_por_segmento(self):
        monto = pd.Series([10.0] * 5 + [20.0] * 5 + [np.nan, np.nan])
        seg = pd.Series(["A"] * 5 + ["B"] * 5 + ["A", "B"])
        nuevo, mask, info = imputar_mediana(monto, seg, set(), umbral=0.5, min_n=3)
        assert info["imputado"] is True
        assert nuevo.iloc[10] == 10.0 and nuevo.iloc[11] == 20.0
        assert mask.sum() == 2

    def test_no_se_imputa_si_falta_mas_del_umbral(self):
        monto = pd.Series([10.0] * 20 + [np.nan] * 5)           # 20 % faltante
        nuevo, mask, info = imputar_mediana(monto, None, set(), umbral=0.10, min_n=3)
        assert info["imputado"] is False
        assert nuevo.isna().sum() == 5 and not mask.any()


# ----------------------------------------------------------------------------------------------- reglas de monto
class TestMontos:
    def test_clasificacion_sin_modificar(self):
        monto = pd.Series([-5.0, 0.0, 0.0, 10.0, np.nan])
        seg = pd.Series(["RETIRO", "RETIRO", "CONSULTA_SALDO", "RETIRO", "RETIRO"])
        estado = clasificar_montos(monto, seg, {"CONSULTA_SALDO"})
        assert estado.tolist() == ["NEGATIVO_REVISAR", "CERO_REVISAR", "CERO_ESPERADO", "POSITIVO", "FALTANTE"]

    def test_negativo_y_cero_no_se_modifican_ni_se_eliminan(self):
        df = hacer_atm()
        df.loc[2, "monto_mxn"] = -500.0
        df.loc[3, "monto_mxn"] = 0.0
        limpio, _ = limpiar_datos(df, "atm")
        assert len(limpio) == len(df)
        assert limpio.loc[2, "monto_mxn"] == -500.0 and limpio.loc[3, "monto_mxn"] == 0.0
        assert limpio.loc[2, "ind_monto_estado"] == "NEGATIVO_REVISAR"
        assert limpio.loc[3, "ind_monto_estado"] == "CERO_REVISAR"


# ----------------------------------------------------------------------------------------------- limpiar_datos
class TestLimpiarDatos:
    def test_no_modifica_el_dataframe_de_entrada(self):
        df = hacer_atm()
        df.loc[1, "monto_mxn"] = np.nan
        copia = df.copy()
        limpiar_datos(df, "atm")
        pd.testing.assert_frame_equal(df, copia)

    def test_imputa_con_mediana_y_conserva_el_original(self):
        df = hacer_atm()
        df.loc[1, "monto_mxn"] = np.nan                           # RETIRO
        esperado = df.loc[(df.tipo_operacion == "RETIRO") & df.monto_mxn.notna(), "monto_mxn"].median()
        limpio, resumen = limpiar_datos(df, "atm")
        assert limpio.loc[1, "monto_mxn"] == esperado
        assert limpio.loc[1, "ind_monto_imputado"] == True  # noqa: E712
        assert pd.isna(limpio.loc[1, "ind_monto_original"])
        assert "IMPUTACION_MEDIANA" in resumen["reglas_aplicadas"]

    def test_etiqueta_controlada_para_cajero_nulo(self):
        df = hacer_atm()
        df.loc[4, "id_cajero"] = np.nan
        limpio, _ = limpiar_datos(df, "atm")
        assert limpio.loc[4, "id_cajero"] == "CAJERO_NO_REGISTRADO"
        assert limpio.loc[4, "ind_id_cajero_imputado"] == True  # noqa: E712

    def test_winsorizacion_conserva_el_valor_original(self):
        df = hacer_atm()
        df.loc[0, "monto_mxn"] = 3_500_000.0
        limpio, resumen = limpiar_datos(df, "atm")
        assert limpio.loc[0, "ind_monto_original"] == 3_500_000.0
        assert limpio.loc[0, "monto_mxn"] == round(resumen["atipicos"]["RETIRO"]["lim_sup"], 2)
        assert limpio.loc[0, "ind_monto_recortado"] == True  # noqa: E712
        assert limpio.loc[0, "ind_monto_atipico"] == "EXTREMO_ALTO"
        aud = resumen["auditoria"]
        assert ((aud.regla == "WINSORIZACION_IQR") & (aud.valor_original == 3_500_000.0)).any()

    def test_sin_capping_el_monto_queda_intacto(self):
        df = hacer_atm()
        df.loc[0, "monto_mxn"] = 3_500_000.0
        limpio, _ = limpiar_datos(df, "atm", aplicar_capping=False)
        assert limpio.loc[0, "monto_mxn"] == 3_500_000.0
        assert limpio.loc[0, "ind_monto_atipico"] == "EXTREMO_ALTO"

    def test_mayusculas_se_unifican_y_se_audita(self):
        df = hacer_atm()
        df.loc[5, "resultado"] = "exitosa"
        limpio, resumen = limpiar_datos(df, "atm")
        assert limpio.loc[5, "resultado"] == "EXITOSA"
        assert resumen["reglas_aplicadas"].get("NORMALIZAR_MAYUSCULAS", 0) >= 1

    def test_duplicado_exacto_se_marca_y_no_se_elimina(self):
        df = hacer_atm()
        df = pd.concat([df, df.iloc[[3]]], ignore_index=True)     # copia exacta de la fila 3
        limpio, resumen = limpiar_datos(df, "atm")
        assert len(limpio) == len(df)
        assert limpio["ind_duplicado_exacto"].sum() == 1
        assert bool(limpio.loc[len(df) - 1, "ind_duplicado_exacto"]) is True
        assert resumen["duplicados_exactos_detectados"] == 1 and len(resumen["duplicados_detectados"]) == 1
        assert limpio["ind_id_repetido"].sum() == 0               # una copia exacta no es conflicto de ID

    def test_excluir_duplicados_los_retira_del_flujo(self):
        df = hacer_atm()
        df = pd.concat([df, df.iloc[[3]]], ignore_index=True)
        limpio, resumen = limpiar_datos(df, "atm", excluir_duplicados=True)
        assert len(limpio) == len(df) - 1
        assert resumen["duplicados_exactos_excluidos"] == 1

    def test_mismo_id_con_contenido_distinto_es_conflicto(self):
        df = hacer_atm()
        df.loc[7, "id_transaccion"] = df.loc[6, "id_transaccion"]
        limpio, _ = limpiar_datos(df, "atm")
        assert limpio.loc[[6, 7], "ind_id_repetido"].all()
        assert len(limpio) == len(df)


# ----------------------------------------------------------------------------------------------- errores de entrada
class TestEntradasInvalidas:
    def test_dataframe_vacio(self):
        with pytest.raises(ValueError):
            limpiar_datos(pd.DataFrame(), "atm")

    def test_configuracion_inexistente(self):
        with pytest.raises(ErrorConfiguracion):
            limpiar_datos(hacer_atm(), "no_existe")

    def test_columnas_faltantes_dan_error_claro(self):
        with pytest.raises(ErrorConfiguracion):
            limpiar_datos(hacer_atm().drop(columns=["monto_mxn"]), "atm")

    def test_parametro_desconocido(self):
        with pytest.raises(ValueError):
            limpiar_datos(hacer_atm(), "atm", parametro_inventado=1)

    def test_archivo_inexistente(self, tmp_path):
        with pytest.raises(ErrorCargaDatos):
            cargar_archivo(tmp_path / "no_existe.csv")

    def test_json_malformado(self, tmp_path):
        ruta = tmp_path / "roto.json"
        ruta.write_text("{esto no es json", encoding="utf-8")
        with pytest.raises(ErrorCargaDatos):
            cargar_archivo(ruta)

    def test_csv_vacio(self, tmp_path):
        ruta = tmp_path / "vacio.csv"
        ruta.write_text("", encoding="utf-8")
        with pytest.raises(ErrorCargaDatos):
            cargar_archivo(ruta)

    def test_lista_de_diccionarios_tambien_se_acepta(self):
        registros = json.loads(hacer_atm().to_json(orient="records"))
        limpio, _ = limpiar_datos(registros, "atm")
        assert len(limpio) == len(registros)


# ----------------------------------------------------------------------------------------------- archivos reales
ATM = RAIZ / "data" / "raw" / "transacciones_atm.csv"
JSON_ = RAIZ / "data" / "raw" / "transferencias_digitales.json"


@pytest.mark.skipif(not (ATM.exists() and JSON_.exists()), reason="no se encontraron los archivos en data/raw")
class TestArchivosReales:
    def test_atm(self):
        limpio, r = limpiar_datos(cargar_archivo(ATM), "atm")
        assert (r["filas_entrada"], r["filas_salida"]) == (620, 620)
        assert limpio["ind_duplicado_exacto"].sum() == 20
        assert limpio["ind_monto_imputado"].sum() == 27
        assert limpio["ind_id_cajero_imputado"].sum() == 44
        assert limpio["ind_monto_recortado"].sum() == 1
        assert limpio.loc[limpio["ind_monto_recortado"], "ind_monto_original"].iloc[0] == 3_500_000.0
        assert limpio[list(cargar_archivo(ATM).columns)].isna().sum().sum() == 0

    def test_transferencias(self):
        limpio, r = limpiar_datos(cargar_archivo(JSON_), "transferencias_digitales")
        assert (r["filas_entrada"], r["filas_salida"]) == (600, 600)
        assert limpio["ind_notification_email_imputado"].sum() == 16
        assert limpio["ind_monto_recortado"].sum() == 18
        assert limpio["fecha_formato_origen"].isin(["ISO8601 con offset", "EPOCH segundos"]).all()
        assert limpio["fecha_hora_std"].notna().all()

    def test_datos_originales_se_conservan_en_la_auditoria(self):
        limpio, r = limpiar_datos(cargar_archivo(ATM), "atm")
        assert len(r["auditoria"]) == 177
        assert set(r["auditoria"].columns) >= {"indice_original", "columna", "valor_original", "valor_nuevo", "regla"}