# Instrucciones de ejecuciÃ³n: `limpieza_datos.py`

## Requisitos
Python 3.10+ con `pandas`, `numpy`, `matplotlib` y (opcional, para las pruebas MCAR/MAR) `scipy`.

```bash
pip install pandas numpy matplotlib scipy
```

## EjecuciÃ³n por lÃ­nea de comandos
```bash
python src/limpieza_datos.py --atm data/raw/transacciones_atm.csv --digitales data/raw/transferencias_digitales.json --salida reports/limpieza
```
Sin argumentos busca los dos archivos en la carpeta actual y escribe en `reports/limpieza/`.

## Archivos generados (por fuente: `atm` y `transferencias_digitales`)
| Archivo | Contenido |
|---|---|
| `<fuente>_limpio.csv` | Datos limpios + columnas de auditorÃ­a (`flag_*`, `*_original`, `monto_estado`, ...) |
| `<fuente>_duplicados_excluidos.csv` | Evidencia de filas duplicadas retiradas |
| `<fuente>_auditoria_cambios.csv` | BitÃ¡cora celda a celda: valor original â†’ valor nuevo, regla y detalle |
| `reporte_calidad_<fuente>.md` | Reporte de calidad inicial y final, MCAR/MAR, atÃ­picos, advertencias |
| `<fuente>_grafica_nulos.png`, `_duplicados.png`, `_atipicos.png` | GrÃ¡ficas antes/despuÃ©s |
| `<fuente>_resumen.json` | Resumen legible por mÃ¡quina |

## Uso como librerÃ­a (Integrantes 2 y 3)
```python
from limpieza_datos import cargar_archivo, limpiar_datos

crudo = cargar_archivo("transacciones_atm.csv")
df_limpio, resumen = limpiar_datos(crudo, config="atm")   # o "transferencias_digitales"

resumen["auditoria"]             # DataFrame: cambios celda a celda
resumen["duplicados_excluidos"]  # DataFrame: filas retiradas (evidencia)
resumen["atipicos"]              # lÃ­mites IQR por segmento
df_limpio[df_limpio["flag_monto_winsorizado"]]   # candidatos a cuarentena
```
ParÃ¡metros opcionales: `factor_iqr=1.5`, `factor_extremo=3.0`, `umbral_imputacion=0.10`, `min_n_segmento=8`,
`aplicar_capping=True`. Una fuente nueva se describe con un diccionario de configuraciÃ³n (ver `CONFIG_ATM`).

## Contrato de salida
* Se conservan las columnas originales (limpias) y el Ã­ndice original (nÃºmero de fila de la fuente).
* Columnas aÃ±adidas: `fecha_hora_std`, `fecha_formato_origen`, `<monto>_original`, `monto_estado`, `monto_atipico`,
  `flag_monto_imputado`, `flag_monto_winsorizado`, `flag_id_repetido`, `flag_<col>_imputado`.
* El mÃ³dulo no rechaza registros: la validaciÃ³n estricta y la cuarentena son del Integrante 3.

