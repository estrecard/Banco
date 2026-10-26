"""
Pruebas de integración y de estrés del pipeline completo (main.py + base_datos.py).

Usan los módulos de REFERENCIA (forzar_referencia=True) para que los resultados esperados no
dependan de los módulos reales de los compañeros. Los datos son SINTÉTICOS.
"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import base_datos as bd  # noqa: E402
import datos_sinteticos as ds  # noqa: E402
import main as pipeline  # noqa: E402


class PipelineBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "banco_etl.db"
        self.reportes = self.dir / "reports"
        self.mod = pipeline.cargar_modulos(forzar_referencia=True)

    def tearDown(self):
        self._tmp.cleanup()

    def correr(self, atm, dig, catalogo=None, **kw):
        kw.setdefault("modulos", self.mod)
        return pipeline.ejecutar_pipeline(
            ruta_atm=atm, ruta_digital=dig, ruta_catalogo=catalogo,
            ruta_db=self.db, dir_reportes=self.reportes, **kw)

    def correr_dataset(self, info, **kw):
        kw.setdefault("catalogo_es_sintetico", True)
        return self.correr(info["ruta_atm"], info["ruta_digital"], info["ruta_catalogo"], **kw)

    def sql(self, consulta, params=()):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(consulta, params).fetchall()
        finally:
            conn.close()

    def n(self, tabla):
        return self.sql(f"SELECT COUNT(*) FROM {tabla}")[0][0]

    def escribir(self, nombre, contenido, binario=False):
        p = self.dir / nombre
        p.write_bytes(contenido) if binario else p.write_text(contenido, encoding="utf-8")
        return p

    def csv_atm(self, *filas):
        enc = "id_atm,fecha_hora,numero_cuenta,tipo_operacion,monto,moneda,ubicacion_atm,estado\n"
        return self.escribir("atm.csv", enc + "".join(f + "\n" for f in filas))

    def json_dig(self, obj):
        return self.escribir("dig.json", json.dumps(obj))


class TestEndToEnd(PipelineBase):
    def test_flujo_completo_conciliado(self):
        info = ds.crear_dataset(self.dir / "d", n_atm=200, n_digital=200)
        r = self.correr_dataset(info)
        self.assertEqual(r["leidos"], info["total_filas_atm"] + info["total_elementos_digital"])
        self.assertEqual(r["leidos"], r["validos"] + r["rechazados"])
        self.assertEqual(r["almacenados_limpias"], r["validos"])
        self.assertEqual(r["almacenados_cuarentena"], r["rechazados"])
        self.assertEqual(self.n("transacciones_limpias"), r["validos"])
        self.assertEqual(self.n("cuarentena_errores"), r["rechazados"])
        self.assertTrue(all(Path(p).is_file() for p in r["archivos_reporte"]))
        self.assertEqual(r["estado"], "COMPLETADA")

    def test_cada_defecto_inyectado_termino_en_cuarentena(self):
        info = ds.crear_dataset(self.dir / "d", n_atm=300, n_digital=300)
        self.correr_dataset(info)
        motivos = [m for (m,) in self.sql("SELECT motivo_rechazo_cuarentena FROM cuarentena_errores")]
        for fragmento, esperado in info["esperados"].items():
            encontrados = sum(1 for m in motivos if fragmento in m)
            self.assertEqual(encontrados, esperado, f"motivo {fragmento!r}")

    def test_ningun_id_queda_en_ambas_tablas_por_error(self):
        info = ds.crear_dataset(self.dir / "d")
        self.correr_dataset(info)
        malos = self.sql("""SELECT COUNT(*) FROM cuarentena_errores q JOIN transacciones_limpias t
                            ON q.fuente_origen = t.fuente_origen AND q.id_origen = t.id_origen
                            WHERE q.motivo_rechazo_cuarentena NOT LIKE '%duplicado%'""")
        self.assertEqual(malos[0][0], 0)

    def test_reejecucion_es_idempotente(self):
        info = ds.crear_dataset(self.dir / "d")
        r1 = self.correr_dataset(info)
        limpias, cuar = self.n("transacciones_limpias"), self.n("cuarentena_errores")
        r2 = self.correr_dataset(info)
        self.assertEqual(self.n("transacciones_limpias"), limpias)
        self.assertEqual(self.n("cuarentena_errores"), cuar)
        self.assertEqual(r2["almacenados_limpias"], 0)
        self.assertEqual(r2["duplicados_omitidos"], r1["almacenados_limpias"])
        self.assertEqual(r2["almacenados_cuarentena"], 0)
        self.assertEqual(r2["duplicados_cuarentena_omitidos"], r1["almacenados_cuarentena"])
        self.assertEqual(self.n("corridas_pipeline"), 2)

    def test_trazabilidad_id_origen_y_corrida(self):
        info = ds.crear_dataset(self.dir / "d", con_defectos=False, n_atm=20, n_digital=20)
        r = self.correr_dataset(info)
        filas = self.sql("SELECT id_transaccion, fuente_origen, id_origen, id_corrida FROM transacciones_limpias")
        self.assertEqual(len(filas), 40)
        for id_t, fuente, id_o, corrida in filas:
            self.assertEqual(id_t, f"{fuente}:{id_o}")
            self.assertEqual(corrida, r["id_corrida"])

    def test_montos_se_guardan_exactos_en_centavos(self):
        atm = self.csv_atm('A1,2026-03-01 10:00:00,1000000001,RETIRO,"$1,234.56",MXN,SUC-01,EXITOSA',
                           "A2,2026-03-01 10:00:00,1000000001,RETIRO,0.10,MXN,SUC-01,EXITOSA")
        r = self.correr(atm, self.json_dig([]))
        self.assertEqual((r["validos"], r["rechazados"]), (2, 0))
        centavos = dict(self.sql("SELECT id_origen, monto_centavos FROM transacciones_limpias"))
        self.assertEqual(centavos, {"A1": 123456, "A2": 10})

    def test_reporte_json_y_md(self):
        info = ds.crear_dataset(self.dir / "d")
        r = self.correr_dataset(info)
        js = json.loads(Path(r["archivos_reporte"][0]).read_text(encoding="utf-8"))
        self.assertEqual(js["leidos"], r["leidos"])
        md = Path(r["archivos_reporte"][1]).read_text(encoding="utf-8")
        self.assertIn("Registros leídos", md)
        self.assertIn("SINTETICO", md)


class TestCatalogo(PipelineBase):
    FILA_OK = "A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK"
    FILA_CUENTA_RARA = "A2,2026-03-01 10:00:00,7777777777,RETIRO,10.00,MXN,S,OK"

    def test_sin_catalogo_informa_limitacion_y_no_valida_existencia(self):
        atm = self.csv_atm(self.FILA_OK, self.FILA_CUENTA_RARA)
        r = self.correr(atm, self.json_dig([]), self.dir / "no_existe.csv")
        self.assertEqual(r["catalogo"]["origen"], "NO_DISPONIBLE")
        self.assertTrue(any("LIMITACIÓN" in a for a in r["advertencias"]))
        self.assertEqual(r["validos"], 2)   # no se inventa un catálogo

    def test_catalogo_sintetico_marcado_como_tal(self):
        atm = self.csv_atm(self.FILA_OK, self.FILA_CUENTA_RARA)
        r = self.correr(atm, self.json_dig([]), self.dir / "no_existe.csv", usar_catalogo_sintetico=True)
        self.assertEqual(r["catalogo"]["origen"], "SINTETICO")
        self.assertTrue(any("SINTÉTICO" in a for a in r["advertencias"]))
        self.assertEqual((r["validos"], r["rechazados"]), (1, 1))
        corrida = self.sql("SELECT origen_catalogo FROM corridas_pipeline")[0][0]
        self.assertEqual(corrida, "SINTETICO")

    def test_catalogo_original_en_archivo(self):
        cat = self.escribir("catalogo_cuentas.csv", "cuenta\n1000000001\n")
        atm = self.csv_atm(self.FILA_OK, self.FILA_CUENTA_RARA)
        r = self.correr(atm, self.json_dig([]), cat)
        self.assertEqual(r["catalogo"]["origen"], "ARCHIVO")
        self.assertEqual((r["validos"], r["rechazados"]), (1, 1))
        self.assertIn("cuenta inexistente en catálogo", r["motivos_rechazo"])

    def test_catalogo_defectuoso_se_trata_como_no_disponible(self):
        cat = self.escribir("catalogo_cuentas.csv", "cuenta\n")
        r = self.correr(self.csv_atm(self.FILA_OK), self.json_dig([]), cat)
        self.assertEqual(r["catalogo"]["origen"], "NO_DISPONIBLE")
        self.assertTrue(any("Catálogo defectuoso" in a for a in r["advertencias"]))


class TestArchivosDefectuosos(PipelineBase):
    OK = "A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK"

    def test_atm_inexistente_continua_con_digital_y_queda_parcial(self):
        dig = self.json_dig([{"id_transferencia": "T1", "timestamp": "2026-03-01T10:00:00",
                              "cuenta_origen": "1000000001", "cuenta_destino": "1000000002",
                              "monto": 5, "moneda": "MXN"}])
        r = self.correr(self.dir / "no_hay.csv", dig)
        self.assertEqual(r["estado"], "COMPLETADA_PARCIAL")
        self.assertEqual(r["fuentes"]["ATM"]["estado"], "OMITIDA")
        self.assertEqual(r["validos"], 1)

    def test_modo_estricto_aborta(self):
        with self.assertRaises(pipeline.PipelineError):
            self.correr(self.dir / "no_hay.csv", self.json_dig([]), estricto=True)

    def test_sin_ninguna_fuente_es_error_y_no_crea_base(self):
        with self.assertRaises(pipeline.PipelineError):
            self.correr(self.dir / "a.csv", self.dir / "b.json")
        self.assertFalse(self.db.exists())

    def test_json_truncado(self):
        dig = self.escribir("dig.json", '[{"id_transferencia": "T1", "monto": 5')
        r = self.correr(self.csv_atm(self.OK), dig)
        self.assertEqual(r["fuentes"]["DIGITAL"]["estado"], "OMITIDA")
        self.assertIn("JSON inválido", r["fuentes"]["DIGITAL"]["error"])
        self.assertEqual(r["validos"], 1)

    def test_archivos_vacios(self):
        r = self.correr(self.escribir("a.csv", ""), self.json_dig([{"id_transferencia": "T1"}]))
        self.assertEqual(r["fuentes"]["ATM"]["estado"], "OMITIDA")
        self.assertEqual(r["rechazados"], 1)

    def test_archivo_binario(self):
        atm = self.escribir("a.csv", b"\x00\x01\x02PK\x03\x04", binario=True)
        r = self.correr(atm, self.json_dig([{"id_transferencia": "T1"}]))
        self.assertEqual(r["fuentes"]["ATM"]["estado"], "OMITIDA")
        self.assertIn("nulos", r["fuentes"]["ATM"]["error"])

    def test_json_con_estructura_inesperada(self):
        for contenido in ('"solo texto"', "42", '{"a": 1}', '{"a": [], "b": []}'):
            with self.subTest(contenido=contenido):
                dig = self.escribir("dig.json", contenido)
                r = self.correr(self.csv_atm(self.OK), dig)
                self.assertEqual(r["fuentes"]["DIGITAL"]["estado"], "OMITIDA")

    def test_json_envuelto_en_objeto(self):
        dig = self.escribir("dig.json", json.dumps({"transferencias": [{"id_transferencia": "T1"}]}))
        r = self.correr(self.csv_atm(self.OK), dig)
        self.assertEqual(r["fuentes"]["DIGITAL"]["leidos"], 1)

    def test_filas_csv_malformadas_van_a_cuarentena(self):
        atm = self.csv_atm(self.OK, "A2,solo,tres", "A3,2026-03-01 10:00:00,1000000001,RETIRO,1,MXN,S,OK,EXTRA")
        r = self.correr(atm, self.json_dig([]))
        self.assertEqual((r["leidos"], r["validos"], r["rechazados"]), (3, 1, 2))
        self.assertEqual(r["motivos_rechazo"]["fila_malformada"], 2)

    def test_elementos_json_no_objeto(self):
        r = self.correr(self.csv_atm(self.OK), self.json_dig([None, "x", 5, [1]]))
        self.assertEqual(r["rechazados"], 4)

    def test_csv_en_latin1(self):
        atm = self.escribir("a.csv", ("id_atm,fecha_hora,numero_cuenta,tipo_operacion,monto,moneda,ubicacion_atm,estado\n"
                                      "A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,Sucursal Ñandú,OK\n").encode("latin-1"),
                            binario=True)
        r = self.correr(atm, self.json_dig([]))
        self.assertEqual(r["validos"], 1)
        self.assertTrue(any("latin-1" in a for a in r["advertencias"]))

    def test_csv_con_bom_y_lineas_en_blanco(self):
        txt = "﻿id_atm,fecha_hora,numero_cuenta,tipo_operacion,monto,moneda,ubicacion_atm,estado\n\n" + self.OK + "\n\n"
        r = self.correr(self.escribir("a.csv", txt), self.json_dig([]))
        self.assertEqual((r["leidos"], r["validos"]), (1, 1))


class TestDatosSucios(PipelineBase):
    """Estrés cualitativo: nulos, anomalías, tipos raros."""

    def test_nulos_y_valores_extremos(self):
        filas = [
            "A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK",   # válida
            "A2,,1000000001,RETIRO,10.00,MXN,S,OK",                       # fecha nula
            "A3,2026-03-01 10:00:00,NULL,RETIRO,10.00,MXN,S,OK",          # cuenta 'NULL'
            "A4,2026-03-01 10:00:00,1000000001,RETIRO,NaN,MXN,S,OK",      # monto NaN
            "A5,2026-03-01 10:00:00,1000000001,RETIRO,inf,MXN,S,OK",      # monto inf
            "A6,2026-03-01 10:00:00,1000000001,RETIRO,1e308,MXN,S,OK",    # monto enorme
            "A7,2026-03-01 10:00:00,1000000001,RETIRO,10.001,MXN,S,OK",   # 3 decimales
            "A8,2026-03-01 10:00:00,1000000001,RETIRO,10.00,,S,OK",       # moneda nula
            "N/A,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK",   # id 'N/A'
            "A10,2026-03-01 10:00:00,'; DROP TABLE transacciones_limpias;--,RETIRO,10.00,MXN,S,OK",
        ]
        r = self.correr(self.csv_atm(*filas), self.json_dig([]))
        self.assertEqual((r["leidos"], r["validos"], r["rechazados"]), (10, 1, 9))
        self.assertEqual(self.n("transacciones_limpias"), 1)   # la tabla sigue viva
        self.assertEqual(self.n("cuarentena_errores"), 9)

    def test_ids_duplicados_en_el_lote_y_entre_fuentes_distintas(self):
        atm = self.csv_atm("X1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK",
                           "X1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK")
        dig = self.json_dig([{"id_transferencia": "X1", "timestamp": "2026-03-01T10:00:00",
                              "cuenta_origen": "1000000001", "cuenta_destino": "1000000002",
                              "monto": 5, "moneda": "MXN"}])
        r = self.correr(atm, dig)
        # X1 de ATM (1 válido + 1 duplicado) y X1 de DIGITAL (válido: otra fuente, otro id_transaccion)
        self.assertEqual((r["validos"], r["rechazados"]), (2, 1))

    def test_unicode_y_campos_enormes(self):
        atm = self.csv_atm("A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN," + "Ñ" * 100000 + ",OK",
                           "😀,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK")
        r = self.correr(atm, self.json_dig([]))
        self.assertEqual(r["leidos"], 2)
        self.assertEqual(r["leidos"], r["validos"] + r["rechazados"])


class TestResilienciaDeModulos(PipelineBase):
    def _mods(self, **reemplazos):
        base = dict(limpieza=self.mod.limpieza, integracion=self.mod.integracion,
                    validacion=self.mod.validacion, origen=dict(self.mod.origen))
        base.update(reemplazos)
        return SimpleNamespace(**base)

    def test_limpieza_que_explota_con_un_registro_se_aisla(self):
        original = self.mod.limpieza.limpiar_registros_atm

        def limpiar_con_bug(registros):
            if any(r.get("id_atm") == "BOOM" for r in registros):
                raise RuntimeError("bug del integrante 1")
            return original(registros)

        limp = SimpleNamespace(limpiar_registros_atm=limpiar_con_bug,
                               limpiar_registros_digitales=self.mod.limpieza.limpiar_registros_digitales)
        atm = self.csv_atm("A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK",
                           "BOOM,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK",
                           "A3,2026-03-01 11:00:00,1000000001,RETIRO,10.00,MXN,S,OK")
        r = self.correr(atm, self.json_dig([]), modulos=self._mods(limpieza=limp))
        self.assertEqual((r["leidos"], r["validos"], r["rechazados"]), (3, 2, 1))
        self.assertIn("excepción en limpieza", r["motivos_rechazo"])

    def test_modulo_que_pierde_registros_se_detecta_y_no_guarda_nada(self):
        def validar_perdiendo(registros, catalogo=None):
            return registros[:-1], []            # pierde el último en silencio

        val = SimpleNamespace(validar_transacciones=validar_perdiendo)
        atm = self.csv_atm("A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK",
                           "A2,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK")
        with self.assertRaises(pipeline.ConciliacionError):
            self.correr(atm, self.json_dig([]), modulos=self._mods(validacion=val))
        self.assertFalse(self.db.exists())

    def test_validador_que_lanza_excepcion_es_error_fatal_sin_guardar(self):
        val = SimpleNamespace(validar_transacciones=lambda r, c=None: 1 / 0)
        with self.assertRaises(pipeline.PipelineError):
            self.correr(self.csv_atm("A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK"),
                        self.json_dig([]), modulos=self._mods(validacion=val))
        self.assertFalse(self.db.exists())

    def test_validador_que_aprueba_algo_no_persistible_lo_manda_a_cuarentena(self):
        """Segunda línea de defensa: el validador (con bug) aprueba un monto con 3 decimales."""
        def validar_todo(registros, catalogo=None):
            return list(registros), []

        val = SimpleNamespace(validar_transacciones=validar_todo)
        atm = self.csv_atm("A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK",
                           "A2,2026-03-01 10:00:00,1000000001,RETIRO,10.001,MXN,S,OK")
        r = self.correr(atm, self.json_dig([]), modulos=self._mods(validacion=val))
        self.assertEqual((r["validos"], r["rechazados"]), (1, 1))
        self.assertTrue(any(m.startswith("no persistible") for m in r["motivos_rechazo"]))

    def test_rechazo_sin_motivo_recibe_motivo_por_defecto(self):
        def validar(registros, catalogo=None):
            return [], [{"fuente_origen": "ATM", "id_origen": r["id_origen"], "motivo_rechazo_cuarentena": ""}
                        for r in registros]

        val = SimpleNamespace(validar_transacciones=validar)
        r = self.correr(self.csv_atm("A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK"),
                        self.json_dig([]), modulos=self._mods(validacion=val))
        motivo = self.sql("SELECT motivo_rechazo_cuarentena FROM cuarentena_errores")[0][0]
        self.assertTrue(motivo.strip())

    def test_modulo_sin_la_funcion_del_contrato(self):
        with mock.patch("importlib.import_module", return_value=SimpleNamespace()):
            with self.assertRaises(pipeline.ContratoIncumplidoError):
                pipeline.cargar_modulos()

    def test_modulo_que_devuelve_formato_incorrecto(self):
        val = SimpleNamespace(validar_transacciones=lambda r, c=None: ("no", "es lista"))
        with self.assertRaises(pipeline.ContratoIncumplidoError):
            self.correr(self.csv_atm("A1,2026-03-01 10:00:00,1000000001,RETIRO,10.00,MXN,S,OK"),
                        self.json_dig([]), modulos=self._mods(validacion=val))

    def test_fallo_de_persistencia_hace_rollback_y_marca_corrida_fallida(self):
        info = ds.crear_dataset(self.dir / "d", n_atm=50, n_digital=50)
        with mock.patch.object(bd, "_insertar_cuarentena", side_effect=sqlite3.OperationalError("disco lleno")):
            with self.assertRaises(pipeline.PipelineError):
                self.correr_dataset(info)
        self.assertEqual(self.n("transacciones_limpias"), 0)   # los válidos NO quedaron a medias
        self.assertEqual(self.n("cuarentena_errores"), 0)
        estado, detalle = self.sql("SELECT estado, detalle_error FROM corridas_pipeline")[0]
        self.assertEqual(estado, "FALLIDA")
        self.assertIn("disco lleno", detalle)
        # y una reejecución posterior (sin el fallo) funciona con normalidad
        r = self.correr_dataset(info)
        self.assertEqual(self.n("transacciones_limpias"), r["validos"])


class TestEstres(PipelineBase):
    def test_diez_mil_registros_con_defectos(self):
        info = ds.crear_dataset(self.dir / "d", n_atm=5000, n_digital=5000, cada=10)
        t0 = time.perf_counter()
        r = self.correr_dataset(info)
        duracion = time.perf_counter() - t0
        self.assertEqual(r["leidos"], r["validos"] + r["rechazados"])
        self.assertEqual(self.n("transacciones_limpias"), r["validos"])
        self.assertEqual(self.n("cuarentena_errores"), r["rechazados"])
        self.assertGreater(r["rechazados"], 1000)
        self.assertLess(duracion, 60, f"tardó {duracion:.1f}s")
        # idempotencia también a escala
        r2 = self.correr_dataset(info)
        self.assertEqual(r2["almacenados_limpias"] + r2["almacenados_cuarentena"], 0)


class TestCLI(PipelineBase):
    def ejecutar(self, *args):
        return subprocess.run([sys.executable, str(RAIZ / "main.py"), *args],
                              capture_output=True, text=True, timeout=120, cwd=self.dir)

    def test_demo_codigo_0(self):
        p = self.ejecutar("--demo", "--db", str(self.db), "--reportes", str(self.reportes))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Registros leídos", p.stdout)
        self.assertTrue(self.db.is_file())

    def test_sin_archivos_codigo_2(self):
        p = self.ejecutar("--atm", "x.csv", "--digital", "y.json", "--db", str(self.db),
                          "--reportes", str(self.reportes))
        self.assertEqual(p.returncode, 2)
        self.assertIn("PIPELINE ABORTADO", p.stderr)

    def test_fuente_omitida_codigo_1(self):
        info = ds.crear_dataset(self.dir / "d")
        p = self.ejecutar("--atm", str(info["ruta_atm"]), "--digital", "no_existe.json",
                          "--catalogo", str(info["ruta_catalogo"]), "--db", str(self.db),
                          "--reportes", str(self.reportes))
        self.assertEqual(p.returncode, 1, p.stderr)


class TestContratoModulosReales(unittest.TestCase):
    """Se activa sola cuando los integrantes 1-3 colocan sus módulos en la raíz del proyecto."""

    @unittest.skipUnless(all((RAIZ / f"{m}.py").is_file()
                             for m in ("limpieza_datos", "integracion_datos", "validacion_cuarentena")),
                         "módulos reales no presentes en la raíz (se usan los de referencia)")
    def test_modulos_reales_cumplen_el_contrato_y_conciliacion(self):
        mods = pipeline.cargar_modulos()
        self.assertEqual(set(mods.origen.values()), {"real"})
        with tempfile.TemporaryDirectory() as tmp:
            info = ds.crear_dataset(Path(tmp) / "d", n_atm=100, n_digital=100)
            r = pipeline.ejecutar_pipeline(
                ruta_atm=info["ruta_atm"], ruta_digital=info["ruta_digital"],
                ruta_catalogo=info["ruta_catalogo"], catalogo_es_sintetico=True,
                ruta_db=Path(tmp) / "t.db", dir_reportes=Path(tmp) / "rep", modulos=mods)
            self.assertEqual(r["leidos"], r["validos"] + r["rechazados"])


if __name__ == "__main__":
    unittest.main()
