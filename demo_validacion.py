"""Demostración del integrante 3 con los módulos reales de sus compañeros."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from integracion_datos import integrar_datos, leer_atm_csv, leer_transferencias_json
from src.limpieza_datos import limpiar_datos
from validacion_cuarentena import validar_transacciones


def ejecutar_validacion(atm: Path, digital: Path, catalogo: Path):
    """Preserva el crudo fuera del DataFrame para no alterar los duplicados.

    Devuelve válidos, rechazados y resumen; no escribe archivos ni usa SQLite.
    El catálogo real es obligatorio para esta demostración referencial.
    """
    a, d = leer_atm_csv(atm), leer_transferencias_json(digital)
    originales = {"ATM_CSV": a.to_dict("records"), "TRANSFERENCIAS_JSON": d.to_dict("records")}
    archivos = {"ATM_CSV": atm, "TRANSFERENCIAS_JSON": digital}
    hashes = {fuente: hashlib.sha256(ruta.read_bytes()).hexdigest() for fuente, ruta in archivos.items()}
    al, _ = limpiar_datos(a, config="atm", excluir_duplicados=False)
    dl, _ = limpiar_datos(d, config="transferencias_digitales", excluir_duplicados=False)
    # El contrato actual preserva todas las filas y su orden. Fallar de forma
    # explícita si eso cambia, antes de atribuir originales a filas equivocadas.
    if list(al.index) != list(a.index) or list(dl.index) != list(d.index):
        raise ValueError("Limpieza alteró filas u orden: revisar el contrato de trazabilidad.")
    resultado = integrar_datos(al, dl, catalogo=catalogo)
    registros = resultado.datos.to_dict("records")
    vistos = set()
    for registro in registros:
        fuente, fila = registro["fuente"], registro["fila_origen"]
        clave = (fuente, fila)
        if clave in vistos or not 0 <= fila < len(originales[fuente]):
            raise ValueError("Cobertura de filas inválida en integración.")
        vistos.add(clave)
        original = originales[fuente][fila]
        campo_id = "id_transaccion" if fuente == "ATM_CSV" else "transfer_id"
        if registro["id_original"] != original[campo_id]:
            raise ValueError("Identificador de origen inconsistente en integración.")
        registro["archivo_origen"] = archivos[fuente].name
        registro["hash_archivo_origen"] = hashes[fuente]
        registro["registro_original"] = original
        # El nombre monto_original en integración aún representa el monto limpio.
        # Se conserva como tal y se transporta aparte la evidencia de limpieza.
        registro["monto_entrada_integracion"] = registro["monto_original"]
        registro["monto_original"] = registro.get("ind_monto_original", registro["monto_original"])
    esperados = {(fuente, fila) for fuente, filas in originales.items() for fila in range(len(filas))}
    if vistos != esperados:
        raise ValueError("No todas las filas crudas tienen un destino integrado.")
    validos, rechazados = validar_transacciones(registros)
    salida = validos + rechazados
    if len(salida) != len(registros) or len({r["posicion_validacion"] for r in salida}) != len(registros):
        raise ValueError("La validación perdió o duplicó registros.")
    motivos = Counter(e["codigo"] for r in rechazados for e in r["errores_validacion"])
    resumen = {
        "filas_atm": len(a), "filas_digitales": len(d), "leidos": len(registros),
        "validos": len(validos), "rechazados": len(rechazados),
        "trazabilidad_comprobada": True, "catalogo": catalogo.name,
        "hashes_sha256": hashes, "motivos_por_campo": dict(sorted(motivos.items())),
        "por_fuente": {fuente: {"validos": sum(r["fuente"] == fuente for r in validos),
                               "rechazados": sum(r["fuente"] == fuente for r in rechazados)} for fuente in originales},
    }
    return validos, rechazados, resumen


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--atm", type=Path, default=Path("data/raw/transacciones_atm.csv"))
    p.add_argument("--digital", type=Path, default=Path("data/raw/transferencias_digitales.json"))
    p.add_argument("--catalogo", type=Path, required=True)
    p.add_argument("--salida", type=Path, default=Path("reports/validacion"))
    args = p.parse_args()
    try:
        validos, rechazados, resumen = ejecutar_validacion(args.atm, args.digital, args.catalogo)
    except (OSError, ValueError) as exc:
        p.exit(2, f"No se completó la demostración ({type(exc).__name__}); revise archivos y contrato.\n")
    args.salida.mkdir(parents=True, exist_ok=True)
    for nombre, datos in (("validos", validos), ("rechazados", rechazados), ("resumen", resumen)):
        (args.salida / f"{nombre}.json").write_text(json.dumps(datos, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(resumen, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
