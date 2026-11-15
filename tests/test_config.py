"""Pruebas de la configuración (fase 1).

Comprueban los valores por defecto, que las rutas del ERP existen y que las
variables de entorno con prefijo ``AGENTE_`` se aplican de verdad.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from config import Configuracion, ajustes


def test_valores_por_defecto() -> None:
    assert ajustes.modelo
    assert ajustes.temperatura == 0.0
    assert ajustes.reintentos_extraccion == 2
    assert ajustes.notificador in {"consola", "telegram"}


def test_umbral_es_decimal() -> None:
    assert isinstance(ajustes.umbral_aprobacion, Decimal)
    assert ajustes.umbral_aprobacion == Decimal("1000.00")


def test_rutas_del_erp_existen() -> None:
    assert ajustes.comprobar_rutas() == []
    assert ajustes.directorio_facturas.is_dir()
    assert ajustes.ruta_albaranes.is_file()
    assert ajustes.ruta_maestro.is_file()


def test_rutas_comparten_el_directorio_de_datos() -> None:
    assert ajustes.directorio_facturas.parent == ajustes.directorio_datos
    assert ajustes.ruta_db.parent == ajustes.directorio_datos
    assert ajustes.ruta_checkpoints.parent == ajustes.directorio_datos


def test_override_por_variables_de_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTE_MODELO", "gpt-4o")
    monkeypatch.setenv("AGENTE_UMBRAL_APROBACION", "2500")
    monkeypatch.setenv("AGENTE_REINTENTOS_EXTRACCION", "4")

    configuracion = Configuracion()

    assert configuracion.modelo == "gpt-4o"
    assert configuracion.umbral_aprobacion == Decimal(2500)
    assert configuracion.reintentos_extraccion == 4


def test_notificador_desconocido_es_rechazado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTE_NOTIFICADOR", "paloma-mensajera")
    with pytest.raises(ValidationError):
        Configuracion()


def test_temperatura_fuera_de_rango_es_rechazada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTE_TEMPERATURA", "3")
    with pytest.raises(ValidationError):
        Configuracion()


def test_reintentos_negativos_son_rechazados(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTE_REINTENTOS_EXTRACCION", "-1")
    with pytest.raises(ValidationError):
        Configuracion()


def test_rutas_datos_devuelve_las_clave_esperadas() -> None:
    assert set(ajustes.rutas_datos) == {
        "facturas",
        "albaranes",
        "maestro",
        "db",
        "checkpoints",
        "pagos",
        "registro",
    }
