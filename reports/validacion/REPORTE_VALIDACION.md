# Evidencia de ejecución del integrante 3

Base: main 23537fa. Rama: codex/validacion-cuarentena-actualizada. Fecha: 27 de octubre de 2026.

## Datos reales y resultados

Se ejecutó `demo_validacion.py --catalogo ../fuentes_drive/catalogo_cuentas.csv` con las fuentes ATM/JSON versionadas y el catálogo oficial descargado de Drive, no con el catálogo sintético de las pruebas.

| Fuente | Leídos | Válidos | Cuarentena |
|---|---:|---:|---:|
| ATM | 620 | 540 | 80 |
| Digital | 600 | 513 | 87 |
| Total | 1,220 | 1,053 | 167 |

Cobertura comprobada por fuente, posición e identificador. La suma de salidas es 1,220. Todos conservan registro original e integrado. Los mensajes de rechazo no contienen valores bancarios.

Causas: CUENTA_HUERFANA 61, CLABE_INVALIDA 46, DUPLICADO_EXACTO 20, MONTO_IMPUTADO 27, MONTO_RECORTADO 19. Se cuentan por campo y pueden coexistir; no deben sumarse como filas distintas.

Los JSON completos se generan en reports/validacion y quedan excluidos de Git. El resumen público con hashes está en resumen.json.

## Pruebas ejecutadas

```powershell
$env:ETL_DATOS_DIR = (Resolve-Path data/raw).Path
python -m pytest tests test_integracion_datos.py -q --tb=short -p no:cacheprovider
```

Resultado observado tras la corrección final: **136 passed, 3 warnings**, código 0, en 17.08 segundos. Comprende las 66 pruebas existentes de integración y 70 casos adicionales de validación/flujo. Las tres advertencias son de una fixture existente de pytest, obsoleta para pytest 10; no hubo fallos.

Se corrigió la revalidación con catálogo explícito: una señal histórica `CUENTA_HUERFANA` ya no provoca rechazo si el catálogo actualizado contiene la cuenta. Se conserva la señal en `registro_integrado`, y los otros motivos de rechazo siguen vigentes. Las pruebas de regresión cubren ambas condiciones.

Entorno usado: Python 3.12, Pydantic 2.14.1, Pandas 3.0.6, NumPy 2.5.3, pytest 9.1.1. También pasó git diff --check.

Ejemplos completos sintéticos reproducibles están en tests/test_validacion_cuarentena.py. Incluyen ATM aceptado, transferencia aceptada, consulta con cero aceptada y lote válido–inválido–válido. Los rechazos incluyen múltiples problemas sin detener el lote.

## Alcance individual

El integrante 3 entrega validacion_cuarentena.py, pruebas, reglas y demostración con originales auditables. Reutiliza limpieza e integración del equipo. El único cambio funcional en integración sustituye el verificador de éxito constante por una comprobación efectiva. No cambia sus conversiones o estadísticas.

La persistencia SQLite del integrante 4 no se ejecutó ni se certifica en este reporte.
