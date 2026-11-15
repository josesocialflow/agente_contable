"""Validación determinista de una factura contra el ERP simulado.

Aquí no interviene ningún modelo: son reglas de negocio comprobables, que es lo
que el caso de estudio exige para no depender de la certeza del LLM.

Se comprueba, en este orden:

1. Datos fiscales contra el maestro de proveedores: CIF, razón social, IBAN y
   forma de pago. Un IBAN distinto al de la ficha es la alerta clásica de
   suplantación.
2. Aritmética: suma de líneas = base imponible y base + IVA - IRPF = total.
3. Cruce con el albarán citado: se compara concepto y cantidad de cada línea y
   se distingue entre mercancía distinta, cantidad distinta y línea de más o
   de menos.
4. Umbral de aprobación: por encima del importe configurado, revisión humana
   aunque no haya ninguna incidencia.

La detección de duplicados (NIF + número + año) necesita el registro de
procesos, así que se hará en la capa del grafo, no aquí.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from config import ajustes
from core.erp import cargar_albaranes, cargar_maestro
from core.schemas import (
    Albaran,
    Albaranes,
    Factura,
    Incidencia,
    MaestroProveedores,
    Proveedor,
    ResultadoValidacion,
    TipoIncidencia,
    normalizar_nombre,
    redondear_euros,
)


# ---------------------------------------------------------------------------
# Normalizadores
# ---------------------------------------------------------------------------
def _normalizar_iban(valor: str) -> str:
    """Deja el IBAN sin espacios y en mayúsculas."""
    return valor.replace(" ", "").upper()


# ---------------------------------------------------------------------------
# Comprobaciones
# ---------------------------------------------------------------------------
def _validar_datos_fiscales(factura: Factura, proveedor: Proveedor) -> list[Incidencia]:
    """Compara los datos fiscales de la factura con la ficha del proveedor."""
    incidencias: list[Incidencia] = []

    if normalizar_nombre(factura.proveedor.nombre) != normalizar_nombre(proveedor.nombre):
        incidencias.append(
            Incidencia(
                codigo=TipoIncidencia.NOMBRE_NO_COINCIDE,
                mensaje="La razón social de la factura no coincide con la ficha del proveedor",
                detalle={"factura": factura.proveedor.nombre, "maestro": proveedor.nombre},
            )
        )

    if factura.iban and _normalizar_iban(factura.iban) != _normalizar_iban(proveedor.iban):
        incidencias.append(
            Incidencia(
                codigo=TipoIncidencia.IBAN_NO_COINCIDE,
                mensaje=(
                    "El IBAN de la factura no coincide con el del proveedor; "
                    "no pagar sin verificar"
                ),
                detalle={
                    "factura": _normalizar_iban(factura.iban),
                    "maestro": _normalizar_iban(proveedor.iban),
                },
            )
        )

    if factura.forma_pago:
        esperada = normalizar_nombre(proveedor.forma_pago)
        if esperada not in normalizar_nombre(factura.forma_pago):
            incidencias.append(
                Incidencia(
                    codigo=TipoIncidencia.FORMA_PAGO_NO_COINCIDE,
                    mensaje="La forma de pago de la factura no coincide con la pactada",
                    detalle={"factura": factura.forma_pago, "maestro": proveedor.forma_pago},
                )
            )

    return incidencias


def _validar_aritmetica(factura: Factura) -> list[Incidencia]:
    """Comprueba que las cuentas de la factura cuadran al céntimo."""
    suma_lineas = redondear_euros(
        sum((linea.importe for linea in factura.lineas), Decimal(0))
    )

    if suma_lineas != factura.base_imponible:
        return [
            Incidencia(
                codigo=TipoIncidencia.IMPORTE_NO_CUADRA,
                mensaje="La suma de las líneas no coincide con la base imponible",
                detalle={
                    "suma_lineas": str(suma_lineas),
                    "base_imponible": str(factura.base_imponible),
                },
            )
        ]

    if factura.total_esperado != factura.total:
        return [
            Incidencia(
                codigo=TipoIncidencia.IMPORTE_NO_CUADRA,
                mensaje="Base + IVA - IRPF no coincide con el total de la factura",
                detalle={
                    "calculado": str(factura.total_esperado),
                    "factura": str(factura.total),
                },
            )
        ]

    return []


def _validar_albaran(factura: Factura, albaran: Albaran) -> list[Incidencia]:
    """Cruza las líneas de la factura con las del albarán entregado."""
    facturadas = Counter(
        (normalizar_nombre(linea.concepto), linea.cantidad) for linea in factura.lineas
    )
    entregadas = Counter(
        (normalizar_nombre(linea.concepto), linea.cantidad) for linea in albaran.lineas
    )

    if facturadas == entregadas:
        return []

    conceptos_factura = {concepto for concepto, _ in facturadas}
    conceptos_albaran = {concepto for concepto, _ in entregadas}

    # Mismos artículos pero con cantidades distintas.
    if conceptos_factura == conceptos_albaran:
        incidencias = []
        for concepto in sorted(conceptos_factura):
            cantidad_factura = sum(c for (n, c) in facturadas if n == concepto)
            cantidad_albaran = sum(c for (n, c) in entregadas if n == concepto)
            if cantidad_factura != cantidad_albaran:
                incidencias.append(
                    Incidencia(
                        codigo=TipoIncidencia.ALBARAN_CANTIDAD,
                        mensaje="La cantidad facturada no coincide con la entregada",
                        detalle={
                            "concepto": concepto,
                            "facturado": str(cantidad_factura),
                            "entregado": str(cantidad_albaran),
                        },
                    )
                )
        return incidencias

    solo_factura = facturadas - entregadas
    solo_albaran = entregadas - facturadas

    # Un artículo sustituido por otro: falta uno y sobra otro.
    if len(solo_factura) == 1 and len(solo_albaran) == 1:
        facturado, _ = next(iter(solo_factura))
        entregado, _ = next(iter(solo_albaran))
        return [
            Incidencia(
                codigo=TipoIncidencia.ALBARAN_CONCEPTO,
                mensaje="La mercancía entregada no coincide con la facturada",
                detalle={"facturado": facturado, "entregado": entregado},
            )
        ]

    incidencias = []
    for concepto, cantidad in sorted(solo_factura.elements()):
        incidencias.append(
            Incidencia(
                codigo=TipoIncidencia.ALBARAN_LINEA_FALTANTE,
                mensaje="Línea facturada que no aparece en el albarán",
                detalle={"concepto": concepto, "cantidad": str(cantidad)},
            )
        )
    for concepto, cantidad in sorted(solo_albaran.elements()):
        incidencias.append(
            Incidencia(
                codigo=TipoIncidencia.ALBARAN_LINEA_EXTRA,
                mensaje="Línea entregada que no está facturada",
                detalle={"concepto": concepto, "cantidad": str(cantidad)},
            )
        )
    return incidencias


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------
def validar_factura(
    factura: Factura,
    *,
    maestro: MaestroProveedores | None = None,
    albaranes: Albaranes | None = None,
) -> ResultadoValidacion:
    """Aplica todas las reglas y devuelve las incidencias encontradas.

    Args:
        factura: factura extraída del PDF.
        maestro: maestro de proveedores; si no se pasa, se lee del ERP.
        albaranes: albaranes del ERP; si no se pasan, se leen del fichero.

    Returns:
        Resultado con las incidencias y la marca de umbral.
    """
    incidencias: list[Incidencia] = []

    # 1. Datos fiscales. Si el CIF no está, se intenta localizar por razón
    #    social: así se distingue una ficha con el CIF mal tecleado (existe el
    #    proveedor) de uno que sencillamente no está dado de alta.
    proveedor = (maestro or cargar_maestro()).buscar(factura.proveedor.cif)
    if proveedor is None:
        por_nombre = (maestro or cargar_maestro()).buscar_por_nombre(
            factura.proveedor.nombre
        )
        if por_nombre is not None:
            incidencias.append(
                Incidencia(
                    codigo=TipoIncidencia.CIF_NO_COINCIDE,
                    mensaje=(
                        "El proveedor existe en el maestro, pero con otro CIF: "
                        "la ficha o la factura no cuadran"
                    ),
                    detalle={
                        "factura": factura.proveedor.cif_normalizado,
                        "maestro": por_nombre.cif,
                    },
                )
            )
            # Se sigue validando contra la ficha encontrada (nombre, IBAN...).
            incidencias.extend(_validar_datos_fiscales(factura, por_nombre))
        else:
            incidencias.append(
                Incidencia(
                    codigo=TipoIncidencia.CIF_DESCONOCIDO,
                    mensaje="El CIF de la factura no está en el maestro de proveedores",
                    detalle={"cif": factura.proveedor.cif_normalizado},
                )
            )
    else:
        incidencias.extend(_validar_datos_fiscales(factura, proveedor))

    # 2. Aritmética.
    incidencias.extend(_validar_aritmetica(factura))

    # 3. Cruce con el albarán.
    albaran = (albaranes or cargar_albaranes()).buscar(factura.referencia_albaran)
    if albaran is None:
        incidencias.append(
            Incidencia(
                codigo=TipoIncidencia.ALBARAN_NO_ENCONTRADO,
                mensaje="No hay ningún albarán con esa referencia en el ERP",
                detalle={"referencia": factura.referencia_albaran},
            )
        )
    else:
        incidencias.extend(_validar_albaran(factura, albaran))

    # 4. Umbral de aprobación (no es una incidencia: es otro motivo de revisión).
    return ResultadoValidacion(
        factura_id=factura.identificador,
        incidencias=incidencias,
        supera_umbral=factura.total > ajustes.umbral_aprobacion,
    )
