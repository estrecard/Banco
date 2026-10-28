"""Regresiones de contrato e integración con los archivos oficiales."""
from pathlib import Path
import pandas as pd
from integracion_datos import verificar_trazabilidad
from validacion_cuarentena import validar_transacciones
from test_validacion_cuarentena import transaccion


def test_copia_marcada_sin_indicador_error_se_rechaza():
    buenos, malos = validar_transacciones([transaccion(ind_duplicado_exacto=True)])
    assert not buenos
    assert any(e["codigo"] == "DUPLICADO_EXACTO" for e in malos[0]["errores_validacion"])


def test_verificador_detecta_filas_perdidas_e_identidad_incorrecta():
    atm = pd.DataFrame([{"id_transaccion": "A"}])
    digital = pd.DataFrame(columns=["transfer_id"])
    salida = pd.DataFrame([{"fuente": "ATM_CSV", "fila_origen": 0, "id_original": "A", "id_registro": "ATM_CSV#0"}])
    assert verificar_trazabilidad(salida, atm, digital)["ok"]
    assert not verificar_trazabilidad(salida.iloc[:0], atm, digital)["ok"]
    salida.loc[0, "id_original"] = "B"
    assert not verificar_trazabilidad(salida, atm, digital)["ok"]


def test_flujo_real_preserva_todos_los_originales(tmp_path):
    from demo_validacion import ejecutar_validacion
    raiz = Path(__file__).resolve().parents[1]
    # El catálogo de este test se deriva de identificadores observados, solo para
    # probar transporte; la demostración entregada usa el catálogo real externo.
    catalogo = tmp_path / "catalogo_PRUEBA.csv"
    catalogo.write_text("id_cuenta\n" + "\n".join(f"CTA-{i:04d}" for i in range(1, 51)), encoding="utf-8")
    validos, rechazados, resumen = ejecutar_validacion(
        raiz / "data/raw/transacciones_atm.csv", raiz / "data/raw/transferencias_digitales.json", catalogo)
    assert len(validos) + len(rechazados) == 1220
    assert resumen["trazabilidad_comprobada"]
    assert all(r["registro_original"] and r["registro_integrado"] for r in validos + rechazados)
    assert sum(r.get("ind_duplicado_exacto") is True for r in rechazados) == 20
    assert not any(r.get("ind_monto_imputado") or r.get("ind_monto_recortado") for r in validos)
    assert any(r["tipo_operacion"] == "CONSULTA_SALDO" and float(r["monto_mxn"]) == 0 for r in validos)
