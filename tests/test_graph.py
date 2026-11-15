"""Pruebas del grafo (fase 5).

Usan un extractor falso y un checkpointer en memoria: no llaman a la API. El
texto del PDF sí se lee de verdad, para ejercitar el camino completo hasta la
extracción.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from config import ajustes
from core import store
from core.erp import cargar_albaranes, cargar_maestro
from core.extraction import Extraccion
from core.graph import construir_grafo
from core.ingest import listar_facturas
from core.metrics import coste_extraccion
from core.schemas import Factura

PDF = listar_facturas(ajustes.directorio_facturas)[8]  # TEC-2026-029
ALBARAN = "ALB-TEC-2026-029"
PRECIO = Decimal("50.00")


class ExtractorFalso:
    """Devuelve siempre la factura que se le indique, con un consumo fijo.

    El consumo es fingido pero constante, para poder comprobar que el grafo lo
    guarda en la base de datos.
    """

    TOKENS_ENTRADA = 1_000
    TOKENS_SALIDA = 200

    def __init__(self) -> None:
        self.factura: Factura | None = None

    def __call__(self, texto: str) -> Extraccion:
        assert self.factura is not None, "el test no ha definido la factura"
        return Extraccion(
            self.factura,
            tokens_entrada=self.TOKENS_ENTRADA,
            tokens_salida=self.TOKENS_SALIDA,
        )


def _factura(*, cif_incorrecto: bool = False, total_extra: Decimal = Decimal(0)) -> Factura:
    albaran = cargar_albaranes().buscar(ALBARAN)
    maestro = cargar_maestro()
    assert albaran is not None
    proveedor = maestro.buscar(albaran.proveedor.cif)
    assert proveedor is not None

    filas = [
        {
            "concepto": linea.concepto,
            "cantidad": linea.cantidad,
            "precio_unitario": PRECIO,
            "importe": PRECIO * linea.cantidad,
        }
        for linea in albaran.lineas
    ]
    base = sum((fila["importe"] for fila in filas), Decimal(0))
    cuota = (base * Decimal(21) / 100).quantize(Decimal("0.01"))

    return Factura.model_validate(
        {
            "numero": "TEC-2026-029",
            "fecha_expedicion": albaran.fecha + timedelta(days=1),
            "fecha_operacion": albaran.fecha,
            "referencia_albaran": albaran.numero,
            "proveedor": {
                "nombre": "Proveedor Ajeno S.L." if cif_incorrecto else albaran.proveedor.nombre,
                "cif": "B00000000" if cif_incorrecto else albaran.proveedor.cif,
            },
            "cliente": {"nombre": "Logística Ibérica S.L.", "cif": "B80000000"},
            "lineas": filas,
            "base_imponible": base,
            "cuota_iva": cuota,
            "total": base + cuota + total_extra,
            "forma_pago": proveedor.forma_pago,
            "iban": proveedor.iban,
        }
    )


@pytest.fixture
def entorno() -> tuple[Any, Any, ExtractorFalso, list[tuple[str, str, str | None]]]:
    conexion = store.conectar(":memory:")
    extractor = ExtractorFalso()
    registros: list[tuple[str, str, str | None]] = []

    def registrador(
        factura: Factura, *, estado: str, aprobado_por: str | None = None
    ) -> dict[str, Any]:
        registros.append((factura.identificador, estado, aprobado_por))
        return {}

    app = construir_grafo(
        extractor=extractor, registrador=registrador, conexion=conexion
    ).compile(checkpointer=InMemorySaver())
    return app, conexion, extractor, registros


def _config(proceso: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": proceso}}


def _arrancar(app: Any, conexion: Any, proceso: str) -> dict[str, Any]:
    store.crear_proceso(conexion, proceso, PDF.name)
    return app.invoke({"proceso": proceso, "fichero": str(PDF)}, _config(proceso))


def _estado_en_db(conexion: Any, proceso: str) -> str:
    """Estado del proceso según la base de datos.

    ``obtener_proceso`` devuelve ``None`` si el proceso no existe, así que se
    comprueba aquí: si el worker no lo hubiera dado de alta, el test falla con
    un mensaje claro en lugar de con un ``TypeError`` al indexar ``None``.
    """
    fila = store.obtener_proceso(conexion, proceso)
    assert fila is not None, f"no existe el proceso {proceso} en la base de datos"
    return str(fila["estado"])


def test_factura_correcta_se_registra(entorno) -> None:
    app, conexion, extractor, registros = entorno
    extractor.factura = _factura()

    estado = _arrancar(app, conexion, "t1")

    assert app.get_state(_config("t1")).next == ()
    assert _estado_en_db(conexion, "t1") == "registrado"
    assert registros == [(estado["factura_id"], "registrado", "automático")]


def test_guarda_tokens_y_coste_de_extraccion(entorno) -> None:
    """El consumo del modelo se anota en el proceso, no solo en el log."""
    app, conexion, extractor, _ = entorno
    extractor.factura = _factura()

    _arrancar(app, conexion, "tokens")

    proceso = store.obtener_proceso(conexion, "tokens")
    assert proceso is not None
    assert proceso["tokens_entrada"] == extractor.TOKENS_ENTRADA
    assert proceso["tokens_salida"] == extractor.TOKENS_SALIDA
    assert proceso["coste"] == pytest.approx(
        coste_extraccion(extractor.TOKENS_ENTRADA, extractor.TOKENS_SALIDA)
    )


def test_factura_con_incidencia_queda_en_revision(entorno) -> None:
    app, conexion, extractor, registros = entorno
    extractor.factura = _factura(cif_incorrecto=True)

    _arrancar(app, conexion, "t2")

    pausado = app.get_state(_config("t2"))
    assert pausado.next == ("revisar",)
    assert _estado_en_db(conexion, "t2") == "en_revision"
    assert registros == []  # no se registra nada todavía


def test_reanudar_aprobado_registra(entorno) -> None:
    app, conexion, extractor, registros = entorno
    extractor.factura = _factura(cif_incorrecto=True)
    _arrancar(app, conexion, "t3")

    app.invoke(Command(resume="aprobado"), _config("t3"))

    assert app.get_state(_config("t3")).next == ()
    assert _estado_en_db(conexion, "t3") == "registrado"
    assert registros[-1][1:] == ("registrado", "aprobado")


def test_reanudar_rechazado_marca_rechazada(entorno) -> None:
    app, conexion, extractor, registros = entorno
    extractor.factura = _factura(cif_incorrecto=True)
    _arrancar(app, conexion, "t4")

    app.invoke(Command(resume="rechazado"), _config("t4"))

    assert _estado_en_db(conexion, "t4") == "rechazado"
    assert registros[-1][1:] == ("rechazado", "rechazado")


def test_importe_alto_va_a_revision_aunque_no_haya_incidencias(entorno) -> None:
    app, conexion, extractor, _ = entorno
    extractor.factura = _factura(total_extra=Decimal("2000.00"))

    _arrancar(app, conexion, "t5")

    assert app.get_state(_config("t5")).next == ("revisar",)


def test_factura_duplicada_se_detecta(entorno) -> None:
    app, conexion, extractor, _ = entorno
    extractor.factura = _factura()

    _arrancar(app, conexion, "t6")  # queda registrada
    estado = _arrancar(app, conexion, "t7")  # misma factura, otro proceso

    codigos = [incidencia["codigo"] for incidencia in estado["resultado"]["incidencias"]]
    assert "duplicado" in codigos
    assert app.get_state(_config("t7")).next == ("revisar",)
