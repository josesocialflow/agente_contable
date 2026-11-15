"""Pruebas de ingestión y extracción (fase 2).

No llaman a la API: el modelo se sustituye por un doble que devuelve respuestas
preparadas, para validar el flujo, los reintentos y el manejo de errores.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from config import ajustes
from core.extraction import ErrorExtraccion, extraer_factura, extraer_factura_con_uso
from core.ingest import extraer_texto, listar_facturas, tiene_texto
from core.schemas import Factura


def factura_extraida(**cambios: Any) -> dict:
    """Datos válidos mínimos que devolvería el modelo."""
    datos: dict = {
        "numero": "MD-2026-022",
        "fecha_expedicion": "2026-11-06",
        "fecha_operacion": "2026-10-28",
        "referencia_albaran": "ALB-MD-2026-022",
        "proveedor": {
            "nombre": "Consultoría y Marketing Digital S.L.",
            "cif": "B98765432",
        },
        "cliente": {"nombre": "Logística Ibérica S.L.", "cif": "B80000000"},
        "lineas": [
            {
                "concepto": "Gestión de campañas SEM",
                "cantidad": "1",
                "precio_unitario": "500.00",
                "importe": "500.00",
            }
        ],
        "base_imponible": "500.00",
        "tipo_iva": "21",
        "cuota_iva": "105.00",
        "tipo_retencion_irpf": "15",
        "total": "530.00",
    }
    datos.update(cambios)
    return datos


def salida_valida() -> dict[str, Any]:
    """Respuesta del modelo que valida a la primera."""
    return {
        "raw": AIMessage(content="{}"),
        "parsed": Factura.model_validate(factura_extraida()),
        "parsing_error": None,
    }


def salida_con_uso(entrada: int = 100, salida: int = 20) -> dict[str, Any]:
    """Respuesta válida que además informa del consumo de tokens."""
    resultado = salida_valida()
    resultado["raw"] = AIMessage(
        content="{}",
        usage_metadata={
            "input_tokens": entrada,
            "output_tokens": salida,
            "total_tokens": entrada + salida,
        },
    )
    return resultado


def salida_invalida_con_uso(entrada: int = 40, salida: int = 10) -> dict[str, Any]:
    """Respuesta que no cumple el esquema, pero que el proveedor cobró igual."""
    resultado = salida_invalida()
    resultado["raw"] = AIMessage(
        content="respuesta incompleta",
        usage_metadata={
            "input_tokens": entrada,
            "output_tokens": salida,
            "total_tokens": entrada + salida,
        },
    )
    return resultado


def salida_invalida(mensaje: str = "esquema inválido") -> dict[str, Any]:
    """Respuesta del modelo que no cumple el esquema."""
    return {
        "raw": AIMessage(content="respuesta incompleta"),
        "parsed": None,
        "parsing_error": ValueError(mensaje),
    }


def salida_descuadrada() -> dict[str, Any]:
    """Respuesta válida para el esquema pero con las líneas mal sumadas."""
    factura = Factura.model_validate(
        factura_extraida(
            lineas=[
                {
                    "concepto": "Base imponible",
                    "cantidad": "0",
                    "precio_unitario": "0.00",
                    "importe": "10.00",
                }
            ]
        )
    )
    return {"raw": AIMessage(content="{}"), "parsed": factura, "parsing_error": None}


class ModeloFalso:
    """Doble de ChatOpenAI que devuelve salidas preparadas en orden.

    Args:
        salidas: respuestas que devolverá, en orden.
        fallar_veces: número de llamadas iniciales que lanzarán un
            ``ValidationError`` real antes de empezar a devolver ``salidas``.
    """

    def __init__(self, salidas: list[dict[str, Any]], fallar_veces: int = 0) -> None:
        self._salidas = salidas
        self._fallar_veces = fallar_veces
        self.llamadas = 0
        self.mensajes_recibidos: list[list[Any]] = []

    def with_structured_output(self, schema: Any, *, include_raw: bool = False) -> Any:
        assert include_raw, "la extracción debe pedir include_raw para reintentar"

        def responder(mensajes: list[Any]) -> dict[str, Any]:
            self.mensajes_recibidos.append(list(mensajes))
            self.llamadas += 1
            if self.llamadas <= self._fallar_veces:
                # Provoca el mismo error que el SDK al validar la respuesta: se
                # valida una factura incompleta para que lance de verdad.
                Factura.model_validate({"numero": "incompleta"})
                raise AssertionError("la factura incompleta debería haber fallado")
            indice = min(self.llamadas - 1 - self._fallar_veces, len(self._salidas) - 1)
            return self._salidas[indice]

        return RunnableLambda(responder)


# ---------------------------------------------------------------------------
# Ingestión
# ---------------------------------------------------------------------------
def test_extraer_texto_de_un_pdf() -> None:
    ruta = listar_facturas(ajustes.directorio_facturas)[0]
    texto = extraer_texto(ruta)
    assert "FACTURA" in texto.upper()
    assert "ALBARÁN FACTURADO" in texto


def test_listar_facturas_encuentra_el_lote() -> None:
    assert len(listar_facturas(ajustes.directorio_facturas)) == 20


def test_extraer_texto_de_fichero_inexistente() -> None:
    with pytest.raises(FileNotFoundError):
        extraer_texto(ajustes.directorio_facturas / "no-existe.pdf")


def test_tiene_texto_detecta_pdf_sin_capa_de_texto() -> None:
    assert tiene_texto("x" * 200)
    assert not tiene_texto("")
    assert not tiene_texto("corto")


# ---------------------------------------------------------------------------
# Extracción
# ---------------------------------------------------------------------------
def test_extraccion_correcta() -> None:
    modelo = ModeloFalso([salida_valida()])

    factura = extraer_factura("texto de prueba", llm=modelo)

    assert factura.numero == "MD-2026-022"
    assert factura.total == Decimal("530.00")
    assert modelo.llamadas == 1


def test_reintenta_cuando_el_modelo_no_cumple_el_esquema() -> None:
    modelo = ModeloFalso([salida_invalida(), salida_valida()])

    factura = extraer_factura("texto de prueba", llm=modelo)

    assert factura.total == Decimal("530.00")
    assert modelo.llamadas == 2
    # En el segundo intento se añaden la respuesta fallida y la corrección.
    assert len(modelo.mensajes_recibidos[1]) > len(modelo.mensajes_recibidos[0])


def test_reintenta_si_la_validacion_lanza_excepcion() -> None:
    """El SDK valida antes de devolver: el error llega como excepción."""
    modelo = ModeloFalso([salida_valida()], fallar_veces=1)

    factura = extraer_factura("texto de prueba", llm=modelo)

    assert factura.numero == "MD-2026-022"
    assert modelo.llamadas == 2


def test_reintenta_si_las_lineas_no_suman_la_base() -> None:
    modelo = ModeloFalso([salida_descuadrada(), salida_valida()])

    factura = extraer_factura("texto de prueba", llm=modelo)

    assert factura.base_imponible == Decimal("500.00")
    assert modelo.llamadas == 2


def test_si_nunca_cuadra_devuelve_la_ultima_lectura() -> None:
    """La validación determinista decidirá; aquí no se pierde la factura."""
    modelo = ModeloFalso([salida_descuadrada()])

    factura = extraer_factura("texto de prueba", llm=modelo)

    assert factura.base_imponible == Decimal("500.00")
    assert modelo.llamadas == 1 + ajustes.reintentos_extraccion


def test_falla_tras_agotar_los_reintentos() -> None:
    modelo = ModeloFalso([salida_invalida()])

    with pytest.raises(ErrorExtraccion):
        extraer_factura("texto de prueba", llm=modelo)

    assert modelo.llamadas == 1 + ajustes.reintentos_extraccion


# ---------------------------------------------------------------------------
# Consumo de tokens (fase 7)
# ---------------------------------------------------------------------------
def test_devuelve_los_tokens_de_la_extraccion() -> None:
    modelo = ModeloFalso([salida_con_uso(entrada=1_500, salida=320)])

    extraccion = extraer_factura_con_uso("texto de prueba", llm=modelo)

    assert extraccion.factura.numero == "MD-2026-022"
    assert extraccion.tokens_entrada == 1_500
    assert extraccion.tokens_salida == 320
    assert extraccion.intentos == 1


def test_acumula_los_tokens_de_los_reintentos() -> None:
    """Cada llamada se cobra: si hubo dos, el coste es la suma de las dos."""
    modelo = ModeloFalso(
        [salida_invalida_con_uso(40, 10), salida_con_uso(100, 20)]
    )

    extraccion = extraer_factura_con_uso("texto de prueba", llm=modelo)

    assert extraccion.intentos == 2
    assert extraccion.tokens_entrada == 140
    assert extraccion.tokens_salida == 30


def test_sin_usage_metadata_el_consumo_es_cero() -> None:
    modelo = ModeloFalso([salida_valida()])

    extraccion = extraer_factura_con_uso("texto de prueba", llm=modelo)

    assert extraccion.tokens_entrada == 0
    assert extraccion.tokens_salida == 0
