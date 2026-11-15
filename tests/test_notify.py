"""Pruebas de los avisos (fase 7).

El envío a Telegram se prueba con ``httpx.MockTransport``: se ejercita el mismo
código que en producción (URL, cuerpo JSON, códigos de estado) sin red, sin
token y sin mandar mensajes de verdad.
"""

from __future__ import annotations

import json

import httpx
import pytest

from config import ajustes
from core.notify import (
    URL_PANEL,
    Aviso,
    NotificadorConsola,
    NotificadorTelegram,
    crear_notificador,
    motivos_de,
    texto_aviso,
)
from core.schemas import Incidencia, ResultadoValidacion, TipoIncidencia


def _transporte(
    codigo: int = 200, capturadas: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    """Transporte falso que responde siempre con el código indicado."""

    def manejar(peticion: httpx.Request) -> httpx.Response:
        if capturadas is not None:
            capturadas.append(peticion)
        return httpx.Response(codigo, json={"ok": codigo < 400})

    return httpx.MockTransport(manejar)


def _transporte_roto() -> httpx.MockTransport:
    """Transporte que simula que no hay red."""

    def manejar(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red", request=peticion)

    return httpx.MockTransport(manejar)


def _resultado(*codigos: TipoIncidencia, umbral: bool = False) -> ResultadoValidacion:
    return ResultadoValidacion(
        factura_id="B12345678-TEST-2026",
        incidencias=[
            Incidencia(codigo=codigo, mensaje=f"incidencia {codigo.value}")
            for codigo in codigos
        ],
        supera_umbral=umbral,
    )


# ---------------------------------------------------------------------------
# Texto del aviso
# ---------------------------------------------------------------------------
def test_texto_de_revision_lleva_los_datos_clave() -> None:
    aviso = Aviso(
        proceso="Factura_21",
        numero="FAC-2026-021",
        proveedor="Suministros de Oficina Global S.A.",
        importe=292.92,
        motivos="albaran_cantidad",
    )

    texto = texto_aviso(aviso)

    assert "FAC-2026-021" in texto
    assert "Suministros de Oficina Global S.A." in texto
    assert "292,92 €" in texto
    assert "albaran_cantidad" in texto
    assert URL_PANEL in texto
    # Texto plano: nada de Markdown que Telegram pudiera interpretar.
    assert "*" not in texto and "<b>" not in texto


def test_texto_de_error() -> None:
    aviso = Aviso(proceso="Factura_99", motivos="ErrorExtraccion: sin JSON", es_error=True)

    texto = texto_aviso(aviso)

    assert "Error" in texto
    assert "Factura_99" in texto
    assert "ErrorExtraccion: sin JSON" in texto


def test_motivos_lista_los_codigos() -> None:
    resultado = _resultado(TipoIncidencia.ALBARAN_CANTIDAD, TipoIncidencia.CIF_NO_COINCIDE)

    assert motivos_de(resultado) == "albaran_cantidad, cif_no_coincide"


def test_motivos_incluye_el_umbral() -> None:
    assert motivos_de(_resultado(umbral=True)) == "supera_umbral"


def test_motivos_sin_resultado() -> None:
    assert motivos_de(None) == "revisión manual"


# ---------------------------------------------------------------------------
# Consola
# ---------------------------------------------------------------------------
def test_consola_imprime_y_confirma(capsys: pytest.CaptureFixture[str]) -> None:
    notificador = NotificadorConsola()

    assert notificador.enviar(Aviso(proceso="p1", numero="FAC-1")) is True
    assert "FAC-1" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def test_telegram_envia_el_mensaje() -> None:
    capturadas: list[httpx.Request] = []
    notificador = NotificadorTelegram(
        "123456:token-de-prueba", "987654321", transporte=_transporte(200, capturadas)
    )

    assert notificador.enviar(Aviso(proceso="p1", numero="FAC-1", importe=10.5)) is True

    assert len(capturadas) == 1
    peticion = capturadas[0]
    assert "bot123456:token-de-prueba/sendMessage" in str(peticion.url)
    cuerpo = json.loads(peticion.content)
    assert cuerpo["chat_id"] == "987654321"
    assert "FAC-1" in cuerpo["text"]


def test_telegram_devuelve_false_si_la_api_rechaza(
    capsys: pytest.CaptureFixture[str],
) -> None:
    notificador = NotificadorTelegram("123:token", "987", transporte=_transporte(400))

    assert notificador.enviar(Aviso(proceso="p1")) is False
    assert "HTTP 400" in capsys.readouterr().out


def test_telegram_devuelve_false_sin_red(capsys: pytest.CaptureFixture[str]) -> None:
    """Sin red el worker no debe caerse: solo se anota y se sigue."""
    notificador = NotificadorTelegram("123:token", "987", transporte=_transporte_roto())

    assert notificador.enviar(Aviso(proceso="p1")) is False
    assert "ConnectError" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Fábrica
# ---------------------------------------------------------------------------
def test_fabrica_devuelve_consola_por_defecto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ajustes, "notificador", "consola")

    assert isinstance(crear_notificador(), NotificadorConsola)


def test_fabrica_devuelve_telegram_si_esta_configurado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ajustes, "notificador", "telegram")
    monkeypatch.setattr(ajustes, "telegram_token", "123:token")
    monkeypatch.setattr(ajustes, "telegram_chat_id", "987")

    assert isinstance(crear_notificador(), NotificadorTelegram)


def test_fabrica_cae_a_consola_si_faltan_credenciales(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ajustes, "notificador", "telegram")
    monkeypatch.setattr(ajustes, "telegram_token", None)
    monkeypatch.setattr(ajustes, "telegram_chat_id", None)

    assert isinstance(crear_notificador(), NotificadorConsola)
    assert "faltan token o chat_id" in capsys.readouterr().out
