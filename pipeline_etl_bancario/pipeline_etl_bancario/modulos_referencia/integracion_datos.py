"""
REFERENCIA del módulo del Integrante 2 (integracion_datos.py). NO es el módulo real.

CONTRATO
--------
    integrar_fuentes(atm_limpios: list[dict], digitales_limpios: list[dict]) -> list[dict]

* Unifica ambas fuentes en un solo esquema (el formato canónico definido en limpieza_datos).
* NO debe perder registros silenciosamente: len(salida) == len(atm) + len(digitales).
  Los duplicados se marcan/rechazan en validacion_cuarentena, no se descartan aquí.
* Debe conservar fuente_origen, id_origen y registro_original (trazabilidad).
"""
from __future__ import annotations

CLAVES_CANONICAS = (
    "fuente_origen", "id_origen", "fecha_hora", "cuenta_origen", "cuenta_destino",
    "monto", "moneda", "tipo_transaccion", "estado", "registro_original",
)


def integrar_fuentes(atm_limpios: list[dict], digitales_limpios: list[dict]) -> list[dict]:
    unificados = []
    for reg in list(atm_limpios) + list(digitales_limpios):
        fila = {clave: reg.get(clave) for clave in CLAVES_CANONICAS}
        unificados.append(fila)
    # Orden cronológico estable; registros sin fecha al final.
    unificados.sort(key=lambda r: (r["fecha_hora"] is None, r["fecha_hora"] or ""))
    return unificados
