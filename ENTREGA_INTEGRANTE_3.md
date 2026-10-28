# Entrega del integrante 3: validación y cuarentena

Rama: `codex/validacion-cuarentena-actualizada`.

## Archivos y responsabilidades

El módulo público es `validacion_cuarentena.py`. Expone `TransaccionBancaria` y `validar_transacciones(registros, catalogo_cuentas=None)`. Se entrega con pruebas, reglas en la sección Parte 3 de `REGLAS_DE_NEGOCIO.md`, instrucciones en `README_VALIDACION.md` y una demostración en `demo_validacion.py`.

## Para el integrante 2

```python
from validacion_cuarentena import validar_transacciones

validos, rechazados = validar_transacciones(
    resultado_integracion.datos.to_dict(orient="records")
)
```

La entrada respeta el esquema actual de integración. Debe conservar `ind_*`, `indicadores_error`, fuente e identidad de origen. Para auditar el crudo anterior a limpieza, adjuntar `registro_original` mediante el recorrido de `ejecutar_validacion` en la demostración. Sin ese campo, solo se puede conservar la entrada integrada recibida.

## Para el integrante 4

Las dos salidas son listas de diccionarios compatibles con JSON. Cada rechazado contiene `motivo_rechazo_cuarentena` y `errores_validacion`. Se conservan `registro_original`, `registro_integrado` y `posicion_validacion`.

- Usar las listas devueltas para decidir la tabla de destino.
- Guardar dinero normalizado como texto decimal exacto o centavos enteros y fechas como texto ISO.
- Serializar los campos anidados con `json.dumps` antes de insertarlos en SQLite.
- Conservar archivo/hash/fuente/fila como identidad física para auditoría e idempotencia. El identificador de negocio puede repetirse.
- La transacción, las restricciones y la idempotencia definitivas corresponden al módulo de persistencia.

## Reglas adoptadas para esta entrega

Se acepta cero en consultas de saldo; los negativos y cero en movimientos monetarios van a cuarentena mientras no haya una convención de reversos. La comisión ATM debe ser explícita y no negativa; la ausencia de comisión digital se permite. Los importes imputados o recortados requieren revisión. CLABE exige 18 dígitos ASCII, sin certificar checksum ni existencia bancaria. Una operación fallida puede ser un registro válido.

Son políticas académicas documentadas y ajustables por el equipo; no se atribuye una aprobación previa a los compañeros.

## Verificación

Seguir los comandos de `README_VALIDACION.md`. El catálogo oficial debe obtenerse del archivo del docente enlazado allí. No se incluye el catálogo ni las salidas con evidencia bancaria completa en Git. Los ejemplos sintéticos de las pruebas están identificados como tales.

La ejecución con los archivos reales conserva las 1,220 filas: 1,053 válidas y 167 rechazadas. El reporte agregado se encuentra en `reports/validacion/REPORTE_VALIDACION.md`. La demostración verifica limpieza, integración y validación; la persistencia SQLite se conectará con el integrante 4.
