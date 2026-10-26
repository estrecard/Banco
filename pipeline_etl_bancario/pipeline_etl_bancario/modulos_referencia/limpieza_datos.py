"""
REFERENCIA del módulo del Integrante 1 (limpieza_datos.py). NO es el módulo real.

CONTRATO
--------
    limpiar_registros_atm(registros: list[dict])      -> list[dict]
    limpiar_registros_digitales(registros: list[dict]) -> list[dict]

* Entrada: registros crudos tal como salen del CSV/JSON (dicts).
* Salida: UNA fila por cada fila de entrada, en el mismo orden (no descartar filas: lo inválido
  lo decide la validación y va a cuarentena). Claves del formato canónico:
      fuente_origen, id_origen, fecha_hora ("YYYY-MM-DD HH:MM:SS" o None),
      cuenta_origen, cuenta_destino, monto (Decimal o None), moneda, tipo_transaccion,
      estado, registro_original (el dict crudo, para trazabilidad)
* Nulos -> None (nunca "", "NULL", "nan"). No debe lanzar excepción por datos sucios.

Los alias de columnas de abajo son SUPUESTOS sobre los archivos originales (que no estaban
disponibles); ajustar al esquema real de transacciones_atm.csv / transferencias_digitales.json.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

NULOS = {"", "null", "none", "nan", "n/a", "na", "-", "sin dato", "nulo"}

FORMATOS_FECHA = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y",
)


def _texto(valor: Any) -> str | None:
    if valor is None:
        return None
    s = str(valor).strip()
    return None if s.lower() in NULOS else s


def _monto(valor: Any) -> Decimal | None:
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, float) and valor != valor:  # NaN
        return None
    if isinstance(valor, (int, float, Decimal)):
        s = str(valor)
    else:
        s = _texto(valor)
        if s is None:
            return None
        s = re.sub(r"(?i)\b(mxn|usd|pesos)\b|[$\s]", "", s).replace(",", "")
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _fecha(valor: Any) -> str | None:
    s = _texto(valor)
    if s is None:
        return None
    for fmt in FORMATOS_FECHA:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _primero(reg: dict, alias: tuple[str, ...]) -> Any:
    for clave in alias:
        if clave in reg:
            return reg[clave]
    return None


def _mayus(valor: Any) -> str | None:
    t = _texto(valor)
    return t.upper() if t else None


def limpiar_registros_atm(registros: list[dict]) -> list[dict]:
    salida = []
    for r in registros:
        salida.append({
            "fuente_origen": "ATM",
            "id_origen": _texto(_primero(r, ("id_atm", "id_transaccion", "id"))),
            "fecha_hora": _fecha(_primero(r, ("fecha_hora", "fecha", "timestamp"))),
            "cuenta_origen": _mayus(_primero(r, ("numero_cuenta", "cuenta", "cuenta_origen"))),
            "cuenta_destino": None,
            "monto": _monto(_primero(r, ("monto", "importe"))),
            "moneda": _mayus(_primero(r, ("moneda", "divisa"))),
            "tipo_transaccion": _mayus(_primero(r, ("tipo_operacion", "tipo", "tipo_transaccion"))),
            "estado": _mayus(_primero(r, ("estado", "estatus"))),
            "registro_original": dict(r),
        })
    return salida


def limpiar_registros_digitales(registros: list[dict]) -> list[dict]:
    salida = []
    for r in registros:
        salida.append({
            "fuente_origen": "DIGITAL",
            "id_origen": _texto(_primero(r, ("id_transferencia", "id_transaccion", "id"))),
            "fecha_hora": _fecha(_primero(r, ("timestamp", "fecha_hora", "fecha"))),
            "cuenta_origen": _mayus(_primero(r, ("cuenta_origen", "origen"))),
            "cuenta_destino": _mayus(_primero(r, ("cuenta_destino", "destino"))),
            "monto": _monto(_primero(r, ("monto", "importe"))),
            "moneda": _mayus(_primero(r, ("moneda", "divisa"))),
            "tipo_transaccion": _mayus(_primero(r, ("tipo", "tipo_transaccion", "tipo_operacion"))) or "TRANSFERENCIA",
            "estado": _mayus(_primero(r, ("estatus", "estado"))),
            "registro_original": dict(r),
        })
    return salida
