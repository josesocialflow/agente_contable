"""Lectura de los PDF de factura.

De momento solo cubre el caso de la demo: PDF digitales de los que se puede
extraer el texto. La detección de escaneos (PDF sin capa de texto) queda
preparada con ``tiene_texto`` para cuando se aborde el OCR.
"""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


def extraer_texto(ruta: Path) -> str:
    """Devuelve el texto de todas las páginas de un PDF.

    Args:
        ruta: fichero PDF a leer.

    Returns:
        Texto de las páginas separado por líneas en blanco.

    Raises:
        FileNotFoundError: si el fichero no existe.
    """
    if not ruta.is_file():
        raise FileNotFoundError(f"no existe el PDF: {ruta}")

    lector = PdfReader(str(ruta))
    paginas = [(pagina.extract_text() or "").strip() for pagina in lector.pages]
    return "\n\n".join(pagina for pagina in paginas if pagina)


def listar_facturas(directorio: Path) -> list[Path]:
    """Lista los PDF de un directorio en orden alfabético."""
    return sorted(directorio.glob("*.pdf"))


def tiene_texto(texto: str, minimo: int = 100) -> bool:
    """Heurística para distinguir un PDF digital de un escaneo.

    Un PDF escaneado no tiene capa de texto (o tiene muy poca), así que el
    extractor no podría trabajar con él sin OCR.
    """
    return len(texto.strip()) >= minimo
