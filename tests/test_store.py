"""Pruebas de la persistencia: procesos y canal de comandos (fases 5 y 6).

Trabajan sobre una base de datos temporal: no tocan ``datos/agente.db``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from core import store


@pytest.fixture
def conexion(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Base de datos recién creada, con el esquema aplicado."""
    base = store.conectar(tmp_path / "agente.db")
    try:
        yield base
    finally:
        base.close()


# ---------------------------------------------------------------------------
# Procesos
# ---------------------------------------------------------------------------
def test_crear_proceso_lo_deja_encolado(conexion: sqlite3.Connection) -> None:
    store.crear_proceso(conexion, "p1", "factura.pdf")

    proceso = store.obtener_proceso(conexion, "p1")

    assert proceso is not None
    assert proceso["estado"] == "encolado"
    assert proceso["fichero"] == "factura.pdf"
    assert proceso["inicio"] is not None


def test_actualizar_proceso_rechaza_columnas_desconocidas(
    conexion: sqlite3.Connection,
) -> None:
    store.crear_proceso(conexion, "p1", "factura.pdf")

    with pytest.raises(ValueError, match="no válidas"):
        store.actualizar_proceso(conexion, "p1", inventada="x")


def test_contar_por_estado(conexion: sqlite3.Connection) -> None:
    for proceso in ("p1", "p2"):
        store.crear_proceso(conexion, proceso, "factura.pdf")
        store.actualizar_proceso(conexion, proceso, estado="en_revision")
    store.crear_proceso(conexion, "p3", "otra.pdf")
    store.actualizar_proceso(conexion, "p3", estado="registrado")

    assert store.contar_por_estado(conexion) == {"en_revision": 2, "registrado": 1}


def test_existe_registrada_ignora_el_proceso_excluido(
    conexion: sqlite3.Connection,
) -> None:
    store.crear_proceso(conexion, "p1", "factura.pdf")
    store.actualizar_proceso(
        conexion, "p1", estado="registrado", factura_id="B12345678-F-2026"
    )

    assert store.existe_registrada(conexion, "B12345678-F-2026") is True
    # El propio proceso no se cuenta como duplicado de sí mismo.
    assert store.existe_registrada(conexion, "B12345678-F-2026", excluir="p1") is False


# ---------------------------------------------------------------------------
# Comandos (el canal panel -> worker)
# ---------------------------------------------------------------------------
def test_enviar_comando_queda_pendiente(conexion: sqlite3.Connection) -> None:
    identificador = store.enviar_comando(conexion, "p1", "aprobado")

    pendientes = store.comandos_pendientes(conexion)

    assert identificador > 0
    assert len(pendientes) == 1
    assert pendientes[0]["proceso"] == "p1"
    assert pendientes[0]["accion"] == "aprobado"
    assert pendientes[0]["aplicado"] == 0
    assert pendientes[0]["ts"] is not None


def test_enviar_comando_rechaza_acciones_no_admitidas(
    conexion: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match="acción no válida"):
        store.enviar_comando(conexion, "p1", "pagar")

    assert store.comandos_pendientes(conexion) == []


def test_marcar_comando_lo_saca_de_los_pendientes(
    conexion: sqlite3.Connection,
) -> None:
    identificador = store.enviar_comando(conexion, "p1", "aprobado")

    store.marcar_comando(conexion, identificador)

    assert store.comandos_pendientes(conexion) == []
    # El historial sigue ahí: el panel lo usa para avisar de lo ya decidido.
    historial = store.comandos_del_proceso(conexion, "p1")
    assert len(historial) == 1
    assert historial[0]["aplicado"] == 1


def test_los_comandos_pendientes_llegan_en_orden(conexion: sqlite3.Connection) -> None:
    store.enviar_comando(conexion, "p1", "aprobado")
    store.enviar_comando(conexion, "p1", "rechazado")

    assert [comando["accion"] for comando in store.comandos_pendientes(conexion)] == [
        "aprobado",
        "rechazado",
    ]


# ---------------------------------------------------------------------------
# Migración del esquema
# ---------------------------------------------------------------------------
def test_migra_una_base_creada_antes_de_las_metricas(tmp_path: Path) -> None:
    """Una base de la fase 5 no tiene columnas de la fase 7: se añaden solas."""
    ruta = tmp_path / "antigua.db"
    antigua = sqlite3.connect(ruta)
    antigua.executescript(
        """
        CREATE TABLE procesos (
            id TEXT PRIMARY KEY, factura_id TEXT, fichero TEXT, numero TEXT,
            proveedor TEXT, estado TEXT NOT NULL, importe REAL,
            incidencias INTEGER DEFAULT 0, decision TEXT, inicio TEXT,
            fin TEXT, heartbeat TEXT, error TEXT
        );
        INSERT INTO procesos (id, estado) VALUES ('p1', 'registrado');
        """
    )
    antigua.commit()
    antigua.close()

    base = store.conectar(ruta)
    try:
        columnas = {
            fila["name"] for fila in base.execute("PRAGMA table_info(procesos)")
        }
        assert {
            "notificado_en",
            "tokens_entrada",
            "tokens_salida",
            "coste",
            "duracion_ms",
        } <= columnas

        # Los datos de antes siguen ahí y se pueden actualizar las columnas nuevas.
        proceso = store.obtener_proceso(base, "p1")
        assert proceso is not None
        assert proceso["estado"] == "registrado"
        assert proceso["tokens_entrada"] is None

        store.actualizar_proceso(base, "p1", tokens_entrada=10, coste=0.5)
        actualizado = store.obtener_proceso(base, "p1")
        assert actualizado is not None
        assert actualizado["tokens_entrada"] == 10
    finally:
        base.close()


def test_conectar_dos_veces_no_rompe_la_migracion(tmp_path: Path) -> None:
    """La migración es idempotente: se ejecuta en cada conexión."""
    ruta = tmp_path / "agente.db"
    store.conectar(ruta).close()
    base = store.conectar(ruta)
    try:
        columnas = [fila["name"] for fila in base.execute("PRAGMA table_info(procesos)")]
        assert columnas.count("coste") == 1
    finally:
        base.close()
