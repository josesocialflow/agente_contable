"""Métricas de uso del agente: coste de extracción y duración.

Ojo con el vocabulario, porque es fácil confundirse: aquí se mide lo que cuesta
**ejecutar el agente** (los tokens de la llamada al modelo por su tarifa), no lo
que se paga a los proveedores. El importe de cada factura vive en
``procesos.importe`` y no se toca en este módulo.

Solo la extracción gasta: la validación fiscal, el cruce con el albarán y la
aritmética son código determinista y cuestan cero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config import ajustes


def coste_extraccion(tokens_entrada: int, tokens_salida: int) -> float:
    """Coste estimado en dólares de una extracción.

    Args:
        tokens_entrada: tokens del prompt y del texto del PDF, sumando reintentos.
        tokens_salida: tokens del JSON devuelto, sumando reintentos.

    Returns:
        Dólares. Es una estimación: refleja la tarifa configurada, no la factura
        real de OpenAI (que puede aplicar descuentos por prompt caching).
    """
    return (
        tokens_entrada * ajustes.precio_entrada_por_millon
        + tokens_salida * ajustes.precio_salida_por_millon
    ) / 1_000_000


def formatear_coste(valor: float) -> str:
    """Coste con cuatro decimales y coma decimal (``0,0005 $``)."""
    texto = f"{valor:,.4f}".replace(",", "\u00a0")  # separador de miles
    return texto.replace(".", ",") + " $"


def formatear_tokens(tokens: int) -> str:
    """Cantidad de tokens con punto de miles (``2.350``)."""
    return f"{tokens:,}".replace(",", ".")


def formatear_duracion(milisegundos: int) -> str:
    """Duración legible: ``850 ms`` o ``1,8 s``."""
    if milisegundos < 1000:
        return f"{milisegundos} ms"
    return f"{milisegundos / 1000:.1f} s".replace(".", ",")


@dataclass(frozen=True)
class Uso:
    """Totales de uso del lote."""

    facturas: int
    tokens_entrada: int
    tokens_salida: int
    coste: float
    duracion_ms: int

    @property
    def tokens(self) -> int:
        """Tokens totales, sumando entrada y salida."""
        return self.tokens_entrada + self.tokens_salida


def resumir(procesos: list[dict[str, Any]]) -> Uso:
    """Suma tokens, coste y duración de una lista de procesos.

    Acepta las filas tal cual salen de ``store.listar_procesos``; las que no
    llegaron a extraerse (o vienen de una base antigua) suman cero.
    """
    return Uso(
        facturas=sum(1 for proceso in procesos if proceso.get("tokens_entrada") is not None),
        tokens_entrada=sum(int(proceso.get("tokens_entrada") or 0) for proceso in procesos),
        tokens_salida=sum(int(proceso.get("tokens_salida") or 0) for proceso in procesos),
        coste=sum(float(proceso.get("coste") or 0.0) for proceso in procesos),
        duracion_ms=sum(int(proceso.get("duracion_ms") or 0) for proceso in procesos),
    )
