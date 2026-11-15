"""Evaluación de la extracción sobre el lote completo (fase 3).

Recorre las facturas de ``datos/fras``, extrae cada una con el modelo y compara
el resultado con los datos impresos en el propio PDF (verdad de referencia
obtenida por expresiones regulares, sin pasar por el LLM).

Sirve para dos cosas:
- medir la precisión campo a campo y detectar regresiones al cambiar el prompt,
  el modelo o el normalizador de importes;
- guardar la extracción de cada factura en ``datos/procesados/`` como referencia
  para las fases siguientes.

Uso:
    python evaluar_extraccion.py                 # las 20 facturas
    python evaluar_extraccion.py --limite 5      # solo las cinco primeras
    python evaluar_extraccion.py --sin-guardar   # no escribe en datos/procesados
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from config import ajustes
from core.extraction import ErrorExtraccion, crear_llm, extraer_factura
from core.ingest import extraer_texto, listar_facturas
from core.schemas import Factura

# Campos que se comprueban en cada factura.
CAMPOS: tuple[str, ...] = (
    "numero",
    "fecha_expedicion",
    "fecha_operacion",
    "cif_emisor",
    "referencia_albaran",
    "lineas",
    "base_imponible",
    "cuota_iva",
    "retencion",
    "total",
)


# ---------------------------------------------------------------------------
# Verdad de referencia (desde el texto del PDF)
# ---------------------------------------------------------------------------
def _importe(texto: str, etiqueta: str) -> Decimal | None:
    """Lee un importe en formato español que sigue a una etiqueta."""
    encontrado = re.search(etiqueta + r"\s+(-?[\d.]+,\d{2})\s*€", texto)
    if encontrado is None:
        return None
    return Decimal(encontrado.group(1).replace(".", "").replace(",", "."))


def _fecha(texto: str, etiqueta: str) -> date | None:
    """Lee una fecha dd/mm/aaaa que sigue a una etiqueta."""
    encontrado = re.search(etiqueta + r":\s*(\d{2}/\d{2}/\d{4})", texto)
    if encontrado is None:
        return None
    return date(*map(int, encontrado.group(1).split("/")[::-1]))


def _lineas_factura(texto: str) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    """Extrae las líneas de la tabla: concepto, cantidad, precio e importe."""
    try:
        seccion = texto.split("CONCEPTO / DESCRIPCIÓN")[1].split("Base imponible")[0]
    except IndexError:
        return []

    filas = [linea.strip() for linea in seccion.splitlines() if linea.strip()]
    filas = [fila for fila in filas if fila not in ("CANT.", "PRECIO UNIT.", "IMPORTE")]

    lineas: list[tuple[str, Decimal, Decimal, Decimal]] = []
    for i in range(0, len(filas) - 3, 4):
        concepto, cantidad, precio, importe = filas[i : i + 4]
        if not cantidad.isdigit():
            continue
        lineas.append(
            (
                concepto,
                Decimal(cantidad),
                Decimal(precio.replace(".", "").replace(",", ".").replace(" €", "")),
                Decimal(importe.replace(".", "").replace(",", ".").replace(" €", "")),
            )
        )
    return lineas


def verdad_de_referencia(texto: str) -> dict[str, Any]:
    """Datos que se pueden leer del PDF sin usar el modelo."""
    numero = re.search(r"Nº: (.+)", texto)
    albaran = re.search(r"ALB-[A-Z]+-\d{4}-\d{3}", texto)
    cif = re.search(r"CIF: ?([A-Z]\d{8})", texto)
    # En el PDF la retención aparece en negativo; se guarda su magnitud.
    retencion = _importe(texto, r"Retención IRPF \(-15%\)")

    return {
        "numero": numero.group(1).strip() if numero else None,
        "fecha_expedicion": _fecha(texto, "Fecha de expedición"),
        "fecha_operacion": _fecha(texto, "Fecha de operación"),
        "cif_emisor": cif.group(1) if cif else None,
        "referencia_albaran": albaran.group(0) if albaran else None,
        "lineas": _lineas_factura(texto),
        "base_imponible": _importe(texto, "Base imponible"),
        "cuota_iva": _importe(texto, r"IVA \(21%\)"),
        "retencion": abs(retencion) if retencion is not None else None,
        "total": _importe(texto, "TOTAL FACTURA"),
    }


# ---------------------------------------------------------------------------
# Comparación
# ---------------------------------------------------------------------------
def _texto(valor: str) -> str:
    """Normaliza texto para comparar sin depender de espacios ni mayúsculas."""
    return " ".join(valor.split()).casefold()


def comparar(factura: Factura, verdad: dict[str, Any]) -> dict[str, bool]:
    """Indica, campo a campo, si la extracción coincide con el PDF."""
    lineas_extraidas = [
        (_texto(linea.concepto), linea.cantidad, linea.precio_unitario, linea.importe)
        for linea in factura.lineas
    ]
    lineas_pdf = [
        (_texto(concepto), cantidad, precio, importe)
        for concepto, cantidad, precio, importe in verdad["lineas"]
    ]

    return {
        "numero": factura.numero == verdad["numero"],
        "fecha_expedicion": factura.fecha_expedicion == verdad["fecha_expedicion"],
        "fecha_operacion": factura.fecha_operacion == verdad["fecha_operacion"],
        "cif_emisor": factura.proveedor.cif_normalizado == verdad["cif_emisor"],
        "referencia_albaran": factura.referencia_albaran == verdad["referencia_albaran"],
        "lineas": lineas_extraidas == lineas_pdf,
        "base_imponible": factura.base_imponible == verdad["base_imponible"],
        "cuota_iva": factura.cuota_iva == verdad["cuota_iva"],
        "retencion": factura.cuota_retencion == (verdad["retencion"] or Decimal(0)),
        "total": factura.total == verdad["total"],
    }


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------
def evaluar(
    rutas: list[Path], *, guardar: bool = True, llm: Any = None
) -> dict[str, Any]:
    """Extrae y evalúa cada factura; devuelve el resumen de la ejecución."""
    modelo = llm or crear_llm()
    if guardar:
        ajustes.directorio_procesados.mkdir(parents=True, exist_ok=True)

    aciertos_por_campo: dict[str, int] = dict.fromkeys(CAMPOS, 0)
    resultados: list[dict[str, Any]] = []
    correctas = 0
    inicio = time.perf_counter()

    for indice, ruta in enumerate(rutas, 1):
        texto = extraer_texto(ruta)
        verdad = verdad_de_referencia(texto)
        comienzo = time.perf_counter()

        try:
            factura = extraer_factura(texto, llm=modelo)
            estado = comparar(factura, verdad)
            fallos = [campo for campo, ok in estado.items() if not ok]
            correctas += 0 if fallos else 1
            for campo, ok in estado.items():
                aciertos_por_campo[campo] += int(ok)

            if guardar:
                destino = ajustes.directorio_procesados / f"{factura.identificador}.json"
                destino.write_text(factura.model_dump_json(indent=2) + "\n", encoding="utf-8")

            resultados.append(
                {
                    "fichero": ruta.name,
                    "numero": factura.numero,
                    "ok": not fallos,
                    "fallos": fallos,
                    "duracion_s": round(time.perf_counter() - comienzo, 2),
                }
            )
            marca = "OK   " if not fallos else "FALLO"
            detalle = "" if not fallos else f" -> {', '.join(fallos)}"
            print(f"[{indice:>2}/{len(rutas)}] {marca} {factura.numero:<14}{detalle}")

        except ErrorExtraccion as exc:
            resultados.append(
                {
                    "fichero": ruta.name,
                    "numero": verdad["numero"],
                    "ok": False,
                    "fallos": ["extraccion"],
                    "error": str(exc),
                    "duracion_s": round(time.perf_counter() - comienzo, 2),
                }
            )
            print(f"[{indice:>2}/{len(rutas)}] ERROR {ruta.name}: {exc}")

    duracion = time.perf_counter() - inicio
    resumen = {
        "modelo": ajustes.modelo,
        "facturas": len(rutas),
        "correctas": correctas,
        "precision_por_campo": {
            campo: round(aciertos / len(rutas), 3) for campo, aciertos in aciertos_por_campo.items()
        },
        "duracion_total_s": round(duracion, 2),
        "duracion_media_s": round(duracion / len(rutas), 2) if rutas else 0.0,
        "detalle": resultados,
    }

    if guardar:
        (ajustes.directorio_procesados / "_resumen.json").write_text(
            json.dumps(resumen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return resumen


def main() -> None:
    parser = argparse.ArgumentParser(description="Evalúa la extracción sobre el lote.")
    parser.add_argument("--limite", type=int, default=0, help="procesa solo las N primeras")
    parser.add_argument(
        "--sin-guardar", action="store_true", help="no escribe en datos/procesados"
    )
    args = parser.parse_args()

    rutas = listar_facturas(ajustes.directorio_facturas)
    if args.limite > 0:
        rutas = rutas[: args.limite]
    if not rutas:
        raise SystemExit(f"no hay facturas en {ajustes.directorio_facturas}")

    resumen = evaluar(rutas, guardar=not args.sin_guardar)

    print("\nResumen")
    print("-" * 40)
    print(f"facturas correctas: {resumen['correctas']}/{resumen['facturas']}")
    print("precisión por campo:")
    for campo, valor in resumen["precision_por_campo"].items():
        print(f"  {campo:<20} {valor:.0%}")
    print(
        f"tiempo: {resumen['duracion_total_s']} s "
        f"({resumen['duracion_media_s']} s por factura)"
    )
    if not args.sin_guardar:
        print(f"resultados en {ajustes.directorio_procesados}")


if __name__ == "__main__":
    main()
