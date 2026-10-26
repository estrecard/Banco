# Reglas de negocio, decisiones de diseño y supuestos

> **Importante:** la guía original del proyecto y los archivos de datos no estaban disponibles al escribir este documento. Las reglas de validación de la sección 1 son las **implementadas en el módulo de referencia** y son **provisionales**: el Integrante 3 debe confirmarlas o reemplazarlas. Las secciones 2 y 3 sí describen decisiones de la capa del Integrante 4.

## 1. Reglas de validación (módulo de referencia, provisionales)

| # | Regla | Motivo registrado en cuarentena |
|---|---|---|
| 1 | `id_origen` obligatorio | `id_origen ausente` |
| 2 | `(fuente_origen, id_origen)` único dentro del lote; el 2.º se rechaza | `id_origen duplicado en el lote` |
| 3 | `fecha_hora` presente, válida, ≥ 2000-01-01 y no futura | `fecha_hora ausente o inválida` / `fecha_hora en el futuro` |
| 4 | `monto` numérico, finito, con ≤ 2 decimales | `monto ausente o no numérico` |
| 5 | `monto` > 0 | `monto no positivo` |
| 6 | `monto` ≤ 1 000 000.00 (umbral **supuesto**) | `monto anómalo (excede el máximo)` |
| 7 | `moneda` ∈ {MXN, USD} (**supuesto**) | `moneda ausente o no reconocida` |
| 8 | `tipo_transaccion` ∈ {RETIRO, DEPOSITO, TRANSFERENCIA, PAGO} (**supuesto**) | `tipo_transaccion ausente o no reconocido` |
| 9 | `cuenta_origen` obligatoria, 6-24 caracteres alfanuméricos (**supuesto**) | `cuenta_origen ausente` / `cuenta con formato inválido` |
| 10 | Transferencia digital: `cuenta_destino` obligatoria y distinta de la de origen | `cuenta_destino ausente en transferencia` / `cuenta_destino igual a cuenta_origen` |
| 11 | Si hay catálogo: la cuenta debe existir en él | `cuenta inexistente en catálogo` |

Varios motivos sobre un mismo registro se unen con `; `.

## 2. Reglas de integración y persistencia (Integrante 4)

* **Lectura:** una fila de CSV con distinto número de columnas que el encabezado, o un elemento JSON que no es objeto, **no se descarta**: va a cuarentena con motivo `fila_malformada: ...` y su contenido crudo.
* **Archivo ilegible** (vacío, binario, JSON truncado, estructura inesperada): esa fuente se omite y se informa; el pipeline continúa con la otra (estado `COMPLETADA_PARCIAL`, código de salida 1). Con `--estricto` se aborta. Si ninguna fuente se puede leer: error fatal y no se crea ni modifica la BD.
* **Conciliación obligatoria antes de guardar:** `leídos = válidos + rechazados`. Si no cuadra (p. ej. un módulo perdió registros) no se guarda nada.
* **Segunda línea de defensa:** un registro que el validador aprobó pero que no es persistible (p. ej. monto con más de 2 decimales) se manda a cuarentena con motivo `no persistible: ...`.
* **Rechazo sin motivo:** si un módulo devuelve un rechazo sin `motivo_rechazo_cuarentena`, se asigna `motivo no especificado por el validador`; nunca se guarda un rechazo sin explicación.
* **Trazabilidad:** cada fila conserva `fuente_origen`, `id_origen` y `id_corrida`; la cuarentena conserva además el registro original completo en JSON.
* **Atomicidad:** válidos y rechazados de una corrida se confirman juntos o no se confirma ninguno.

## 3. Estrategia de idempotencia

| Elemento | Identificador | Si se repite |
|---|---|---|
| Transacción válida | `id_transaccion = FUENTE:id_origen` (+ `UNIQUE(fuente_origen, id_origen)`) | Se omite; si el contenido cambió se cuenta como **conflicto** y se conserva la fila original |
| Registro en cuarentena | `hash_registro` = SHA-256(fuente + JSON canónico del registro original) | Se omite |

Consecuencias aceptadas: dos filas **idénticas** y sin identificador dentro del mismo archivo producen un solo registro en cuarentena (se cuenta como duplicado omitido); corregir una transacción en el origen **no** actualiza la existente, se reporta como conflicto para revisión humana.

## 4. Supuestos pendientes de confirmar con el equipo

1. Nombres de columnas reales de `transacciones_atm.csv` y `transferencias_digitales.json` (las referencias aceptan varios alias).
2. Existencia, formato y contenido del `catalogo_cuentas.csv` original.
3. Umbral de monto anómalo, monedas y tipos de transacción permitidos.
4. Si el identificador de origen de ATM y de transferencias comparte espacio de nombres (hoy se separan por `fuente_origen`).
5. Si una transacción puede aparecer a la vez en ATM y en transferencias digitales (no se concilian entre fuentes).
