"""Acceso al ERP simulado: maestro de proveedores y albaranes.

Los dos ficheros JSON hacen de base de datos. Se **releen en cada consulta** a
propósito: el worker puede estar corriendo mientras alguien da de alta un
albarán, y el cambio debe surtir efecto sin reiniciar (es lo que haría un ERP
de verdad al consultar en vivo).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import ajustes
from core.schemas import Albaran, Albaranes, Factura, MaestroProveedores, Proveedor


def cargar_maestro(ruta: Path | None = None) -> MaestroProveedores:
    """Lee el maestro de proveedores del "ERP"."""
    origen = ruta or ajustes.ruta_maestro
    return MaestroProveedores.model_validate_json(origen.read_text(encoding="utf-8"))


def cargar_albaranes(ruta: Path | None = None) -> Albaranes:
    """Lee los albaranes de entrega del "ERP"."""
    origen = ruta or ajustes.ruta_albaranes
    return Albaranes.model_validate_json(origen.read_text(encoding="utf-8"))


def obtener_proveedor(
    cif: str, maestro: MaestroProveedores | None = None
) -> Proveedor | None:
    """Busca un proveedor por CIF (normalizado), o ``None`` si no existe."""
    return (maestro or cargar_maestro()).buscar(cif)


def obtener_albaran(
    numero: str, albaranes: Albaranes | None = None
) -> Albaran | None:
    """Busca un albarán por número, o ``None`` si no existe."""
    return (albaranes or cargar_albaranes()).buscar(numero)


def leer_registro(ruta: Path | None = None) -> list[dict[str, Any]]:
    """Devuelve las facturas ya anotadas en el registro del ERP simulado."""
    origen = ruta or ajustes.ruta_registro
    if not origen.exists():
        return []
    return json.loads(origen.read_text(encoding="utf-8"))


def registrar_factura(
    factura: Factura,
    *,
    estado: str = "registrado",
    aprobado_por: str | None = None,
    ruta: Path | None = None,
) -> dict[str, Any]:
    """Anota una factura en el registro del ERP simulado.

    El agente **no paga**: deja la factura con ``estado_pago = pendiente``, la
    fecha de vencimiento calculada con el plazo del maestro y el IBAN de la
    ficha (nunca el impreso en la factura). La remesa la simularía tesorería.

    Es idempotente por ``factura_id``: si ya estaba anotada con el mismo estado,
    no se duplica.
    """
    destino = ruta or ajustes.ruta_registro
    proveedor = obtener_proveedor(factura.proveedor.cif)
    dias = (
        proveedor.dias_vencimiento
        if proveedor
        else ajustes.dias_vencimiento_por_defecto
    )

    entrada: dict[str, Any] = {
        "factura_id": factura.identificador,
        "numero": factura.numero,
        "proveedor": factura.proveedor.nombre,
        "cif": factura.proveedor.cif_normalizado,
        "fecha_expedicion": factura.fecha_expedicion.isoformat(),
        "fecha_vencimiento": (factura.fecha_expedicion + timedelta(days=dias)).isoformat(),
        "base_imponible": str(factura.base_imponible),
        "cuota_iva": str(factura.cuota_iva),
        "retencion": str(factura.cuota_retencion),
        "total": str(factura.total),
        "forma_pago": proveedor.forma_pago if proveedor else None,
        "iban_maestro": proveedor.iban if proveedor else None,
        "estado": estado,
        "estado_pago": "pendiente",
        "aprobado_por": aprobado_por,
        "registrado_en": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    registros = leer_registro(destino)
    for existente in registros:
        if existente["factura_id"] == entrada["factura_id"] and existente["estado"] == estado:
            return existente

    registros.append(entrada)
    destino.write_text(
        json.dumps(registros, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return entrada
