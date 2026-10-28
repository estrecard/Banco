"""Pruebas con casos sintéticos del contrato de validación y cuarentena.

Los datos de este archivo son ejemplos de prueba, no transacciones reales.
Ejecutar desde la raíz: python -m pytest tests/test_validacion_cuarentena.py -q
"""

from copy import deepcopy
from decimal import Decimal
import json

from pydantic import BaseModel
import pytest

from validacion_cuarentena import TransaccionBancaria, validar_transacciones


def transaccion(**cambios):
    registro = {
        "fuente": "ATM_CSV",
        "id_registro": "ATM_CSV:archivo-prueba.csv:0",
        "archivo_origen": "archivo-prueba.csv",
        "fila_origen": 0,
        "id_original": "ATM-PRUEBA-1",
        "id_cuenta": "CTA-0001",
        "tipo_operacion": "RETIRO",
        "estado_operacion": "COMPLETADA",
        "fecha_hora_iso": "2026-09-15T08:45:00-06:00",
        "monto_mxn": "500.00",
        "monto_original": "500.00",
        "moneda_original": "MXN",
        "tipo_cambio_aplicado": "1",
        "comision_mxn": "15.00",
        "clabe_beneficiario": None,
        "cuenta_en_catalogo": True,
        "indicadores_error": "",
        "indicadores_alerta": "",
        "registro_original": {"id": "PRUEBA-1", "detalles": ["sin cambios"]},
    }
    registro.update(cambios)
    return registro


def transferencia(**cambios):
    registro = transaccion(
        fuente="TRANSFERENCIAS_JSON",
        id_registro="TRANSFERENCIAS_JSON:archivo-prueba.json:0",
        archivo_origen="archivo-prueba.json",
        id_original="SPEI-PRUEBA-1",
        tipo_operacion="TRANSFERENCIA_SPEI",
        comision_mxn=None,
        clabe_beneficiario="000000000000000001",
    )
    registro.update(cambios)
    return registro


def rechazar(registro):
    validos, rechazados = validar_transacciones([registro])
    assert validos == []
    assert len(rechazados) == 1
    rechazado = rechazados[0]
    assert rechazado["motivo_rechazo_cuarentena"]
    assert rechazado["errores_validacion"]
    for error in rechazado["errores_validacion"]:
        assert {"campo", "codigo", "mensaje"} <= error.keys()
        assert "input" not in error
        assert "ctx" not in error
    return rechazado


@pytest.mark.parametrize("registro", [
    transaccion(),
    transferencia(),
    transaccion(tipo_operacion="CONSULTA_SALDO", monto_mxn="0", monto_original="0", comision_mxn="0"),
    transaccion(estado_operacion="FALLIDA"),
    transferencia(estado_operacion="RECHAZADA"),
    transferencia(moneda_original="USD", monto_original="100", monto_mxn="1950", tipo_cambio_aplicado="19.50"),
])
def test_acepta_operaciones_validas_y_estados_no_exitosos(registro):
    validos, rechazados = validar_transacciones([registro])
    assert len(validos) == 1
    assert rechazados == []
    assert validos[0]["registro_original"] == registro["registro_original"]
    assert validos[0]["registro_integrado"]


def test_modelo_pydantic_con_decimal_y_serializacion_json():
    assert issubclass(TransaccionBancaria, BaseModel)
    modelo = TransaccionBancaria.model_validate(transaccion(monto_mxn="123.45", monto_original="123.45"))
    assert modelo.monto_mxn == Decimal("123.45")
    validos, rechazados = validar_transacciones([transaccion(monto_mxn="123.45", monto_original="123.45")])
    assert isinstance(validos[0]["monto_mxn"], str)
    assert Decimal(validos[0]["monto_mxn"]) == Decimal("123.45")
    json.dumps([validos, rechazados], allow_nan=False)


def test_valido_invalido_valido_continua_sin_perder_orden():
    registros = [transaccion(id_registro="primero"), transaccion(monto_mxn="invalido"), transaccion(id_registro="tercero")]
    validos, rechazados = validar_transacciones(registros)
    assert [r["id_registro"] for r in validos] == ["primero", "tercero"]
    assert len(rechazados) == 1
    assert len(validos) + len(rechazados) == len(registros)


@pytest.mark.parametrize("monto", [None, "", "no-es-dinero", True, "NaN", "Infinity", "-Infinity", "-1", "0"])
def test_montos_invalidos_en_operacion_monetaria(monto):
    rechazar(transaccion(monto_mxn=monto))


@pytest.mark.parametrize("campo,valor", [
    ("id_original", ""),
    ("id_cuenta", "CTA-12"),
    ("id_cuenta", 1234),
    ("fila_origen", -1),
    ("fila_origen", True),
    ("fuente", "DESCONOCIDA"),
    ("estado_operacion", "INVENTADO"),
    ("moneda_original", "EUR"),
    ("cuenta_en_catalogo", "True"),
    ("fecha_hora_iso", "2026-02-31T08:45:00-06:00"),
    ("fecha_hora_iso", "2026-09-15T08:45:00"),
    ("fecha_hora_iso", 1788346560),
])
def test_tipos_identificadores_fechas_y_categorias_invalidas(campo, valor):
    rechazar(transaccion(**{campo: valor}))


@pytest.mark.parametrize("comision", [None, "-0.01", True, "NaN"])
def test_comision_atm_requerida_finita_y_no_negativa(comision):
    rechazar(transaccion(comision_mxn=comision))


@pytest.mark.parametrize("clabe", [None, "12345", "0123456789012345678", "０１２３４５６７８９０１２３４５６７", 123456789012345678])
def test_clabe_digital_exige_texto_y_dieciocho_digitos_ascii(clabe):
    rechazar(transferencia(clabe_beneficiario=clabe))


def test_clabe_preserva_ceros_sin_imponer_checksum_no_acordado():
    clabe = "000000000000000001"
    validos, rechazados = validar_transacciones([transferencia(clabe_beneficiario=clabe)])
    assert rechazados == []
    assert validos[0]["clabe_beneficiario"] == clabe


@pytest.mark.parametrize("pertenencia,codigo", [(None, "CATALOGO_NO_VERIFICADO"), (False, "CUENTA_HUERFANA")])
def test_pertenencia_no_verificada_o_huerfana(pertenencia, codigo):
    rechazado = rechazar(transaccion(cuenta_en_catalogo=pertenencia))
    assert codigo in {e["codigo"] for e in rechazado["errores_validacion"]}


@pytest.mark.parametrize("indicador", ["", "CUENTA_HUERFANA"])
def test_catalogo_real_sustituye_indicador_y_catalogo_vacio_no_es_ausencia(indicador):
    registro = transaccion(cuenta_en_catalogo=False, indicadores_error=indicador)
    validos, rechazados = validar_transacciones([registro], catalogo_cuentas={"CTA-0001"})
    assert len(validos) == 1 and rechazados == []
    assert validos[0]["registro_integrado"]["indicadores_error"] == indicador
    assert validos[0]["cuenta_en_catalogo"] is True
    validos, rechazados = validar_transacciones([transaccion(cuenta_en_catalogo=True)], catalogo_cuentas=set())
    assert validos == [] and len(rechazados) == 1
    assert "CUENTA_HUERFANA" in {e["codigo"] for e in rechazados[0]["errores_validacion"]}


def test_catalogo_actualizado_no_oculta_otros_errores():
    registro = transaccion(cuenta_en_catalogo=False, indicadores_error="CUENTA_HUERFANA|FECHA_INVALIDA")
    validos, rechazados = validar_transacciones([registro], catalogo_cuentas={"CTA-0001"})
    assert not validos
    assert {e["codigo"] for e in rechazados[0]["errores_validacion"]} == {"FECHA_INVALIDA"}


@pytest.mark.parametrize("indicador", [
    "CUENTA_HUERFANA", "CLABE_INVALIDA", "MONTO_FALTANTE", "MONTO_NO_POSITIVO",
    "FECHA_INVALIDA", "MONEDA_NO_SOPORTADA", "CUENTA_INVALIDA", "DUPLICADO_EXACTO",
])
def test_indicadores_error_no_desaparecen_al_validar(indicador):
    rechazado = rechazar(transaccion(indicadores_error=indicador))
    assert indicador in {e["codigo"] for e in rechazado["errores_validacion"]}


@pytest.mark.parametrize("bandera", ["flag_monto_imputado", "flag_monto_winsorizado", "ind_monto_imputado", "ind_monto_winsorizado"])
def test_montos_transformados_conservan_evidencia_y_se_revisan(bandera):
    registro = transaccion(**{bandera: True})
    rechazado = rechazar(registro)
    assert rechazado["registro_integrado"][bandera] is True
    assert rechazado["registro_original"] == registro["registro_original"]


def test_alertas_no_son_rechazos_y_ids_repetidos_no_se_borran():
    primero = transaccion(indicadores_alerta="ID_REPETIDO_CONFLICTO|POSIBLE_REINTENTO_ATM")
    segundo = transaccion(fila_origen=1, id_registro="ATM_CSV:archivo-prueba.csv:1")
    tercero = transferencia(id_original=primero["id_original"])
    validos, rechazados = validar_transacciones([primero, segundo, tercero])
    assert len(validos) == 3
    assert rechazados == []


def test_acumula_errores_sin_revelar_valores_en_mensajes():
    secreto = "VALOR_SENSIBLE_NO_PUBLICAR"
    registro = transferencia(monto_mxn=secreto, clabe_beneficiario=secreto, indicadores_error="CUENTA_HUERFANA|MONEDA_NO_SOPORTADA")
    rechazado = rechazar(registro)
    errores = rechazado["errores_validacion"]
    assert len(errores) >= 4
    assert {"CUENTA_HUERFANA", "MONEDA_NO_SOPORTADA"} <= {e["codigo"] for e in errores}
    assert secreto not in rechazado["motivo_rechazo_cuarentena"]
    assert secreto not in json.dumps(errores)
    assert rechazado["registro_integrado"]["monto_mxn"] == secreto


@pytest.mark.parametrize("malformado", [None, 17, "texto", ["lista"]])
def test_registro_no_tabular_se_aisla_sin_detener_lote(malformado):
    validos, rechazados = validar_transacciones([malformado, transaccion()])
    assert len(validos) == 1
    assert len(rechazados) == 1
    assert rechazados[0]["motivo_rechazo_cuarentena"]


def test_no_muta_entradas_ni_comparte_el_original_anidado():
    registros = [transaccion(), transaccion(monto_mxn="mal")]
    antes = deepcopy(registros)
    validos, rechazados = validar_transacciones(registros)
    assert registros == antes
    validos[0]["registro_original"]["detalles"][0] = "editado"
    rechazados[0]["registro_original"]["detalles"].append("editado")
    assert registros == antes


def test_iterable_vacio_y_generador():
    assert validar_transacciones([]) == ([], [])
    assert validar_transacciones(iter(())) == ([], [])
    validos, rechazados = validar_transacciones(transaccion(fila_origen=i) for i in range(2))
    assert len(validos) == 2 and rechazados == []


def test_salida_con_nan_original_permite_json_estricto():
    registro = transaccion(monto_mxn=float("nan"), registro_original={"importe": float("nan")})
    validos, rechazados = validar_transacciones([registro])
    assert validos == [] and len(rechazados) == 1
    json.dumps(rechazados, allow_nan=False)


def test_tipo_cambio_debe_corresponder_a_moneda_original():
    rechazar(transaccion(moneda_original="MXN", tipo_cambio_aplicado="19.50"))
    rechazar(transferencia(moneda_original="USD", tipo_cambio_aplicado="1"))


def test_campo_identificador_requerido_ausente():
    registro = transaccion()
    del registro["id_original"]
    rechazar(registro)
