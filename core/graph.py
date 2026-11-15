r"""Grafo del agente de cuentas por pagar.

Flujo:

    START -> extraer -> validar -+-> registrar -> END
                                 |
                                 +-> revisar -(aprobado)-> registrar -> END
                                              \-(rechazado)-> rechazar -> END

- ``extraer`` es el único paso con modelo de lenguaje.
- ``validar`` aplica las reglas deterministas y la detección de duplicados.
- ``revisar`` usa ``interrupt()``: la factura se queda pausada y el worker
  puede seguir con las demás. Se reanuda con ``Command(resume="aprobado")``.

El estado guarda la factura como diccionario JSON (no como modelo Pydantic)
para que el checkpointer lo serialice sin sorpresas.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from core import store
from core.erp import registrar_factura
from core.extraction import Extraccion, extraer_factura_con_uso
from core.ingest import extraer_texto
from core.metrics import coste_extraccion
from core.schemas import Factura, Incidencia, ResultadoValidacion, TipoIncidencia
from core.validation import validar_factura

Extractor = Callable[[str], Extraccion]
Validador = Callable[[Factura], ResultadoValidacion]
Registrador = Callable[..., dict[str, Any]]


class EstadoAgente(TypedDict):
    """Estado que viaja entre nodos.

    Las claves son obligatorias salvo ``decision`` y ``motivo``, que no existen
    hasta que hay una revisión humana. Declararlas así permite que el
    comprobador de tipos detecte un nodo que lea un campo que aún no está,
    en lugar de dejar pasar ``estado["resultado"]`` como si fuera opcional.
    """

    proceso: str  # thread_id: nombre del fichero
    fichero: str
    factura: dict[str, Any]
    factura_id: str
    numero: str
    proveedor: str
    importe: float
    resultado: dict[str, Any]
    decision: NotRequired[str]
    motivo: NotRequired[str]


def construir_grafo(
    *,
    extractor: Extractor | None = None,
    validador: Validador | None = None,
    registrador: Registrador | None = None,
    conexion: sqlite3.Connection | None = None,
) -> StateGraph:
    """Monta el grafo sin compilar (el checkpointer se añade al compilar).

    Args:
        extractor: sustituye a ``extraer_factura_con_uso`` (pruebas sin API).
        validador: sustituye a ``validar_factura``.
        registrador: sustituye a ``core.erp.registrar_factura``.
        conexion: base de datos de procesos; si falta, no se registra traza.
    """

    def _estado(proceso: str, **campos: Any) -> None:
        if conexion is not None:
            store.actualizar_proceso(conexion, proceso, **campos)

    def _evento(proceso: str, nodo: str, detalle: str = "") -> None:
        if conexion is not None:
            store.registrar_evento(conexion, proceso, nodo, detalle)

    # El primer parámetro de un nodo debe llamarse ``state``: LangGraph tipa
    # los nodos con un ``Protocol`` y el comprobador de tipos exige que el
    # nombre coincida. El resto del código sigue en español.
    def nodo_extraer(state: EstadoAgente) -> dict[str, Any]:
        proceso = state["proceso"]
        ruta = Path(state["fichero"])

        _estado(proceso, estado="extrayendo")
        _evento(proceso, "extraer", f"leyendo {ruta.name}")

        texto = extraer_texto(ruta)
        extraccion = (extractor or extraer_factura_con_uso)(texto)
        factura = extraccion.factura
        coste = coste_extraccion(extraccion.tokens_entrada, extraccion.tokens_salida)

        _estado(
            proceso,
            factura_id=factura.identificador,
            numero=factura.numero,
            proveedor=factura.proveedor.nombre,
            importe=float(factura.total),
            tokens_entrada=extraccion.tokens_entrada,
            tokens_salida=extraccion.tokens_salida,
            coste=coste,
            estado="validando",
        )
        _evento(
            proceso,
            "extraer",
            (
                f"{len(factura.lineas)} líneas, {factura.total} € · "
                f"{extraccion.tokens_entrada + extraccion.tokens_salida} tokens · "
                f"{coste:.6f} $ · {extraccion.intentos} intento(s)"
            ),
        )

        return {
            "factura": factura.model_dump(mode="json"),
            "factura_id": factura.identificador,
            "numero": factura.numero,
            "proveedor": factura.proveedor.nombre,
            "importe": float(factura.total),
        }

    def nodo_validar(state: EstadoAgente) -> dict[str, Any]:
        proceso = state["proceso"]
        factura = Factura.model_validate(state["factura"])
        resultado = (validador or validar_factura)(factura)

        # Regla de duplicados: misma factura ya registrada en otro proceso.
        if conexion is not None and store.existe_registrada(
            conexion, factura.identificador, excluir=proceso
        ):
            resultado = resultado.model_copy(
                update={
                    "incidencias": [
                        *resultado.incidencias,
                        Incidencia(
                            codigo=TipoIncidencia.DUPLICADO,
                            mensaje="Esa factura ya está registrada en el ERP",
                            detalle={"factura_id": factura.identificador},
                        ),
                    ]
                }
            )

        _estado(proceso, incidencias=len(resultado.incidencias))
        _evento(
            proceso,
            "validar",
            ", ".join(codigo.value for codigo in resultado.codigos) or "sin incidencias",
        )
        return {"resultado": resultado.model_dump(mode="json")}

    def decidir(state: EstadoAgente) -> Literal["registrar", "revisar"]:
        resultado = ResultadoValidacion.model_validate(state["resultado"])
        return "revisar" if resultado.requiere_revision else "registrar"

    def nodo_revisar(state: EstadoAgente) -> dict[str, Any]:
        proceso = state["proceso"]
        _estado(proceso, estado="en_revision")
        _evento(proceso, "revisar", "esperando decisión humana")

        # Punto de pausa. Al reanudar, esta llamada devuelve el valor que llega
        # en Command(resume=...).
        decision = interrupt(
            {
                "factura": state.get("numero"),
                "factura_id": state.get("factura_id"),
                "importe": state.get("importe"),
                "incidencias": state["resultado"].get("incidencias", []),
                "supera_umbral": state["resultado"].get("supera_umbral", False),
            }
        )
        return {"decision": str(decision).lower(), "motivo": "revisión humana"}

    def decidir_revision(state: EstadoAgente) -> Literal["registrar", "rechazar"]:
        aprobada = str(state.get("decision", "")).startswith("aprob")
        return "registrar" if aprobada else "rechazar"

    def _anotar(state: EstadoAgente, situacion: str, quien: str | None) -> None:
        factura = Factura.model_validate(state["factura"])
        (registrador or registrar_factura)(
            factura, estado=situacion, aprobado_por=quien
        )

    def nodo_registrar(state: EstadoAgente) -> dict[str, Any]:
        proceso = state["proceso"]
        quien = state.get("decision") or "automático"
        _anotar(state, "registrado", quien)
        _estado(proceso, estado="registrado", fin=store.ahora())
        _evento(proceso, "registrar", f"registrada por {quien}")
        return {"motivo": "registrada"}

    def nodo_rechazar(state: EstadoAgente) -> dict[str, Any]:
        proceso = state["proceso"]
        _anotar(state, "rechazado", state.get("decision"))
        _estado(proceso, estado="rechazado", fin=store.ahora())
        _evento(proceso, "rechazar", "rechazada en revisión")
        return {"motivo": "rechazada"}

    grafo = StateGraph(EstadoAgente)
    grafo.add_node("extraer", nodo_extraer)
    grafo.add_node("validar", nodo_validar)
    grafo.add_node("revisar", nodo_revisar)
    grafo.add_node("registrar", nodo_registrar)
    grafo.add_node("rechazar", nodo_rechazar)

    grafo.add_edge(START, "extraer")
    grafo.add_edge("extraer", "validar")
    grafo.add_conditional_edges(
        "validar", decidir, {"registrar": "registrar", "revisar": "revisar"}
    )
    grafo.add_conditional_edges(
        "revisar", decidir_revision, {"registrar": "registrar", "rechazar": "rechazar"}
    )
    grafo.add_edge("registrar", END)
    grafo.add_edge("rechazar", END)

    return grafo
