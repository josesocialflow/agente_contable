"""Evaluación de la validación sobre el lote (fase 4).

Trabaja sobre las extracciones guardadas en ``datos/procesados`` (fase 3), así
que no llama a la API. Comprueba que se marcan exactamente las 10 facturas
previstas: 5 por discrepancia de albarán, 2 por CIF y 3 por importe.

Uso:
    python evaluar_validacion.py
"""

from __future__ import annotations

import sys
from collections import Counter

from config import ajustes
from core.erp import cargar_albaranes, cargar_maestro
from core.schemas import Factura, TipoIncidencia
from core.validation import validar_factura

# Incidencias esperadas por factura (el resto no debe tener ninguna).
ESPERADAS: dict[str, list[TipoIncidencia]] = {
    "FAC-2026-021": [TipoIncidencia.ALBARAN_CANTIDAD],
    "LIM-2026-023": [TipoIncidencia.ALBARAN_CANTIDAD],
    "CRB-2026-024": [TipoIncidencia.ALBARAN_CONCEPTO],
    "FAC-2026-031": [TipoIncidencia.ALBARAN_LINEA_EXTRA],
    "IMP-2026-035": [TipoIncidencia.ALBARAN_LINEA_FALTANTE],
    "MOM-2026-026": [TipoIncidencia.CIF_NO_COINCIDE],
    "MOM-2026-036": [TipoIncidencia.CIF_NO_COINCIDE],
}

# Facturas que deben superar el umbral de aprobación.
SUPERAN_UMBRAL = {"LAE-2026-028", "LIM-2026-033", "TEC-2026-039"}


def cargar_facturas() -> list[Factura]:
    """Lee las facturas extraídas en la fase 3."""
    ficheros = sorted(
        ruta
        for ruta in ajustes.directorio_procesados.glob("*.json")
        if not ruta.name.startswith("_")
    )
    return [Factura.model_validate_json(ruta.read_text(encoding="utf-8")) for ruta in ficheros]


def main() -> int:
    facturas = cargar_facturas()
    if not facturas:
        print(
            f"No hay extracciones en {ajustes.directorio_procesados}.\n"
            "Ejecuta antes: python evaluar_extraccion.py"
        )
        return 1

    maestro = cargar_maestro()
    albaranes = cargar_albaranes()

    a_revision = 0
    fallos: list[str] = []
    codigos_vistos: Counter[TipoIncidencia] = Counter()

    print(f"{'factura':<16}{'incidencias':<46}{'umbral':<8}resultado")
    print("-" * 88)

    for factura in facturas:
        resultado = validar_factura(factura, maestro=maestro, albaranes=albaranes)
        codigos = resultado.codigos
        codigos_vistos.update(codigos)

        esperadas = ESPERADAS.get(factura.numero, [])
        umbral_esperado = factura.numero in SUPERAN_UMBRAL
        correcto = sorted(codigos) == sorted(esperadas) and (
            resultado.supera_umbral == umbral_esperado
        )
        if resultado.requiere_revision:
            a_revision += 1
        if not correcto:
            fallos.append(factura.numero)

        detalle = ", ".join(codigo.value for codigo in codigos) or "-"
        print(
            f"{factura.numero:<16}{detalle:<46}"
            f"{'sí' if resultado.supera_umbral else 'no':<8}"
            f"{'OK' if correcto else 'FALLO'}"
        )

    print("-" * 88)
    print(f"facturas a revisión: {a_revision}/{len(facturas)} (esperadas 10)")
    print(f"automáticas:         {len(facturas) - a_revision}/{len(facturas)} (esperadas 10)")
    print(f"incidencias por tipo: {codigos_vistos}")

    if fallos:
        print(f"\nFALLOS en: {', '.join(fallos)}")
        return 1

    print("\nSin falsos positivos ni falsos negativos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
