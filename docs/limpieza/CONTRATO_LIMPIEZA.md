# Contrato de salida: módulo de limpieza (Integrante 1)

Qué entrega `src/limpieza_datos.py` y qué deben (y no deben) hacer los demás módulos con su salida.

## Cómo se llama
```python
from src.limpieza_datos import cargar_archivo, limpiar_datos

df_atm, res_atm = limpiar_datos(cargar_archivo("data/raw/transacciones_atm.csv"), config="atm")
df_dig, res_dig = limpiar_datos(cargar_archivo("data/raw/transferencias_digitales.json"),
                                config="transferencias_digitales")
```
* Entrada: DataFrame crudo (no se modifica; la función trabaja sobre una copia).
* Salida: `(df_limpio, resumen)`. `resumen["auditoria"]` (cambios celda a celda) y
  `resumen["duplicados_detectados"]` (evidencia de copias exactas) son DataFrames.
* **No se elimina ninguna fila por defecto** (ATM: 620 → 620; JSON: 600 → 600). Con `excluir_duplicados=True`
  las copias exactas salen del flujo (ATM: 620 → 600).
* No lanza excepciones por datos sucios; solo por entradas inválidas (`ErrorCargaDatos`, `ErrorConfiguracion`, `ValueError`).

## Columnas
**Originales (limpias, mismos nombres que la fuente)**
* ATM (8): `id_transaccion, fecha_hora, id_cuenta, tipo_operacion, monto_mxn, id_cajero, resultado, comision_aplicada`
* Transferencias (10): `transfer_id, timestamp, account_id, amount, currency, beneficiary_clabe, notification_email, concept, status, platform`

**Derivadas.** Convención acordada con el Integrante 2: todo indicador lleva el prefijo `ind_`, que es lo que su módulo copia a su esquema común.
| Columna | Significado |
|---|---|
| `ind_monto_original` | Monto tal como llegó, antes de imputar o recortar |
| `ind_monto_estado` | `POSITIVO`, `FALTANTE`, `CERO_ESPERADO`, `CERO_REVISAR`, `NEGATIVO_REVISAR` |
| `ind_monto_atipico` | `NO`, `LEVE_ALTO/BAJO`, `EXTREMO_ALTO/BAJO`, `NO_EVALUADO` |
| `ind_monto_imputado` | `True` si el monto era nulo y se imputó con la mediana del segmento |
| `ind_monto_recortado` | `True` si el monto se recortó al límite IQR (winsorización) |
| `ind_id_cajero_imputado` (ATM), `ind_notification_email_imputado` (JSON) | `True` si el nulo se reemplazó por una etiqueta controlada |
| `ind_duplicado_exacto` | `True` en las copias exactas posteriores a la primera aparición |
| `ind_id_repetido` | `True` si el ID se repite con contenido **distinto** (conflicto). Hoy: ninguno |
| `fecha_hora_std`, `fecha_formato_origen` | Fecha ISO 8601 con zona UTC-06:00 y formato detectado en la fuente (no las copia el módulo de integración, que normaliza por su cuenta) |

El índice del DataFrame es la fila original de la fuente (base 0, sin contar el encabezado).

## Reglas para quien consume la salida
**Integrante 2 (integración)**
1. Recibes las mismas filas que el archivo original; ninguna se elimina. Las 20 copias exactas del ATM llegan marcadas con `ind_duplicado_exacto`, y tu regla R1 coincide con esa marca.
2. **No se convirtieron monedas.** El JSON trae `currency` (MXN/USD) y la conversión a 19.50 es tuya. `monto_mxn` del ATM se asumió ya en MXN por su nombre.
3. En `monto_mxn` / `amount` el valor ya viene imputado y recortado; el original está en `ind_monto_original`. Escala (Min-Max) y segmenta sobre el monto limpio.
4. El IQR se calculó por tipo de operación (ATM) y por moneda (JSON), antes de la conversión. Está pendiente decidir si el recorte debe repetirse sobre el monto ya convertido a MXN (ver puntos abiertos).

**Integrante 3 (validación)**
1. Este módulo **no rechaza** nada. La cuarentena es tuya.
2. `CAJERO_NO_REGISTRADO` y `SIN_CORREO` son etiquetas controladas, no datos reales: distínguelas con `ind_*_imputado` para no rechazarlas como cajero o correo inválidos.
3. Para rechazar montos anómalos usa `ind_monto_original`, `ind_monto_atipico` e `ind_monto_estado`: en la columna principal el extremo ya viene recortado.
4. Las copias marcadas con `ind_duplicado_exacto` comparten ID con su original; si la llave de la BD es el ID, deben ir a cuarentena.
5. `CERO_REVISAR` y `NEGATIVO_REVISAR` quedaron sin modificar a la espera de la regla de negocio. En los archivos actuales no hay ninguno; el único cero legítimo es `CONSULTA_SALDO`.
6. CLABE, formato de correo, cuentas y estatus **no se validan** aquí. Los datos llegan como estaban (solo con espacios recortados y mayúsculas en `status`, `currency`, `resultado`, `tipo_operacion` e `id_cajero`).

**Integrante 4 (base de datos y `main.py`)**
1. Si se usa el ID como llave, tras descartar las copias marcadas `id_transaccion` y `transfer_id` son únicos en cada fuente.
2. `resumen` trae los conteos para el resumen final (`filas_entrada`, `filas_salida`, `duplicados_exactos_detectados`, `reglas_aplicadas`).
3. `resumen["auditoria"]` y `resumen["duplicados_detectados"]` pueden guardarse como evidencia.

## Supuestos y puntos abiertos
* Las fechas sin zona horaria se asumieron en UTC-6.
* Pendiente: regla de negocio para montos ≤ 0 fuera de consultas de saldo.
* Pendiente: catálogo de cuentas (`catalogo_cuentas.csv`) para identificar cuentas huérfanas (tarea del Integrante 2).
* Pendiente: los montos en USD tienen la misma escala que los MXN; al convertir a 19.50 aparecen importes muy altos. Decidir si el recorte se repite después de convertir.
* Pendiente: estructura de carpetas (módulos en `src/` o en la raíz) con el Integrante 4.
