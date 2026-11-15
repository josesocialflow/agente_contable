"""Pruebas de las métricas de uso (fase 7).

Se comprueba el coste de extracción —lo que cuesta *ejecutar el agente*— y no el
importe de las facturas, que es otra cosa y no pasa por aquí.
"""

from __future__ import annotations

import pytest

from config import ajustes
from core import metrics


def test_coste_de_una_extraccion() -> None:
    # 1.000 tokens de entrada a 0,15 $/M y 200 de salida a 0,60 $/M.
    esperado = (1_000 * 0.15 + 200 * 0.60) / 1_000_000

    assert metrics.coste_extraccion(1_000, 200) == pytest.approx(esperado)


def test_coste_cero_sin_consumo() -> None:
    assert metrics.coste_extraccion(0, 0) == 0.0


def test_el_coste_crece_con_los_tokens() -> None:
    assert metrics.coste_extraccion(10_000, 0) > metrics.coste_extraccion(1_000, 0)


def test_formatear_coste_con_coma_decimal() -> None:
    assert metrics.formatear_coste(0.00051) == "0,0005 $"
    assert metrics.formatear_coste(0.0) == "0,0000 $"


def test_formatear_tokens_con_punto_de_miles() -> None:
    assert metrics.formatear_tokens(2350) == "2.350"
    assert metrics.formatear_tokens(0) == "0"


def test_formatear_duracion() -> None:
    assert metrics.formatear_duracion(850) == "850 ms"
    assert metrics.formatear_duracion(1_800) == "1,8 s"


def test_resumir_suma_las_columna_de_uso() -> None:
    procesos = [
        {"tokens_entrada": 1_000, "tokens_salida": 200, "coste": 0.5, "duracion_ms": 900},
        {"tokens_entrada": 2_000, "tokens_salida": 300, "coste": 0.25, "duracion_ms": 1_100},
    ]

    uso = metrics.resumir(procesos)

    assert uso.facturas == 2
    assert uso.tokens == 3_500
    assert uso.coste == pytest.approx(0.75)
    assert uso.duracion_ms == 2_000


def test_resumir_ignora_los_procesos_sin_extraer() -> None:
    uso = metrics.resumir([{"tokens_entrada": None, "estado": "encolado"}])

    assert uso.facturas == 0
    assert uso.tokens == 0
    assert uso.coste == 0.0


def test_la_tarifa_sale_de_la_configuracion() -> None:
    esperado = (1_000_000 * ajustes.precio_entrada_por_millon) / 1_000_000

    assert metrics.coste_extraccion(1_000_000, 0) == pytest.approx(esperado)
