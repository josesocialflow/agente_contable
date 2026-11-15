"""Extracción estructurada de la factura con el modelo de lenguaje.

Es el único punto del proyecto donde interviene un LLM: convierte el texto del
PDF en un objeto ``Factura`` validado. Todo lo que viene después (validación
fiscal, cruce con el albarán, aritmética) es código determinista.

Si el modelo devuelve algo que no cumple el esquema, se reintenta con el error
como feedback; si se agotan los intentos, se lanza ``ErrorExtraccion``.

Además de la factura se devuelve el consumo del modelo (tokens de entrada y
salida), que es lo único del proyecto que cuesta dinero: con eso se calcula el
coste de extracción que muestra el panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from config import ajustes
from core.ingest import extraer_texto
from core.schemas import Factura, redondear_euros

PROMPT_SISTEMA = """\
Eres un asistente experto en facturación española. Recibes el texto de una \
factura y devuelves sus datos con el esquema indicado.

Reglas:
- Expresa importes y cantidades como número JSON (por ejemplo 91.76): sin 
símbolo de euro, sin separador de miles y con punto decimal. No recalcules ni 
redondees los importes.
- En "lineas" van SOLO las filas de detalle de la tabla. No incluyas las filas 
de totales (Base imponible, IVA, Total factura) ni la forma de pago.
- La suma de los importes de las líneas debe coincidir con la base imponible.
- Las fechas van en formato ISO (AAAA-MM-DD). "Fecha de operación" es la del 
albarán cuando aparece; si no, null.
- "referencia_albaran" es el número que empieza por ALB, el del recuadro \
"ALBARÁN FACTURADO".
- "proveedor" es quien emite la factura; "cliente" es el receptor (Logística \
Ibérica S.L.).
- Incluye TODAS las líneas de la tabla, en su orden, con cantidad, precio \
unitario e importe.
- "tipo_retencion_irpf" es el porcentaje de retención si aparece (por ejemplo \
15); 0 si no hay retención.
- "tipo_iva" es el porcentaje de IVA aplicado (21 en este lote).
- Si un dato opcional no aparece, déjalo en null. No inventes valores.
"""


class ModeloConEsquema(Protocol):
    """Contrato mínimo que necesita la extracción.

    Lo cumple ``ChatOpenAI`` y también los dobles de prueba, que no heredan de
    ``BaseChatModel`` pero sí implementan este método.
    """

    def with_structured_output(
        self, schema: Any, *, include_raw: bool = False
    ) -> Any: ...


class ErrorExtraccion(RuntimeError):
    """No se pudo obtener una ``Factura`` válida tras agotar los reintentos."""


@dataclass(frozen=True)
class Extraccion:
    """Resultado de la extracción: la factura y lo que costó obtenerla.

    Los tokens los informa el proveedor en cada respuesta (``usage_metadata``).
    Se acumulan entre reintentos: si una factura necesitó dos llamadas, el coste
    real es el de las dos, no el de la última.
    """

    factura: Factura
    tokens_entrada: int = 0
    tokens_salida: int = 0
    intentos: int = 1


def _consumo(crudo: Any) -> tuple[int, int]:
    """Tokens de entrada y salida que informó el modelo en esa llamada.

    ``usage_metadata`` es opcional: los dobles de las pruebas no lo traen, y
    algunos proveedores compatibles con la API de OpenAI tampoco, así que se
    asume cero en lugar de fallar.
    """
    uso = getattr(crudo, "usage_metadata", None) or {}
    return int(uso.get("input_tokens", 0)), int(uso.get("output_tokens", 0))


def _mensaje_correccion(error: str) -> str:
    """Instrucción que se envía al modelo tras una respuesta no válida."""
    detalle = error if len(error) < 600 else error[:600] + "…"
    return (
        "La respuesta anterior no cumple el esquema de la factura: "
        f"{detalle}. Devuélvela de nuevo corrigiendo ese problema y sin añadir "
        "campos. Recuerda: importes y cantidades son números JSON (91.76), sin "
        "símbolo € ni separador de miles; las fechas, AAAA-MM-DD."
    )


def crear_llm() -> BaseChatModel:
    """Crea el modelo configurado (por defecto ``gpt-4o-mini``)."""
    return ChatOpenAI(model=ajustes.modelo, temperature=ajustes.temperatura)


def _cuadra(factura: Factura) -> bool:
    """Comprueba que la suma de las líneas coincide con la base imponible.

    Es el error más frecuente del modelo en estas facturas: colar las filas de
    totales como si fueran líneas de detalle. Detectarlo aquí permite reintentar
    antes de que la factura llegue a la validación.
    """
    suma = redondear_euros(sum((linea.importe for linea in factura.lineas), Decimal(0)))
    return suma == redondear_euros(factura.base_imponible)


def extraer_factura_con_uso(
    texto: str, *, llm: ModeloConEsquema | None = None
) -> Extraccion:
    """Convierte el texto de una factura en una ``Extraccion`` validada.

    Args:
        texto: contenido textual del PDF.
        llm: modelo a usar; se inyecta en las pruebas para no llamar a la API.

    Returns:
        La factura extraída, los tokens consumidos y cuántos intentos hicieron
        falta.

    Raises:
        ErrorExtraccion: si tras todos los reintentos el modelo no devuelve un
            JSON que cumpla el esquema.
    """
    modelo = llm or crear_llm()
    estructurado = modelo.with_structured_output(Factura, include_raw=True)

    mensajes: list[BaseMessage] = [
        SystemMessage(content=PROMPT_SISTEMA),
        HumanMessage(content=f"Texto de la factura:\n\n{texto}"),
    ]
    intentos = 1 + max(0, ajustes.reintentos_extraccion)
    ultimo_error: Any = None
    ultima_factura: Factura | None = None
    tokens_entrada = 0
    tokens_salida = 0
    intentos_realizados = 0

    for _ in range(intentos):
        intentos_realizados += 1
        try:
            # `include_raw=True` devuelve un dict, pero los stubs declaran la
            # unión con BaseModel; el cast deja el tipo claro para el analizador.
            salida = cast(dict[str, Any], estructurado.invoke(mensajes))
        except ValidationError as exc:
            # El SDK valida la respuesta contra el esquema antes de devolverla,
            # así que un importe mal formateado llega como excepción y no como
            # `parsing_error`. Se reintenta con la corrección como feedback.
            ultimo_error = exc
            mensajes.append(HumanMessage(content=_mensaje_correccion(str(exc))))
            continue

        # La llamada se cobró aunque la respuesta no sirva: se suma siempre.
        entrada_llamada, salida_llamada = _consumo(salida.get("raw"))
        tokens_entrada += entrada_llamada
        tokens_salida += salida_llamada

        if salida.get("parsed") is not None:
            factura: Factura = salida["parsed"]
            if _cuadra(factura):
                return Extraccion(
                    factura, tokens_entrada, tokens_salida, intentos_realizados
                )

            # Cuadra el esquema pero no las cuentas: se reintenta indicándolo.
            ultima_factura = factura
            suma = redondear_euros(
                sum((linea.importe for linea in factura.lineas), Decimal(0))
            )
            ultimo_error = (
                f"la suma de las líneas ({suma} €) no coincide con la base "
                f"imponible ({factura.base_imponible} €)"
            )
            crudo = salida.get("raw")
            if isinstance(crudo, AIMessage):
                mensajes.append(crudo)
            mensajes.append(HumanMessage(content=_mensaje_correccion(str(ultimo_error))))
            continue

        # El modelo respondió algo que no encaja con el esquema: se le devuelve
        # su propia respuesta junto al error para que la corrija.
        ultimo_error = salida.get("parsing_error")
        crudo = salida.get("raw")
        if isinstance(crudo, AIMessage):
            mensajes.append(crudo)
        mensajes.append(
            HumanMessage(content=_mensaje_correccion(str(ultimo_error)))
        )

    if ultima_factura is not None:
        # Se devuelve la última lectura aunque no cuadre: la validación
        # determinista la marcará para revisión en lugar de perderla.
        return Extraccion(
            ultima_factura, tokens_entrada, tokens_salida, intentos_realizados
        )

    raise ErrorExtraccion(
        f"el modelo no devolvió una factura válida tras {intentos} intento(s): {ultimo_error}"
    )


def extraer_factura(texto: str, *, llm: ModeloConEsquema | None = None) -> Factura:
    """Igual que ``extraer_factura_con_uso``, pero devuelve solo la factura.

    Atajo para los scripts de evaluación, que no miran el consumo.
    """
    return extraer_factura_con_uso(texto, llm=llm).factura


def extraer_factura_de_pdf(
    ruta: Path, *, llm: ModeloConEsquema | None = None
) -> Factura:
    """Lee un PDF y devuelve la factura extraída."""
    return extraer_factura(extraer_texto(ruta), llm=llm)


if __name__ == "__main__":
    # Comprobación de la fase 2: extrae una factura y contrasta los datos con
    # el propio PDF. Uso: python -m core.extraction [ruta.pdf]
    import re
    import sys
    from decimal import Decimal

    from core.ingest import listar_facturas

    ruta = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else listar_facturas(ajustes.directorio_facturas)[0]
    )
    texto = extraer_texto(ruta)
    factura = extraer_factura(texto)

    print(f"Factura extraída de {ruta.name}\n")
    print(factura.model_dump_json(indent=2))

    # Contraste con los datos impresos en el PDF (verdad de referencia).
    def _buscar(patron: str) -> str:
        encontrado = re.search(patron, texto)
        if encontrado is None:
            raise ValueError(f"no se encontró el patrón {patron!r} en el PDF")
        return encontrado.group(1).strip()

    def _importe(etiqueta: str) -> Decimal:
        return Decimal(_buscar(etiqueta + r"\s+(-?[\d.]+,\d{2})").replace(".", "").replace(",", "."))

    comprobaciones = {
        "número": _buscar(r"Nº: (.+)") == factura.numero,
        "fecha expedición": factura.fecha_expedicion.strftime("%d/%m/%Y")
        in texto,
        "CIF emisor": factura.proveedor.cif_normalizado
        in re.sub(r"[^A-Z0-9]", "", texto.upper()),
        "referencia albarán": factura.referencia_albaran in texto,
        "base imponible": _importe("Base imponible") == factura.base_imponible,
        "cuota IVA": _importe(r"IVA \(21%\)") == factura.cuota_iva,
        "total": _importe("TOTAL FACTURA") == factura.total,
        "líneas": len(factura.lineas) >= 1,
    }

    print("\nContraste con el PDF:")
    for nombre, correcto in comprobaciones.items():
        print(f"  {'OK  ' if correcto else 'FALLO'} {nombre}")

    fallos = [n for n, ok in comprobaciones.items() if not ok]
    print("\nTODO CORRECTO" if not fallos else f"\nREVISAR: {', '.join(fallos)}")
