# Pipeline ETL Robusto para la Limpieza, Integración y Validación de Transacciones Bancarias con Zona de Cuarentena

Parte del **Integrante 4**: base de datos SQLite, transacciones seguras (`commit`/`rollback`), idempotencia, pruebas de resiliencia e integración final.

> **Estado honesto del proyecto**
> * Al construir esto **no estaban disponibles** `transacciones_atm.csv`, `transferencias_digitales.json`, `catalogo_cuentas.csv` ni los módulos de los integrantes 1, 2 y 3.
> * Por eso `modulos_referencia/` contiene implementaciones **de referencia** (documentan el contrato y permiten correr el pipeline) y `datos_sinteticos.py` genera datos **sintéticos**. Ninguno es el trabajo de los compañeros ni datos bancarios reales.
> * El esquema de columnas de los archivos de entrada que usan las referencias es un **supuesto**. Debe contrastarse con los archivos reales.
> * El `banco_etl.db` y los `reports/` incluidos provienen de `python main.py --demo` (catálogo y datos **sintéticos**). Bórralos antes de la primera corrida real.

## 1. Estructura

```
main.py                    # Orquestador: lee, limpia, integra, valida, concilia, guarda, reporta
base_datos.py              # SQLite: esquema, transacciones, guardado atómico e idempotente
limpieza_datos.py          # (Integrante 1)  -> colocar aquí; si falta se usa modulos_referencia/
integracion_datos.py       # (Integrante 2)  -> colocar aquí
validacion_cuarentena.py   # (Integrante 3)  -> colocar aquí
modulos_referencia/        # Implementaciones de REFERENCIA del contrato (no son las reales)
datos_sinteticos.py        # Generador de datos y catálogo SINTÉTICOS (solo pruebas/demo)
tests/                     # Pruebas unitarias, de integración y de estrés
data/                      # Poner aquí los archivos originales (data/demo/ = sintéticos)
reports/                   # Reportes de cada corrida (.json y .md)
REGLAS_DE_NEGOCIO.md       # Reglas, decisiones de diseño y supuestos
banco_etl.db               # Base de datos (generada por el pipeline)
```

## 2. Instalación desde cero

Solo usa la **biblioteca estándar** de Python (no hay nada que instalar con `pip`).

```bash
# 1. Verifica versiones (probado con Python 3.13 y SQLite 3.53; requiere SQLite >= 3.24)
python3 --version
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"

# 2. (Opcional, recomendado) entorno virtual
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Dependencias (no hay obligatorias)
pip install -r requirements.txt    # solo comentarios; pandas es opcional si tus compañeros lo usan
```

## 3. Ejecución

```bash
# A) Demostración con datos SINTÉTICOS (no necesita archivos tuyos)
python main.py --demo

# B) Con los archivos originales
#    Copia transacciones_atm.csv, transferencias_digitales.json y catalogo_cuentas.csv a data/
rm -f banco_etl.db                 # elimina la BD de la demo
python main.py

# C) Rutas personalizadas
python main.py --atm ruta/atm.csv --digital ruta/dig.json --catalogo ruta/catalogo.csv \
               --db banco_etl.db --reportes reports

# D) Sin catálogo original, solo para PRUEBAS (queda marcado como SINTETICO en BD y reporte)
python main.py --catalogo-sintetico

# E) Abortar si alguna fuente no se puede leer
python main.py --estricto
```

**Códigos de salida:** `0` completado · `1` completado parcial (alguna fuente se omitió) · `2` error fatal (no se guardó nada).

**Sin `catalogo_cuentas.csv`:** el pipeline **no** inventa uno. Continúa, **no verifica la existencia de cuentas** y lo declara en el reporte (`LIMITACIÓN: ...`, catálogo `NO_DISPONIBLE`). `--catalogo-sintetico` genera un catálogo sintético, válido solo para pruebas; con datos reales rechazaría casi todo.

Cada corrida escribe `reports/resumen_corrida_<id>.json` y `.md` con: leídos, válidos, rechazados, almacenados, duplicados omitidos, conflictos, motivos de rechazo, estado por fuente, origen del catálogo y qué módulos fueron reales o de referencia.

## 4. Pruebas

```bash
python -m unittest discover -s tests -v          # todas
python -m unittest tests.test_base_datos -v      # solo la capa SQLite
```

Resultado de la última ejecución en el entorno de desarrollo (Python 3.13.17, SQLite 3.53.1):
**67 pruebas: 66 OK, 1 omitida, 0 fallos** (~1.6 s). La omitida (`TestContratoModulosReales`) se activa sola cuando existan los tres módulos reales en la raíz.

| Archivo | Qué cubre |
|---|---|
| `tests/test_base_datos.py` | Esquema, PK/FK, restricciones `CHECK`, conversión a centavos, rollback total, idempotencia, conflictos |
| `tests/test_pipeline_integracion.py` | Flujo completo conciliado, cada defecto inyectado en cuarentena, reejecución, catálogo (archivo / sintético / ausente), archivos defectuosos, nulos y anomalías, módulos que fallan, fallo de persistencia, 10 000 registros, CLI y códigos de salida |

Además se comprobó que las pruebas detectan fallos reales: en una copia aparte se cambió `ROLLBACK` por `COMMIT` (fallaron 5 pruebas) y se hizo que un duplicado sobrescribiera en vez de omitirse (fallaron 4).

Las pruebas de integración fuerzan los módulos de referencia, de modo que los resultados esperados no dependen del código de tus compañeros.

## 5. Contrato de integración (acordar con los integrantes 1-3)

`main.py` carga primero los módulos reales de la raíz y, si no existen, los de `modulos_referencia/`. Los cambios necesarios en los módulos reales deben ser mínimos:

| Módulo | Función | Entrada → Salida |
|---|---|---|
| `limpieza_datos` | `limpiar_registros_atm(registros)` y `limpiar_registros_digitales(registros)` | `list[dict]` crudo → `list[dict]` canónico, **una fila por fila de entrada** |
| `integracion_datos` | `integrar_fuentes(atm, digitales)` | dos `list[dict]` → un `list[dict]` unificado, **sin perder registros** |
| `validacion_cuarentena` | `validar_transacciones(registros, catalogo_cuentas=None)` | → `(validos, rechazados)`; `catalogo_cuentas=None` significa "sin catálogo: no validar existencia" |

**Formato canónico** (claves): `fuente_origen` (`"ATM"`/`"DIGITAL"`), `id_origen`, `fecha_hora` (`"YYYY-MM-DD HH:MM:SS"`), `cuenta_origen`, `cuenta_destino`, `monto` (`Decimal`), `moneda`, `tipo_transaccion`, `estado`, `registro_original` (dict crudo, para trazabilidad).

**Rechazados:** dicts con `motivo_rechazo_cuarentena` (texto no vacío) y, idealmente, `fuente_origen`, `id_origen`, `registro_original`.

Se acepta también `DataFrame` de pandas (se convierte a registros, NaN → `None`). Si un módulo pierde registros, devuelve un formato incorrecto o le falta una función, el pipeline **se detiene antes de guardar** (`ConciliacionError` / `ContratoIncumplidoError`). Si la limpieza lanza una excepción, se aísla registro por registro y solo el culpable va a cuarentena.

## 6. Base de datos `banco_etl.db`

| Tabla | Clave primaria | Propósito |
|---|---|---|
| `transacciones_limpias` | `id_transaccion` = `"<FUENTE>:<id_origen>"` | Transacciones válidas |
| `cuarentena_errores` | `id_cuarentena` (autoincremental), `hash_registro` único | Rechazadas, con `motivo_rechazo_cuarentena` y `registro_original` (JSON) |
| `corridas_pipeline` | `id_corrida` | Bitácora: cada fila queda ligada a la corrida que la creó |

Restricciones y su justificación:

* `monto_centavos INTEGER CHECK > 0`: el dinero se guarda en enteros para evitar errores de punto flotante; un monto ≤ 0 nunca entra aunque falle un módulo upstream.
* `fuente_origen CHECK IN ('ATM','DIGITAL')` y `id_transaccion = fuente || ':' || id_origen`: consistencia de la clave.
* `UNIQUE (fuente_origen, id_origen)`: base de la idempotencia.
* `moneda` de 3 caracteres, textos no vacíos, `fecha_hora` con formato mínimo.
* `FOREIGN KEY ... id_corrida` con `PRAGMA foreign_keys = ON`.
* `motivo_rechazo_cuarentena NOT NULL` y no vacío: no se admite un rechazo sin explicación.

**Transacciones.** `guardar_lote()` guarda válidos **y** rechazados dentro de un único `BEGIN IMMEDIATE ... COMMIT`. Cualquier excepción provoca `ROLLBACK` de todo el lote (ambas tablas), se marca la corrida como `FALLIDA` y se informa el error. Lo confirmado en corridas anteriores no se toca.

**Idempotencia.** Válidos: `INSERT ... ON CONFLICT DO NOTHING` sobre `(fuente_origen, id_origen)`. Si la fila ya existía se cuenta como duplicado; si además el contenido difiere (`hash_contenido`), se cuenta como **conflicto** y se conserva la original sin sobrescribir. Cuarentena: se deduplica por hash del registro original. Verificado: la segunda corrida de la demo insertó 0 filas y omitió 380 válidos y 27 rechazados.

Consultas útiles (con el CLI `sqlite3`, si lo tienes instalado; si no, usa el equivalente en Python o DB Browser for SQLite):

```bash
sqlite3 banco_etl.db "SELECT motivo_rechazo_cuarentena, COUNT(*) FROM cuarentena_errores GROUP BY 1 ORDER BY 2 DESC;"
sqlite3 banco_etl.db "SELECT id_corrida, estado, leidos, validos, rechazados FROM corridas_pipeline;"
sqlite3 banco_etl.db "SELECT id_transaccion, monto_centavos/100.0 AS monto FROM transacciones_limpias LIMIT 5;"

# Equivalente sin el CLI:
python -c "import sqlite3; c=sqlite3.connect('banco_etl.db'); print(c.execute('SELECT id_corrida, estado, leidos, validos, rechazados FROM corridas_pipeline').fetchall())"
```

## 7. Solución de errores comunes

| Síntoma | Causa probable | Solución |
|---|---|---|
| `Fuente ATM omitida: ... archivo no encontrado` | Archivos no están en `data/` | Copiarlos o usar `--atm/--digital` |
| `JSON inválido o truncado` | JSON cortado o con coma sobrante | Revisar con `python -m json.tool archivo.json` |
| `contiene bytes nulos` | El archivo es binario (p. ej. un `.xlsx` renombrado) | Exportar de nuevo como CSV UTF-8 |
| `no es UTF-8; se interpretó como latin-1` | Archivo en otra codificación | Es solo un aviso; para evitarlo, guardar como UTF-8 |
| `ContratoIncumplidoError` | Un módulo real no tiene el nombre de función del contrato | Renombrar o añadir una función "adaptadora" (sección 5) |
| `ConciliacionError` | Un módulo descartó o duplicó registros | Corregir el módulo: debe devolver tantos registros como recibió |
| `database is locked` | Otro proceso (p. ej. DB Browser) tiene la BD abierta escribiendo | Cerrarlo y reintentar (el pipeline espera hasta 30 s) |
| `sqlite3.OperationalError: near "DO"` | SQLite < 3.24 | Actualizar Python/SQLite |
| `ModuleNotFoundError: No module named 'modulos_referencia'` | Se ejecutó fuera de la carpeta del proyecto | Ejecutar desde la raíz del proyecto |
| Casi todo rechazado con `cuenta inexistente en catálogo` | Se usó `--catalogo-sintetico` con datos reales | Usar el `catalogo_cuentas.csv` original o ninguno |
| Quiero empezar de cero | BD con datos de la demo | `rm banco_etl.db reports/*` |

## 8. Limitaciones conocidas

* No se validó contra los archivos ni los módulos reales; las pruebas usan datos sintéticos y módulos de referencia.
* Las reglas de `validacion_cuarentena` de referencia (monto máximo, tipos, monedas, formato de cuenta) son **provisionales** y no vienen de la guía del proyecto. Ver `REGLAS_DE_NEGOCIO.md`.
* Probado únicamente con Python 3.13.17 y SQLite 3.53.1 en Linux. El código usa solo biblioteca estándar y sintaxis compatible con 3.9+, pero no se probó en otras versiones ni en Windows/macOS.
* Un registro rechazado que luego se corrige en el origen creará un registro nuevo (otro hash) y la fila antigua seguirá en cuarentena: la cuarentena es histórica, no se "limpia" sola.
* El pipeline carga el lote completo en memoria; está probado con 10 000 registros, no con millones.
