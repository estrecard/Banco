"""
datos_sinteticos.py - GENERADOR DE DATOS SINTÉTICOS (solo pruebas y demostración).

*** Nada de lo que produce este módulo son datos bancarios reales ni el catálogo original
    ``catalogo_cuentas.csv``. El catálogo sintético se usa únicamente para probar el pipeline. ***

El esquema de columnas es un SUPUESTO (los archivos originales no estaban disponibles).
Inyecta defectos conocidos y cuenta cuántos de cada tipo, para que las pruebas puedan verificar
que cada uno terminó en cuarentena.
"""
from __future__ import annotations

import csv
import json
import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

AVISO_SINTETICO = "DATOS SINTÉTICOS: solo para pruebas; no son datos bancarios reales."

COLUMNAS_ATM = ["id_atm", "fecha_hora", "numero_cuenta", "tipo_operacion", "monto", "moneda",
                "ubicacion_atm", "estado"]

CUENTA_INEXISTENTE = "9999999999"


def generar_catalogo(n: int = 100) -> list[str]:
    """Catálogo SINTÉTICO de cuentas: 1000000001 ... (10 dígitos)."""
    return [f"{1_000_000_000 + i:010d}" for i in range(1, n + 1)]


def _fecha(rng: random.Random) -> datetime:
    base = datetime(2026, 1, 1)
    return base + timedelta(seconds=rng.randint(0, 270 * 24 * 3600))


def _fila_atm(i: int, cuentas: list[str], rng: random.Random) -> dict:
    return {
        "id_atm": f"ATM-{i:06d}",
        "fecha_hora": _fecha(rng).strftime("%Y-%m-%d %H:%M:%S"),
        "numero_cuenta": rng.choice(cuentas),
        "tipo_operacion": rng.choice(["RETIRO", "DEPOSITO"]),
        "monto": f"{rng.randint(1, 2_000_000) / 100:.2f}",
        "moneda": "MXN",
        "ubicacion_atm": f"SUC-{rng.randint(1, 30):02d}",
        "estado": "EXITOSA",
    }


def _fila_digital(i: int, cuentas: list[str], rng: random.Random) -> dict:
    origen, destino = rng.sample(cuentas, 2)
    return {
        "id_transferencia": f"TRF-{i:06d}",
        "timestamp": _fecha(rng).strftime("%Y-%m-%dT%H:%M:%S"),
        "cuenta_origen": origen,
        "cuenta_destino": destino,
        "monto": round(rng.randint(1, 5_000_000) / 100, 2),
        "moneda": rng.choice(["MXN", "MXN", "USD"]),
        "canal": rng.choice(["APP", "WEB"]),
        "estatus": "COMPLETADA",
    }


# Defectos: nombre -> (función que muta la fila, fragmento de motivo esperado en cuarentena)
def _defectos_atm() -> dict:
    from modulos_referencia import validacion_cuarentena as V
    return {
        "monto_negativo": (lambda r: r.update(monto="-150.00"), V.MOTIVO_MONTO_NO_POSITIVO),
        "monto_cero": (lambda r: r.update(monto="0.00"), V.MOTIVO_MONTO_NO_POSITIVO),
        "monto_anomalo": (lambda r: r.update(monto="9999999.99"), V.MOTIVO_MONTO_ANOMALO),
        "monto_texto": (lambda r: r.update(monto="abc"), V.MOTIVO_MONTO_INVALIDO),
        "monto_nulo": (lambda r: r.update(monto=""), V.MOTIVO_MONTO_INVALIDO),
        "cuenta_nula": (lambda r: r.update(numero_cuenta=""), V.MOTIVO_CUENTA_ORIGEN_AUSENTE),
        "cuenta_inexistente": (lambda r: r.update(numero_cuenta=CUENTA_INEXISTENTE), V.MOTIVO_CUENTA_NO_CATALOGO),
        "fecha_invalida": (lambda r: r.update(fecha_hora="31/02/2026 10:00"), V.MOTIVO_FECHA_INVALIDA),
        "fecha_futura": (lambda r: r.update(fecha_hora="2999-01-01 00:00:00"), V.MOTIVO_FECHA_FUTURA),
        "tipo_desconocido": (lambda r: r.update(tipo_operacion="BAILE"), V.MOTIVO_TIPO_INVALIDO),
        "id_nulo": (lambda r: r.update(id_atm=""), V.MOTIVO_ID_AUSENTE),
    }


def _defectos_digitales() -> dict:
    from modulos_referencia import validacion_cuarentena as V
    return {
        "monto_negativo": (lambda r: r.update(monto=-5.5), V.MOTIVO_MONTO_NO_POSITIVO),
        "monto_anomalo": (lambda r: r.update(monto=50_000_000.0), V.MOTIVO_MONTO_ANOMALO),
        "monto_nulo": (lambda r: r.update(monto=None), V.MOTIVO_MONTO_INVALIDO),
        "origen_nulo": (lambda r: r.update(cuenta_origen=None), V.MOTIVO_CUENTA_ORIGEN_AUSENTE),
        "destino_nulo": (lambda r: r.update(cuenta_destino=None), V.MOTIVO_DESTINO_AUSENTE),
        "destino_igual_origen": (lambda r: r.update(cuenta_destino=r["cuenta_origen"]), V.MOTIVO_DESTINO_IGUAL),
        "destino_inexistente": (lambda r: r.update(cuenta_destino=CUENTA_INEXISTENTE), V.MOTIVO_CUENTA_NO_CATALOGO),
        "moneda_invalida": (lambda r: r.update(moneda="XXX"), V.MOTIVO_MONEDA_INVALIDA),
        "fecha_invalida": (lambda r: r.update(timestamp="no-es-fecha"), V.MOTIVO_FECHA_INVALIDA),
    }


def crear_dataset(
    directorio: str | Path,
    n_atm: int = 200,
    n_digital: int = 200,
    semilla: int = 7,
    cada: int = 20,
    con_defectos: bool = True,
) -> dict:
    """
    Escribe ``transacciones_atm.csv``, ``transferencias_digitales.json`` y
    ``catalogo_cuentas_SINTETICO.csv`` en ``directorio``.

    Cada ``cada``-ésima fila recibe un defecto (rotando por los tipos). Además añade:
      * 1 duplicado exacto por fuente (id repetido -> "id_origen duplicado en el lote"),
      * 2 filas CSV malformadas (columnas de menos / de más),
      * 3 elementos JSON que no son objetos.
    Devuelve rutas y los conteos esperados (``esperados``: fragmento de motivo -> cantidad).
    """
    rng = random.Random(semilla)
    d = Path(directorio)
    d.mkdir(parents=True, exist_ok=True)
    catalogo = generar_catalogo()
    esperados: Counter = Counter()

    atm = [_fila_atm(i, catalogo, rng) for i in range(1, n_atm + 1)]
    dig = [_fila_digital(i, catalogo, rng) for i in range(1, n_digital + 1)]

    if con_defectos:
        for lista, defs in ((atm, _defectos_atm()), (dig, _defectos_digitales())):
            nombres = list(defs)
            k = 0
            for idx in range(cada - 1, len(lista), cada):
                mutar, fragmento = defs[nombres[k % len(nombres)]]
                mutar(lista[idx])
                esperados[fragmento] += 1
                k += 1
        from modulos_referencia import validacion_cuarentena as V
        # Duplicados exactos (se añaden al final; el segundo aparece como duplicado).
        if len(atm) > 1:
            atm.append(dict(atm[0]))
            esperados[V.MOTIVO_ID_DUPLICADO] += 1
        if len(dig) > 1:
            dig.append(dict(dig[0]))
            esperados[V.MOTIVO_ID_DUPLICADO] += 1

    ruta_atm = d / "transacciones_atm.csv"
    with open(ruta_atm, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS_ATM)
        w.writeheader()
        w.writerows(atm)
        if con_defectos:
            f.write("ATM-MALFORMADA-1,2026-02-02 10:00:00,1000000001\n")            # columnas de menos
            f.write("ATM-MALFORMADA-2,2026-02-02 10:00:00,1000000001,RETIRO,10.00,MXN,SUC-01,EXITOSA,EXTRA,EXTRA\n")
            esperados["fila_malformada"] += 2

    ruta_dig = d / "transferencias_digitales.json"
    contenido = list(dig)
    if con_defectos:
        contenido += ["texto suelto", None, [1, 2, 3]]
        esperados["fila_malformada"] += 3
    ruta_dig.write_text(json.dumps(contenido, ensure_ascii=False, indent=1), encoding="utf-8")

    ruta_cat = d / "catalogo_cuentas_SINTETICO.csv"
    with open(ruta_cat, "w", newline="", encoding="utf-8") as f:
        f.write(f"# {AVISO_SINTETICO}\n")
        f.write("cuenta\n")
        for c in catalogo:
            f.write(c + "\n")

    return {
        "ruta_atm": ruta_atm,
        "ruta_digital": ruta_dig,
        "ruta_catalogo": ruta_cat,
        "catalogo": set(catalogo),
        "esperados": esperados,
        "total_filas_atm": len(atm) + (2 if con_defectos else 0),
        "total_elementos_digital": len(contenido),
    }
