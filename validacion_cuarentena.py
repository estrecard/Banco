"""Validación Pydantic 2 por registro sobre el esquema del integrante 2.

``validar_transacciones(registros, catalogo_cuentas=None)`` devuelve dos listas
JSON compatibles. El catálogo opcional es un conjunto de identificadores internos;
si no se proporciona se usa la comprobación explícita ``cuenta_en_catalogo``.
La evidencia se conserva separada de los mensajes de rechazo, que nunca muestran
valores recibidos. Véase REGLAS_DE_NEGOCIO.md para las políticas académicas.
"""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from numbers import Real
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from pydantic import ValidationError, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

Dinero = Annotated[Decimal, Field(allow_inf_nan=False, decimal_places=2)]

# Los textos son constantes: nunca se interpola el valor que ocasionó el error.
ERRORES_INTEGRACION = {
    "CUENTA_HUERFANA": ("cuenta_en_catalogo", "Cuenta interna ausente del catálogo."),
    "CLABE_INVALIDA": ("clabe_beneficiario", "La CLABE debe contener 18 dígitos ASCII."),
    "MONTO_FALTANTE": ("monto_mxn", "Falta el importe de la operación."),
    "MONTO_NO_POSITIVO": ("monto_mxn", "Importe incompatible con el tipo de operación."),
    "FECHA_INVALIDA": ("fecha_hora_iso", "Fecha inválida o no interpretable."),
    "MONEDA_NO_SOPORTADA": ("moneda_original", "Moneda de origen no soportada."),
    "CUENTA_INVALIDA": ("id_cuenta", "Identificador de cuenta interna inválido."),
    "DUPLICADO_EXACTO": ("rol_duplicado", "Copia secundaria exacta de una operación."),
}
FLAGS_MONTO = {
    "flag_monto_imputado": ("MONTO_IMPUTADO", "Importe imputado: requiere confirmación del importe real."),
    "flag_monto_winsorizado": ("MONTO_RECORTADO", "Importe recortado: requiere confirmación del importe real."),
    "ind_monto_imputado": ("MONTO_IMPUTADO", "Importe imputado: requiere confirmación del importe real."),
    "ind_monto_recortado": ("MONTO_RECORTADO", "Importe recortado: requiere confirmación del importe real."),
    "ind_monto_winsorizado": ("MONTO_RECORTADO", "Importe recortado: requiere confirmación del importe real."),
}


def _decimal(valor: Any) -> Decimal | None:
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, (str, Decimal, Real)):
        raise PydanticCustomError("DINERO_INVALIDO", "Se requiere un importe numérico finito; no se admiten booleanos.")
    try:
        numero = Decimal(str(valor))
    except InvalidOperation:
        raise PydanticCustomError("DINERO_INVALIDO", "Se requiere un importe numérico finito.") from None
    if not numero.is_finite():
        raise PydanticCustomError("DINERO_INVALIDO", "Se requiere un importe numérico finito.")
    return numero


class TransaccionBancaria(BaseModel):
    """Campos comunes y reglas condicionales; la auditoría queda fuera del modelo.

    Las columnas analíticas adicionales se conservan en el sobre de salida.
    No se valida checksum, existencia ni titularidad bancaria de la CLABE.
    """

    model_config = ConfigDict(extra="ignore", validate_default=True, hide_input_in_errors=True)

    id_registro: StrictStr = Field(min_length=1)
    fuente: Literal["ATM_CSV", "TRANSFERENCIAS_JSON"]
    archivo_origen: StrictStr = Field(min_length=1)
    fila_origen: StrictInt = Field(ge=0)
    id_original: StrictStr = Field(min_length=1)
    id_cuenta: StrictStr
    tipo_operacion: Literal["RETIRO", "DEPOSITO", "PAGO_SERVICIOS", "CONSULTA_SALDO", "TRANSFERENCIA_SPEI"]
    estado_operacion: Literal["COMPLETADA", "FALLIDA", "PENDIENTE", "RECHAZADA", "DEVUELTA"]
    fecha_hora_iso: datetime
    moneda_original: Literal["MXN", "USD"]
    tipo_cambio_aplicado: Decimal
    monto_mxn: Dinero
    monto_original: Decimal | None = None
    comision_mxn: Dinero | None = None
    clabe_beneficiario: StrictStr | None = None
    cuenta_en_catalogo: StrictBool | None = None
    flag_monto_imputado: StrictBool = False
    flag_monto_winsorizado: StrictBool = False
    ind_monto_imputado: StrictBool = False
    ind_monto_recortado: StrictBool = False
    ind_monto_winsorizado: StrictBool = False

    @field_validator("id_registro", "archivo_origen", "id_original")
    @classmethod
    def identificador_no_vacio(cls, valor: str) -> str:
        if not valor.strip():
            raise PydanticCustomError("IDENTIFICADOR_VACIO", "Se requiere un identificador no vacío.")
        return valor

    @field_validator("id_cuenta")
    @classmethod
    def cuenta_interna(cls, valor: str) -> str:
        if re.fullmatch(r"CTA-[0-9]{4}", valor) is None:
            raise PydanticCustomError("CUENTA_INVALIDA", "Identificador de cuenta interna inválido.")
        return valor

    @field_validator("tipo_operacion")
    @classmethod
    def operacion_por_fuente(cls, valor: str, info: ValidationInfo) -> str:
        fuente = info.data.get("fuente")
        if (fuente == "TRANSFERENCIAS_JSON" and valor != "TRANSFERENCIA_SPEI") or (
            fuente == "ATM_CSV" and valor == "TRANSFERENCIA_SPEI"
        ):
            raise PydanticCustomError("TIPO_FUENTE_INCOMPATIBLE", "El tipo de operación no corresponde a la fuente.")
        return valor

    @field_validator("fecha_hora_iso", mode="before")
    @classmethod
    def fecha_iso(cls, valor: Any) -> datetime:
        if isinstance(valor, str):
            try:
                valor = datetime.fromisoformat(valor.replace("Z", "+00:00"))
            except ValueError:
                raise PydanticCustomError("FECHA_INVALIDA", "Se requiere fecha ISO 8601 con hora y zona horaria.") from None
        if not isinstance(valor, datetime) or valor.tzinfo is None or valor.utcoffset() is None:
            raise PydanticCustomError("FECHA_INVALIDA", "Se requiere fecha ISO 8601 con hora y zona horaria.")
        return valor

    @field_validator("monto_mxn", "monto_original", "comision_mxn", "tipo_cambio_aplicado", mode="before")
    @classmethod
    def dinero_finito(cls, valor: Any) -> Decimal | None:
        return _decimal(valor)

    @field_validator("tipo_cambio_aplicado")
    @classmethod
    def tipo_cambio(cls, valor: Decimal, info: ValidationInfo) -> Decimal:
        moneda = info.data.get("moneda_original")
        esperado = {"MXN": Decimal("1"), "USD": Decimal("19.50")}.get(moneda)
        if esperado is not None and valor != esperado:
            raise PydanticCustomError("TIPO_CAMBIO_INVALIDO", "Tipo de cambio distinto del convenio académico.")
        return valor

    @field_validator("monto_mxn")
    @classmethod
    def monto_por_operacion(cls, valor: Decimal, info: ValidationInfo) -> Decimal:
        tipo = info.data.get("tipo_operacion")
        if valor < 0 or (tipo is not None and tipo != "CONSULTA_SALDO" and valor == 0):
            raise PydanticCustomError("MONTO_NO_POSITIVO", "Importe incompatible con el tipo de operación.")
        return valor

    @field_validator("comision_mxn")
    @classmethod
    def comision_por_fuente(cls, valor: Decimal | None, info: ValidationInfo) -> Decimal | None:
        if valor is None and info.data.get("fuente") == "ATM_CSV":
            raise PydanticCustomError("COMISION_FALTANTE", "La comisión ATM requiere un valor explícito; cero es válido.")
        if valor is not None and valor < 0:
            raise PydanticCustomError("COMISION_NEGATIVA", "Comisión negativa sin regla de devolución acordada.")
        return valor

    @field_validator("clabe_beneficiario")
    @classmethod
    def clabe_por_fuente(cls, valor: str | None, info: ValidationInfo) -> str | None:
        if info.data.get("fuente") == "TRANSFERENCIAS_JSON" and (
            valor is None or re.fullmatch(r"[0-9]{18}", valor) is None
        ):
            raise PydanticCustomError("CLABE_INVALIDA", "La CLABE debe contener 18 dígitos ASCII.")
        return valor

    @field_validator("cuenta_en_catalogo")
    @classmethod
    def pertenencia_catalogo(cls, valor: bool | None) -> bool:
        if valor is None:
            raise PydanticCustomError("CATALOGO_NO_VERIFICADO", "No se comprobó la pertenencia de la cuenta al catálogo.")
        if not valor:
            raise PydanticCustomError("CUENTA_HUERFANA", "Cuenta interna ausente del catálogo.")
        return valor

    @field_validator(*FLAGS_MONTO)
    @classmethod
    def importe_confirmado(cls, valor: bool, info: ValidationInfo) -> bool:
        if valor:
            codigo, mensaje = FLAGS_MONTO[info.field_name]
            raise PydanticCustomError(codigo, mensaje)
        return valor


def _normalizar_nulos(valor: Any) -> Any:
    """Solo adapta escalares de Pandas/NumPy; la copia auditada no se altera."""
    if valor is None or type(valor).__name__ in ("NAType", "NaTType"):
        return None
    if type(valor).__module__.startswith("numpy") and hasattr(valor, "item"):
        valor = valor.item()
    if isinstance(valor, float) and math.isnan(valor):
        return None
    return valor


def a_json_seguro(valor: Any) -> Any:
    """Convierte auditoría a tipos JSON sin emitir NaN/Infinity ni perder su señal.

    Los números no finitos del original se representan mediante una etiqueta,
    y los tipos ajenos al contrato JSON mediante su nombre de tipo.
    """
    if valor is None or type(valor).__name__ in ("NAType", "NaTType"):
        return None
    if type(valor).__module__.startswith("numpy") and hasattr(valor, "item"):
        return a_json_seguro(valor.item())
    if isinstance(valor, Mapping):
        return {str(k): a_json_seguro(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [a_json_seguro(v) for v in valor]
    if isinstance(valor, Decimal):
        return str(valor) if valor.is_finite() else {"numero_no_finito": str(valor)}
    if isinstance(valor, float) and not math.isfinite(valor):
        return {"numero_no_finito": str(valor)}
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, (str, int, float, bool)):
        return valor
    return {"tipo_no_json": type(valor).__name__}


def _error(campo: str, codigo: str, mensaje: str) -> dict[str, str]:
    return {"campo": campo, "codigo": codigo, "mensaje": mensaje}


def _errores_indicadores(registro: Mapping) -> list[dict[str, str]]:
    errores = []
    duplicado = _normalizar_nulos(registro.get("ind_duplicado_exacto"))
    if duplicado is True:
        campo, mensaje = ERRORES_INTEGRACION["DUPLICADO_EXACTO"]
        errores.append(_error(campo, "DUPLICADO_EXACTO", mensaje))
    elif duplicado is not None and not isinstance(duplicado, bool):
        errores.append(_error("ind_duplicado_exacto", "INDICADORES_INVALIDOS", "La marca de duplicado debe ser booleana."))
    indicadores = _normalizar_nulos(registro.get("indicadores_error"))
    if indicadores is not None and not isinstance(indicadores, str):
        errores.append(_error("indicadores_error", "INDICADORES_INVALIDOS", "Formato inválido de indicadores de integración."))
    elif indicadores:
        for codigo in indicadores.split("|"):
            if codigo in ERRORES_INTEGRACION:
                campo, mensaje = ERRORES_INTEGRACION[codigo]
                errores.append(_error(campo, codigo, mensaje))
            elif codigo.strip():
                errores.append(_error("indicadores_error", "INDICADOR_NO_RECONOCIDO", "Indicador de integración desconocido: revisar el contrato."))
    # Se conservan motivos aun si otros campos impiden ejecutar un validador.
    for campo, (codigo, mensaje) in FLAGS_MONTO.items():
        if _normalizar_nulos(registro.get(campo)) is True:
            errores.append(_error(campo, codigo, mensaje))
    return errores


def validar_transacciones(
    registros: Iterable[Any], catalogo_cuentas: set[str] | frozenset[str] | None = None
) -> tuple[list[dict], list[dict]]:
    """Separa filas sin mutarlas; conserva una salida por cada entrada.

    Consuma un DataFrame con ``.to_dict(orient='records')``. Un registro escalar
    se rechaza individualmente. Un catálogo proporcionado se usa para volver a
    comprobar pertenencia; ``None`` usa el resultado explícito de integración.
    La salida financiera normalizada usa cadenas decimales y fechas ISO.
    """
    if catalogo_cuentas is not None and not isinstance(catalogo_cuentas, (set, frozenset)):
        raise TypeError("catalogo_cuentas debe ser un conjunto de identificadores o None.")
    validos, rechazados = [], []
    for posicion, entrada in enumerate(registros):
        original_integrado = copy.deepcopy(entrada)
        es_registro = isinstance(entrada, Mapping)
        evidencia = entrada.get("registro_original", entrada) if es_registro else entrada
        sobre = a_json_seguro(original_integrado) if es_registro else {}
        sobre["registro_integrado"] = a_json_seguro(original_integrado)
        sobre["registro_original"] = a_json_seguro(copy.deepcopy(evidencia))
        sobre["posicion_validacion"] = posicion
        errores = _errores_indicadores(entrada) if es_registro else []
        datos = {k: _normalizar_nulos(v) for k, v in entrada.items() if k in TransaccionBancaria.model_fields} if es_registro else entrada
        if es_registro and catalogo_cuentas is not None:
            cuenta = datos.get("id_cuenta")
            datos["cuenta_en_catalogo"] = isinstance(cuenta, str) and cuenta in catalogo_cuentas
            # El catálogo explícito sustituye la comprobación anterior. La
            # señal histórica permanece intacta dentro de registro_integrado.
            errores = [e for e in errores if e["codigo"] != "CUENTA_HUERFANA"]
        try:
            modelo = TransaccionBancaria.model_validate(datos)
        except ValidationError as exc:
            for detalle in exc.errors(include_input=False, include_context=False, include_url=False):
                campo = detalle["loc"][0] if detalle["loc"] else "registro"
                campo = campo if campo in TransaccionBancaria.model_fields else "registro"
                codigo = detalle["type"]
                # Los mensajes de errores incorporados podrían incluir valores
                # de un Enum/contexto; solo se usan textos propios controlados.
                mensaje = detalle["msg"] if codigo.isupper() else "Campo ausente, tipo incorrecto o valor fuera del esquema."
                errores.append(_error(campo, codigo, mensaje))
        else:
            sobre.update(modelo.model_dump(mode="json"))
        if errores:
            unicos = {(e["campo"], e["codigo"]): e for e in errores}
            sobre["errores_validacion"] = list(unicos.values())
            sobre["motivo_rechazo_cuarentena"] = "; ".join(
                f"{e['campo']} [{e['codigo']}]: {e['mensaje']}" for e in unicos.values()
            )
            rechazados.append(sobre)
        else:
            validos.append(sobre)
    return validos, rechazados
