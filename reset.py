"""Reinicia el estado derivado de la demo del agente contable.

Borra únicamente lo que genera el agente (base de datos de procesos, checkpoints,
remesa simulada y extracciones), dejando intacto el "ERP" simulado: facturas,
albaranes y maestro de proveedores.

Es idempotente: si algún artefacto todavía no existe, no hace nada y continúa.
Por eso puede ejecutarse desde el primer día, antes de que exista el agente.

Uso:
    python reset.py                 # muestra lo que borrará y pide confirmación
    python reset.py --si            # sin preguntar (entre tomas del vídeo)
    python reset.py --datos         # además regenera facturas y albaranes
    python reset.py --datos --fecha-base 2026-11-14
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DIRECTORIO_SCRIPT = Path(__file__).resolve().parent
DIRECTORIO_DATOS = DIRECTORIO_SCRIPT / "datos"
GENERADOR = DIRECTORIO_SCRIPT / "gen_factu.py"

# Artefactos que produce el agente. Se borran siempre.
ESTADO_DERIVADO: tuple[Path, ...] = (
    DIRECTORIO_DATOS / "agente.db",           # procesos, eventos y comandos
    DIRECTORIO_DATOS / "checkpoints.sqlite",  # estado del grafo (si va aparte)
    DIRECTORIO_DATOS / "pagos.json",          # remesa simulada
    DIRECTORIO_DATOS / "registro_facturas.json",  # facturas anotadas en el ERP
    DIRECTORIO_DATOS / "procesados",          # JSON extraído de cada factura
)

# Datos del ERP simulado. Solo se tocan con --datos, y solo para regenerarlos.
DIRECTORIO_FRAS = DIRECTORIO_DATOS / "fras"
ALBARANES_JSON = DIRECTORIO_DATOS / "albaranes" / "albaranes.json"


def _tamano(ruta: Path) -> int:
    """Tamaño en bytes de un fichero o de todo el árbol de un directorio."""
    if ruta.is_file():
        return ruta.stat().st_size
    return sum(archivo.stat().st_size for archivo in ruta.rglob("*") if archivo.is_file())


def _formato_tamano(bytes_: int) -> str:
    """Presenta el tamaño en unidades legibles, con coma decimal."""
    if bytes_ < 1024:
        return f"{bytes_} B"
    if bytes_ < 1024 * 1024:
        return f"{bytes_ / 1024:.1f}".replace(".", ",") + " KB"
    return f"{bytes_ / (1024 * 1024):.1f}".replace(".", ",") + " MB"


def _describir(ruta: Path) -> str:
    """Describe brevemente un artefacto para el listado previo."""
    if not ruta.exists():
        return "no existe"
    if ruta.is_dir():
        ficheros = sum(1 for _ in ruta.rglob("*") if _.is_file())
        return f"{ficheros} ficheros, {_formato_tamano(_tamano(ruta))}"
    return _formato_tamano(_tamano(ruta))


def _dentro_de_datos(ruta: Path) -> bool:
    """Salvaguarda: solo se borra dentro de ``datos/``."""
    try:
        return ruta.resolve().is_relative_to(DIRECTORIO_DATOS.resolve())
    except OSError:
        return False


def _borrar(ruta: Path) -> None:
    """Borra un fichero o un árbol de directorios, sin fallar si no existe."""
    if not ruta.exists():
        return
    if not _dentro_de_datos(ruta):
        raise RuntimeError(f"ruta fuera de datos/, se cancela: {ruta}")
    if ruta.is_dir():
        shutil.rmtree(ruta, ignore_errors=True)
    else:
        ruta.unlink(missing_ok=True)


def _recolectar(con_datos: bool) -> list[Path]:
    """Devuelve las rutas existentes que se van a eliminar."""
    objetivos = [ruta for ruta in ESTADO_DERIVADO if ruta.exists()]
    if con_datos:
        objetivos.extend(sorted(DIRECTORIO_FRAS.glob("*.pdf")))
        if ALBARANES_JSON.exists():
            objetivos.append(ALBARANES_JSON)
    return objetivos


def _confirmar(con_datos: bool) -> bool:
    """Pide confirmación explícita al usuario."""
    try:
        respuesta = input("\n¿Continuar? [s/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return respuesta in {"s", "si", "sí", "y", "yes"}


def _regenerar(fecha_base: str | None) -> int:
    """Vuelve a generar facturas y albaranes con el generador del proyecto."""
    if not GENERADOR.exists():
        print(f"No se encuentra el generador: {GENERADOR}")
        return 1
    comando = [sys.executable, str(GENERADOR)]
    if fecha_base:
        comando += ["--fecha-base", fecha_base]
    # Se vacía el buffer antes de lanzar el subproceso: si la salida va por una
    # tubería, el hijo escribiría antes que el padre y el orden se vería raro.
    sys.stdout.flush()
    print("\nRegenerando datos:", " ".join(comando))
    sys.stdout.flush()
    return subprocess.run(comando, check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reinicia el estado derivado de la demo sin tocar los datos fuente."
    )
    parser.add_argument(
        "--si",
        action="store_true",
        help="no pedir confirmación (pensado para encadenar comandos)",
    )
    parser.add_argument(
        "--datos",
        action="store_true",
        help="borrar y regenerar también facturas y albaranes",
    )
    parser.add_argument(
        "--fecha-base",
        metavar="AAAA-MM-DD",
        help="fecha base que se pasa a gen_factu.py al regenerar (requiere --datos)",
    )
    args = parser.parse_args()

    if args.fecha_base and not args.datos:
        parser.error("--fecha-base solo tiene sentido junto con --datos")

    objetivos = _recolectar(args.datos)

    print("Estado derivado que se eliminará:")
    for ruta in ESTADO_DERIVADO:
        print(f"  {ruta.relative_to(DIRECTORIO_SCRIPT)!s:<32} {_describir(ruta)}")

    if args.datos:
        pdfs = list(DIRECTORIO_FRAS.glob("*.pdf"))
        print("\nTambién se regenerarán los datos fuente:")
        print(f"  {'datos/fras/*.pdf':<32} {len(pdfs)} ficheros")
        print(f"  {'datos/albaranes/albaranes.json':<32} {_describir(ALBARANES_JSON)}")
    else:
        print("\nSe conservan los datos fuente:")
        print(f"  {'datos/fras/*.pdf':<32} {len(list(DIRECTORIO_FRAS.glob('*.pdf')))} ficheros")
        print(f"  {'datos/albaranes/albaranes.json':<32} {'intacto' if ALBARANES_JSON.exists() else 'no existe'}")
        print(f"  {'datos/maestro_proveedores.json':<32} intacto")

    if not objetivos:
        print("\nNada que borrar: el estado ya está limpio.")
        if not args.datos:
            return 0
    else:
        if not args.si and not _confirmar(args.datos):
            print("Cancelado.")
            return 1
        for ruta in objetivos:
            _borrar(ruta)
        print(f"\nEliminados {len(objetivos)} elementos.")

    codigo = 0
    if args.datos:
        codigo = _regenerar(args.fecha_base)
        if codigo != 0:
            return codigo

    print("\nListo. Reinicia worker.py y recarga el panel de Streamlit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
