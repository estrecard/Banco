"""
Pruebas de integracion_datos.py (Integrante 2).

Ejecutar:   python -m pytest test_integracion_datos.py -v

Las pruebas con los archivos REALES buscan transacciones_atm.csv y
transferencias_digitales.json en (por orden):
  1) la variable de entorno ETL_DATOS_DIR
  2) ./datos  y  ./datasets
  3) la carpeta de este archivo
  4) /mnt/user-data/uploads
Si no los encuentran, esas pruebas se OMITEN (skip) con un mensaje; las unitarias siempre corren.
"""
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import integracion_datos as idd
from integracion_datos import (
    ColumnasFaltantesError, ArchivoNoEncontradoError, CatalogoInvalidoError, ErrorIntegracion,
    integrar_datos, crear_catalogo_sintetico, cargar_catalogo, normalizar_fecha_atm,
    normalizar_timestamp_digital, ParametrosTransformacion, verificar_trazabilidad,
)


# ------------------------------------------------------------------ localización de datos reales
def _buscar(nombre: str):
    candidatos = []
    if os.environ.get("ETL_DATOS_DIR"):
        candidatos.append(Path(os.environ["ETL_DATOS_DIR"]))
    candidatos += [Path("datos"), Path("datasets"), Path(__file__).parent, Path("/mnt/user-data/uploads")]
    for c in candidatos:
        if (c / nombre).is_file():
            return c / nombre
    return None


RUTA_ATM = _buscar("transacciones_atm.csv")
RUTA_JSON = _buscar("transferencias_digitales.json")
requiere_datos = pytest.mark.skipif(
    RUTA_ATM is None or RUTA_JSON is None,
    reason="No se encontraron transacciones_atm.csv / transferencias_digitales.json (define ETL_DATOS_DIR).")


# ------------------------------------------------------------------ fixtures sintéticos mínimos
def _atm_min(**kw):
    base = dict(id_transaccion="ATM-TX-1", fecha_hora="2026/09/15 08:45:00", id_cuenta="CTA-0001",
                tipo_operacion="RETIRO", monto_mxn="500.0", id_cajero="ATM-COL-01",
                resultado="EXITOSA", comision_aplicada="15.0")
    base.update(kw)
    return base


def _dig_min(**kw):
    base = dict(transfer_id="SPEI-1", timestamp="2026-09-12T19:49:00-06:00", account_id="CTA-0001",
                amount=1000.0, currency="MXN", beneficiary_clabe="012210100614023846",
                notification_email="a@b.com", concept="Nómina", status="LIQUIDADA", platform="App iOS")
    base.update(kw)
    return base


def _df(filas):
    return pd.DataFrame(filas, dtype=object)


# ======================================================================== FECHAS
class TestFechas:
    @pytest.mark.parametrize("entrada,esperado,formato", [
        ("2026/09/15 08:45:00", "2026-09-15T08:45:00-06:00", "ATM_YYYY/MM/DD"),
        ("2026-10-12 22:22:00", "2026-10-12T22:22:00-06:00", "ATM_YYYY-MM-DD"),
        ("12/09/2026 13:26", "2026-09-12T13:26:00-06:00", "ATM_DD/MM/YYYY"),     # día primero
        ("30/09/2026 07:04", "2026-09-30T07:04:00-06:00", "ATM_DD/MM/YYYY"),
    ])
    def test_atm_formatos(self, entrada, esperado, formato):
        f = normalizar_fecha_atm(entrada)
        assert f.valido and f.iso_local == esperado and f.formato == formato
        assert f.origen_zona == "ASUMIDA_LOCAL"

    @pytest.mark.parametrize("entrada", ["31/02/2026 10:00", "2026/13/01 10:00:00", "", None, "ayer", "2026/09/15"])
    def test_atm_invalidas_no_lanzan(self, entrada):
        assert not normalizar_fecha_atm(entrada).valido

    def test_json_iso_conserva_offset_y_hora(self):
        f = normalizar_timestamp_digital("2026-09-12T19:49:00-06:00")
        assert f.iso_local == "2026-09-12T19:49:00-06:00"
        assert f.iso_utc == "2026-09-13T01:49:00Z"
        assert f.origen_zona == "EXPLICITA_EN_FUENTE" and f.offset == "-06:00"

    def test_json_iso_con_otro_offset_se_preserva(self):
        f = normalizar_timestamp_digital("2026-09-12T19:49:00+01:00")
        assert f.offset == "+01:00" and f.iso_utc == "2026-09-12T18:49:00Z"

    def test_json_epoch_entero_no_se_trata_como_texto(self):
        f = normalizar_timestamp_digital(1788816180)          # int, no str
        assert f.formato == "EPOCH_SEGUNDOS"
        assert f.iso_utc == "2026-09-07T21:23:00Z"
        assert f.iso_local == "2026-09-07T15:23:00-06:00"
        assert f.origen_zona == "EPOCH_UTC_CONVERTIDA"

    def test_json_epoch_float_milisegundos_y_texto_numerico(self):
        assert normalizar_timestamp_digital(1788816180.0).iso_utc == "2026-09-07T21:23:00Z"
        ms = normalizar_timestamp_digital(1788816180000)
        assert ms.formato == "EPOCH_MILISEGUNDOS" and ms.iso_utc == "2026-09-07T21:23:00Z"
        assert normalizar_timestamp_digital("1788816180").formato == "EPOCH_SEGUNDOS"

    @pytest.mark.parametrize("entrada", [None, "", True, "no-es-fecha", -5, 99999999999999999, float("nan"), 12])
    def test_json_invalidos_no_lanzan(self, entrada):
        assert not normalizar_timestamp_digital(entrada).valido


# ======================================================================== MONEDA
class TestMoneda:
    def test_usd_se_convierte_una_vez_y_mxn_no(self):
        r = integrar_datos(_df([_atm_min()]), _df([_dig_min(currency="USD", amount=100.0, transfer_id="U"),
                                                    _dig_min(currency="MXN", amount=100.0, transfer_id="M")]))
        d = r.datos.set_index("id_original")
        assert d.loc["U", "monto_mxn"] == 1950.0 and d.loc["U", "tipo_cambio_aplicado"] == 19.5
        assert d.loc["U", "monto_original"] == 100.0 and d.loc["U", "moneda_original"] == "USD"
        assert d.loc["M", "monto_mxn"] == 100.0 and d.loc["M", "tipo_cambio_aplicado"] == 1.0
        assert d.loc["ATM-TX-1", "monto_mxn"] == 500.0          # ATM ya viene en MXN: intacto

    def test_moneda_no_soportada_se_marca_y_no_inventa_monto(self):
        r = integrar_datos(_df([_atm_min()]), _df([_dig_min(currency="EUR", amount=10.0)]))
        fila = r.datos[r.datos.fuente == idd.FUENTE_DIGITAL].iloc[0]
        assert pd.isna(fila.monto_mxn) and "MONEDA_NO_SOPORTADA" in fila.indicadores_error


# ======================================================================== ESQUEMA / NORMALIZACIÓN
class TestEsquema:
    def test_columnas_del_esquema_y_categorias(self):
        r = integrar_datos(_df([_atm_min(resultado="exitosa", id_cuenta=" cta-0001 ")]),
                           _df([_dig_min(concept="Pago Renta", platform="API Empresarial")]))
        d = r.datos
        assert [c for c in idd.ESQUEMA_COMUN if c not in d.columns] == []
        a, t = d.iloc[0], d.iloc[1]
        assert a.estado_operacion == "COMPLETADA" and a.id_cuenta == "CTA-0001"
        assert t.concepto == "PAGO_RENTA" and t.plataforma == "API_EMPRESARIAL"
        assert t.tipo_operacion == "TRANSFERENCIA_SPEI" and a.canal == "ATM" and t.canal == "DIGITAL"

    def test_resultado_mayus_minus_se_unifica(self):
        r = integrar_datos(_df([_atm_min(id_transaccion="1", resultado="exitosa"),
                                _atm_min(id_transaccion="2", resultado="EXITOSA", monto_mxn="700.0")]),
                           _df([_dig_min()]))
        assert set(r.datos[r.datos.canal == "ATM"].estado_operacion) == {"COMPLETADA"}

    def test_faltan_columnas_error_claro(self):
        mal = _df([_atm_min()]).drop(columns=["monto_mxn"])
        with pytest.raises(ColumnasFaltantesError, match="monto_mxn"):
            integrar_datos(mal, _df([_dig_min()]))

    def test_archivo_inexistente(self):
        with pytest.raises(ArchivoNoEncontradoError):
            integrar_datos("no_existe.csv", _df([_dig_min()]))

    def test_json_malformado(self, tmp_path):
        p = tmp_path / "roto.json"
        p.write_text("{no es json", encoding="utf-8")
        with pytest.raises(ErrorIntegracion):
            integrar_datos(_df([_atm_min()]), p)

    def test_no_se_pierde_ninguna_fila_con_basura(self):
        atm = _df([_atm_min(id_transaccion=str(i), monto_mxn=m, fecha_hora=f, id_cuenta=c)
                   for i, (m, f, c) in enumerate([("", "x", ""), ("abc", "", "CTA-9"), ("-5", "31/02/2026 1:1", None)])])
        dig = _df([_dig_min(transfer_id=str(i), amount=a, timestamp=t, beneficiary_clabe=cl)
                   for i, (a, t, cl) in enumerate([(None, None, None), (float("nan"), "zzz", "123"), (-1.0, True, "")])])
        r = integrar_datos(atm, dig)
        assert len(r.datos) == 6 and r.reporte["filas_perdidas"] == 0
        assert r.datos.requiere_cuarentena_sugerida.all()          # todo marcado, nada descartado
        assert r.datos.segmento_monto.isna().all() and r.datos.monto_mxn_minmax.isna().all()
        assert any("Sin filas válidas" in a for a in r.reporte["advertencias"])


# ======================================================================== CATÁLOGO
class TestCatalogo:
    def test_sin_catalogo_no_evalua_huerfanas(self):
        r = integrar_datos(_df([_atm_min(id_cuenta="CTA-9999")]), _df([_dig_min()]))
        assert r.datos.cuenta_en_catalogo.isna().all()
        assert (r.datos.origen_catalogo == "NO_DISPONIBLE").all()
        assert not r.datos.indicadores_error.str.contains("CUENTA_HUERFANA").any()
        assert len(r.reporte_huerfanas()) == 0
        assert any("no fue evaluada" in a for a in r.reporte["advertencias"])

    def test_catalogo_sintetico_va_etiquetado(self):
        cat = crear_catalogo_sintetico(1, 50)
        assert cat.es_sintetico and cat.origen == "SINTETICO"
        r = integrar_datos(_df([_atm_min(id_cuenta="CTA-9999")]), _df([_dig_min()]), cat)
        assert (r.datos.origen_catalogo == "SINTETICO").all()
        huerfana = r.datos[r.datos.id_cuenta == "CTA-9999"].iloc[0]
        assert huerfana.cuenta_en_catalogo == False and "CUENTA_HUERFANA" in huerfana.indicadores_error  # noqa: E712
        assert r.reporte["catalogo"]["estado"] == "SINTETICO" and r.reporte["catalogo"]["ADVERTENCIA"]

    def test_catalogo_real_desde_dataframe_y_deteccion_de_columna(self):
        cat = cargar_catalogo(pd.DataFrame({"Cuenta": ["cta-0001 ", "CTA-0002"], "titular": ["a", "b"]}))
        assert cat.cuentas == frozenset({"CTA-0001", "CTA-0002"}) and not cat.es_sintetico
        r = integrar_datos(_df([_atm_min(id_cuenta="CTA-0002"), _atm_min(id_transaccion="2", id_cuenta="CTA-0003")]),
                           _df([_dig_min()]), cat)
        assert r.datos.origen_catalogo.str.startswith("REAL").all()
        assert list(r.reporte_huerfanas().id_cuenta) == ["CTA-0003"]

    def test_catalogo_sin_columna_reconocible(self):
        with pytest.raises(CatalogoInvalidoError, match="columna"):
            cargar_catalogo(pd.DataFrame({"x": ["1"], "y": ["2"]}))

    def test_catalogo_inexistente_mensaje_util(self):
        with pytest.raises(ArchivoNoEncontradoError, match="catalogo_cuentas"):
            cargar_catalogo("catalogo_cuentas.csv")


# ======================================================================== DUPLICADOS
class TestDuplicados:
    def test_exacto_marca_secundario_y_no_borra(self):
        r = integrar_datos(_df([_atm_min(), _atm_min()]), _df([_dig_min()]))
        a = r.datos[r.datos.canal == "ATM"]
        assert len(a) == 2 and list(a.rol_duplicado) == ["PRIMARIO", "SECUNDARIO"]
        assert "DUPLICADO_EXACTO" in a.iloc[1].indicadores_error
        assert "DUPLICADO_EXACTO" not in a.iloc[0].indicadores_error
        assert a.id_registro.is_unique                              # misma id_original, distinto id_registro

    def test_mismo_id_contenido_distinto_es_conflicto_y_no_se_descarta(self):
        r = integrar_datos(_df([_atm_min(), _atm_min(monto_mxn="999.0")]), _df([_dig_min()]))
        a = r.datos[r.datos.canal == "ATM"]
        assert set(a.rol_duplicado) == {"REVISAR"} and a.indicadores_alerta.str.contains("ID_REPETIDO_CONFLICTO").all()
        assert not a.requiere_cuarentena_sugerida.any()             # alerta ≠ cuarentena automática

    def test_reintento_atm_dentro_de_ventana(self):
        atm = _df([_atm_min(id_transaccion="A", resultado="FALLO_CONEXION", fecha_hora="2026/09/15 08:45:00"),
                   _atm_min(id_transaccion="B", resultado="EXITOSA", fecha_hora="2026/09/15 08:47:00")])
        r = integrar_datos(atm, _df([_dig_min()]))
        b = r.datos[r.datos.id_original == "B"].iloc[0]
        assert b.rol_duplicado == "REVISAR" and "POSIBLE_REINTENTO_ATM" in b.indicadores_alerta
        assert not b.requiere_cuarentena_sugerida                    # legítimo hasta que se revise

    def test_fuera_de_ventana_y_consultas_no_se_marcan(self):
        atm = _df([_atm_min(id_transaccion="A"),
                   _atm_min(id_transaccion="B", fecha_hora="2026/09/15 09:45:00"),
                   _atm_min(id_transaccion="C", tipo_operacion="CONSULTA_SALDO", monto_mxn="0.0",
                            fecha_hora="2026/09/16 10:00:00"),
                   _atm_min(id_transaccion="D", tipo_operacion="CONSULTA_SALDO", monto_mxn="0.0",
                            fecha_hora="2026/09/16 10:01:00")])
        r = integrar_datos(atm, _df([_dig_min()]))
        assert r.datos.rol_duplicado.isna().all()

    def test_duplicado_digital_posible(self):
        dig = _df([_dig_min(transfer_id="S1"),
                   _dig_min(transfer_id="S2", timestamp="2026-09-12T19:50:00-06:00")])
        r = integrar_datos(_df([_atm_min()]), dig)
        s2 = r.datos[r.datos.id_original == "S2"].iloc[0]
        assert "POSIBLE_DUPLICADO_DIGITAL" in s2.indicadores_alerta

    def test_copias_en_fuentes_distintas_no_se_mezclan(self):
        r = integrar_datos(_df([_atm_min(id_transaccion="X")]), _df([_dig_min(transfer_id="X")]))
        assert r.datos.rol_duplicado.isna().all()


# ======================================================================== ESCALADO / SEGMENTOS / FUGA
class TestTransformaciones:
    def _lote(self, montos):
        return _df([_atm_min(id_transaccion=str(i), monto_mxn=str(m), fecha_hora=f"2026/09/{1+i%28:02d} 10:00:00",
                             id_cuenta=f"CTA-{1+i%50:04d}") for i, m in enumerate(montos)])

    def test_minmax_en_rango_y_segmentos_cuantiles(self):
        r = integrar_datos(self._lote(range(100, 1000, 10)), _df([_dig_min(amount=5000.0)]))
        d = r.datos[r.datos.es_operacion_monetaria]
        assert d.monto_mxn_minmax.min() == 0.0 and d.monto_mxn_minmax.max() == 1.0
        assert set(d.segmento_monto.dropna()) == {"Bajo", "Medio", "Alto"}
        c1, c2 = r.parametros.segmentacion["cortes"]
        assert c1 < c2 and (d[d.segmento_monto == "Bajo"].monto_mxn <= c1).all()
        assert (d[d.segmento_monto == "Alto"].monto_mxn > c2).all()

    def test_consulta_de_saldo_no_se_escala_ni_segmenta(self):
        atm = _df([_atm_min(id_transaccion="Q", tipo_operacion="CONSULTA_SALDO", monto_mxn="0.0"), _atm_min()])
        r = integrar_datos(atm, _df([_dig_min()]))
        q = r.datos[r.datos.id_original == "Q"].iloc[0]
        assert pd.isna(q.segmento_monto) and pd.isna(q.monto_mxn_minmax) and not q.escala_recortada

    def test_parametros_guardados_reproducen_sin_reajustar(self, tmp_path):
        ruta = tmp_path / "params.json"
        lote1 = self._lote(range(100, 1000, 10))
        r1 = integrar_datos(lote1, _df([_dig_min(amount=5000.0)]), ruta_parametros=ruta)
        p = ParametrosTransformacion.cargar(ruta)
        assert p.minmax == r1.parametros.minmax and p.segmentacion["cortes"] == r1.parametros.segmentacion["cortes"]

        # Lote nuevo MUY distinto: con parámetros cargados NO se reajusta (sin fuga hacia/desde el lote nuevo)
        lote2 = self._lote([50000, 60000, 70000])
        r2 = integrar_datos(lote2, _df([_dig_min(amount=8000.0, transfer_id="Z")]), parametros=ruta)
        assert r2.parametros.minmax == r1.parametros.minmax
        assert r2.datos.loc[r2.datos.canal == "ATM", "monto_mxn_minmax"].eq(1.0).all()   # recortado a 1
        assert r2.datos.loc[r2.datos.canal == "ATM", "escala_recortada"].all()           # y avisado
        # Re-ejecución con los mismos parámetros => idéntico resultado
        r3 = integrar_datos(lote2, _df([_dig_min(amount=8000.0, transfer_id="Z")]), parametros=ruta)
        pd.testing.assert_frame_equal(r2.datos, r3.datos)

    def test_mascara_de_ajuste_solo_particion_entrenamiento(self):
        atm = self._lote([100, 200, 300, 400, 100000])
        mascara = pd.Series([True, True, True, True, False] + [False])      # excluye el valor grande y la transferencia
        # La máscara se aplica sobre el DataFrame integrado (5 ATM + 1 digital)
        r = integrar_datos(atm, _df([_dig_min()]), mascara_ajuste=mascara)
        assert r.parametros.minmax["monto_mxn"]["max"] == 400.0

    def test_segmentacion_por_reglas(self):
        r = integrar_datos(self._lote([100, 2000, 9000]), _df([_dig_min(amount=50000.0)]),
                           metodo_segmentacion="reglas", umbrales_reglas=(1000, 5000))
        s = r.datos.set_index("id_original").segmento_monto
        assert s["0"] == "Bajo" and s["1"] == "Medio" and s["2"] == "Alto"
        with pytest.raises(ErrorIntegracion):
            integrar_datos(self._lote([1, 2]), _df([_dig_min()]), metodo_segmentacion="reglas")


# ======================================================================== INDICADORES / INTERFAZ CON INT. 1 Y 3
class TestIntegracionEquipo:
    def test_indicadores_externos_ind_se_conservan(self):
        atm = _df([_atm_min()]); atm["ind_monto_imputado"] = [True]
        r = integrar_datos(atm, _df([_dig_min()]))
        assert "ind_monto_imputado" in r.datos.columns and bool(r.datos.iloc[0]["ind_monto_imputado"]) is True

    def test_clabe_y_email_marcados_sin_corregirse(self):
        r = integrar_datos(_df([_atm_min()]), _df([_dig_min(beneficiary_clabe="12345", notification_email="correo@@x")]))
        t = r.datos[r.datos.canal == "DIGITAL"].iloc[0]
        assert t.clabe_beneficiario == "12345" and t.email_notificacion == "correo@@x"   # valor original intacto
        assert "CLABE_INVALIDA" in t.indicadores_error and "EMAIL_INVALIDO" in t.indicadores_alerta

    def test_clabe_con_cero_inicial_no_pierde_digitos(self):
        t = integrar_datos(_df([_atm_min()]), _df([_dig_min(beneficiary_clabe="012210100614023846")])).datos.iloc[1]
        assert t.clabe_beneficiario == "012210100614023846"


# ======================================================================== ARCHIVOS REALES
@requiere_datos
class TestArchivosReales:
    @pytest.fixture(scope="class")
    def crudos(self):
        return idd.leer_atm_csv(RUTA_ATM), idd.leer_transferencias_json(RUTA_JSON)

    @pytest.fixture(scope="class")
    def res_sin_cat(self):
        return integrar_datos(RUTA_ATM, RUTA_JSON)

    @pytest.fixture(scope="class")
    def res_sint(self):
        return integrar_datos(RUTA_ATM, RUTA_JSON, crear_catalogo_sintetico())

    def test_lectura_y_columnas(self, crudos):
        atm, dig = crudos
        assert list(atm.columns) == list(idd.COLUMNAS_ATM)
        assert set(idd.COLUMNAS_DIGITAL) <= set(dig.columns)
        assert len(atm) == 620 and len(dig) == 600

    def test_json_timestamp_mixto_detectado(self, crudos):
        _, dig = crudos
        tipos = dig["timestamp"].map(lambda x: type(x).__name__).value_counts().to_dict()
        assert tipos == {"str": 405, "int": 195}

    def test_integracion_sin_perdida_y_con_ambas_fuentes(self, res_sin_cat):
        d = res_sin_cat.datos
        assert len(d) == 1220 and d.fuente.value_counts().to_dict() == {idd.FUENTE_ATM: 620, idd.FUENTE_DIGITAL: 600}

    def test_trazabilidad_completa(self, crudos, res_sin_cat):
        atm, dig = crudos
        v = verificar_trazabilidad(res_sin_cat.datos, atm, dig)
        assert v["ok"], v
        d = res_sin_cat.datos
        assert d[["fuente", "archivo_origen", "fila_origen", "id_original"]].notna().all().all()
        assert set(d.archivo_origen) == {RUTA_ATM.name, RUTA_JSON.name}

    def test_cada_fila_se_reconstruye_desde_su_origen(self, crudos, res_sin_cat):
        atm, dig = crudos
        d = res_sin_cat.datos
        a = d[d.fuente == idd.FUENTE_ATM].set_index("fila_origen")
        assert (a.fecha_hora_original == atm.fecha_hora).all()
        assert (a.estatus_original == atm.resultado).all()
        j = d[d.fuente == idd.FUENTE_DIGITAL].set_index("fila_origen")
        assert (j.id_original == dig.transfer_id).all()
        assert (j.estatus_original == dig.status).all()

    def test_todas_las_fechas_son_validas_y_iso(self, res_sin_cat):
        d = res_sin_cat.datos
        assert d.fecha_hora_iso.notna().all()
        assert d.fecha_hora_iso.str.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}").all()
        assert d.fecha_hora_utc.str.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z").all()
        assert d.fecha.between("2026-09-01", "2026-10-31").all()
        assert d[d.formato_fecha_detectado == "EPOCH_SEGUNDOS"].shape[0] == 195

    def test_conversion_usd_sobre_datos_reales(self, crudos, res_sin_cat):
        _, dig = crudos
        j = res_sin_cat.datos[res_sin_cat.datos.fuente == idd.FUENTE_DIGITAL].set_index("fila_origen")
        usd = dig.currency == "USD"
        assert usd.sum() == 134
        amount = dig.amount.astype(float)
        esperado = (amount[usd] * 19.5).round(2)
        assert np.allclose(j.loc[usd[usd].index, "monto_mxn"].astype(float), esperado)
        assert np.allclose(j.loc[(~usd)[~usd].index, "monto_mxn"].astype(float), amount[~usd])   # MXN sin reconvertir
        a = res_sin_cat.datos[res_sin_cat.datos.fuente == idd.FUENTE_ATM]
        assert (a.moneda_original == "MXN").all() and (a.tipo_cambio_aplicado == 1.0).all()

    def test_duplicados_exactos_marcados_no_borrados(self, res_sin_cat):
        d = res_sin_cat.datos
        assert (d.rol_duplicado == "SECUNDARIO").sum() == 20
        assert d.id_registro.is_unique
        dup = d[d.grupo_duplicado.notna()]
        assert dup.fuente.eq(idd.FUENTE_ATM).all() and dup.groupby("grupo_duplicado").size().eq(2).all()

    def test_sin_catalogo_no_hay_huerfanas_evaluadas(self, res_sin_cat):
        assert res_sin_cat.datos.cuenta_en_catalogo.isna().all()
        assert res_sin_cat.reporte["catalogo"]["estado"] == "NO_DISPONIBLE"
        assert len(res_sin_cat.reporte_huerfanas()) == 0

    def test_con_catalogo_sintetico_etiquetado(self, res_sint):
        r = res_sint.reporte
        assert r["catalogo"]["estado"] == "SINTETICO" and r["catalogo"]["ADVERTENCIA"]
        assert (res_sint.datos.origen_catalogo == "SINTETICO").all()
        assert r["huerfanas"]["registros"] == 61 and r["huerfanas"]["por_fuente"] == {idd.FUENTE_ATM: 32, idd.FUENTE_DIGITAL: 29}

    def test_indicadores_de_cuarentena_para_integrante_3(self, res_sint):
        d = res_sint.datos
        e = res_sint.reporte["indicadores"]["indicadores_error"]
        assert e["CLABE_INVALIDA"] == 46 and e["MONTO_FALTANTE"] == 27 and e["DUPLICADO_EXACTO"] == 20
        assert (d.requiere_cuarentena_sugerida == d.indicadores_error.ne("")).all()
        assert len(d) == 1220            # las causas viajan con la fila; nada se eliminó

    def test_escalado_y_segmentos_validos(self, res_sint):
        d = res_sint.datos
        m = d[d.es_operacion_monetaria & d.monto_mxn.notna()]
        assert m.monto_mxn_minmax.between(0, 1).all()
        assert set(m.segmento_monto.unique()) == {"Bajo", "Medio", "Alto"}
        assert d[~d.es_operacion_monetaria].segmento_monto.isna().all()

    def test_parametros_reproducibles_en_archivos_reales(self, tmp_path):
        ruta = tmp_path / "p.json"
        r1 = integrar_datos(RUTA_ATM, RUTA_JSON, crear_catalogo_sintetico(), ruta_parametros=ruta)
        r2 = integrar_datos(RUTA_ATM, RUTA_JSON, crear_catalogo_sintetico(), parametros=ruta)
        pd.testing.assert_frame_equal(r1.datos, r2.datos)
        assert json.loads(ruta.read_text(encoding="utf-8"))["tipo_cambio_usd_mxn"] == 19.5

    def test_guarda_todas_las_salidas(self, res_sint, tmp_path):
        rutas = res_sint.guardar_todo(tmp_path, "T_")
        assert all(p.is_file() and p.stat().st_size > 0 for p in rutas.values())
        assert "SINTETICO" in rutas["reporte"].read_text(encoding="utf-8")

    def test_cli_sin_catalogo(self, tmp_path):
        assert idd._main(["--atm", str(RUTA_ATM), "--json", str(RUTA_JSON), "--salida", str(tmp_path)]) == 0

    def test_cli_error_controlado_si_falta_archivo(self, tmp_path):
        assert idd._main(["--atm", "no.csv", "--json", str(RUTA_JSON), "--salida", str(tmp_path)]) == 2

    def test_stress_datos_corruptos_inyectados_no_hace_crash(self, crudos):
        atm, dig = crudos
        atm = atm.copy(); dig = dig.copy()
        atm = atm.astype(object); dig = dig.astype(object)
        atm.at[0, "fecha_hora"] = "¿?"; atm.at[0, "monto_mxn"] = "NaN"; atm.at[0, "id_cuenta"] = ""
        atm.at[1, "tipo_operacion"] = None
        dig.at[0, "timestamp"] = {"anidado": 1}           # tipo imprevisto dentro del JSON
        dig.at[1, "amount"] = "mil pesos"
        dig.at[2, "currency"] = None
        r = integrar_datos(atm, dig)
        assert len(r.datos) == 1220
        assert "FECHA_INVALIDA" in r.datos.iloc[0].indicadores_error