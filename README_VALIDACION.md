# Integrante 3: validación y cuarentena

Implementación sobre main `23537fa`, compatible con `docs/limpieza/CONTRATO_LIMPIEZA.md`. Rama de entrega: `codex/validacion-cuarentena-actualizada`.

## Instalación y ejecución

Python 3.12 o posterior. Desde la raíz del repositorio:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m pytest tests test_integracion_datos.py -q
.venv/Scripts/python demo_validacion.py --catalogo RUTA/catalogo_cuentas.csv
```

Si las pruebas existentes no encuentran las fuentes, establecer `$env:ETL_DATOS_DIR = (Resolve-Path data/raw).Path`.

El catálogo oficial se obtiene del [archivo entregado por el docente](https://drive.google.com/file/d/1j84M1c6udV734oiblCrY_CPvGRCrbnW9/view). No se incluye en Git para evitar publicar titulares. La ejecución exige ese archivo real; no se suplanta automáticamente con datos sintéticos.

`demo_validacion.py` lee los archivos oficiales, llama a limpieza e integración y ejecuta el módulo 3. Guarda `validos.json`, `rechazados.json` y `resumen.json` en `reports/validacion`. Las primeras dos salidas contienen evidencia bancaria y se excluyen de Git. El resumen solo contiene cifras, hashes y códigos.

## Interfaz reutilizable

```python
from validacion_cuarentena import validar_transacciones
validos, rechazados = validar_transacciones(resultado.datos.to_dict(orient="records"))
```

También acepta `catalogo_cuentas=set_de_ids` para volver a comprobar pertenencia. Sin él se exige `cuenta_en_catalogo=True`, proporcionado por integración; un valor desconocido no se confunde con una cuenta inexistente.

La función devuelve listas de diccionarios JSON compatibles y no modifica las entradas. Usa un `BaseModel`, validadores de campo y captura de `ValidationError` por registro. Un dato inválido no interrumpe los siguientes. Los importes normalizados son cadenas decimales y las fechas conservan su zona horaria.

Cada rechazado incluye `motivo_rechazo_cuarentena`, `errores_validacion` (campo, código, mensaje), `registro_original`, `registro_integrado`, identidad y posición en el lote. Los errores usan mensajes controlados sin valores de entrada. Los números no finitos en auditoría se representan mediante etiquetas JSON para conservar su señal.

## Evidencia e integración con compañeros

El validador conserva la evidencia que recibe. Para conservar también el crudo anterior a limpieza se debe usar el recorrido de `ejecutar_validacion()` en `demo_validacion.py`: captura las fuentes aparte, comprueba orden, identidad y cobertura, y adjunta los originales después de integrar. Así no añade metadatos a la comparación de duplicados ni altera las estadísticas de limpieza.

`monto_entrada_integracion` conserva el importe procesado; `monto_original` recupera `ind_monto_original`. El token exacto permanece en `registro_original`. El archivo y su SHA-256, fuente y posición identifican cada fila física. El SHA-256 depende de los bytes, incluidos finales de línea.

El integrante 4 debe almacenar importes como centavos enteros o texto decimal exacto, las fechas como ISO y la auditoría/errores como JSON. Debe usar identidad de archivo/fuente/fila para cuarentena; el ID de negocio original puede repetirse. La idempotencia, commit/rollback y SQLite pertenecen a su módulo. No se restaura la carpeta SQLite eliminada por el equipo.

`verificar_trazabilidad()` de integración se corrigió para verificar cobertura e identidad; no certifica importes ni captura por sí mismo originales crudos.

## Resultado con los archivos reales

Ejecución: 620 ATM + 600 digitales = **1,220 filas**. Resultado: **1,053 válidos y 167 rechazados**. ATM: 540/80; digital: 513/87. Causas por campo: 61 cuentas huérfanas, 46 CLABEs inválidas, 20 copias exactas, 27 montos imputados y 19 recortados. Las causas se superponen.

Los casos de prueba sintéticos están etiquetados como tales. El test del flujo genera un catálogo de prueba en una carpeta temporal; el reporte de la demostración se produjo con el catálogo oficial.

## Ejemplos para explicar el módulo

Un registro ATM con fecha ISO válida, cuenta catalogada, importe positivo y comisión cero explícita es aceptado. Una consulta de saldo con importe cero también es aceptada. Una transferencia con CLABE corta se rechaza con `CLABE_INVALIDA`; la siguiente operación sigue procesándose. Ver pruebas para ejemplos sintéticos completos.

Para la defensa: el rechazo se basa en calidad del registro y reglas documentadas, no en que la operación haya sido fallida. No se comprueba checksum, existencia ni titularidad de la CLABE. Un registro con formato correcto todavía puede requerir comprobaciones bancarias externas.

## Límites

Esta entrega implementa el integrante 3 y una demostración de conexión con los módulos reales 1 y 2. No ejecuta la persistencia del integrante 4. Las políticas académicas para importes transformados y negativos se documentan en REGLAS_DE_NEGOCIO.md y deben revisarse cuando el equipo incorpore reversos u otras operaciones. No se infiere el mecanismo MCAR/MAR ni se vuelven a limpiar los importes desde validación.
