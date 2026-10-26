"""
REFERENCIA del módulo del Integrante 3 (validacion_cuarentena.py). NO es el módulo real.

CONTRATO
--------
    validar_transacciones(registros: list[dict], catalogo_cuentas: set[str] | None = None)
        -> tuple[list[dict], list[dict]]          # (validos, rechazados)

* ``catalogo_cuentas`` es ``None`` cuando NO hay catálogo disponible: en ese caso NO se debe
  validar existencia de cuentas (y no inventar un catálogo).
* ``validos``: los mismos dicts de entrada (formato canónico).
* ``rechazados``: dicts con ``motivo_rechazo_cuarentena`` (str no vacío; varios motivos se
  unen con "; ") y, idealmente, ``fuente_origen``, ``id_origen`` y ``registro_original``.
* len(validos) + len(rechazados) == len(registros). Nunca lanzar excepción por datos sucios.

Las reglas y umbrales de abajo son PROVISIONALES (ver REGLAS_DE_NEGOCIO.md).
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

MONTO_MAXIMO = Decimal("1000000.00")
TIPOS_VALIDOS = {"RETIRO", "DEPOSITO", "TRANSFERENCIA", "PAGO"}
MONEDAS_VALIDAS = {"MXN", "USD"}
PATRON_CUENTA = re.compile(r"^[A-Z0-9]{6,24}$")
FECHA_MINIMA = "2000-01-01 00:00:00"

MOTIVO_ID_AUSENTE = "id_origen ausente"
MOTIVO_ID_DUPLICADO = "id_origen duplicado en el lote"
MOTIVO_FECHA_INVALIDA = "fecha_hora ausente o inválida"
MOTIVO_FECHA_FUTURA = "fecha_hora en el futuro"
MOTIVO_CUENTA_ORIGEN_AUSENTE = "cuenta_origen ausente"
MOTIVO_CUENTA_FORMATO = "cuenta con formato inválido"
MOTIVO_DESTINO_AUSENTE = "cuenta_destino ausente en transferencia"
MOTIVO_DESTINO_IGUAL = "cuenta_destino igual a cuenta_origen"
MOTIVO_CUENTA_NO_CATALOGO = "cuenta inexistente en catálogo"
MOTIVO_MONTO_INVALIDO = "monto ausente o no numérico"
MOTIVO_MONTO_NO_POSITIVO = "monto no positivo"
MOTIVO_MONTO_ANOMALO = "monto anómalo (excede el máximo)"
MOTIVO_TIPO_INVALIDO = "tipo_transaccion ausente o no reconocido"
MOTIVO_MONEDA_INVALIDA = "moneda ausente o no reconocida"


def _motivos(reg: dict, catalogo: set[str] | None, ids_vistos: set, ahora: str) -> list[str]:
    motivos: list[str] = []
    fuente = reg.get("fuente_origen")
    id_origen = reg.get("id_origen")

    if not id_origen:
        motivos.append(MOTIVO_ID_AUSENTE)
    else:
        clave = (fuente, id_origen)
        if clave in ids_vistos:
            motivos.append(MOTIVO_ID_DUPLICADO)
        ids_vistos.add(clave)

    fecha = reg.get("fecha_hora")
    if not fecha:
        motivos.append(MOTIVO_FECHA_INVALIDA)
    elif fecha > ahora:
        motivos.append(MOTIVO_FECHA_FUTURA)
    elif fecha < FECHA_MINIMA:
        motivos.append(MOTIVO_FECHA_INVALIDA)

    monto = reg.get("monto")
    if not isinstance(monto, Decimal) or not monto.is_finite():
        motivos.append(MOTIVO_MONTO_INVALIDO)
    elif monto <= 0:
        motivos.append(MOTIVO_MONTO_NO_POSITIVO)
    elif monto > MONTO_MAXIMO:
        motivos.append(MOTIVO_MONTO_ANOMALO)
    elif monto != monto.quantize(Decimal("0.01")):
        motivos.append(MOTIVO_MONTO_INVALIDO)

    if reg.get("moneda") not in MONEDAS_VALIDAS:
        motivos.append(MOTIVO_MONEDA_INVALIDA)
    if reg.get("tipo_transaccion") not in TIPOS_VALIDOS:
        motivos.append(MOTIVO_TIPO_INVALIDO)

    origen, destino = reg.get("cuenta_origen"), reg.get("cuenta_destino")
    if not origen:
        motivos.append(MOTIVO_CUENTA_ORIGEN_AUSENTE)
    elif not PATRON_CUENTA.match(origen):
        motivos.append(MOTIVO_CUENTA_FORMATO)
    elif catalogo is not None and origen not in catalogo:
        motivos.append(MOTIVO_CUENTA_NO_CATALOGO)

    if fuente == "DIGITAL":
        if not destino:
            motivos.append(MOTIVO_DESTINO_AUSENTE)
        elif destino == origen:
            motivos.append(MOTIVO_DESTINO_IGUAL)
        elif not PATRON_CUENTA.match(destino):
            motivos.append(MOTIVO_CUENTA_FORMATO)
        elif catalogo is not None and destino not in catalogo and MOTIVO_CUENTA_NO_CATALOGO not in motivos:
            motivos.append(MOTIVO_CUENTA_NO_CATALOGO)
    return motivos


def validar_transacciones(registros: list[dict], catalogo_cuentas: set[str] | None = None):
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    vistos: set = set()
    validos: list[dict] = []
    rechazados: list[dict] = []
    for reg in registros:
        motivos = _motivos(reg, catalogo_cuentas, vistos, ahora)
        if motivos:
            rechazados.append({
                "fuente_origen": reg.get("fuente_origen"),
                "id_origen": reg.get("id_origen"),
                "motivo_rechazo_cuarentena": "; ".join(motivos),
                "registro_original": reg.get("registro_original"),
            })
        else:
            validos.append(reg)
    return validos, rechazados
