"""Pruebas de la validación determinista (fase 4).

Construyen facturas a partir de un albarán real del ERP y del maestro, y van
introduciendo un error de cada tipo para comprobar que se detecta con el código
correcto. No usan el modelo ni la API.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from core.erp import (
    cargar_albaranes,
    cargar_maestro,
    obtener_albaran,
    obtener_proveedor,
)
from core.schemas import Albaran, Factura, TipoIncidencia
from core.validation import validar_factura

ALBARAN_LIMPIO = "ALB-TEC-2026-029"  # proveedor sin errores en el lote
PRECIO = Decimal("50.00")


@pytest.fixture(scope="module")
def maestro():
    return cargar_maestro()


@pytest.fixture(scope="module")
def albaranes():
    return cargar_albaranes()


@pytest.fixture(scope="module")
def albaran(albaranes) -> Albaran:
    albaran = albaranes.buscar(ALBARAN_LIMPIO)
    assert albaran is not None
    return albaran


def _factura(
    albaran: Albaran,
    maestro,
    *,
    lineas: list[tuple[str, Decimal]] | None = None,
    cif: str | None = None,
    nombre: str | None = None,
    iban: str | None = None,
    referencia: str | None = None,
    total_extra: Decimal = Decimal(0),
    precio: Decimal = PRECIO,
) -> Factura:
    """Monta una factura coherente y permite alterar un dato concreto."""
    proveedor = maestro.buscar(albaran.proveedor.cif)
    lineas = lineas or [(linea.concepto, linea.cantidad) for linea in albaran.lineas]

    filas = [
        {
            "concepto": concepto,
            "cantidad": cantidad,
            "precio_unitario": precio,
            "importe": precio * cantidad,
        }
        for concepto, cantidad in lineas
    ]
    base = sum((fila["importe"] for fila in filas), Decimal(0))
    cuota = (base * Decimal(21) / 100).quantize(Decimal("0.01"))

    return Factura.model_validate(
        {
            "numero": "TEC-2026-029",
            "fecha_expedicion": albaran.fecha + timedelta(days=1),
            "fecha_operacion": albaran.fecha,
            "referencia_albaran": referencia or albaran.numero,
            "proveedor": {
                "nombre": nombre or albaran.proveedor.nombre,
                "cif": cif or albaran.proveedor.cif,
            },
            "cliente": {"nombre": "Logística Ibérica S.L.", "cif": "B80000000"},
            "lineas": filas,
            "base_imponible": base,
            "cuota_iva": cuota,
            "total": base + cuota + total_extra,
            "forma_pago": proveedor.forma_pago,
            "iban": iban or proveedor.iban,
        }
    )


# ---------------------------------------------------------------------------
# Caso correcto
# ---------------------------------------------------------------------------
def test_factura_coherente_no_tiene_incidencias(albaran, maestro, albaranes) -> None:
    resultado = validar_factura(
        _factura(albaran, maestro), maestro=maestro, albaranes=albaranes
    )
    assert resultado.incidencias == []
    assert not resultado.requiere_revision


# ---------------------------------------------------------------------------
# Datos fiscales
# ---------------------------------------------------------------------------
def test_cif_distinto_pero_proveedor_conocido(albaran, maestro, albaranes) -> None:
    """Hay nombre en el maestro pero con otro CIF: ficha mal mantenida."""
    factura = _factura(albaran, maestro, cif="B00000000")
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert resultado.codigos == [TipoIncidencia.CIF_NO_COINCIDE]


def test_proveedor_desconocido(albaran, maestro, albaranes) -> None:
    """Ni el CIF ni la razón social están en el maestro."""
    factura = _factura(
        albaran, maestro, cif="B00000000", nombre="Proveedor Fantasma S.L."
    )
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert resultado.codigos == [TipoIncidencia.CIF_DESCONOCIDO]


def test_razon_social_distinta(albaran, maestro, albaranes) -> None:
    factura = _factura(albaran, maestro, nombre="Tecnología Avanzada Falsa S.L.")
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert TipoIncidencia.NOMBRE_NO_COINCIDE in resultado.codigos


def test_iban_distinto(albaran, maestro, albaranes) -> None:
    factura = _factura(albaran, maestro, iban="ES00 0000 0000 0000 0000 0000")
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert TipoIncidencia.IBAN_NO_COINCIDE in resultado.codigos


# ---------------------------------------------------------------------------
# Aritmética
# ---------------------------------------------------------------------------
def test_total_que_no_cuadra(albaran, maestro, albaranes) -> None:
    factura = _factura(albaran, maestro, total_extra=Decimal("1.00"))
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert resultado.codigos == [TipoIncidencia.IMPORTE_NO_CUADRA]


# ---------------------------------------------------------------------------
# Albarán
# ---------------------------------------------------------------------------
def test_albaran_inexistente(albaran, maestro, albaranes) -> None:
    factura = _factura(albaran, maestro, referencia="ALB-NO-EXISTE")
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert resultado.codigos == [TipoIncidencia.ALBARAN_NO_ENCONTRADO]


def test_cantidad_distinta(albaran, maestro, albaranes) -> None:
    lineas = [(linea.concepto, linea.cantidad) for linea in albaran.lineas]
    lineas[0] = (lineas[0][0], lineas[0][1] - 1)
    factura = _factura(albaran, maestro, lineas=lineas)
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert TipoIncidencia.ALBARAN_CANTIDAD in resultado.codigos


def test_mercancia_distinta(albaran, maestro, albaranes) -> None:
    lineas = [(linea.concepto, linea.cantidad) for linea in albaran.lineas]
    lineas[0] = ("Monitor 24 pulgadas", lineas[0][1])
    factura = _factura(albaran, maestro, lineas=lineas)
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert resultado.codigos == [TipoIncidencia.ALBARAN_CONCEPTO]


def test_linea_facturada_sin_entregar(albaran, maestro, albaranes) -> None:
    lineas = [(linea.concepto, linea.cantidad) for linea in albaran.lineas]
    lineas.append(("Cable HDMI 2 m", Decimal(2)))
    factura = _factura(albaran, maestro, lineas=lineas)
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert resultado.codigos == [TipoIncidencia.ALBARAN_LINEA_FALTANTE]


def test_linea_entregada_sin_facturar(albaran, maestro, albaranes) -> None:
    lineas = [(linea.concepto, linea.cantidad) for linea in albaran.lineas][:1]
    factura = _factura(albaran, maestro, lineas=lineas)
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert resultado.codigos == [TipoIncidencia.ALBARAN_LINEA_EXTRA]


# ---------------------------------------------------------------------------
# Umbral y capa ERP
# ---------------------------------------------------------------------------
def test_superar_el_umbral_obliga_a_revision(albaran, maestro, albaranes) -> None:
    factura = _factura(albaran, maestro, precio=Decimal("1000.00"))
    resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
    assert resultado.incidencias == []
    assert resultado.supera_umbral
    assert resultado.requiere_revision


def test_capa_erp_busca_por_cif_y_numero(maestro, albaranes) -> None:
    assert obtener_proveedor("B12345678", maestro) is not None
    assert obtener_proveedor("B00000000", maestro) is None
    assert obtener_albaran(ALBARAN_LIMPIO, albaranes) is not None
    assert obtener_albaran("ALB-NO-EXISTE", albaranes) is None


def test_fechas_de_la_factura_de_ejemplo(albaran) -> None:
    assert isinstance(albaran.fecha, date)
