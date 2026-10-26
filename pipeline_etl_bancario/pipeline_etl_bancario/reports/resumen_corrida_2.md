# Reporte del pipeline ETL - corrida 2

- Fecha (UTC): 2026-10-26T02:45:28+00:00
- Estado: **COMPLETADA**  |  Duración: 0.029 s
- Base de datos: `/home/claude/pipeline_etl_bancario/banco_etl.db`
- Catálogo de cuentas: **SINTETICO** (100 cuentas)
- Módulos: limpieza_datos=referencia, integracion_datos=referencia, validacion_cuarentena=referencia

## Resumen de conteos

| Concepto | Cantidad |
|---|---:|
| Registros leídos | 407 |
| Válidos | 380 |
| Rechazados (cuarentena) | 27 |
| Almacenados en `transacciones_limpias` (nuevos) | 0 |
| Válidos omitidos por ya existir (idempotencia) | 380 |
| Conflictos (mismo id, contenido distinto) | 0 |
| Almacenados en `cuarentena_errores` (nuevos) | 0 |
| Rechazados omitidos por ya existir | 27 |

Control: leídos = válidos + rechazados -> 407 = 380 + 27

## Total acumulado en la base de datos

| Tabla | Filas |
|---|---:|
| `transacciones_limpias` | 380 |
| `cuarentena_errores` | 27 |
| `corridas_pipeline` | 2 |

## Fuentes

| Fuente | Estado | Leídos | Rechazados en lectura |
|---|---|---:|---:|
| ATM | OK | 203 | 2 |
| DIGITAL | OK | 204 | 3 |

## Motivos de rechazo

- fila_malformada: 5
- monto no positivo: 4
- monto ausente o no numérico: 3
- id_origen duplicado en el lote: 2
- cuenta_origen ausente: 2
- monto anómalo (excede el máximo): 2
- cuenta inexistente en catálogo: 2
- fecha_hora ausente o inválida: 2
- cuenta_destino ausente en transferencia: 1
- moneda ausente o no reconocida: 1
- tipo_transaccion ausente o no reconocido: 1
- cuenta_destino igual a cuenta_origen: 1
- fecha_hora en el futuro: 1

## Advertencias y limitaciones

- Se usó un catálogo SINTÉTICO: solo válido para pruebas; no verifica cuentas reales.
- Se usaron módulos de REFERENCIA para: limpieza_datos, integracion_datos, validacion_cuarentena. Sustituir por los módulos reales de los integrantes 1-3.
