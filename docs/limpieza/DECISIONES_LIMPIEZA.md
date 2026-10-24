# Decisiones de limpieza (Integrante 1)

Cada decisión indica **qué se hace, por qué y qué evidencia se conserva**. Las cifras provienen de la ejecución real con
`transacciones_atm.csv` (620 filas) y `transferencias_digitales.json` (600 registros).

## Principios

1. **No se destruye evidencia.** El DataFrame de entrada nunca se modifica; cada cambio se registra celda a celda en
   `*_auditoria_cambios.csv` (valor original, valor nuevo, regla, detalle), y el monto original queda en `<monto>_original`.
2. **Marcar antes que corregir.** Todo valor imputado o recortado lleva una bandera (`flag_*`), para que el Integrante 3
   decida qué va a cuarentena.
3. **Este módulo no valida reglas estrictas** (CLABE de 18 dígitos, correo válido, catálogo de cuentas, estatus permitidos,
   límites de monto). Solo las *diagnostica* en el perfil; no genera rechazos ni flags por fila.

## Decisiones por tema

| Tema | Decisión | Justificación | Evidencia |
|---|---|---|---|
| Duplicados exactos | Se separan 20 filas del ATM (se conserva la 1.ª aparición). En el JSON hay 0. | Un duplicado exacto con el mismo ID es una carga repetida, no una transacción nueva. | `atm_duplicados_excluidos.csv` (con `duplicado_de_indice`) |
| ID repetido con contenido distinto | No se elimina; se marca `flag_id_repetido`. | Es un conflicto, no un duplicado; requiere revisión. Hoy: 0 casos. | columna `flag_id_repetido` |
| Mayúsculas | `resultado` (ATM, 85 celdas) y `status`/`currency` (JSON, 121 celdas) se pasan a MAYÚSCULAS. | `exitosa` y `EXITOSA` son el mismo valor del catálogo. No se toca `concept` ni `platform`. | regla `NORMALIZAR_MAYUSCULAS` |
| Nulos disfrazados | Cadenas vacías / `N/A` / `-` se convierten a nulo. En `notification_email` había 8 `null` **y 8 cadenas vacías** (16 nulos reales). | Pandas no cuenta `""` como nulo, así que el perfil crudo subestima. | regla `CENTINELA_A_NULO` |
| Fechas | Se agrega `fecha_hora_std` (ISO 8601, UTC-06:00) y `fecha_formato_origen`. Se conservan los valores crudos. Formatos hallados: `YYYY/MM/DD HH:MM:SS`, `DD/MM/YYYY HH:MM`, `YYYY-MM-DD HH:MM:SS` (ATM); ISO con offset y epoch en segundos (JSON). | `DD/MM` se confirma porque aparecen días hasta 30 y meses hasta 10, nunca al revés. **Supuesto:** los formatos sin zona están en UTC-06:00 (el JSON ya usa ese offset). | columnas derivadas |
| Faltantes MCAR/MAR | Prueba chi-cuadrada / Mann-Whitney del indicador de faltante contra las demás variables, con corrección de Bonferroni. Resultado: `monto_mxn`, `id_cajero` y `notification_email` **sin evidencia contra MCAR**. | No se puede demostrar el mecanismo con los datos observados: un p alto no confirma MCAR y MNAR no se descarta. Con pocos faltantes la potencia es baja. | sección 3 del reporte |
| Imputación numérica | `monto_mxn`: 27 nulos (5.4 % de la población aplicable) → **mediana por tipo de operación** (RETIRO 2,000; DEPOSITO 4,000; PAGO_SERVICIOS 4,000). | Es apropiada porque falta < 10 % (umbral configurable), hay más de 100 observaciones por segmento y los montos son asimétricos/con extremos (la mediana es robusta). Se calcula por segmento porque las escalas difieren. Si el faltante superara el umbral, **no** se imputa. | `flag_monto_imputado`, `monto_mxn_original` |
| Imputación categórica | `id_cajero` (42 nulos) → `CAJERO_NO_REGISTRADO`; `notification_email` (16) → `SIN_CORREO`. | Etiqueta controlada: no inventa un cajero ni un correo. Se eligió esta etiqueta y no `ANONIMO` porque aquí el dato no es “anónimo”, es no registrado (cambiable en `cols_categoricas_nulos`). No se imputan `id_cuenta`, `beneficiary_clabe` ni IDs. | `flag_<col>_imputado` |
| Monto cero / negativo | **No se modifican.** Se clasifica en `monto_estado`: `CERO_ESPERADO` (consulta de saldo: 97 filas), `CERO_REVISAR`, `NEGATIVO_REVISAR`, `FALTANTE`. Hoy no hay negativos ni ceros en operaciones que muevan dinero. | Un $0 en `CONSULTA_SALDO` es legítimo; un negativo podría ser reverso/contracargo o un error. Falta la regla de negocio (ver preguntas abiertas). | `monto_estado` |
| Atípicos (IQR × 1.5) | Se calcula por segmento (tipo de operación en ATM; moneda en JSON) sobre montos observados > 0. **Leve** = fuera de Q±1.5·IQR (solo se marca). **Extremo** = fuera de Q±3·IQR. | No se mezclan escalas distintas ni los $0 estructurales. Los cuartiles se calculan antes de imputar para que las medianas imputadas no los sesguen. | `monto_atipico` |
| Capping / winsorización | Solo los **extremos altos** se recortan al límite superior 1.5·IQR: 1 en ATM (3,500,000 → 11,750 en RETIRO) y 18 en el JSON (MXN → 50,667.57; USD → 49,975.76). No se recortan los atípicos leves (por ejemplo, los $12,000 del ATM son una denominación real). | Se recorta solo cuando el valor es inverosímil para el segmento. El original se conserva. Es desactivable con `aplicar_capping=False`. | `flag_monto_winsorizado`, regla `WINSORIZACION_IQR` |

## Advertencias importantes

* **Los extremos del JSON no parecen ruido.** 18 de 600 transferencias (3 %) superan 3 M en una distribución cuyo Q3 es ~26 K.
  Un patrón así sugiere un error sistemático de escala o captura. El recorte es una convención estadística, **no una
  corrección**: lo prudente es enviarlos a revisión/cuarentena con el monto original.
* **MXN y USD**: el JSON mezcla monedas con importes de escala similar y no hay tipo de cambio. No se convirtió nada;
  el IQR se calculó por moneda.
* **Imputar un monto es una decisión de análisis, no de contabilidad.** Un monto imputado no debe usarse para
  conciliar ni sumar saldos; por eso queda marcado.
* **Cuentas fuera de patrón:** 32 registros del ATM tienen cuentas `CTA-8xxx/9xxx` frente al patrón dominante `CTA-00xx`.
  El patrón se infirió de los datos (no hay catálogo): se reporta en el perfil, sin modificar ni marcar por fila.
* **CLABE `12345` (46 registros) y correos inválidos** (`correo@@sinformato` ×19, `invalido.com` ×11): solo se reportan.
  Su rechazo corresponde al Integrante 3.

## Preguntas abiertas para el equipo

1. ¿Qué regla de negocio aplica a montos negativos o cero fuera de consultas de saldo? (hoy no hay casos, pero el código ya los marca)
2. ¿Los formatos de fecha sin zona están en hora de Colima (UTC-06:00)?
3. ¿Existe un catálogo de cuentas válidas o un tipo de cambio MXN/USD?
4. Integrante 2: ¿le sirven los nombres de columnas derivadas (`fecha_hora_std`, `monto_estado`, `flag_*`) o prefiere otros?
5. Integrante 3: ¿los registros con `flag_monto_winsorizado` o `monto_atipico = EXTREMO_*` van directo a cuarentena?
