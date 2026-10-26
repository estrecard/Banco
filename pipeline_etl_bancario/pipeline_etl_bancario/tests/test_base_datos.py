"""Pruebas unitarias de base_datos.py (esquema, restricciones, rollback, idempotencia)."""
import sqlite3
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import base_datos as bd  # noqa: E402


def valido(i=1, **cambios):
    reg = {
        "fuente_origen": "ATM", "id_origen": f"ATM-{i:04d}", "fecha_hora": "2026-03-01 10:00:00",
        "cuenta_origen": "1000000001", "cuenta_destino": None, "monto": Decimal("100.50"),
        "moneda": "MXN", "tipo_transaccion": "RETIRO", "estado": "EXITOSA",
    }
    reg.update(cambios)
    return reg


def rechazo(i=1, motivo="monto no positivo", **cambios):
    r = {"fuente_origen": "ATM", "id_origen": f"ATM-R{i}", "motivo_rechazo_cuarentena": motivo,
         "registro_original": {"id_atm": f"ATM-R{i}", "monto": "-1"}}
    r.update(cambios)
    return r


class BaseDatosTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = bd.conectar(":memory:")
        bd.inicializar_db(self.conn)
        self.corrida = bd.iniciar_corrida(self.conn, "PRUEBA")

    def tearDown(self):
        self.conn.close()

    def n(self, tabla):
        return bd.contar_filas(self.conn, tabla)


class TestEsquema(BaseDatosTestCase):
    def test_tablas_obligatorias_existen(self):
        tablas = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"transacciones_limpias", "cuarentena_errores", "corridas_pipeline"} <= tablas)

    def test_columna_motivo_en_cuarentena(self):
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(cuarentena_errores)")]
        self.assertIn("motivo_rechazo_cuarentena", cols)
        self.assertIn("registro_original", cols)

    def test_claves_primarias(self):
        pk = [r[1] for r in self.conn.execute("PRAGMA table_info(transacciones_limpias)") if r[5]]
        self.assertEqual(pk, ["id_transaccion"])
        pk = [r[1] for r in self.conn.execute("PRAGMA table_info(cuarentena_errores)") if r[5]]
        self.assertEqual(pk, ["id_cuarentena"])

    def test_claves_foraneas_activas(self):
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_inicializar_es_idempotente(self):
        bd.guardar_lote(self.conn, self.corrida, [valido()], [])
        bd.inicializar_db(self.conn)  # segunda vez no debe borrar ni fallar
        self.assertEqual(self.n("transacciones_limpias"), 1)


class TestRestriccionesCheck(BaseDatosTestCase):
    """La BD rechaza datos inválidos aunque se salten las validaciones de Python."""

    SQL = """INSERT INTO transacciones_limpias (id_transaccion, fuente_origen, id_origen, fecha_hora,
             cuenta_origen, monto_centavos, moneda, tipo_transaccion, hash_contenido, id_corrida, registrado_en)
             VALUES (?, ?, ?, '2026-01-01 00:00:00', '1000000001', ?, ?, 'RETIRO', 'h', ?, 'x')"""

    def _insertar(self, id_t="ATM:1", fuente="ATM", id_o="1", centavos=100, moneda="MXN", corrida=None):
        self.conn.execute(self.SQL, (id_t, fuente, id_o, centavos, moneda, corrida or self.corrida))

    def test_fila_correcta_entra(self):
        self._insertar()
        self.assertEqual(self.n("transacciones_limpias"), 1)

    def test_monto_cero_o_negativo(self):
        for c in (0, -5):
            with self.assertRaises(sqlite3.IntegrityError):
                self._insertar(centavos=c)

    def test_fuente_desconocida(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insertar(id_t="XYZ:1", fuente="XYZ")

    def test_id_transaccion_debe_ser_consistente(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insertar(id_t="ATM:OTRO", id_o="1")

    def test_moneda_longitud(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insertar(moneda="PESOS")

    def test_clave_foranea_corrida_inexistente(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insertar(corrida=9999)

    def test_clave_primaria_duplicada(self):
        self._insertar()
        with self.assertRaises(sqlite3.IntegrityError):
            self._insertar()

    def test_cuarentena_exige_motivo(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO cuarentena_errores (hash_registro, fuente_origen, motivo_rechazo_cuarentena,"
                " registro_original, id_corrida, registrado_en) VALUES ('h','ATM','   ','{}',?, 'x')",
                (self.corrida,))


class TestConversiones(unittest.TestCase):
    def test_a_centavos_validos(self):
        self.assertEqual(bd.a_centavos("10.5"), 1050)
        self.assertEqual(bd.a_centavos(Decimal("0.01")), 1)
        self.assertEqual(bd.a_centavos(100), 10000)
        self.assertEqual(bd.a_centavos(0.1), 10)

    def test_a_centavos_invalidos(self):
        for malo in (None, True, "abc", "", float("nan"), float("inf"), Decimal("1.005"), "1.234"):
            with self.subTest(malo=malo), self.assertRaises(bd.RegistroInvalidoError):
                bd.a_centavos(malo)

    def test_hash_cuarentena_independiente_del_orden_de_claves(self):
        a = bd.hash_registro_original("ATM", {"a": 1, "b": 2})
        b = bd.hash_registro_original("ATM", {"b": 2, "a": 1})
        self.assertEqual(a, b)
        self.assertNotEqual(a, bd.hash_registro_original("DIGITAL", {"a": 1, "b": 2}))

    def test_normalizar_rechaza_registros_no_persistibles(self):
        for cambio in ({"monto": Decimal("-1")}, {"monto": None}, {"fuente_origen": "X"},
                       {"id_origen": ""}, {"cuenta_origen": None}, {"moneda": "PESOS"},
                       {"fecha_hora": "hoy"}, {"monto": Decimal("1.001")}):
            with self.subTest(cambio=cambio), self.assertRaises(bd.RegistroInvalidoError):
                bd.normalizar_fila_limpia(valido(**cambio))

    def test_normalizar_construye_id_determinista(self):
        fila = bd.normalizar_fila_limpia(valido(3))
        self.assertEqual(fila["id_transaccion"], "ATM:ATM-0003")
        self.assertEqual(fila["monto_centavos"], 10050)


class TestGuardadoYTransacciones(BaseDatosTestCase):
    def test_guardado_basico_y_trazabilidad(self):
        r = bd.guardar_lote(self.conn, self.corrida, [valido(1), valido(2)], [rechazo(1)])
        self.assertEqual((r["insertados_limpias"], r["insertados_cuarentena"]), (2, 1))
        fila = self.conn.execute("SELECT * FROM transacciones_limpias WHERE id_origen='ATM-0001'").fetchone()
        self.assertEqual(fila["id_corrida"], self.corrida)
        self.assertEqual(fila["fuente_origen"], "ATM")
        q = self.conn.execute("SELECT * FROM cuarentena_errores").fetchone()
        self.assertEqual(q["motivo_rechazo_cuarentena"], "monto no positivo")
        self.assertIn("ATM-R1", q["registro_original"])

    def test_idempotencia_reejecutar_no_duplica(self):
        lote = ([valido(1), valido(2)], [rechazo(1)])
        bd.guardar_lote(self.conn, self.corrida, *lote)
        r2 = bd.guardar_lote(self.conn, self.corrida, *lote)
        self.assertEqual((r2["insertados_limpias"], r2["duplicados_limpias"]), (0, 2))
        self.assertEqual((r2["insertados_cuarentena"], r2["duplicados_cuarentena"]), (0, 1))
        self.assertEqual(self.n("transacciones_limpias"), 2)
        self.assertEqual(self.n("cuarentena_errores"), 1)

    def test_conflicto_conserva_original(self):
        bd.guardar_lote(self.conn, self.corrida, [valido(1, monto=Decimal("10.00"))], [])
        r = bd.guardar_lote(self.conn, self.corrida, [valido(1, monto=Decimal("99.00"))], [])
        self.assertEqual((r["duplicados_limpias"], r["conflictos_limpias"]), (1, 1))
        centavos = self.conn.execute("SELECT monto_centavos FROM transacciones_limpias").fetchone()[0]
        self.assertEqual(centavos, 1000)

    def test_rollback_total_si_un_registro_falla(self):
        lote = [valido(1), valido(2), valido(3, monto=Decimal("-5")), valido(4)]
        with self.assertRaises(bd.ErrorPersistencia):
            bd.guardar_lote(self.conn, self.corrida, lote, [rechazo(1)])
        self.assertEqual(self.n("transacciones_limpias"), 0)   # ni siquiera las 2 primeras
        self.assertEqual(self.n("cuarentena_errores"), 0)
        self.assertFalse(self.conn.in_transaction)

    def test_rollback_si_falla_cuarentena_revierte_tambien_los_validos(self):
        with self.assertRaises(bd.ErrorPersistencia):
            bd.guardar_lote(self.conn, self.corrida, [valido(1), valido(2)],
                            [rechazo(1), rechazo(2, motivo="   ")])
        self.assertEqual(self.n("transacciones_limpias"), 0)
        self.assertEqual(self.n("cuarentena_errores"), 0)

    def test_fallo_no_afecta_lo_ya_confirmado(self):
        bd.guardar_lote(self.conn, self.corrida, [valido(1)], [rechazo(1)])
        with self.assertRaises(bd.ErrorPersistencia):
            bd.guardar_lote(self.conn, self.corrida, [valido(2), valido(3, cuenta_origen=None)], [])
        self.assertEqual(self.n("transacciones_limpias"), 1)
        self.assertEqual(self.n("cuarentena_errores"), 1)

    def test_fk_corrida_inexistente_revierte(self):
        with self.assertRaises(bd.ErrorPersistencia):
            bd.guardar_lote(self.conn, 12345, [valido(1)], [])
        self.assertEqual(self.n("transacciones_limpias"), 0)

    def test_context_manager_transaccion(self):
        with self.assertRaises(RuntimeError):
            with bd.transaccion(self.conn):
                self.conn.execute("INSERT INTO corridas_pipeline (iniciada_en) VALUES ('x')")
                raise RuntimeError("falla simulada")
        self.assertEqual(self.n("corridas_pipeline"), 1)  # solo la corrida de setUp
        with bd.transaccion(self.conn):
            self.conn.execute("INSERT INTO corridas_pipeline (iniciada_en) VALUES ('y')")
        self.assertEqual(self.n("corridas_pipeline"), 2)

    def test_cerrar_corrida(self):
        bd.cerrar_corrida(self.conn, self.corrida, "COMPLETADA", {"leidos": 5, "validos": 3, "rechazados": 2})
        f = self.conn.execute("SELECT * FROM corridas_pipeline WHERE id_corrida=?", (self.corrida,)).fetchone()
        self.assertEqual((f["estado"], f["leidos"], f["rechazados"]), ("COMPLETADA", 5, 2))
        self.assertIsNotNone(f["finalizada_en"])

    def test_contar_filas_rechaza_tablas_no_permitidas(self):
        with self.assertRaises(ValueError):
            bd.contar_filas(self.conn, "sqlite_master; DROP TABLE x")


if __name__ == "__main__":
    unittest.main()
