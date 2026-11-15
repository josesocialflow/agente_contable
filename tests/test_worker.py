"""Pruebas del modo vigilancia del worker (fase 6).

El panel no reanuda el grafo: encola un comando. Aquí se comprueba que el
worker, al pasar por ``aplicar_comandos``, lo aplica y deja la factura como
debe. Se usan un extractor y un registrador falsos: no hay llamadas a la API.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from config import ajustes
from core import store
from core.extraction import ErrorExtraccion, Extraccion
from core.graph import construir_grafo
from core.ingest import listar_facturas
from core.notify import Aviso
from core.schemas import DatosFiscales, Factura, LineaFactura
from worker import aplicar_comandos, procesar_lote

PDF = listar_facturas(ajustes.directorio_facturas)[0]

Registro = tuple[str, str | None]


def _factura(total: Decimal = Decimal("2000.00")) -> Factura:
    """Factura mínima que siempre requiere revisión: supera el umbral."""
    return Factura(
        numero="TEST-001",
        fecha_expedicion=date(2026, 11, 2),
        referencia_albaran="ALB-TEST-001",
        proveedor=DatosFiscales(nombre="Proveedor de Prueba S.L.", cif="B12345678"),
        cliente=DatosFiscales(nombre="Logística Ibérica S.A.", cif="A12345678"),
        lineas=[
            LineaFactura(
                concepto="Servicio de prueba",
                cantidad=Decimal(1),
                precio_unitario=total,
                importe=total,
            )
        ],
        base_imponible=total,
        cuota_iva=Decimal("420.00"),
        total=Decimal("2420.00"),
    )


@pytest.fixture
def entorno(
    tmp_path: Path,
) -> Iterator[tuple[Any, sqlite3.Connection, list[Registro]]]:
    """Grafo con checkpointer en memoria, base de datos temporal y registros."""
    registros: list[Registro] = []

    def registrador(
        factura: Factura, *, estado: str, aprobado_por: str | None = None
    ) -> dict[str, Any]:
        """Sustituye a ``core.erp.registrar_factura``: solo apunta la llamada."""
        registros.append((estado, aprobado_por))
        return {}

    conexion = store.conectar(tmp_path / "agente.db")
    factura = _factura()
    app = construir_grafo(
        extractor=lambda texto: Extraccion(factura),
        registrador=registrador,
        conexion=conexion,
    ).compile(checkpointer=InMemorySaver())
    try:
        yield app, conexion, registros
    finally:
        conexion.close()


def _config(proceso: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": proceso}}


def _pausar(app: Any, conexion: sqlite3.Connection, proceso: str) -> None:
    """Procesa la factura hasta dejarla esperando decisión humana."""
    store.crear_proceso(conexion, proceso, PDF.name)
    app.invoke({"proceso": proceso, "fichero": str(PDF)}, _config(proceso))


def test_comando_aprobado_registra_la_factura(entorno) -> None:
    app, conexion, registros = entorno
    _pausar(app, conexion, "p1")
    assert app.get_state(_config("p1")).next == ("revisar",)

    store.enviar_comando(conexion, "p1", "aprobado")
    atendidos = aplicar_comandos(app, conexion)

    assert atendidos == [("p1", "registrado")]
    assert registros == [("registrado", "aprobado")]
    assert app.get_state(_config("p1")).next == ()  # el grafo terminó
    assert store.comandos_pendientes(conexion) == []
    proceso = store.obtener_proceso(conexion, "p1")
    assert proceso is not None
    assert proceso["estado"] == "registrado"


def test_comando_rechazado_descarta_la_factura(entorno) -> None:
    app, conexion, registros = entorno
    _pausar(app, conexion, "p2")

    store.enviar_comando(conexion, "p2", "rechazado")
    atendidos = aplicar_comandos(app, conexion)

    assert atendidos == [("p2", "rechazado")]
    assert registros == [("rechazado", "rechazado")]
    proceso = store.obtener_proceso(conexion, "p2")
    assert proceso is not None
    assert proceso["estado"] == "rechazado"


def test_comando_sobre_proceso_ya_terminado_se_descarta(entorno) -> None:
    app, conexion, registros = entorno
    _pausar(app, conexion, "p3")
    # Se reanuda a mano, como haría `worker.py --reanudar`.
    app.invoke(Command(resume="aprobado"), _config("p3"))

    # Ahora el panel pulsa sobre algo que ya no espera a nadie.
    store.enviar_comando(conexion, "p3", "rechazado")
    atendidos = aplicar_comandos(app, conexion)

    assert atendidos == [("p3", "descartado")]
    assert registros == [("registrado", "aprobado")]  # no se ha vuelto a tocar
    assert store.comandos_pendientes(conexion) == []


def test_dos_comandos_se_aplican_en_orden(entorno) -> None:
    app, conexion, registros = entorno
    _pausar(app, conexion, "p4")

    store.enviar_comando(conexion, "p4", "aprobado")
    store.enviar_comando(conexion, "p4", "rechazado")
    atendidos = aplicar_comandos(app, conexion)

    # El primero decide; el segundo llega cuando ya no hay nada que reanudar.
    assert atendidos == [("p4", "registrado"), ("p4", "descartado")]
    assert registros == [("registrado", "aprobado")]


def test_sin_comandos_no_pasa_nada(entorno) -> None:
    app, conexion, registros = entorno

    assert aplicar_comandos(app, conexion) == []
    assert registros == []


# ---------------------------------------------------------------------------
# Avisos (fase 7)
# ---------------------------------------------------------------------------
class NotificadorFalso:
    """Apunta los avisos y puede fingir que el envío falla."""

    def __init__(self, exito: bool = True) -> None:
        self.avisos: list[Aviso] = []
        self.exito = exito

    def enviar(self, aviso: Aviso) -> bool:
        self.avisos.append(aviso)
        return self.exito


def test_el_lote_avisa_de_las_revisiones(entorno) -> None:
    app, conexion, _ = entorno
    notificador = NotificadorFalso()

    resultados = procesar_lote(app, conexion, [PDF], notificador)

    assert resultados == [(PDF.stem, "en_revision")]
    assert len(notificador.avisos) == 1
    aviso = notificador.avisos[0]
    assert aviso.numero == "TEST-001"
    assert aviso.importe == pytest.approx(2420.00)
    assert "supera_umbral" in aviso.motivos

    proceso = store.obtener_proceso(conexion, PDF.stem)
    assert proceso is not None
    assert proceso["notificado_en"] is not None
    assert proceso["duracion_ms"] is not None


def test_si_el_aviso_falla_no_se_marca_como_notificado(entorno) -> None:
    """Con la red caída el aviso queda pendiente de reintento, pero el lote sigue."""
    app, conexion, _ = entorno
    notificador = NotificadorFalso(exito=False)

    resultados = procesar_lote(app, conexion, [PDF], notificador)

    assert resultados == [(PDF.stem, "en_revision")]
    proceso = store.obtener_proceso(conexion, PDF.stem)
    assert proceso is not None
    assert proceso["notificado_en"] is None


def test_el_lote_avisa_tambien_de_los_errores(tmp_path: Path) -> None:
    def extractor_roto(texto: str) -> Extraccion:
        raise ErrorExtraccion("el modelo no devolvió nada útil")

    conexion = store.conectar(tmp_path / "errores.db")
    app = construir_grafo(extractor=extractor_roto, conexion=conexion).compile(
        checkpointer=InMemorySaver()
    )
    notificador = NotificadorFalso()
    try:
        resultados = procesar_lote(app, conexion, [PDF], notificador)

        assert resultados == [(PDF.stem, "error")]
        assert len(notificador.avisos) == 1
        assert notificador.avisos[0].es_error is True
        assert "ErrorExtraccion" in notificador.avisos[0].motivos

        proceso = store.obtener_proceso(conexion, PDF.stem)
        assert proceso is not None
        assert proceso["estado"] == "error"
        assert proceso["duracion_ms"] is not None
    finally:
        conexion.close()
