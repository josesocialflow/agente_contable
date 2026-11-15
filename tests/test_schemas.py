"""Pruebas de los modelos de datos (fase 1).

Verifican que los contratos aceptan el ERP simulado real y rechazan datos mal
formados, además de las propiedades de cálculo (retención, totales, importes).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from config import ajustes
from core.schemas import (
    Albaranes,
    Factura,
    Incidencia,
    MaestroProveedores,
    ResultadoValidacion,
    TipoIncidencia,
    normalizar_cif,
    normalizar_importe,
    redondear_euros,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def maestro() -> MaestroProveedores:
    """Maestro de proveedores real del proyecto."""
    return MaestroProveedores.model_validate_json(
        ajustes.ruta_maestro.read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def albaranes() -> Albaranes:
    """Albaranes reales del proyecto."""
    return Albaranes.model_validate_json(
        ajustes.ruta_albaranes.read_text(encoding="utf-8")
    )


def factura_valida(**cambios: object) -> dict:
    """Factura coherente de ejemplo (MD-2026-022, con retención del 15 %)."""
    datos: dict = {
        "numero": "MD-2026-022",
        "fecha_expedicion": "2026-11-06",
        "fecha_operacion": "2026-10-28",
        "referencia_albaran": "ALB-MD-2026-022",
        "proveedor": {
            "nombre": "Consultoría y Marketing Digital S.L.",
            "cif": "B-98765432",
        },
        "cliente": {"nombre": "Logística Ibérica S.L.", "cif": "B80000000"},
        "lineas": [
            {
                "concepto": "Copywriting para landing pages",
                "cantidad": "1",
                "precio_unitario": "374.12",
                "importe": "374.12",
            },
            {
                "concepto": "Gestión de campañas SEM y LinkedIn Ads",
                "cantidad": "1",
                "precio_unitario": "1494.68",
                "importe": "1494.68",
            },
            {
                "concepto": "Informe mensual de analítica y KPIs",
                "cantidad": "1",
                "precio_unitario": "330.32",
                "importe": "330.32",
            },
            {
                "concepto": "Auditoría SEO técnico",
                "cantidad": "1",
                "precio_unitario": "668.88",
                "importe": "668.88",
            },
        ],
        "base_imponible": "2868.00",
        "cuota_iva": "602.28",
        "tipo_retencion_irpf": "15",
        "total": "3040.08",
    }
    datos.update(cambios)
    return datos


# ---------------------------------------------------------------------------
# ERP simulado
# ---------------------------------------------------------------------------
def test_maestro_carga_los_diez_proveedores(maestro: MaestroProveedores) -> None:
    assert len(maestro.proveedores) == 10
    assert len({p.cif for p in maestro.proveedores}) == 10  # CIF únicos


def test_albaranes_carga_los_veinte(albaranes: Albaranes) -> None:
    assert len(albaranes.albaranes) == 20
    assert len({a.numero for a in albaranes.albaranes}) == 20


def test_maestro_rechaza_json_incompleto() -> None:
    roto = '{"version": 1, "proveedores": [{"id": "PRV-X"}]}'
    with pytest.raises(ValidationError):
        MaestroProveedores.model_validate_json(roto)


def test_busqueda_por_cif_normalizado(maestro: MaestroProveedores) -> None:
    proveedor = maestro.buscar("b-987 654 32")
    assert proveedor is not None
    assert proveedor.nombre == "Consultoría y Marketing Digital S.L."


def test_busqueda_cif_inexistente_devuelve_none(maestro: MaestroProveedores) -> None:
    assert maestro.buscar("B00000000") is None


def test_busqueda_albaran_tolera_espacios(albaranes: Albaranes) -> None:
    albaran = albaranes.buscar("  alb-md-2026-022 ")
    assert albaran is not None
    assert len(albaran.lineas) == 4


def test_busqueda_albaran_inexistente_devuelve_none(albaranes: Albaranes) -> None:
    assert albaranes.buscar("ALB-NO-EXISTE") is None


def test_el_lote_incluye_un_cif_alterado_a_proposito(
    maestro: MaestroProveedores, albaranes: Albaranes
) -> None:
    """El ERP simulado lleva un error deliberado: el CIF de Mobiliario.

    Facturas y albaranes usan ``B44556677``; el maestro dice ``B44556678``.
    Son las dos facturas de Mobiliario (MOM-2026-026 y MOM-2026-036), que el
    agente debe marcar con ``cif_no_coincide``. El resto de proveedores sí
    coinciden, así que un fallo aquí indicaría que el error se ha perdido o que
    ha aparecido otro no previsto.
    """
    cifs_albaranes = {albaran.proveedor.cif_normalizado for albaran in albaranes.albaranes}
    cifs_maestro = set(maestro.indice_por_cif())

    assert cifs_albaranes - cifs_maestro == {"B44556677"}
    assert cifs_maestro - cifs_albaranes == {"B44556678"}
    assert len(cifs_albaranes & cifs_maestro) == 9

    afectados = {
        albaran.numero
        for albaran in albaranes.albaranes
        if albaran.proveedor.cif_normalizado not in cifs_maestro
    }
    assert afectados == {"ALB-MOM-2026-026", "ALB-MOM-2026-036"}


# ---------------------------------------------------------------------------
# Factura
# ---------------------------------------------------------------------------
def test_factura_calcula_retencion_y_total() -> None:
    factura = Factura.model_validate(factura_valida())
    assert factura.cuota_retencion == Decimal("430.20")
    assert factura.total_esperado == factura.total == Decimal("3040.08")


def test_identificador_usa_cif_normalizado_y_anio() -> None:
    factura = Factura.model_validate(factura_valida())
    assert factura.identificador == "B98765432-MD-2026-022-2026"


def test_factura_rechaza_campos_extra() -> None:
    with pytest.raises(ValidationError):
        Factura.model_validate(factura_valida(campo_inventado="alucinación"))


def test_factura_exige_al_menos_una_linea() -> None:
    with pytest.raises(ValidationError):
        Factura.model_validate(factura_valida(lineas=[]))


def test_importe_calculado_por_linea() -> None:
    factura = Factura.model_validate(
        factura_valida(
            lineas=[
                {
                    "concepto": "Silla ergonómica",
                    "cantidad": "23",
                    "precio_unitario": "215.17",
                    "importe": "4948.91",
                }
            ]
        )
    )
    assert factura.lineas[0].importe_calculado == Decimal("4948.91")


# ---------------------------------------------------------------------------
# Utilidades y resultado de validación
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("B-987 654 32", "B98765432"),
        (" b80000000 ", "B80000000"),
        ("A.87654321", "A87654321"),
    ],
)
def test_normalizar_cif(entrada: str, esperado: str) -> None:
    assert normalizar_cif(entrada) == esperado


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("2.345", "2.35"),
        ("0.005", "0.01"),
        ("1.004", "1.00"),
        ("-430.205", "-430.21"),
    ],
)
def test_redondear_euros(entrada: str, esperado: str) -> None:
    assert redondear_euros(Decimal(entrada)) == Decimal(esperado)


@pytest.mark.parametrize(
    ("entrada", "esperado"),
    [
        ("1.234,56 €", "1234.56"),
        ("91,76", "91.76"),
        ("1,234.56", "1234.56"),
        ("917,20 EUR", "917.20"),
        ("15 %", "15"),
        (500, "500"),
        (Decimal("1.5"), "1.5"),
    ],
)
def test_normalizar_importe(entrada: object, esperado: str) -> None:
    assert Decimal(str(normalizar_importe(entrada))) == Decimal(esperado)


def test_factura_acepta_importes_impresos_en_formato_espanol() -> None:
    """El LLM copia a veces los importes tal cual se imprimen."""
    factura = Factura.model_validate(
        factura_valida(
            base_imponible="2.868,00 €",
            cuota_iva="602,28 €",
            total="3.040,08 €",
            tipo_retencion_irpf="15 %",
            lineas=[
                {
                    "concepto": "Copywriting",
                    "cantidad": "1",
                    "precio_unitario": "374,12 €",
                    "importe": "374,12 €",
                }
            ],
        )
    )
    assert factura.base_imponible == Decimal("2868.00")
    assert factura.tipo_retencion_irpf == Decimal(15)
    assert factura.lineas[0].importe == Decimal("374.12")


def test_resultado_sin_incidencias_no_requiere_revision() -> None:
    resultado = ResultadoValidacion(factura_id="B98765432-MD-2026-022-2026")
    assert not resultado.tiene_incidencias
    assert not resultado.requiere_revision
    assert resultado.codigos == []


def test_cualquier_incidencia_obliga_a_revision() -> None:
    resultado = ResultadoValidacion(
        factura_id="X",
        incidencias=[
            Incidencia(
                codigo=TipoIncidencia.ALBARAN_CANTIDAD,
                mensaje="Facturadas 10, entregadas 8",
                detalle={"concepto": "Tóner", "facturado": 10, "entregado": 8},
            )
        ],
    )
    assert resultado.tiene_incidencias
    assert resultado.requiere_revision
    assert resultado.codigos == [TipoIncidencia.ALBARAN_CANTIDAD]


def test_superar_umbral_obliga_a_revision_aunque_no_haya_incidencias() -> None:
    resultado = ResultadoValidacion(factura_id="X", supera_umbral=True)
    assert not resultado.tiene_incidencias
    assert resultado.requiere_revision
