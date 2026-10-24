#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
limpieza_datos.py
=================
Módulo de análisis de calidad y limpieza de datos (Integrante 1)
Proyecto: Pipeline ETL Robusto para la Limpieza, Integración y Validación de
          Transacciones Bancarias con Zona de Cuarentena.

INTERFAZ PÚBLICA (contrato con el Integrante 2 y el Integrante 3)
-----------------------------------------------------------------
    df_limpio, resumen = limpiar_datos(datos, config="atm")      # o "transferencias_digitales"

* ``datos``  : pandas.DataFrame (o lista de diccionarios) SIN modificar; la función trabaja sobre una copia.
* ``config`` : nombre de una configuración predefinida (CONFIGURACIONES), un diccionario propio, o None
               (autodetección por columnas).
* ``df_limpio``: DataFrame con las columnas originales (ya limpias) + columnas derivadas de auditoría:
      fecha_hora_std          fecha estandarizada (ISO 8601, zona UTC-06:00)
      fecha_formato_origen    formato detectado en la fuente
      <monto>_original        monto tal como llegó (antes de imputar / winsorizar)
      monto_estado            FALTANTE | POSITIVO | CERO_ESPERADO | CERO_REVISAR | NEGATIVO_REVISAR
      monto_atipico           NO | LEVE_ALTO | EXTREMO_ALTO | LEVE_BAJO | EXTREMO_BAJO | NO_EVALUADO
      flag_monto_imputado     True si el monto se imputó con mediana
      flag_monto_winsorizado  True si el monto se recortó al límite IQR
      flag_<col>_imputado     True si la etiqueta categórica controlada reemplazó un nulo
      flag_id_repetido        True si el ID se repite con contenido DISTINTO (conflicto, no se elimina)
  El índice del DataFrame conserva el número de fila original (trazabilidad hacia el archivo fuente).
* ``resumen``: diccionario con estadísticas y decisiones. Incluye además dos DataFrames:
      resumen["auditoria"]            bitácora celda a celda (valor original -> valor nuevo, regla aplicada)
      resumen["duplicados_excluidos"] filas duplicadas retiradas del flujo principal (evidencia, sin borrar)

LÍMITES DE RESPONSABILIDAD
--------------------------
Este módulo NO rechaza registros ni valida reglas estrictas (CLABE de 18 dígitos, formato de correo, catálogo
de cuentas, rangos de monto, estatus válidos): eso corresponde al Integrante 3 / zona de cuarentena.
Solo estandariza formato, marca (flags), imputa y recorta de forma documentada y reversible.

EJECUCIÓN POR LÍNEA DE COMANDOS
-------------------------------
    python limpieza_datos.py --atm transacciones_atm.csv --digitales transferencias_digitales.json --salida salida_limpieza
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # backend sin ventana: genera PNG sin necesidad de pantalla
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:  # scipy es opcional: solo se usa para las pruebas de hipótesis de faltantes
    from scipy import stats
except ImportError:  # pragma: no cover
    stats = None

LOG = logging.getLogger("limpieza_datos")

# ----------------------------------------------------------------------------------------------------------------
# Constantes y configuraciones
# ----------------------------------------------------------------------------------------------------------------
TZ_LOCAL = timezone(timedelta(hours=-6))  # UTC-06:00 (es el offset que ya usa el JSON de transferencias)
COL_FECHA_STD = "fecha_hora_std"
CENTINELAS_NULO = {"", "NA", "N/A", "NULL", "NONE", "NAN", "-", "?", "S/D", "SD"}
REGEX_EMAIL = r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$"

CONFIG_ATM: dict[str, Any] = {
    "nombre": "atm",
    "col_id": "id_transaccion",
    "col_fecha": "fecha_hora",
    "col_monto": "monto_mxn",
    "col_segmento": "tipo_operacion",              # el IQR y la mediana se calculan por tipo de operación
    "segmentos_monto_cero_valido": ["CONSULTA_SALDO"],  # una consulta de saldo mueve $0 por naturaleza
    "cols_mayusculas": ["tipo_operacion", "id_cajero", "resultado"],
    "cols_categoricas_nulos": {"id_cajero": "CAJERO_NO_REGISTRADO"},
    "cols_numericas_imputar": ["monto_mxn"],
    # Patrones SOLO para el diagnóstico (no generan flags ni rechazos). Inferidos de los datos: confirmar.
    "patrones_formato": {"id_transaccion": r"^ATM-TX-\d{5}$", "id_cuenta": r"^CTA-00\d{2}$"},
}

CONFIG_DIGITALES: dict[str, Any] = {
    "nombre": "transferencias_digitales",
    "col_id": "transfer_id",
    "col_fecha": "timestamp",
    "col_monto": "amount",
    "col_segmento": "currency",                    # no hay tipo de cambio: no se mezclan MXN y USD en el IQR
    "segmentos_monto_cero_valido": [],
    "cols_mayusculas": ["status", "currency"],
    "cols_categoricas_nulos": {"notification_email": "SIN_CORREO"},
    "cols_numericas_imputar": ["amount"],
    "patrones_formato": {
        "transfer_id": r"^SPEI-\d{4}-\d{5}$",
        "account_id": r"^CTA-00\d{2}$",
        "beneficiary_clabe": r"^\d{18}$",
        "notification_email": REGEX_EMAIL,
    },
}

CONFIGURACIONES = {"atm": CONFIG_ATM, "transferencias_digitales": CONFIG_DIGITALES}

PARAMETROS_POR_DEFECTO = {
    "factor_iqr": 1.5,               # factor pedido para detectar atípicos
    "factor_extremo": 3.0,           # cerca externa de Tukey: solo los EXTREMOS se recortan
    "min_n_segmento": 8,             # mínimo de observaciones para calcular IQR / mediana por segmento
    "umbral_imputacion": 0.10,       # no se imputa si falta más del 10 % de la población aplicable
    "aplicar_capping": True,
}


class ErrorCargaDatos(Exception):
    """Error al leer un archivo de entrada."""


class ErrorConfiguracion(ValueError):
    """La configuración no corresponde a los datos recibidos."""


# ----------------------------------------------------------------------------------------------------------------
# Carga de archivos
# ----------------------------------------------------------------------------------------------------------------
def cargar_archivo(ruta: str | Path) -> pd.DataFrame:
    """Carga un CSV o JSON (lista de objetos) conservando los valores tal como vienen (sin forzar tipos)."""
    ruta = Path(ruta)
    if not ruta.exists():
        raise ErrorCargaDatos(f"No existe el archivo: {ruta}")
    try:
        if ruta.suffix.lower() == ".csv":
            df = pd.read_csv(ruta)
        elif ruta.suffix.lower() == ".json":
            with open(ruta, encoding="utf-8") as f:
                contenido = json.load(f)
            if isinstance(contenido, dict):  # algunos exportes envuelven la lista en una llave
                listas = [v for v in contenido.values() if isinstance(v, list)]
                if len(listas) != 1:
                    raise ErrorCargaDatos("El JSON es un objeto sin una única lista de registros.")
                contenido = listas[0]
            if not isinstance(contenido, list):
                raise ErrorCargaDatos("El JSON debe contener una lista de registros.")
            # json_normalize respeta tipos mixtos (p. ej. timestamp texto + entero), a diferencia de read_json
            df = pd.json_normalize(contenido)
        else:
            raise ErrorCargaDatos(f"Extensión no soportada: {ruta.suffix}")
    except (pd.errors.EmptyDataError, pd.errors.ParserError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ErrorCargaDatos(f"No se pudo leer {ruta.name}: {exc}") from exc
    if df.empty:
        raise ErrorCargaDatos(f"{ruta.name} no contiene registros.")
    LOG.info("Cargado %s: %d filas x %d columnas", ruta.name, *df.shape)
    return df


# ----------------------------------------------------------------------------------------------------------------
# Utilidades internas
# ----------------------------------------------------------------------------------------------------------------
def _resolver_config(df: pd.DataFrame, config: str | dict | None) -> dict[str, Any]:
    """Devuelve la configuración completa y verifica que las columnas declaradas existan."""
    if config is None:
        candidatas = [c for c in CONFIGURACIONES.values()
                      if {c["col_id"], c["col_fecha"], c["col_monto"]} <= set(df.columns)]
        if len(candidatas) != 1:
            raise ErrorConfiguracion("No se pudo autodetectar la configuración; pasa config='atm', "
                                     "'transferencias_digitales' o un diccionario propio.")
        cfg = dict(candidatas[0])
    elif isinstance(config, str):
        if config not in CONFIGURACIONES:
            raise ErrorConfiguracion(f"Configuración desconocida '{config}'. Opciones: {list(CONFIGURACIONES)}")
        cfg = dict(CONFIGURACIONES[config])
    else:
        cfg = dict(config)
    for k, v in {"nombre": "fuente", "segmentos_monto_cero_valido": [], "cols_mayusculas": [],
                 "cols_categoricas_nulos": {}, "cols_numericas_imputar": [], "patrones_formato": {},
                 "col_segmento": None, "col_monto": None, "col_fecha": None, "col_id": None}.items():
        cfg.setdefault(k, v)
    declaradas = [cfg[k] for k in ("col_id", "col_fecha", "col_monto", "col_segmento") if cfg.get(k)]
    declaradas += cfg["cols_mayusculas"] + list(cfg["cols_categoricas_nulos"]) + cfg["cols_numericas_imputar"]
    faltantes = sorted({c for c in declaradas if c not in df.columns})
    if faltantes:
        raise ErrorConfiguracion(f"Columnas declaradas en la configuración que no existen en los datos: {faltantes}")
    return cfg


def _distintos(a: pd.Series, b: pd.Series) -> pd.Series:
    """True donde a y b difieren (dos nulos se consideran iguales)."""
    return ~((a == b) | (a.isna() & b.isna()))


def _registrar(auditoria: list, fuente: str, df_ref: pd.DataFrame, col_id: str | None, mascara: pd.Series,
               columna: str, antes: pd.Series, despues: pd.Series | Any, regla: str, detalle: str | pd.Series = ""):
    """Agrega a la bitácora una fila por cada celda modificada (valor original y valor nuevo)."""
    for idx in mascara[mascara].index:
        nuevo = despues.loc[idx] if isinstance(despues, pd.Series) else despues
        det = detalle.loc[idx] if isinstance(detalle, pd.Series) else detalle
        auditoria.append({
            "fuente": fuente, "indice_original": idx,
            "id_registro": df_ref.loc[idx, col_id] if col_id and idx in df_ref.index else None,
            "columna": columna, "valor_original": antes.loc[idx], "valor_nuevo": nuevo,
            "regla": regla, "detalle": det})


def _forma(valor: Any) -> str:
    """Convierte un valor a su 'forma' (dígitos->d, letras->a) para detectar formatos mezclados."""
    return re.sub(r"\d+", "d", re.sub(r"[A-Za-z]+", "a", str(valor)))


# ----------------------------------------------------------------------------------------------------------------
# Perfil de calidad
# ----------------------------------------------------------------------------------------------------------------
def perfilar_calidad(df: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    """Perfil de calidad: tamaño, tipos, nulos, duplicados, únicos e inconsistencias detectadas."""
    n, k = df.shape
    filas_col = []
    for c in df.columns:
        s = df[c]
        tipos_py = s.dropna().map(lambda x: type(x).__name__).value_counts().to_dict()
        top = s.dropna().astype(str).value_counts().head(3)
        filas_col.append({
            "columna": c, "dtype_pandas": str(s.dtype),
            "tipos_python": ", ".join(f"{t}:{v}" for t, v in tipos_py.items()),
            "n_nulos": int(s.isna().sum()), "pct_nulos": round(float(s.isna().mean() * 100), 2),
            "n_unicos": int(s.nunique(dropna=True)),
            "ejemplos_frecuentes": "; ".join(f"{i}({v})" for i, v in top.items())[:80]})
    columnas = pd.DataFrame(filas_col)

    inconsist: list[dict[str, Any]] = []
    for c in df.columns:
        s = df[c]
        textos = s[s.map(lambda x: isinstance(x, str))]
        # 1) tipos mezclados en una misma columna
        tipos = s.dropna().map(lambda x: type(x).__name__).value_counts()
        if len(tipos) > 1:
            inconsist.append({"columna": c, "tipo": "TIPOS_MEZCLADOS", "cantidad": int(tipos.iloc[1:].sum()),
                              "detalle": ", ".join(f"{t}={v}" for t, v in tipos.items())})
        if textos.empty:
            continue
        # 2) espacios sobrantes
        n_esp = int((textos != textos.str.strip()).sum())
        if n_esp:
            inconsist.append({"columna": c, "tipo": "ESPACIOS_SOBRANTES", "cantidad": n_esp, "detalle": ""})
        # 3) centinelas de texto que significan "nulo"
        n_cent = int(textos.str.strip().str.upper().isin(CENTINELAS_NULO).sum())
        if n_cent:
            inconsist.append({"columna": c, "tipo": "CENTINELA_DE_NULO", "cantidad": n_cent, "detalle": ""})
        # 4) variantes de mayúsculas/minúsculas (solo columnas de baja cardinalidad)
        if textos.nunique() <= 50:
            grupos = textos.str.strip().groupby(textos.str.strip().str.lower()).agg(lambda x: x.value_counts().to_dict())
            variantes = {k_: v for k_, v in grupos.items() if len(v) > 1}
            if variantes:
                # filas que NO usan la variante más frecuente de cada grupo
                cant = int(sum(sum(d.values()) - max(d.values()) for d in variantes.values()))
                inconsist.append({"columna": c, "tipo": "MAYUSCULAS_MEZCLADAS", "cantidad": cant,
                                  "detalle": "; ".join(str(d) for d in variantes.values())})
    # 5) formatos de fecha mezclados
    cf = config.get("col_fecha")
    if cf:
        formas = df[cf].dropna().map(_forma).value_counts()
        if len(formas) > 1:
            inconsist.append({"columna": cf, "tipo": "FORMATOS_FECHA_MEZCLADOS", "cantidad": int(formas.iloc[1:].sum()),
                              "detalle": "; ".join(f"{f}={v}" for f, v in formas.items())})
    # 6) patrones esperados (solo diagnóstico)
    for c, patron in config.get("patrones_formato", {}).items():
        if c in df.columns:
            s = df[c].dropna().astype(str)
            malos = s[~s.str.match(patron)]
            if len(malos):
                inconsist.append({"columna": c, "tipo": "FUERA_DE_PATRON_ESPERADO", "cantidad": int(len(malos)),
                                  "detalle": f"patrón {patron}; ejemplos: {malos.drop_duplicates().head(3).tolist()}"})
    # 7) montos cero / negativos
    cm = config.get("col_monto")
    if cm:
        m = pd.to_numeric(df[cm], errors="coerce")
        if (m < 0).any():
            inconsist.append({"columna": cm, "tipo": "MONTO_NEGATIVO", "cantidad": int((m < 0).sum()), "detalle": ""})
        if (m == 0).any():
            inconsist.append({"columna": cm, "tipo": "MONTO_CERO", "cantidad": int((m == 0).sum()),
                              "detalle": "puede ser legítimo (p. ej. consulta de saldo); ver monto_estado"})
    ci = config.get("col_id")
    ids_rep = int(df[ci].duplicated().sum()) if ci else 0

    return {
        "n_filas": int(n), "n_columnas": int(k), "columnas": columnas,
        "duplicados_exactos": int(df.astype(str).duplicated().sum()),
        "ids_repetidos": ids_rep,
        "n_nulos_total": int(df.isna().sum().sum()),
        "inconsistencias": pd.DataFrame(inconsist, columns=["columna", "tipo", "cantidad", "detalle"]),
    }


# ----------------------------------------------------------------------------------------------------------------
# Fechas
# ----------------------------------------------------------------------------------------------------------------
_FORMATOS_FECHA = [
    ("YYYY/MM/DD HH:MM:SS", re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$"), "%Y/%m/%d %H:%M:%S"),
    ("DD/MM/YYYY HH:MM", re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$"), "%d/%m/%Y %H:%M"),
    ("YYYY-MM-DD HH:MM:SS", re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"), "%Y-%m-%d %H:%M:%S"),
]
_RX_ISO_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$")


def parsear_fecha(valor: Any) -> tuple[pd.Timestamp, str]:
    """Convierte un valor de fecha heterogéneo a Timestamp UTC-06:00 y devuelve también el formato detectado.

    Formatos sin zona horaria se asumen en UTC-06:00 (ver DECISIONES_LIMPIEZA.md). Las cadenas DD/MM/YYYY se
    interpretan día/mes: en los datos reales aparecen días > 12 (p. ej. 15/09/2026) pero nunca meses > 12.
    """
    try:
        if valor is None or (not isinstance(valor, str) and pd.isna(valor)):
            return pd.NaT, "FALTANTE"
        if isinstance(valor, (int, np.integer)) or (isinstance(valor, float) and float(valor).is_integer()) \
                or (isinstance(valor, str) and valor.strip().isdigit()):
            n = int(float(valor)) if not isinstance(valor, str) else int(valor.strip())
            if len(str(abs(n))) == 10:
                return pd.Timestamp(n, unit="s", tz="UTC").tz_convert(TZ_LOCAL), "EPOCH segundos"
            if len(str(abs(n))) == 13:
                return pd.Timestamp(n, unit="ms", tz="UTC").tz_convert(TZ_LOCAL), "EPOCH milisegundos"
            return pd.NaT, "NO_RECONOCIDO"
        s = str(valor).strip()
        if _RX_ISO_OFFSET.match(s):
            return pd.Timestamp(s).tz_convert(TZ_LOCAL), "ISO8601 con offset"
        for nombre, rx, fmt in _FORMATOS_FECHA:
            if rx.match(s):
                return pd.Timestamp(datetime.strptime(s, fmt).replace(tzinfo=TZ_LOCAL)), nombre + " (sin zona)"
    except (ValueError, OverflowError):
        pass
    return pd.NaT, "NO_RECONOCIDO"


def estandarizar_fechas(serie: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Aplica parsear_fecha a toda la serie. No toca la columna original (queda como evidencia)."""
    res = [parsear_fecha(v) for v in serie]
    fechas = pd.Series([r[0] for r in res], index=serie.index)
    formatos = pd.Series([r[1] for r in res], index=serie.index)
    return fechas, formatos


# ----------------------------------------------------------------------------------------------------------------
# Texto, duplicados, faltantes
# ----------------------------------------------------------------------------------------------------------------
def normalizar_texto(df: pd.DataFrame, cfg: dict[str, Any], auditoria: list) -> pd.DataFrame:
    """Quita espacios, convierte centinelas ('N/A', '-', ...) a nulo y unifica mayúsculas en columnas controladas.
    No toca la columna de fecha (se conserva cruda) ni columnas con mayúsculas significativas (conceptos, plataformas)."""
    df = df.copy()
    for c in df.columns:
        if c == cfg["col_fecha"] or not (df[c].dtype == object or pd.api.types.is_string_dtype(df[c])):
            continue
        original = df[c]
        paso1 = original.map(lambda v: v.strip() if isinstance(v, str) else v)
        _registrar(auditoria, cfg["nombre"], df, cfg["col_id"], _distintos(original, paso1), c, original, paso1,
                   "TEXTO_ESPACIOS_RECORTADOS")
        es_cent = paso1.map(lambda v: isinstance(v, str) and v.upper() in CENTINELAS_NULO)
        paso2 = paso1.mask(es_cent, np.nan)
        _registrar(auditoria, cfg["nombre"], df, cfg["col_id"], es_cent, c, paso1, paso2, "CENTINELA_A_NULO")
        paso3 = paso2
        if c in cfg["cols_mayusculas"]:
            paso3 = paso2.map(lambda v: v.upper() if isinstance(v, str) else v)
            _registrar(auditoria, cfg["nombre"], df, cfg["col_id"], _distintos(paso2, paso3), c, paso2, paso3,
                       "NORMALIZAR_MAYUSCULAS")
        df[c] = paso3
    return df


def detectar_duplicados(df: pd.DataFrame, datos_crudos: pd.DataFrame, cfg: dict[str, Any], auditoria: list):
    """Separa los duplicados exactos (se conserva la primera aparición). Las filas retiradas NO se destruyen:
    se devuelven en un DataFrame aparte con la referencia a la fila que se conservó."""
    marca = df.duplicated(keep="first")
    primera = df[~marca].reset_index().set_index(list(df.columns))["index"] if marca.any() else None
    dups = datos_crudos.loc[marca[marca].index].copy()
    if marca.any():
        claves = pd.MultiIndex.from_frame(df.loc[marca[marca].index])
        dups["duplicado_de_indice"] = primera.reindex(claves).values
        dups["motivo"] = "DUPLICADO_EXACTO (tras normalizar texto)"
        for idx in dups.index:
            auditoria.append({"fuente": cfg["nombre"], "indice_original": idx,
                              "id_registro": df.loc[idx, cfg["col_id"]], "columna": "*", "valor_original": "fila completa",
                              "valor_nuevo": "excluida del flujo principal", "regla": "DUPLICADO_EXACTO_EXCLUIDO",
                              "detalle": f"idéntica a la fila {dups.loc[idx, 'duplicado_de_indice']}"})
    return df[~marca].copy(), dups


def analizar_faltantes(df: pd.DataFrame, cfg: dict[str, Any], alfa: float = 0.05, max_card: int = 20,
                       min_faltantes: int = 5) -> list[dict[str, Any]]:
    """Evalúa si los nulos de cada columna parecen MCAR o MAR.

    Método: se crea un indicador 'faltante / no faltante' y se prueba su asociación con las demás variables
    (chi-cuadrada para categóricas/discretas, Mann-Whitney para numéricas continuas) con corrección de Bonferroni.
    LIMITACIÓN IMPORTANTE: con los datos observados no se puede demostrar MCAR ni descartar MNAR (que dependa del
    propio valor faltante). Un p-valor alto solo significa 'sin evidencia en contra de MCAR'; con pocos faltantes
    la prueba tiene poca potencia.
    """
    resultados = []
    cm, seg, seg_cero = cfg["col_monto"], cfg["col_segmento"], set(cfg["segmentos_monto_cero_valido"])
    excluidas = {cfg["col_id"], cfg["col_fecha"]}
    for col in df.columns[df.isna().any()]:
        pob = pd.Series(True, index=df.index)
        nota = ""
        if col == cm and seg and seg_cero:  # el 0 estructural de CONSULTA_SALDO no es "faltante"
            pob = ~df[seg].isin(seg_cero)
            nota = f"Población evaluada sin {sorted(seg_cero)} (monto $0 estructural, nunca falta)."
        sub = df[pob]
        ind = sub[col].isna()
        reg = {"columna": col, "n_nulos": int(ind.sum()), "poblacion": int(len(sub)),
               "pct_nulos": round(float(ind.mean() * 100), 2), "nota_poblacion": nota,
               "pruebas": [], "significativas": []}
        if stats is None or reg["n_nulos"] < min_faltantes:
            reg["mecanismo_sugerido"] = "NO EVALUABLE (pocos faltantes o scipy no disponible)"
            resultados.append(reg)
            continue
        pruebas = []
        for otra in sub.columns:
            if otra == col or otra in excluidas:
                continue
            x = sub[otra]
            nun = x.nunique(dropna=True)
            if nun < 2:
                continue
            try:
                if nun <= max_card:
                    tabla = pd.crosstab(ind, x.astype(object).where(x.notna(), "<<NULO>>"))
                    if tabla.shape[0] < 2 or tabla.shape[1] < 2:
                        continue
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        chi2, p, _, esp = stats.chi2_contingency(tabla)
                    v = float(np.sqrt(chi2 / (tabla.values.sum() * (min(tabla.shape) - 1))))
                    pruebas.append({"variable": otra, "prueba": "chi-cuadrada", "p_valor": float(p),
                                    "efecto(V Cramér)": round(v, 3), "pct_celdas_esp<5": round(float((esp < 5).mean() * 100), 1)})
                elif pd.api.types.is_numeric_dtype(x):
                    a, b = x[ind].dropna(), x[~ind].dropna()
                    if len(a) < 3 or len(b) < 3:
                        continue
                    u, p = stats.mannwhitneyu(a, b, alternative="two-sided")
                    pruebas.append({"variable": otra, "prueba": "Mann-Whitney", "p_valor": float(p),
                                    "efecto(r biserial)": round(float(1 - 2 * u / (len(a) * len(b))), 3),
                                    "pct_celdas_esp<5": None})
            except Exception as exc:  # una prueba fallida no debe detener el análisis
                LOG.warning("Prueba de faltantes omitida (%s vs %s): %s", col, otra, exc)
        reg["pruebas"] = sorted(pruebas, key=lambda t: t["p_valor"])[:5]
        umbral = alfa / max(len(pruebas), 1)  # Bonferroni
        reg["alfa_bonferroni"] = umbral
        reg["significativas"] = [t["variable"] for t in pruebas if t["p_valor"] < umbral]
        if reg["significativas"]:
            reg["mecanismo_sugerido"] = ("POSIBLE MAR: el faltante se asocia con " + ", ".join(reg["significativas"]) +
                                         " (no descarta MNAR)")
        else:
            reg["mecanismo_sugerido"] = ("SIN EVIDENCIA CONTRA MCAR con las variables disponibles "
                                         "(no demuestra MCAR; MNAR no puede descartarse)")
        resultados.append(reg)
    return resultados


# ----------------------------------------------------------------------------------------------------------------
# Montos: reglas, atípicos IQR, imputación, capping
# ----------------------------------------------------------------------------------------------------------------
def clasificar_montos(monto: pd.Series, segmento: pd.Series | None, seg_cero: set) -> pd.Series:
    """Etiqueta el estado del monto SIN modificarlo (las reglas de negocio las define el equipo):
    CERO_ESPERADO si el segmento admite $0; CERO_REVISAR / NEGATIVO_REVISAR requieren regla de negocio."""
    estado = pd.Series("POSITIVO", index=monto.index, dtype=object)
    cero_ok = segmento.isin(seg_cero) if segmento is not None else pd.Series(False, index=monto.index)
    estado[monto == 0] = "CERO_REVISAR"
    estado[(monto == 0) & cero_ok] = "CERO_ESPERADO"
    estado[monto < 0] = "NEGATIVO_REVISAR"
    estado[monto.isna()] = "FALTANTE"
    return estado


def calcular_limites_iqr(valores: pd.Series, factor: float = 1.5, factor_extremo: float = 3.0) -> dict[str, float]:
    """Límites de Tukey: cerca interna (factor, 1.5) para atípicos y cerca externa (3.0) para extremos."""
    q1, q3 = valores.quantile(0.25), valores.quantile(0.75)
    iqr = q3 - q1
    return {"q1": float(q1), "q3": float(q3), "iqr": float(iqr),
            "lim_inf": float(q1 - factor * iqr), "lim_sup": float(q3 + factor * iqr),
            "ext_inf": float(q1 - factor_extremo * iqr), "ext_sup": float(q3 + factor_extremo * iqr)}


def detectar_atipicos_iqr(monto: pd.Series, segmento: pd.Series | None, seg_cero: set, factor: float,
                          factor_extremo: float, min_n: int) -> tuple[pd.Series, dict[str, dict]]:
    """Detecta atípicos por IQR dentro de cada segmento, usando solo montos observados y > 0.
    No se mezclan segmentos con escalas distintas (tipos de operación, monedas) ni se incluyen los $0 estructurales."""
    seg = segmento if segmento is not None else pd.Series("TODOS", index=monto.index)
    base = monto.where(~seg.isin(seg_cero) & (monto > 0))
    etiqueta = pd.Series("NO_EVALUADO", index=monto.index, dtype=object)
    limites: dict[str, dict] = {}
    for valor_seg in seg.dropna().unique():
        idx = seg.index[seg == valor_seg]
        x = base.loc[idx]
        obs = x.dropna()
        if valor_seg in seg_cero:
            continue
        if len(obs) < min_n:
            limites[str(valor_seg)] = {"evaluado": False, "motivo": f"menos de {min_n} observaciones", "n": int(len(obs))}
            continue
        lim = calcular_limites_iqr(obs, factor, factor_extremo)
        if lim["iqr"] == 0:
            limites[str(valor_seg)] = {"evaluado": False, "motivo": "IQR = 0", "n": int(len(obs))}
            continue
        etiqueta.loc[obs.index] = "NO"
        etiqueta.loc[x.index[x > lim["lim_sup"]]] = "LEVE_ALTO"
        etiqueta.loc[x.index[x < lim["lim_inf"]]] = "LEVE_BAJO"
        etiqueta.loc[x.index[x > lim["ext_sup"]]] = "EXTREMO_ALTO"
        etiqueta.loc[x.index[x < lim["ext_inf"]]] = "EXTREMO_BAJO"
        lim.update({"evaluado": True, "n": int(len(obs)),
                    "n_leve_alto": int((etiqueta.loc[idx] == "LEVE_ALTO").sum()),
                    "n_leve_bajo": int((etiqueta.loc[idx] == "LEVE_BAJO").sum()),
                    "n_extremo_alto": int((etiqueta.loc[idx] == "EXTREMO_ALTO").sum()),
                    "n_extremo_bajo": int((etiqueta.loc[idx] == "EXTREMO_BAJO").sum()),
                    "maximo_observado": float(obs.max()), "minimo_observado": float(obs.min())})
        limites[str(valor_seg)] = lim
    return etiqueta, limites


def imputar_mediana(monto: pd.Series, segmento: pd.Series | None, seg_cero: set, umbral: float, min_n: int):
    """Imputa montos faltantes con la mediana (robusta a asimetría y atípicos) del segmento.

    Es 'estadísticamente apropiado' solo si: (a) falta menos del umbral de la población aplicable, (b) hay al menos
    min_n valores observados. Los segmentos con $0 estructural no se imputan. Devuelve (serie, máscara, info)."""
    seg = segmento if segmento is not None else pd.Series("TODOS", index=monto.index)
    aplicable = ~seg.isin(seg_cero)
    faltan = monto.isna() & aplicable
    info: dict[str, Any] = {"n_faltantes": int(faltan.sum()), "poblacion_aplicable": int(aplicable.sum()),
                            "pct_faltante": round(float(faltan.sum() / max(aplicable.sum(), 1) * 100), 2),
                            "imputado": False, "medianas": {}}
    if faltan.sum() == 0:
        info["motivo"] = "sin faltantes"
        return monto, pd.Series(False, index=monto.index), info
    if faltan.sum() / aplicable.sum() > umbral:
        info["motivo"] = f"faltante > {umbral:.0%} de la población aplicable: no se imputa"
        return monto, pd.Series(False, index=monto.index), info
    validos = monto.where(aplicable & (monto > 0))
    if validos.count() < min_n:
        info["motivo"] = "muy pocos valores observados"
        return monto, pd.Series(False, index=monto.index), info
    med_global, cuentas = validos.median(), validos.groupby(seg).count()
    medianas = validos.groupby(seg).median()
    nuevo = monto.copy()
    for idx in faltan[faltan].index:
        s = seg.loc[idx]
        usa_seg = pd.notna(s) and cuentas.get(s, 0) >= min_n
        nuevo.loc[idx] = medianas[s] if usa_seg else med_global
    info["imputado"] = True
    info["medianas"] = {str(k): float(v) for k, v in medianas.items() if pd.notna(v)}
    info["mediana_global"] = float(med_global)
    info["asimetria_observada"] = round(float(validos.dropna().skew()), 2)
    return nuevo, faltan, info


def imputar_categoricas(df: pd.DataFrame, cfg: dict[str, Any], auditoria: list):
    """Reemplaza nulos categóricos por una etiqueta controlada, solo en las columnas donde la configuración lo
    declara con sentido (p. ej. cajero no registrado). Nunca se inventa un valor 'real'. Deja una bandera por columna."""
    info = {}
    for col, etiqueta in cfg["cols_categoricas_nulos"].items():
        mask = df[col].isna()
        info[col] = {"etiqueta": etiqueta, "n_imputados": int(mask.sum())}
        if mask.any():
            _registrar(auditoria, cfg["nombre"], df, cfg["col_id"], mask, col, df[col], etiqueta,
                       "NULO_A_ETIQUETA_CONTROLADA", f"etiqueta={etiqueta}")
            df.loc[mask, col] = etiqueta
        df[f"flag_{col}_imputado"] = mask
    return info


# ----------------------------------------------------------------------------------------------------------------
# Función principal reutilizable
# ----------------------------------------------------------------------------------------------------------------
def limpiar_datos(datos: pd.DataFrame | list[dict], config: str | dict | None = None, **parametros):
    """Limpia un conjunto de datos transaccionales y devuelve ``(df_limpio, resumen)``. Ver docstring del módulo.

    Parámetros opcionales (``**parametros``): factor_iqr, factor_extremo, min_n_segmento, umbral_imputacion,
    aplicar_capping (ver PARAMETROS_POR_DEFECTO)."""
    if isinstance(datos, list):
        datos = pd.DataFrame(datos)
    if not isinstance(datos, pd.DataFrame) or datos.empty:
        raise ValueError("'datos' debe ser un DataFrame (o lista de diccionarios) con al menos un registro.")
    par = {**PARAMETROS_POR_DEFECTO, **parametros}
    desconocidos = set(parametros) - set(PARAMETROS_POR_DEFECTO)
    if desconocidos:
        raise ValueError(f"Parámetros desconocidos: {sorted(desconocidos)}")
    crudo = datos.copy()
    if not crudo.index.is_unique:
        LOG.warning("El índice no es único: se reinicia para poder rastrear cada fila.")
        crudo = crudo.reset_index(drop=True)
    cfg = _resolver_config(crudo, config)
    fuente, col_id, cm, seg_col = cfg["nombre"], cfg["col_id"], cfg["col_monto"], cfg["col_segmento"]
    seg_cero = set(cfg["segmentos_monto_cero_valido"])
    auditoria: list[dict] = []
    advertencias: list[str] = []
    LOG.info("[%s] Limpieza iniciada (%d filas)", fuente, len(crudo))

    perfil_ini = perfilar_calidad(crudo, cfg)

    # 1) Normalización de texto (espacios, centinelas, mayúsculas controladas)
    df = normalizar_texto(crudo, cfg, auditoria)

    # 2) Montos no numéricos -> nulo (con evidencia). El monto original se conserva en <monto>_original
    monto_num = pd.to_numeric(df[cm], errors="coerce")
    no_num = monto_num.isna() & df[cm].notna()
    if no_num.any():
        _registrar(auditoria, fuente, df, col_id, no_num, cm, df[cm], np.nan, "MONTO_NO_NUMERICO_A_NULO")
        advertencias.append(f"{int(no_num.sum())} montos no numéricos se convirtieron a nulo.")
    df[cm] = monto_num

    # 3) Duplicados exactos: se separan, no se destruyen
    df, df_dups = detectar_duplicados(df, crudo, cfg, auditoria)
    df["flag_id_repetido"] = df[col_id].duplicated(keep=False)  # mismo ID con contenido distinto = conflicto
    n_conflictos = int(df["flag_id_repetido"].sum())
    if n_conflictos:
        advertencias.append(f"{n_conflictos} filas comparten ID con contenido distinto (no se eliminan; ver flag_id_repetido).")

    # 4) Fechas estandarizadas (la columna cruda se conserva)
    df[COL_FECHA_STD], df["fecha_formato_origen"] = estandarizar_fechas(df[cfg["col_fecha"]])
    n_fecha_mala = int((df["fecha_formato_origen"] == "NO_RECONOCIDO").sum())
    if n_fecha_mala:
        advertencias.append(f"{n_fecha_mala} fechas no reconocidas quedaron como NaT (valor crudo intacto).")
    cols_originales = list(crudo.columns)

    # 5) Análisis de faltantes (antes de imputar)
    faltantes = analizar_faltantes(df[cols_originales], cfg)

    # 6) Reglas de monto (sin modificar): cero / negativo
    segmento = df[seg_col] if seg_col else None
    df[f"{cm}_original"] = df[cm]
    df["monto_estado"] = clasificar_montos(df[cm], segmento, seg_cero)
    n_rev = int(df["monto_estado"].isin(["CERO_REVISAR", "NEGATIVO_REVISAR"]).sum())
    if n_rev:
        advertencias.append(f"{n_rev} montos cero/negativos fuera de segmentos permitidos requieren regla de negocio; NO se modificaron.")

    # 7) Atípicos IQR sobre montos observados (antes de imputar para que las medianas imputadas no sesguen los cuartiles)
    df["monto_atipico"], limites = detectar_atipicos_iqr(df[cm], segmento, seg_cero, par["factor_iqr"],
                                                         par["factor_extremo"], par["min_n_segmento"])
    for s, lim in limites.items():
        if not lim.get("evaluado"):
            advertencias.append(f"Segmento '{s}' sin IQR ({lim['motivo']}).")

    # 8) Imputación por mediana (numéricas) y etiquetas controladas (categóricas)
    info_imp: dict[str, Any] = {}
    for col in cfg["cols_numericas_imputar"]:
        if col == cm:
            nuevo, mask, info = imputar_mediana(df[col], segmento, seg_cero, par["umbral_imputacion"], par["min_n_segmento"])
            det = (segmento.map(lambda s: f"mediana de {s}: {info['medianas'].get(str(s), info.get('mediana_global'))}")
                   if info["imputado"] and segmento is not None else "")
        else:
            nuevo, mask, info = imputar_mediana(df[col], None, set(), par["umbral_imputacion"], par["min_n_segmento"])
            det = ""
        if mask.any():
            _registrar(auditoria, fuente, df, col_id, mask, col, df[col], nuevo, "IMPUTACION_MEDIANA", det)
        df[col] = nuevo
        if col == cm:
            df["flag_monto_imputado"] = mask
        info_imp[col] = info
    if "flag_monto_imputado" not in df:
        df["flag_monto_imputado"] = False
    info_cat = imputar_categoricas(df, cfg, auditoria)

    # 9) Winsorización: solo EXTREMOS (más allá de la cerca externa) y solo si aplicar_capping
    df["flag_monto_winsorizado"] = False
    if par["aplicar_capping"]:
        for s, lim in limites.items():
            if not lim.get("evaluado"):
                continue
            en_seg = (df[seg_col] == s) if seg_col else pd.Series(True, index=df.index)
            alto = en_seg & (df["monto_atipico"] == "EXTREMO_ALTO")
            bajo = en_seg & (df["monto_atipico"] == "EXTREMO_BAJO") & (lim["lim_inf"] > 0)
            for mask, tope, nombre in ((alto, round(lim["lim_sup"], 2), "lim_sup"), (bajo, round(lim["lim_inf"], 2), "lim_inf")):
                if mask.any():
                    _registrar(auditoria, fuente, df, col_id, mask, cm, df[cm], tope, "WINSORIZACION_IQR",
                               f"segmento={s}; {nombre}={tope:,.2f}; factor={par['factor_iqr']}; extremo>{par['factor_extremo']}*IQR")
                    df.loc[mask, cm] = tope
                    df.loc[mask, "flag_monto_winsorizado"] = True
                    prop = mask.sum() / max(en_seg.sum(), 1)
                    if prop > 0.02:
                        advertencias.append(
                            f"Segmento '{s}': {int(mask.sum())} extremos ({prop:.1%} de los registros). Una proporción así sugiere "
                            "un error sistemático (escala/unidad/captura) y no ruido aleatorio: conviene enviarlos a revisión o "
                            "cuarentena usando el monto original (columna *_original) en lugar de confiar en el valor recortado.")

    # 10) Perfil final y resumen
    perfil_fin = perfilar_calidad(df, cfg)
    df_aud = pd.DataFrame(auditoria, columns=["fuente", "indice_original", "id_registro", "columna", "valor_original",
                                              "valor_nuevo", "regla", "detalle"])
    if seg_col == "currency" and df[seg_col].nunique() > 1:
        advertencias.append("Hay montos en varias monedas y no hay tipo de cambio: el IQR se calculó por moneda y NO se "
                            "convirtieron importes. El Integrante 2 debe definir la regla antes de sumar montos entre monedas.")
    advertencias.append("Fechas sin zona horaria se asumieron en UTC-06:00 (confirmar con el equipo).")

    resumen = {
        "fuente": fuente, "filas_entrada": int(len(crudo)), "filas_salida": int(len(df)),
        "duplicados_exactos_excluidos": int(len(df_dups)),
        "reglas_aplicadas": df_aud["regla"].value_counts().to_dict(),
        "formatos_fecha": df["fecha_formato_origen"].value_counts().to_dict(),
        "faltantes": faltantes, "imputacion_numerica": info_imp, "imputacion_categorica": info_cat,
        "atipicos": limites, "estados_monto": df["monto_estado"].value_counts().to_dict(),
        "montos_winsorizados": int(df["flag_monto_winsorizado"].sum()),
        "parametros": par, "advertencias": advertencias,
        "columnas_nuevas": [c for c in df.columns if c not in cols_originales],
        "perfil_inicial": perfil_ini, "perfil_final": perfil_fin,
        "auditoria": df_aud, "duplicados_excluidos": df_dups,
    }
    LOG.info("[%s] Limpieza terminada: %d -> %d filas, %d cambios auditados", fuente, len(crudo), len(df), len(df_aud))
    return df, resumen


# ----------------------------------------------------------------------------------------------------------------
# Gráficas
# ----------------------------------------------------------------------------------------------------------------
def graficar_nulos(resumen: dict, ruta: Path):
    """Barras de % de nulos por columna original, antes vs. después."""
    ini = resumen["perfil_inicial"]["columnas"].set_index("columna")
    fin = resumen["perfil_final"]["columnas"].set_index("columna")
    cols = list(ini.index)
    y = np.arange(len(cols))
    fig, ax = plt.subplots(figsize=(9, 0.55 * len(cols) + 2))
    ax.barh(y - 0.2, ini["pct_nulos"], 0.4, label="Antes", color="#c0392b")
    ax.barh(y + 0.2, fin.loc[cols, "pct_nulos"], 0.4, label="Después", color="#27ae60")
    for i, c in enumerate(cols):
        if ini.loc[c, "n_nulos"]:
            ax.text(ini.loc[c, "pct_nulos"] + 0.1, i - 0.2, f"{int(ini.loc[c, 'n_nulos'])}", va="center", fontsize=8)
    ax.set_yticks(y, cols)
    ax.invert_yaxis()
    ax.set_xlabel("% de valores nulos")
    ax.set_title(f"Valores nulos por columna: {resumen['fuente']}")
    ax.legend()
    fig.tight_layout()
    fig.savefig(ruta, dpi=130)
    plt.close(fig)


def graficar_duplicados(resumen: dict, ruta: Path):
    """Duplicados exactos e IDs repetidos, antes vs. después."""
    ini, fin = resumen["perfil_inicial"], resumen["perfil_final"]
    etiquetas = ["Filas duplicadas\nexactas", "IDs repetidos"]
    antes = [ini["duplicados_exactos"], ini["ids_repetidos"]]
    despues = [fin["duplicados_exactos"], fin["ids_repetidos"]]
    x = np.arange(2)
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    b1 = ax.bar(x - 0.2, antes, 0.4, label="Antes", color="#c0392b")
    b2 = ax.bar(x + 0.2, despues, 0.4, label="Después", color="#27ae60")
    ax.bar_label(b1)
    ax.bar_label(b2)
    ax.set_xticks(x, etiquetas)
    ax.set_ylabel("Registros")
    ax.set_title(f"Duplicados: {resumen['fuente']}\n(excluidos a tabla de evidencia: {resumen['duplicados_exactos_excluidos']})")
    ax.legend()
    fig.tight_layout()
    fig.savefig(ruta, dpi=130)
    plt.close(fig)


def graficar_atipicos(datos_crudos: pd.DataFrame, df_limpio: pd.DataFrame, cfg: dict, resumen: dict, ruta: Path):
    """Boxplots (escala log) del monto por segmento antes y después, con las cercas IQR calculadas."""
    cm, seg = cfg["col_monto"], cfg["col_segmento"]
    segs = [s for s, l in resumen["atipicos"].items() if l.get("evaluado")]
    if not segs:
        return
    antes_raw = pd.to_numeric(datos_crudos[cm], errors="coerce")
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)
    for ax, (titulo, serie, idx) in zip(axes, (("Antes (valores originales)", antes_raw, datos_crudos.index),
                                               ("Después (imputado y winsorizado)", df_limpio[cm], df_limpio.index))):
        base = datos_crudos if serie is antes_raw else df_limpio
        datos = [serie[(base[seg] == s) & (serie > 0)].dropna().values for s in segs] if seg else [serie.dropna().values]
        ax.boxplot(datos, tick_labels=[f"{s}\n(n={len(d)})" for s, d in zip(segs, datos)], showfliers=False)
        for i, (s, d) in enumerate(zip(segs, datos), start=1):
            lim = resumen["atipicos"][s]["lim_sup"]
            fuera = d[d > lim]  # puntos fuera de la cerca IQR, dibujados explícitamente
            if len(fuera):
                ax.scatter(np.full(len(fuera), float(i)) + np.linspace(-0.12, 0.12, len(fuera)), fuera, s=28,
                           color="#c0392b", zorder=3, label="Fuera de la cerca IQR×1.5" if i == 1 or "Fuera" not in
                           ax.get_legend_handles_labels()[1] else None)
                ax.annotate(f"máx {fuera.max():,.0f}", (i, fuera.max()), textcoords="offset points", xytext=(10, 3), fontsize=8)
            ax.hlines(lim, i - 0.35, i + 0.35, colors="#2980b9", linestyles="--", linewidth=1.5,
                      label="Cerca superior IQR×1.5" if i == 1 else None)
        ax.set_yscale("log")
        ax.set_title(titulo)
        ax.grid(axis="y", alpha=0.3)
    axes[0].set_ylabel("Monto (escala logarítmica)")
    axes[0].legend(loc="lower right", fontsize=8)
    fig.suptitle(f"Valores atípicos por IQR: {resumen['fuente']}")
    fig.tight_layout()
    fig.savefig(ruta, dpi=130)
    plt.close(fig)


# ----------------------------------------------------------------------------------------------------------------
# Reporte en Markdown
# ----------------------------------------------------------------------------------------------------------------
def _fmt(v: Any) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if isinstance(v, (float, np.floating)):
        return f"{v:,.2f}"
    return str(v).replace("|", "/").replace("\n", " ")


def _tabla_md(df: pd.DataFrame) -> str:
    if df is None or len(df) == 0:
        return "_(sin registros)_"
    cab = "| " + " | ".join(map(str, df.columns)) + " |"
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    filas = ["| " + " | ".join(_fmt(v) for v in fila) + " |" for fila in df.itertuples(index=False)]
    return "\n".join([cab, sep, *filas])


def generar_reporte_md(resumen: dict, prefijo: str, ruta: Path):
    """Reporte de calidad antes/después en Markdown (con las gráficas incrustadas)."""
    ini, fin = resumen["perfil_inicial"], resumen["perfil_final"]
    cols_orig = list(ini["columnas"]["columna"])
    nulos_fin = int(fin["columnas"].set_index("columna").loc[cols_orig, "n_nulos"].sum())
    L = [f"# Reporte de calidad: {resumen['fuente']}", "",
         f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Módulo: `limpieza_datos.py`", "",
         "## 1. Resumen antes / después", "",
         _tabla_md(pd.DataFrame([
             ["Filas", ini["n_filas"], fin["n_filas"]],
             ["Columnas", ini["n_columnas"], f"{fin['n_columnas']} (+{len(resumen['columnas_nuevas'])} derivadas de auditoría)"],
             ["Celdas nulas (columnas originales)", ini["n_nulos_total"], nulos_fin],
             ["Filas duplicadas exactas", ini["duplicados_exactos"], fin["duplicados_exactos"]],
             ["IDs repetidos", ini["ids_repetidos"], fin["ids_repetidos"]],
             ["Montos winsorizados", 0, resumen["montos_winsorizados"]]],
             columns=["Métrica", "Antes", "Después"])), "",
         "## 2. Perfil inicial", "", "### Columnas, tipos y nulos", "", _tabla_md(ini["columnas"]), "",
         "### Inconsistencias detectadas", "", _tabla_md(ini["inconsistencias"]), "",
         "## 3. Valores faltantes: ¿MCAR o MAR?", "",
         "> No es posible *demostrar* el mecanismo solo con los datos observados: un p-valor alto indica "
         "ausencia de evidencia contra MCAR, no su confirmación, y MNAR (el faltante depende del propio valor "
         "ausente) no puede descartarse. Con pocos faltantes las pruebas tienen poca potencia.", ""]
    for f in resumen["faltantes"]:
        L += [f"**{f['columna']}**: {f['n_nulos']} nulos de {f['poblacion']} ({f['pct_nulos']} %). {f['nota_poblacion']}", "",
              f"- Lectura: {f['mecanismo_sugerido']}"]
        if f["pruebas"]:
            L += ["", _tabla_md(pd.DataFrame(f["pruebas"])), f"_Umbral Bonferroni: p < {f.get('alfa_bonferroni', 0):.4f}_"]
        L.append("")
    L += ["## 4. Transformaciones aplicadas", "",
          _tabla_md(pd.DataFrame(list(resumen["reglas_aplicadas"].items()), columns=["Regla", "Celdas/filas afectadas"])), "",
          "Detalle celda a celda (valor original -> valor nuevo): `*_auditoria_cambios.csv`. "
          "Filas duplicadas retiradas: `*_duplicados_excluidos.csv`.", "",
          "### Imputación", ""]
    for col, i in resumen["imputacion_numerica"].items():
        L.append(f"- `{col}`: {i['n_faltantes']} faltantes ({i['pct_faltante']} % de la población aplicable); "
                 f"{'imputado con mediana por segmento ' + json.dumps(i['medianas'], ensure_ascii=False) if i['imputado'] else 'NO imputado: ' + i.get('motivo', '')}")
    for col, i in resumen["imputacion_categorica"].items():
        L.append(f"- `{col}`: {i['n_imputados']} nulos -> etiqueta `{i['etiqueta']}`")
    L += ["", "### Estados de monto (sin modificar)", "",
          _tabla_md(pd.DataFrame(list(resumen["estados_monto"].items()), columns=["monto_estado", "n"])), "",
          "### Formatos de fecha detectados", "",
          _tabla_md(pd.DataFrame(list(resumen["formatos_fecha"].items()), columns=["Formato origen", "n"])), "",
          "## 5. Valores atípicos (IQR × 1.5)", "",
          "Se calcula por segmento sobre montos observados > 0. **Leve** = fuera de Q±1.5·IQR (solo se marca); "
          "**Extremo** = fuera de Q±3·IQR (se winsoriza al límite 1.5·IQR y se conserva el original).", ""]
    filas = []
    for s, l in resumen["atipicos"].items():
        if l.get("evaluado"):
            filas.append([s, l["n"], l["q1"], l["q3"], l["lim_inf"], l["lim_sup"], l["maximo_observado"],
                          l["n_leve_alto"], l["n_extremo_alto"], l["n_leve_bajo"] + l["n_extremo_bajo"]])
        else:
            filas.append([s, l["n"], None, None, None, None, None, None, None, l["motivo"]])
    L += [_tabla_md(pd.DataFrame(filas, columns=["Segmento", "n", "Q1", "Q3", "Lím. inf.", "Lím. sup.", "Máximo",
                                                 "Leves altos", "Extremos altos", "Bajos / nota"])), "",
          "## 6. Perfil final", "", _tabla_md(fin["columnas"]), "",
          "## 7. Gráficas", "",
          f"![Nulos]({prefijo}_grafica_nulos.png)", "", f"![Duplicados]({prefijo}_grafica_duplicados.png)", "",
          f"![Atípicos]({prefijo}_grafica_atipicos.png)", "",
          "## 8. Advertencias para el equipo", ""] + [f"- {a}" for a in resumen["advertencias"]]
    ruta.write_text("\n".join(L) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------------------------------------------
# Orquestación
# ----------------------------------------------------------------------------------------------------------------
def _json_default(o: Any):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, pd.DataFrame):
        return o.to_dict(orient="records")
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    return str(o)


def procesar_fuente(ruta_entrada: str | Path, config: str, dir_salida: Path) -> dict:
    """Carga un archivo, lo limpia y guarda datos limpios, evidencia, reporte, JSON de resumen y gráficas."""
    dir_salida.mkdir(parents=True, exist_ok=True)
    crudo = cargar_archivo(ruta_entrada)
    df, resumen = limpiar_datos(crudo, config=config)
    cfg = _resolver_config(crudo, config)
    p = cfg["nombre"]
    df.to_csv(dir_salida / f"{p}_limpio.csv", index_label="indice_original", encoding="utf-8")
    resumen["duplicados_excluidos"].to_csv(dir_salida / f"{p}_duplicados_excluidos.csv", index_label="indice_original", encoding="utf-8")
    resumen["auditoria"].to_csv(dir_salida / f"{p}_auditoria_cambios.csv", index=False, encoding="utf-8")
    graficar_nulos(resumen, dir_salida / f"{p}_grafica_nulos.png")
    graficar_duplicados(resumen, dir_salida / f"{p}_grafica_duplicados.png")
    graficar_atipicos(crudo, df, cfg, resumen, dir_salida / f"{p}_grafica_atipicos.png")
    generar_reporte_md(resumen, p, dir_salida / f"reporte_calidad_{p}.md")
    ligero = {k: v for k, v in resumen.items() if k not in ("auditoria", "duplicados_excluidos")}
    (dir_salida / f"{p}_resumen.json").write_text(json.dumps(ligero, ensure_ascii=False, indent=2, default=_json_default),
                                                  encoding="utf-8")
    return resumen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Limpieza y perfil de calidad de transacciones bancarias.")
    ap.add_argument("--atm", default="transacciones_atm.csv", help="CSV de transacciones de cajero")
    ap.add_argument("--digitales", default="transferencias_digitales.json", help="JSON de transferencias digitales")
    ap.add_argument("--salida", default="salida_limpieza", help="Carpeta de salida")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    errores = 0
    for ruta, cfg in ((args.atm, "atm"), (args.digitales, "transferencias_digitales")):
        try:
            r = procesar_fuente(ruta, cfg, Path(args.salida))
            print(f"[OK] {cfg}: {r['filas_entrada']} -> {r['filas_salida']} filas | duplicados excluidos: "
                  f"{r['duplicados_exactos_excluidos']} | cambios auditados: {len(r['auditoria'])}")
        except (ErrorCargaDatos, ErrorConfiguracion, ValueError) as exc:
            errores += 1
            LOG.error("[%s] %s", cfg, exc)
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())
