"""Pruebas del evaluador (fase 3): verdad de referencia y comparación.

No usan el modelo: trabajan sobre el texto de los PDF y una ``Factura``
construida a mano.
"""

from __future__ import annotations

from decimal import Decimal

from config import ajustes
from core.ingest import extraer_texto, listar_facturas
from core.schemas import Factura
from evaluar_extraccion import comparar, verdad_de_referencia


def _verdad_de_la_primera_factura() -> dict:
    ruta = listar_facturas(ajustes.directorio_facturas)[0]
    return verdad_de_referencia(extraer_texto(ruta))


def _factura_desde_verdad(verdad: dict, **cambios: object) -> Factura:
    datos: dict = {
        "numero": verdad["numero"],
        "fecha_expedicion": verdad["fecha_expedicion"],
        "fecha_operacion": verdad["fecha_operacion"],
        "referencia_albaran": verdad["referencia_albaran"],
        "proveedor": {"nombre": "Proveedor", "cif": verdad["cif_emisor"]},
        "cliente": {"nombre": "Logística Ibérica S.L.", "cif": "B80000000"},
        "lineas": [
            {
                "concepto": concepto,
                "cantidad": cantidad,
                "precio_unitario": precio,
                "importe": importe,
            }
            for concepto, cantidad, precio, importe in verdad["lineas"]
        ],
        "base_imponible": verdad["base_imponible"],
        "cuota_iva": verdad["cuota_iva"],
        "tipo_retencion_irpf": "0",
        "total": verdad["total"],
    }
    datos.update(cambios)
    return Factura.model_validate(datos)


def test_verdad_de_referencia_lee_los_datos_basicos() -> None:
    verdad = _verdad_de_la_primera_factura()

    assert verdad["numero"]
    assert verdad["cif_emisor"]
    assert verdad["referencia_albaran"].startswith("ALB-")
    assert verdad["lineas"]
    assert verdad["base_imponible"] is not None
    assert verdad["cuota_iva"] is not None
    # La aritmética del PDF debe cuadrar.
    assert verdad["base_imponible"] + verdad["cuota_iva"] == verdad["total"]


def test_verdad_de_referencia_detecta_la_retencion() -> None:
    ruta = ajustes.directorio_facturas / "Factura_22_Consultoria_y_Marketing_Digital_S_L.pdf"
    verdad = verdad_de_referencia(extraer_texto(ruta))
    assert verdad["retencion"] is not None
    assert verdad["retencion"] > 0


def test_comparar_da_todo_correcto_con_los_datos_del_pdf() -> None:
    verdad = _verdad_de_la_primera_factura()
    factura = _factura_desde_verdad(verdad)

    estado = comparar(factura, verdad)

    assert all(estado.values()), estado


def test_comparar_detecta_un_campo_alterado() -> None:
    verdad = _verdad_de_la_primera_factura()
    factura = _factura_desde_verdad(verdad, numero="OTRA-2026-999")

    estado = comparar(factura, verdad)

    assert estado["numero"] is False
    assert sum(not ok for ok in estado.values()) == 1


def test_comparar_detecta_una_cantidad_distinta() -> None:
    verdad = _verdad_de_la_primera_factura()
    lineas_alteradas = [
        {
            "concepto": concepto,
            "cantidad": cantidad + Decimal(1),
            "precio_unitario": precio,
            "importe": importe,
        }
        for concepto, cantidad, precio, importe in verdad["lineas"]
    ]
    factura = _factura_desde_verdad(verdad, lineas=lineas_alteradas)

    estado = comparar(factura, verdad)

    assert estado["lineas"] is False
