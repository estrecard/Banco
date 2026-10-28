# Documentación del módulo de integración (`integracion_datos.py`)

**Proyecto:** Pipeline ETL Robusto con Zona de Cuarentena · ATD-2602 · Grupo 2 (Fintech)
**Rol:** Integrante 2 – Transformación e Integración (Fase 4 y Fase 5 de la guía)
**Versión del módulo:** 1.0.0

---

## 1. Qué se inspeccionó (datos reales entregados)

| Archivo | Estado | Hallazgos |
|---|---|---|
| `transacciones_atm.csv` | Leído | 620 filas × 8 columnas. 600 `id_transaccion` únicos: **20 filas son copias exactas** (mismo ID y mismo contenido). 3 formatos de fecha mezclados. 27 `monto_mxn` vacíos. 44 `id_cajero` vacíos. `resultado` mezcla `EXITOSA`/`exitosa`. 32 cuentas fuera del patrón `CTA-00xx` (`CTA-8xxx`/`CTA-9xxx`). Un monto de 3 500 000. |
| `transferencias_digitales.json` | Leído | 600 objetos con las mismas 10 llaves. **`timestamp` es mixto: 405 str ISO 8601 con offset `-06:00` y 195 enteros epoch de 10 dígitos (segundos).** 466 MXN y 134 USD. 46 CLABE de 5 dígitos. 46 correos inválidos/vacíos. 29 cuentas fuera del patrón (`CTA-7xxx`). |
| `catalogo_cuentas.csv` | **NO disponible** | No venía entre los archivos. El módulo **no lo lee ni afirma haberlo leído** (§5). |

Observaciones que **no** coinciden con lo que describe la guía: en estos archivos no hay montos negativos ni comisiones nulas. El módulo igualmente los detecta si aparecen (`MONTO_NO_POSITIVO`, `MONTO_FALTANTE`).

---

## 2. Esquema común de transacciones

Una fila = una operación, venga de ATM o de transferencia. Definido en `ESQUEMA_COMUN` y exportado a `esquema_comun.json`.

**Trazabilidad**

| Columna | Descripción |
|---|---|
| `id_registro` | Llave única `<FUENTE>#<fila_origen>` (necesaria porque `id_original` se repite en los duplicados) |
| `fuente` | `ATM_CSV` \| `TRANSFERENCIAS_JSON` |
| `archivo_origen` | Nombre del archivo |
| `fila_origen` | Posición 0-based en el archivo de origen |
| `id_original` | `id_transaccion` / `transfer_id`, sin modificar |
| `canal` | `ATM` \| `DIGITAL` |
| `plataforma` | `id_cajero` (ATM) o plataforma normalizada (`API_EMPRESARIAL`, `APP_IOS`, `APP_ANDROID`, `WEB_BANKING`) |

**Tiempo**

| Columna | Descripción |
|---|---|
| `fecha_hora_original` | Texto/valor crudo, sin alterar |
| `formato_fecha_detectado` | `ATM_YYYY/MM/DD`, `ATM_YYYY-MM-DD`, `ATM_DD/MM/YYYY`, `ISO8601_OFFSET`, `EPOCH_SEGUNDOS`, … |
| `fecha_hora_iso` | ISO 8601 con offset: `2026-09-15T08:45:00-06:00` |
| `fecha_hora_utc` | Mismo instante en UTC (`…Z`), para ordenar entre fuentes |
| `fecha`, `hora` | `YYYY-MM-DD` y `HH:MM:SS` locales |
| `zona_horaria`, `origen_zona_horaria` | Offset y su procedencia: `EXPLICITA_EN_FUENTE`, `ASUMIDA_LOCAL`, `EPOCH_UTC_CONVERTIDA` |

**Negocio**

| Columna | Descripción |
|---|---|
| `id_cuenta` | Normalizada (mayúsculas, sin espacios) |
| `tipo_operacion` | `RETIRO`, `DEPOSITO`, `PAGO_SERVICIOS`, `CONSULTA_SALDO`, `TRANSFERENCIA_SPEI` |
| `concepto` | Sólo transferencias (`NOMINA`, `PRESTAMO`, `PAGO_RENTA`, …) |
| `estado_operacion` / `estatus_original` | Estado unificado y valor crudo (ver §3.4) |
| `monto_original`, `moneda_original`, `tipo_cambio_aplicado`, `monto_mxn` | Importe antes/después de homologar moneda |
| `comision_mxn` | Sólo ATM |
| `es_operacion_monetaria` | `False` para `CONSULTA_SALDO` |
| `clabe_beneficiario`, `email_notificacion` | Tal como llegaron (texto) |

**Catálogo, duplicados e indicadores** → §5, §6, §7.
**Transformaciones numéricas** → `monto_mxn_minmax`, `comision_mxn_minmax`, `escala_recortada`, `segmento_monto` (§8).

---

## 3. Conversiones y transformaciones

### 3.1 Fechas del CSV ATM
| Patrón | Ejemplo | Interpretación |
|---|---|---|
| `YYYY/MM/DD HH:MM:SS` | `2026/09/15 08:45:00` | año-mes-día |
| `YYYY-MM-DD HH:MM:SS` | `2026-10-12 22:22:00` | año-mes-día |
| `DD/MM/YYYY HH:MM` | `12/09/2026 13:26` | **día-mes-año** |

**Por qué DD/MM y no MM/DD:** en las 200 fechas con este patrón el primer campo llega hasta 30 y el segundo nunca pasa de 12 (sólo toma 9 y 10). Es una inferencia empírica de estos datos, no una regla general; el módulo sólo acepta ese orden para esa forma.
Fechas imposibles (`31/02/2026`) → `FECHA_INVALIDA`, sin excepción.
**Zona horaria:** el CSV no trae zona. Se **asume** UTC-6 (hora local de Colima, sin horario de verano) y se marca `ASUMIDA_LOCAL` + indicador informativo `ZONA_HORARIA_ASUMIDA`. Parametrizable con `offset_local_horas`.

### 3.2 Campo `timestamp` del JSON (no se asume que sea texto)
| Tipo Python | Formato | Tratamiento |
|---|---|---|
| `str` ISO 8601 con offset | `2026-09-12T19:49:00-06:00` | Se **conserva** hora local y offset de origen (`EXPLICITA_EN_FUENTE`) |
| `int`/`float` de 10 dígitos | `1788816180` | Epoch en **segundos, UTC** por definición → `2026-09-07T21:23:00Z`; se expresa en hora local (`-06:00`) → `…T15:23:00-06:00` (`EPOCH_UTC_CONVERTIDA`) |
| `int`/`float` de 13 dígitos | ms | Epoch en milisegundos (soportado por robustez; no aparece en los datos) |
| `str` de sólo dígitos | `"1788816180"` | Se trata como epoch |
| Otro / vacío / fuera de 2000–2100 | | `FECHA_INVALIDA` |

Los 195 epoch caen entre 2026-09-02 y 2026-10-16, coherente con el resto del lote, lo que respalda la lectura "segundos, UTC".

### 3.3 Moneda
`monto_mxn = monto_original × 19.50` **sólo si** `moneda_original == "USD"`, redondeado a 2 decimales. Los montos MXN (todo el CSV, que ya se llama `monto_mxn`, y 466 transferencias) **no se reconvierten** (`tipo_cambio_aplicado = 1.0`). Moneda distinta de MXN/USD → `MONEDA_NO_SOPORTADA` y `monto_mxn` nulo (no se inventa un valor). Verificado contra recálculo independiente: 134 USD y 466 MXN correctos.

### 3.4 Identificadores y categorías
* `id_cuenta`: `strip` + mayúsculas. Formato válido esperado `CTA-NNNN`, si no → `CUENTA_INVALIDA`.
* Categorías: sin tildes, mayúsculas, `_` como separador (`Nómina`→`NOMINA`, `App iOS`→`APP_IOS`).
* Estado unificado: `EXITOSA`/`exitosa`/`LIQUIDADA`/`liquidada` → `COMPLETADA`; `FALLO_FONDOS`/`FALLO_CONEXION` → `FALLIDA`; `PENDIENTE`; `RECHAZADA`; `DEVUELTA`. El valor crudo siempre se conserva en `estatus_original`.
* CLABE y correo **no se corrigen**; se marcan (§7).

---

## 4. Trazabilidad de la fuente
Cada fila conserva `fuente`, `archivo_origen`, `fila_origen` e `id_original`. `verificar_trazabilidad()` comprueba que (a) `id_registro` es único, (b) cada fila de cada archivo aparece exactamente una vez, (c) el `id_original` coincide con el de su fila de origen. Resultado con los archivos reales: **1 220 filas = 620 + 600, 0 discrepancias**.

---

## 5. Catálogo de cuentas y huérfanas
| Situación | Comportamiento |
|---|---|
| `catalogo=None` (hoy) | `cuenta_en_catalogo` queda **nulo**, `origen_catalogo = NO_DISPONIBLE`, no se genera `CUENTA_HUERFANA`. El reporte advierte que no se evaluó. |
| `catalogo="catalogo_cuentas.csv"` o DataFrame | `cargar_catalogo()` detecta la columna (`id_cuenta`, `cuenta`, `account_id`, …) o usa `columna_cuenta=`; si no la reconoce, falla con mensaje claro. Etiqueta `REAL:<archivo>`. |
| `crear_catalogo_sintetico()` | Etiqueta `SINTETICO` en cada fila, en el reporte y con advertencia. |

**Sobre el catálogo sintético:** es una **hipótesis mía**, no un dato oficial. Se basa en que `CTA-0001…CTA-0050` aparecen repetidamente en ambos archivos y el resto de cuentas (`7xxx/8xxx/9xxx`) una sola vez. Con él salen 61 registros huérfanos (32 ATM + 29 JSON). **Esos números no deben citarse en el reporte final**; se reemplazan al llegar el catálogo real.

---

## 6. Duplicados y reintentos (se marcan, nunca se borran)
| Regla | Condición | Marca |
|---|---|---|
| R1 `DUPLICADO_EXACTO` | misma fuente + mismo `id_original` + mismo contenido | 1ª = `PRIMARIO`; copias = `SECUNDARIO` (indicador **ERROR**) |
| R2 `ID_REPETIDO_CONFLICTO` | mismo `id_original`, contenido distinto | todas `REVISAR` (**ALERTA**) |
| R3 `POSIBLE_REINTENTO_ATM` | ATM: misma cuenta + cajero + tipo + monto, IDs distintos, ≤ 5 min; se excluye `CONSULTA_SALDO` | la(s) posterior(es) `REVISAR` (**ALERTA**) |
| R4 `POSIBLE_DUPLICADO_DIGITAL` | misma cuenta + CLABE + `monto_mxn`, IDs distintos, ≤ 5 min | la(s) posterior(es) `REVISAR` (**ALERTA**) |

R1 se trata como ERROR porque dos filas con el mismo ID y contenido son el mismo evento registrado dos veces. R3/R4 son sólo ALERTA porque dos retiros idénticos seguidos pueden ser legítimos; decide una persona.
**Resultado en los datos:** 20 grupos R1 (todos en ATM). R3 y R4 no disparan: probé ventanas de hasta 120 min sin encontrar casi-duplicados con IDs distintos, así que los "reintentos de cajero" de la guía se manifiestan aquí como copias exactas. Las reglas R2–R4 están probadas con datos sintéticos en `test_integracion_datos.py`.

---

## 7. Interfaz con el Integrante 3 (indicadores, no rechazos)
El módulo **no elimina filas ni causas de rechazo**. Cada fila trae:

* `indicadores_error` — códigos separados por `|`; `requiere_cuarentena_sugerida = (indicadores_error != "")`.
* `indicadores_alerta` — revisión humana.
* `indicadores_info` — contexto.

| Severidad | Códigos |
|---|---|
| ERROR | `CUENTA_HUERFANA`, `CLABE_INVALIDA` (≠ 18 dígitos), `MONTO_FALTANTE`, `MONTO_NO_POSITIVO`, `FECHA_INVALIDA`, `MONEDA_NO_SOPORTADA`, `CUENTA_INVALIDA`, `DUPLICADO_EXACTO` (sólo copias secundarias) |
| ALERTA | `ID_REPETIDO_CONFLICTO`, `POSIBLE_REINTENTO_ATM`, `POSIBLE_DUPLICADO_DIGITAL`, `EMAIL_INVALIDO`, `PLATAFORMA_FALTANTE` |
| INFO | `ZONA_HORARIA_ASUMIDA` |

La tabla `SEVERIDAD` del módulo es el único lugar donde cambiar esta clasificación. **La decisión final de cuarentena es de la validación Pydantic**; esto es una sugerencia con causa.
Sugerencia para `motivo_rechazo_cuarentena`: usar `indicadores_error` como punto de partida y añadir los errores que lance Pydantic.
CLABE: sólo se valida el **formato** (18 dígitos). El dígito verificador no se usa: el 89 % de las CLABE de 18 dígitos del archivo no lo cumple, lo que indica datos sintéticos.

## 8. Interfaz con el Integrante 1 (datos limpios de entrada)
`integrar_datos` espera las columnas originales de cada fuente (`COLUMNAS_ATM`, `COLUMNAS_DIGITAL`). Acuerdo propuesto:

1. **No eliminar filas** en la limpieza; lo inválido se deja y se marca.
2. Si imputa o recorta, añadir columnas booleanas **`ind_*`** (p. ej. `ind_monto_imputado`, `ind_monto_recortado`). Se conservan sin cambios en la salida.
3. Entregar `monto_mxn` ya winsorizado antes del escalado. Motivo medido con los datos reales:

| Escenario | Mediana de `monto_mxn_minmax` |
|---|---:|
| Sin tratar outliers (máx. 80 757 319.5 MXN) | **0.0001** (escala inservible) |
| Con capping IQR 1.5 (tope ≈ 56 312; 141 filas recortadas) | **0.159** |

(El capping de esa comparación fue una **simulación mía** de lo que haría el Integrante 1; no es su código.) Los cortes de segmentación por cuantiles no cambian, porque los cuantiles son robustos a extremos.

## 9. Min-Max, segmentación y fuga de información
* **Min-Max:** `(x − min) / (max − min)` sobre `monto_mxn` (sólo operaciones monetarias) y `comision_mxn`. IDs, CLABE y fechas no se escalan.
* **Segmentación:** `Bajo: x ≤ c1 · Medio: c1 < x ≤ c2 · Alto: x > c2`. Por defecto `c1, c2` = cuantiles 33.3 % / 66.7 %; alternativa `metodo_segmentacion="reglas", umbrales_reglas=(c1, c2)`. Con los datos: cortes 5 000 y 18 771.2 MXN. `CONSULTA_SALDO` y montos faltantes quedan **nulos** (no se fuerza a "Bajo").
* **Población de ajuste:** operaciones monetarias, monto > 0, sin copias secundarias, fecha válida y (si hay catálogo) cuenta en catálogo, para no contaminar los parámetros con registros que irán a cuarentena. Se documenta en `criterio_ajuste`.
* **Reproducibilidad:** los parámetros se guardan en `parametros_transformacion.json` (`ruta_parametros=`). Pasar `parametros=<ruta o objeto>` **aplica sin reajustar**: un lote nuevo no altera la escala. Valores fuera del rango de ajuste se recortan a [0, 1] y se marcan `escala_recortada`.
* **Si después se hace partición entrenamiento/prueba**, ajustar sólo con entrenamiento: `integrar_datos(..., mascara_ajuste=<serie booleana>)`.
* En un ETL por lote sin partición, ajustar con el propio lote no es fuga *per se*; lo que importa es poder fijar y reutilizar los parámetros.

## 10. Supuestos y límites (para la defensa oral)
1. `DD/MM/YYYY` inferido de los datos (§3.1). 2. ATM en UTC-6 asumido. 3. Epoch = segundos UTC. 4. Catálogo sintético = hipótesis. 5. Cuantiles calculados sobre ambos canales juntos: el canal digital concentra los montos altos (355 de 600 transferencias caen en "Alto" frente a 1 de 492 ATM monetarios); si el equipo prefiere segmentar por canal, es un cambio pequeño y debe decidirse y justificarse en `REGLAS_DE_NEGOCIO.md`. 6. **Dato llamativo:** la mediana de las transferencias en USD (19 357) es casi igual a la de las MXN (18 427); aplicar ×19.50 produce montos de hasta 80.7 M MXN. Se sigue la guía, pero conviene mencionarlo.

## 11. Cómo ejecutar
```bash
pip install pandas numpy pytest
# Sin catálogo (situación actual)
python integracion_datos.py --atm datos/transacciones_atm.csv --json datos/transferencias_digitales.json --salida salida
# Con catálogo sintético (sólo pruebas, sale etiquetado)
python integracion_datos.py --atm ... --json ... --catalogo-sintetico --salida salida
# Con catálogo real, cuando llegue
python integracion_datos.py --atm ... --json ... --catalogo datos/catalogo_cuentas.csv --salida salida
# Reutilizar parámetros ya ajustados
python integracion_datos.py --atm ... --json ... --catalogo datos/catalogo_cuentas.csv --parametros salida/parametros_transformacion.json
# Pruebas
python -m pytest test_integracion_datos.py -v
```
Uso desde Python:
```python
from integracion_datos import integrar_datos, cargar_catalogo
res = integrar_datos("datos/transacciones_atm.csv", "datos/transferencias_digitales.json",
                     catalogo=cargar_catalogo("datos/catalogo_cuentas.csv"))   # o catalogo=None
df = res.datos                    # esquema común, 1 fila por operación
res.reporte_huerfanas(); res.reporte_duplicados(); res.guardar_todo("salida")
```
