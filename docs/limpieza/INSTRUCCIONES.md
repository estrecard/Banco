# Instrucciones de ejecución: `limpieza_datos.py`

## Requisitos
Python 3.10+ con `pandas`, `numpy`, `matplotlib` y (opcional, para las pruebas MCAR/MAR) `scipy`.
```bash
pip install -r requirements.txt
```

## Ejecución (desde la raíz del repositorio)
```bash
python src/limpieza_datos.py --atm data/raw/transacciones_atm.csv --digitales data/raw/transferencias_digitales.json --salida reports/limpieza
```

## Archivos generados (por fuente: `atm` y `transferencias_digitales`)
| Archivo | Contenido |
|---|---|
| `<fuente>_limpio.csv` | Datos limpios + columnas de auditoría (`ind_*`, `fecha_hora_std`, ...) |
| `<fuente>_duplicados_detectados.csv` | Evidencia de las copias exactas detectadas |
| `<fuente>_auditoria_cambios.csv` | Bitácora celda a celda: valor original → valor nuevo, regla y detalle |
| `reporte_calidad_<fuente>.md` | Reporte de calidad inicial y final, MCAR/MAR, atípicos, advertencias |
| `<fuente>_grafica_nulos.png`, `_duplicados.png`, `_atipicos.png` | Gráficas antes/después |
| `<fuente>_resumen.json` | Resumen legible por máquina |

## Uso como librería
```python
from src.limpieza_datos import cargar_archivo, limpiar_datos

crudo = cargar_archivo("data/raw/transacciones_atm.csv")
df_limpio, resumen = limpiar_datos(crudo, config="atm")   # o "transferencias_digitales"

resumen["auditoria"]               # DataFrame: cambios celda a celda
resumen["duplicados_detectados"]   # DataFrame: copias exactas (evidencia)
resumen["atipicos"]                # límites IQR por segmento
df_limpio[df_limpio["ind_monto_recortado"]]   # candidatos a cuarentena
```
Parámetros opcionales: `factor_iqr=1.5`, `factor_extremo=3.0`, `umbral_imputacion=0.10`, `min_n_segmento=8`,
`aplicar_capping=True`, `excluir_duplicados=False`. Una fuente nueva se describe con un diccionario de
configuración (ver `CONFIG_ATM`). El contrato completo está en `docs/limpieza/CONTRATO_LIMPIEZA.md`.
