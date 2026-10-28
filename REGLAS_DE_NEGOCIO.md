## Parte 1: Calidad y limpieza de datos (Integrante 1)

Cifras obtenidas de la ejecución real con `transacciones_atm.csv` (620 filas) y `transferencias_digitales.json` (600 registros).
Detalle completo en `docs/limpieza/DECISIONES_LIMPIEZA.md` y `reports/limpieza/`.

| Regla | Qué se hace | Justificación | Evidencia |
|---|---|---|---|
| R1 Duplicados exactos | Se detectan las filas idénticas (20 en ATM, 0 en JSON) y las copias posteriores a la primera se marcan con `ind_duplicado_exacto`. **No se eliminan** (parámetro `excluir_duplicados`, por defecto `False`) | Una fila idéntica con el mismo ID es una carga repetida; la decisión de enviarla a cuarentena corresponde a la validación. La evidencia se guarda aparte | `*_duplicados_detectados.csv` |
| R2 Normalización | Espacios recortados; valores como `N/A` o `""` pasan a nulo; mayúsculas unificadas en campos de catálogo (`resultado`, `status`, `currency`, ...) | `exitosa` y `EXITOSA` son el mismo valor. En `notification_email` había 8 nulos y 8 cadenas vacías (16 nulos reales) | regla `NORMALIZAR_MAYUSCULAS`, `CENTINELA_A_NULO` |
| R3 Fechas | Se agrega `fecha_hora_std` en ISO 8601 con zona; la columna original se conserva | Hay 3 formatos de texto en ATM, y en el JSON ISO con offset y epoch en segundos. Supuesto: sin zona = UTC-06:00 | `fecha_formato_origen` |
| R4 Clasificación de faltantes | Prueba chi-cuadrada / Mann-Whitney con Bonferroni. `monto_mxn`, `id_cajero` y `notification_email`: **sin evidencia contra MCAR** | Con los datos observados no se puede demostrar el mecanismo ni descartar MNAR; con pocos faltantes la prueba tiene poca potencia | sección 3 de los reportes |
| R5 Imputación numérica | `monto_mxn`: 27 nulos (5.2 % de la población aplicable) → **mediana por tipo de operación** (RETIRO 2,000; DEPOSITO 4,000; PAGO_SERVICIOS 4,000) | Se usa mediana y no media porque la distribución es muy asimétrica (asimetría observada 21.8) y la media se distorsiona con un solo valor extremo. Solo se imputa si falta menos del 10 % | `ind_monto_imputado`, `ind_monto_original` |
| R6 Imputación categórica | `id_cajero` → `CAJERO_NO_REGISTRADO`; `notification_email` → `SIN_CORREO` | Etiqueta controlada: no inventa un cajero ni un correo. No se imputan cuentas, CLABE ni identificadores | `ind_*_imputado` |
| R7 Montos ≤ 0 | **No se modifican ni eliminan.** Se clasifican en `ind_monto_estado` | `CONSULTA_SALDO` con $0 es legítimo (97 filas). Un monto ≤ 0 en una operación que mueve dinero es imposible o requiere regla de negocio: el módulo de validación decide su destino. Hoy no hay negativos | `ind_monto_estado` |
| R8 Atípicos | IQR con factor 1.5, por segmento (tipo de operación en ATM; moneda en JSON), sobre montos observados > 0 | Los segmentos tienen escalas distintas y el IQR global mezclaría MXN con USD. No se usan los $0 estructurales | `ind_monto_atipico` |
| R9 Capping | Los valores fuera de la cerca extrema (3×IQR) se recortan al límite superior de 1.5×IQR: 1 en ATM (3,500,000 → 11,750) y 18 en JSON (MXN → 50,667.57; USD → 49,975.76) | Se recorta en vez de eliminar filas para no perder registros. En estos datos no hay atípicos leves: todos son extremos | `ind_monto_recortado`, `ind_monto_original`, regla `WINSORIZACION_IQR` |
| R10 No destruir evidencia | Todo cambio se registra celda a celda (valor original, valor nuevo, regla) | Permite auditar y revertir cualquier transformación | `*_auditoria_cambios.csv` |

### Límites de esta parte
No valida CLABE, formato de correo, existencia de cuentas ni estatus permitidos (Integrante 3), no convierte monedas ni cruza con catálogo (Integrante 2) y no escribe en la base de datos (Integrante 4).

### Advertencias
* Los 18 extremos del JSON son el 3 % de los registros: parece un error sistemático de escala o captura, no ruido. Se recomienda enviarlos a cuarentena usando el monto original.
* Un monto imputado no debe usarse para conciliar saldos; por eso queda marcado.


## Parte 1: Calidad y limpieza de datos (Integrante 1)

Cifras obtenidas de la ejecución real con `transacciones_atm.csv` (620 filas) y `transferencias_digitales.json` (600 registros).
Detalle completo en `docs/limpieza/DECISIONES_LIMPIEZA.md` y `reports/limpieza/`.

| Regla | Qué se hace | Justificación | Evidencia |
|---|---|---|---|
| R1 Duplicados exactos | Se detectan las filas idénticas (20 en ATM, 0 en JSON) y las copias posteriores a la primera se marcan con `ind_duplicado_exacto`. **No se eliminan** (parámetro `excluir_duplicados`, por defecto `False`) | Una fila idéntica con el mismo ID es una carga repetida; la decisión de enviarla a cuarentena corresponde a la validación. La evidencia se guarda aparte | `*_duplicados_detectados.csv` |
| R2 Normalización | Espacios recortados; valores como `N/A` o `""` pasan a nulo; mayúsculas unificadas en campos de catálogo (`resultado`, `status`, `currency`, ...) | `exitosa` y `EXITOSA` son el mismo valor. En `notification_email` había 8 nulos y 8 cadenas vacías (16 nulos reales) | regla `NORMALIZAR_MAYUSCULAS`, `CENTINELA_A_NULO` |
| R3 Fechas | Se agrega `fecha_hora_std` en ISO 8601 con zona; la columna original se conserva | Hay 3 formatos de texto en ATM, y en el JSON ISO con offset y epoch en segundos. Supuesto: sin zona = UTC-06:00 | `fecha_formato_origen` |
| R4 Clasificación de faltantes | Prueba chi-cuadrada / Mann-Whitney con Bonferroni. `monto_mxn`, `id_cajero` y `notification_email`: **sin evidencia contra MCAR** | Con los datos observados no se puede demostrar el mecanismo ni descartar MNAR; con pocos faltantes la prueba tiene poca potencia | sección 3 de los reportes |
| R5 Imputación numérica | `monto_mxn`: 27 nulos (5.2 % de la población aplicable) → **mediana por tipo de operación** (RETIRO 2,000; DEPOSITO 4,000; PAGO_SERVICIOS 4,000) | Se usa mediana y no media porque la distribución es muy asimétrica (asimetría observada 21.8) y la media se distorsiona con un solo valor extremo. Solo se imputa si falta menos del 10 % | `ind_monto_imputado`, `ind_monto_original` |
| R6 Imputación categórica | `id_cajero` → `CAJERO_NO_REGISTRADO`; `notification_email` → `SIN_CORREO` | Etiqueta controlada: no inventa un cajero ni un correo. No se imputan cuentas, CLABE ni identificadores | `ind_*_imputado` |
| R7 Montos ≤ 0 | **No se modifican ni eliminan.** Se clasifican en `ind_monto_estado` | `CONSULTA_SALDO` con $0 es legítimo (97 filas). Un monto ≤ 0 en una operación que mueve dinero es imposible o requiere regla de negocio: el módulo de validación decide su destino. Hoy no hay negativos | `ind_monto_estado` |
| R8 Atípicos | IQR con factor 1.5, por segmento (tipo de operación en ATM; moneda en JSON), sobre montos observados > 0 | Los segmentos tienen escalas distintas y el IQR global mezclaría MXN con USD. No se usan los $0 estructurales | `ind_monto_atipico` |
| R9 Capping | Los valores fuera de la cerca extrema (3×IQR) se recortan al límite superior de 1.5×IQR: 1 en ATM (3,500,000 → 11,750) y 18 en JSON (MXN → 50,667.57; USD → 49,975.76) | Se recorta en vez de eliminar filas para no perder registros. En estos datos no hay atípicos leves: todos son extremos | `ind_monto_recortado`, `ind_monto_original`, regla `WINSORIZACION_IQR` |
| R10 No destruir evidencia | Todo cambio se registra celda a celda (valor original, valor nuevo, regla) | Permite auditar y revertir cualquier transformación | `*_auditoria_cambios.csv` |

### Límites de esta parte
No valida CLABE, formato de correo, existencia de cuentas ni estatus permitidos (Integrante 3), no convierte monedas ni cruza con catálogo (Integrante 2) y no escribe en la base de datos (Integrante 4).

### Advertencias
* Los 18 extremos del JSON son el 3 % de los registros: parece un error sistemático de escala o captura, no ruido. Se recomienda enviarlos a cuarentena usando el monto original.
* Un monto imputado no debe usarse para conciliar saldos; por eso queda marcado.

