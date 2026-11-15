"""Avisos cuando una factura necesita intervención humana.

Dos implementaciones de la misma interfaz:

- ``NotificadorConsola``: imprime el aviso en el terminal. Es el modo por
  defecto, el que se usa en las pruebas y el que sirve de red de seguridad si
  Telegram no está configurado.
- ``NotificadorTelegram``: ``httpx`` directo a la Bot API. Sin botones inline a
  propósito: la decisión se toma en el panel, donde están la factura y el
  albarán a la vista.

Principio de diseño: **notificar nunca interrumpe el procesamiento**. Si el
envío falla (sin red, token revocado, chat bloqueado), se informa por el
terminal y se devuelve ``False``; la factura sigue guardada en revisión y el
worker continúa con la siguiente.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from config import ajustes
from core.schemas import ResultadoValidacion

URL_PANEL = "http://localhost:8501"
URL_ENVIO = "https://api.telegram.org/bot{token}/sendMessage"


@dataclass(frozen=True)
class Aviso:
    """Contenido de un aviso, ya extraído de la base de datos y del estado."""

    proceso: str
    numero: str | None = None
    proveedor: str | None = None
    importe: float | None = None
    motivos: str = ""
    es_error: bool = False


class Notificador(Protocol):
    """Contrato mínimo: entregar un aviso e indicar si se pudo."""

    def enviar(self, aviso: Aviso) -> bool: ...


def _euros(valor: float | None) -> str:
    """Importe con coma decimal y punto de miles (``292,92 €``)."""
    if valor is None:
        return "—"
    texto = f"{valor:,.2f}".replace(",", "\u00a0")
    return texto.replace(".", ",") + " €"


def motivos_de(resultado: ResultadoValidacion | None) -> str:
    """Resumen de por qué una factura va a revisión.

    Se listan los códigos y no las frases largas: en el móvil lo que se lee de
    un vistazo es ``albaran_cantidad`` o ``cif_no_coincide``, con el detalle
    disponible en el panel.
    """
    if resultado is None:
        return "revisión manual"

    codigos = [incidencia.codigo.value for incidencia in resultado.incidencias]
    if resultado.supera_umbral:
        codigos.append("supera_umbral")
    return ", ".join(codigos) or "revisión manual"


def texto_aviso(aviso: Aviso) -> str:
    """Mensaje corto, en texto plano.

    Sin Markdown ni HTML a propósito: los CIF, números de factura y ficheros
    llevan guiones y guiones bajos que romperían el formateo de Telegram.
    """
    if aviso.es_error:
        return (
            "⚠️ Error al procesar una factura\n"
            f"Fichero: {aviso.proceso}\n"
            f"Motivo: {aviso.motivos}\n"
            f"Panel: {URL_PANEL}"
        )

    return (
        f"🧾 {aviso.numero or aviso.proceso} · {aviso.proveedor or 'sin datos'}\n"
        f"Importe: {_euros(aviso.importe)}\n"
        f"Motivo: {aviso.motivos}\n"
        f"Panel: {URL_PANEL}"
    )


class NotificadorConsola:
    """Imprime el aviso. Es el modo por defecto y el que usan las pruebas."""

    def enviar(self, aviso: Aviso) -> bool:
        print(texto_aviso(aviso), flush=True)
        return True


class NotificadorTelegram:
    """Envía el aviso por la Bot API de Telegram.

    El transporte se puede inyectar (``httpx.MockTransport``) para probar el
    envío sin red ni token reales.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transporte: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._transporte = transporte
        self._timeout = timeout

    def enviar(self, aviso: Aviso) -> bool:
        """Manda el aviso y devuelve ``True`` solo si Telegram lo aceptó."""
        url = URL_ENVIO.format(token=self._token)
        carga = {"chat_id": self._chat_id, "text": texto_aviso(aviso)}

        try:
            with httpx.Client(
                transport=self._transporte, timeout=self._timeout
            ) as cliente:
                respuesta = cliente.post(url, json=carga)
        except httpx.HTTPError as exc:
            # Sin red, DNS caído, timeout... el worker debe seguir.
            print(f"aviso no enviado ({type(exc).__name__}: {exc})", flush=True)
            return False

        if respuesta.status_code >= 400:
            detalle = respuesta.text[:200]
            print(
                f"aviso no enviado (HTTP {respuesta.status_code}: {detalle})",
                flush=True,
            )
            return False

        return True


def crear_notificador() -> Notificador:
    """Devuelve el notificador configurado, con reserva a consola.

    Si se pide Telegram pero faltan las credenciales, no se rompe la demo: se
    avisa por el terminal y se sigue.
    """
    if ajustes.notificador != "telegram":
        return NotificadorConsola()

    if not ajustes.telegram_token or not ajustes.telegram_chat_id:
        print(
            "AGENTE_NOTIFICADOR=telegram pero faltan token o chat_id: "
            "se avisa por consola.",
            flush=True,
        )
        return NotificadorConsola()

    return NotificadorTelegram(ajustes.telegram_token, ajustes.telegram_chat_id)
