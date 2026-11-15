"""Panel de control del agente de cuentas por pagar (fase 6).

Qué hace
--------
Enseña el estado del lote de facturas y deja que una persona decida qué hacer
con las que el agente ha dejado en revisión: ve la factura, el albarán del ERP
simulado y las incidencias detectadas, y pulsa *Aprobar* o *Rechazar*.

Qué **no** hace
---------------
No mueve el grafo ni escribe el checkpointer. Todas las lecturas abren SQLite
con ``mode=ro`` y la única escritura es el ``INSERT`` en la tabla ``comandos``.
El worker sondea esa tabla (``python worker.py --vigilar``) y es él quien
reanuda la factura, de modo que sigue habiendo **un único escritor** del estado.

Ese reparto es lo que evita que Streamlit y el worker se peleen por el fichero
del checkpointer (bloqueos de SQLite) justo cuando se está grabando la demo.

Uso
---
    streamlit run app.py        # desde la carpeta del proyecto
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import streamlit as st
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver

from config import ajustes
from core import metrics, store
from core.erp import cargar_albaranes, cargar_maestro
from core.schemas import Factura, ResultadoValidacion

# Cómo se representa cada estado. Un único sitio para la etiqueta, el punto de
# color de los contadores y la insignia de la ficha: si se añade un estado al
# esquema, se añade aquí y aparece bien en todas partes.
ColorInsignia = Literal["red", "orange", "yellow", "blue", "green", "violet", "gray"]


@dataclass(frozen=True)
class Estado:
    """Apariencia de un estado del proceso.

    ``emoji`` se usa en los contadores porque las etiquetas de ``st.metric`` no
    admiten iconos Material; ``icono`` (Material) se usa en la insignia de la
    ficha, que hereda el color del texto y no desentona en un panel de trabajo.
    """

    etiqueta: str  # singular: se lee en la insignia
    plural: str  # se lee en los contadores
    emoji: str
    icono: str
    color: ColorInsignia


ESTADOS: dict[str, Estado] = {
    "en_revision": Estado(
        "En revisión", "En revisión", "🟡", ":material/pending_actions:", "orange"
    ),
    "registrado": Estado(
        "Registrada", "Registradas", "🟢", ":material/task_alt:", "green"
    ),
    "rechazado": Estado(
        "Rechazada", "Rechazadas", "🔴", ":material/block:", "red"
    ),
    "error": Estado("Error", "Errores", "⚠️", ":material/error:", "red"),
    "encolado": Estado("En cola", "En cola", "⚪", ":material/schedule:", "gray"),
    "extrayendo": Estado(
        "Extrayendo", "Extrayendo", "🔵", ":material/document_scanner:", "blue"
    ),
    "validando": Estado(
        "Validando", "Validando", "🔵", ":material/fact_check:", "blue"
    ),
}

# Streamlit no permite elegir el color de un botón suelto: el color sale del tema
# (``primaryColor``) o del tipo (``primary``/``secondary``), y eso teñiría toda la
# app. La salida limpia es la que ofrece el propio Streamlit: los widgets con
# ``key`` reciben una clase CSS estable (``st-key-<key>``), así que se puede
# pintar únicamente el par aprobar/rechazar sin tocar el resto del panel.
#
# Los colores son claros a propósito: sobre el fondo oscuro del tema destacan sin
# competir con las insignias de estado, y llevan texto oscuro para que el
# contraste no dependa del brillo de la pantalla.
ESTILOS_DECISION = """
<style>
/* Aprobar: verde claro */
div[class*="st-key-aprobar-"] button {
    background-color: #A9D8B8 !important;
    border: 1px solid #7FBF95 !important;
    color: #10301D !important;
}
div[class*="st-key-aprobar-"] button:hover {
    background-color: #96CBA6 !important;
    border-color: #6DB287 !important;
    color: #10301D !important;
}
div[class*="st-key-aprobar-"] button:active {
    background-color: #83BF98 !important;
    border-color: #6DB287 !important;
    color: #10301D !important;
}
/* Rechazar: rojo claro */
div[class*="st-key-rechazar-"] button {
    background-color: #F1AFA9 !important;
    border: 1px solid #DF8E86 !important;
    color: #4B1410 !important;
}
div[class*="st-key-rechazar-"] button:hover {
    background-color: #EB9F98 !important;
    border-color: #D47A71 !important;
    color: #4B1410 !important;
}
div[class*="st-key-rechazar-"] button:active {
    background-color: #E3908A !important;
    border-color: #D47A71 !important;
    color: #4B1410 !important;
}
</style>
"""


# ---------------------------------------------------------------------------
# Formato
# ---------------------------------------------------------------------------
def _euros(valor: Any) -> str:
    """Importe con separador de miles y coma decimal, al estilo español."""
    numero = Decimal(str(valor))
    texto = f"{numero:,.2f}".replace(",", "\u00a0")  # agrupación de miles
    return texto.replace(".", ",") + " €"


def _cantidad(valor: Any) -> str:
    """Cantidad sin ceros de relleno (``12.00`` -> ``12``, ``2.50`` -> ``2,5``)."""
    texto = f"{Decimal(str(valor)):f}"
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def _etiqueta(proceso: dict[str, Any]) -> str:
    """Línea del selector: número, proveedor e importe."""
    numero = proceso["numero"] or proceso["id"]
    proveedor = proceso["proveedor"] or "sin datos"
    importe = "—" if proceso["importe"] is None else _euros(proceso["importe"])
    return f"{numero} · {proveedor} · {importe}"


# ---------------------------------------------------------------------------
# Lectura del estado
# ---------------------------------------------------------------------------
@contextmanager
def conexion_lectura(ruta: Path) -> Iterator[sqlite3.Connection]:
    """Abre SQLite en modo solo lectura.

    ``mode=ro`` es la garantía de que el panel no puede modificar el estado del
    agente ni por error de programación: SQLite rechazaría la escritura en vez
    de corromper el fichero mientras el worker lo está usando.
    """
    conexion = sqlite3.connect(
        f"file:{ruta}?mode=ro", uri=True, check_same_thread=False
    )
    conexion.row_factory = sqlite3.Row
    try:
        yield conexion
    finally:
        conexion.close()


def leer_procesos() -> list[dict[str, Any]]:
    """Procesos registrados por el worker, del más antiguo al más nuevo."""
    if not ajustes.ruta_db.exists():
        return []
    with conexion_lectura(ajustes.ruta_db) as conexion:
        return store.listar_procesos(conexion)


def leer_comandos(proceso: str) -> list[dict[str, Any]]:
    """Comandos que el panel ha encolado para ese proceso."""
    if not ajustes.ruta_db.exists():
        return []
    with conexion_lectura(ajustes.ruta_db) as conexion:
        return store.comandos_del_proceso(conexion, proceso)


def leer_estado_grafo(proceso: str) -> dict[str, Any] | None:
    """Valores que el grafo guardó para ese ``thread_id``.

    Se leen del checkpointer **sin compilar el grafo**: el panel solo necesita
    los datos (la factura extraída y el resultado de la validación), no la
    topología ni los nodos. Así esta pestaña no arrastra la pila del extractor
    (LangChain, el SDK de OpenAI, pypdf), que es pesada y no pinta nada en una
    vista de solo lectura: el único proceso que ejecuta el agente es el worker.
    """
    if not ajustes.ruta_checkpoints.exists():
        return None

    config: RunnableConfig = {"configurable": {"thread_id": proceso}}
    try:
        with conexion_lectura(ajustes.ruta_checkpoints) as conexion:
            tupla = SqliteSaver(conexion).get_tuple(config)
    except sqlite3.Error:
        # Base vacía o esquema sin crear (por ejemplo, justo después de
        # reset.py): el panel se queda sin detalle, pero sigue funcionando.
        return None

    if tupla is None:
        return None
    valores = tupla.checkpoint.get("channel_values") or {}
    return dict(valores) or None


def leer_registro() -> list[dict[str, Any]]:
    """Facturas anotadas en el registro del ERP simulado."""
    if not ajustes.ruta_registro.exists():
        return []
    with suppress(ValueError):
        return json.loads(ajustes.ruta_registro.read_text(encoding="utf-8"))
    return []


def _factura_de(estado: dict[str, Any] | None) -> Factura | None:
    """Reconstruye la factura guardada en el estado del grafo."""
    if not estado or "factura" not in estado:
        return None
    with suppress(ValueError):
        return Factura.model_validate(estado["factura"])
    return None


def _resultado_de(estado: dict[str, Any] | None) -> ResultadoValidacion | None:
    """Reconstruye el resultado de la validación guardado en el estado."""
    if not estado or "resultado" not in estado:
        return None
    with suppress(ValueError):
        return ResultadoValidacion.model_validate(estado["resultado"])
    return None


# ---------------------------------------------------------------------------
# Escritura (la única del panel)
# ---------------------------------------------------------------------------
def encolar_decision(proceso: str, accion: str) -> None:
    """Deja la decisión en la tabla ``comandos`` y refresca el panel.

    Se abre una conexión de escritura aparte, porque el resto del panel es de
    solo lectura. El grafo no se toca aquí: reanudarlo desde Streamlit crearía
    un segundo escritor del checkpointer.
    """
    with closing(store.conectar()) as conexion:
        store.enviar_comando(conexion, proceso, accion)
    st.toast(f"{accion.capitalize()}: {proceso}", icon=":material/send:")
    st.rerun()


# ---------------------------------------------------------------------------
# Bloques de la ficha
# ---------------------------------------------------------------------------
def _bloque_factura(factura: Factura | None) -> None:
    """Columna izquierda: lo que se extrajo del PDF."""
    st.markdown("#### :material/receipt_long: Factura")

    if factura is None:
        st.warning(
            "Sin datos de extracción: el checkpointer no tiene estado para este "
            "proceso (¿se ha reiniciado con `reset.py`?).",
            icon=":material/search_off:",
        )
        return

    st.caption(
        f"{factura.numero} · expedida el {factura.fecha_expedicion:%d/%m/%Y} · "
        f"albarán **{factura.referencia_albaran}**"
    )
    st.dataframe(
        [
            {
                "Concepto": linea.concepto,
                "Cantidad": _cantidad(linea.cantidad),
                "Precio": _euros(linea.precio_unitario),
                "Importe": _euros(linea.importe),
            }
            for linea in factura.lineas
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"Base {_euros(factura.base_imponible)} · "
        f"IVA {_cantidad(factura.tipo_iva)} % ({_euros(factura.cuota_iva)}) · "
        f"IRPF {_cantidad(factura.tipo_retencion_irpf)} % "
        f"({_euros(factura.cuota_retencion)}) · "
        f"**Total {_euros(factura.total)}**"
    )
    st.caption(
        f"Proveedor: {factura.proveedor.nombre} · CIF {factura.proveedor.cif} · "
        f"forma de pago {factura.forma_pago or '—'} · IBAN {factura.iban or '—'}"
    )


def _bloque_albaran(factura: Factura | None) -> None:
    """Columna derecha: lo que dice el ERP simulado sobre la entrega."""
    st.markdown("#### :material/local_shipping: Albarán (ERP simulado)")

    if factura is None:
        st.info("Sin referencia de albarán que consultar.", icon=":material/help:")
        return

    try:
        albaran = cargar_albaranes().buscar(factura.referencia_albaran)
    except (OSError, ValueError) as exc:
        st.error(f"No se pudo leer el ERP simulado: {exc}")
        return

    if albaran is None:
        st.error(f"No hay ningún albarán {factura.referencia_albaran} en el ERP")
        return

    st.caption(
        f"{albaran.numero} · entregado el {albaran.fecha:%d/%m/%Y} · "
        f"{albaran.direccion_entrega or 'sin dirección de entrega'}"
    )
    st.dataframe(
        [
            {
                "Concepto": linea.concepto,
                "Cantidad": _cantidad(linea.cantidad),
            }
            for linea in albaran.lineas
        ],
        hide_index=True,
        width="stretch",
    )


def _bloque_fiscal(factura: Factura) -> None:
    """Datos fiscales en paralelo: los de la factura y los de la ficha del ERP.

    Es la comparación que hace el agente antes de pagar nada: un CIF o un IBAN
    distintos son la señal clásica de una factura falsa o de una ficha mal
    tecleada.
    """
    st.markdown("#### :material/verified_user: Datos fiscales frente al maestro")

    try:
        maestro = cargar_maestro()
    except (OSError, ValueError) as exc:
        st.error(f"No se pudo leer el maestro de proveedores: {exc}")
        return

    ficha = maestro.buscar(factura.proveedor.cif) or maestro.buscar_por_nombre(
        factura.proveedor.nombre
    )
    if ficha is None:
        st.error("Ese proveedor no está en el maestro de proveedores")
        st.dataframe(
            [
                {"Dato": "CIF", "Factura": factura.proveedor.cif, "ERP": "—"},
                {"Dato": "Razón social", "Factura": factura.proveedor.nombre, "ERP": "—"},
            ],
            hide_index=True,
            width="stretch",
        )
        return

    st.dataframe(
        [
            {"Dato": "CIF", "Factura": factura.proveedor.cif, "ERP": ficha.cif},
            {
                "Dato": "Razón social",
                "Factura": factura.proveedor.nombre,
                "ERP": ficha.nombre,
            },
            {
                "Dato": "Forma de pago",
                "Factura": factura.forma_pago or "—",
                "ERP": ficha.forma_pago,
            },
            {"Dato": "IBAN", "Factura": factura.iban or "—", "ERP": ficha.iban},
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        f"Plazo pactado: {ficha.dias_vencimiento} días. El agente usaría el IBAN "
        "de la ficha, nunca el impreso en la factura."
    )


def _bloque_incidencias(resultado: ResultadoValidacion | None) -> None:
    """Motivos por los que la factura está en revisión."""
    st.markdown("#### :material/report: Incidencias")

    if resultado is None:
        st.info(
            "Sin resultado de validación guardado: el proceso no llegó a validarse.",
            icon=":material/help:",
        )
        return

    if not resultado.incidencias and not resultado.supera_umbral:
        st.success(
            "Sin incidencias: esta factura se registró automáticamente.",
            icon=":material/check_circle:",
        )
        return

    for incidencia in resultado.incidencias:
        st.error(f"**{incidencia.codigo.value}** — {incidencia.mensaje}")
        if incidencia.detalle:
            detalle = " · ".join(
                f"{clave}: {valor}" for clave, valor in incidencia.detalle.items()
            )
            st.caption(detalle)

    if resultado.supera_umbral:
        st.warning(
            "Supera el umbral de aprobación "
            f"({_euros(ajustes.umbral_aprobacion)}): necesita visto bueno humano "
            "aunque no tenga ninguna otra incidencia."
        )


def _bloque_decision(proceso: dict[str, Any]) -> None:
    """Botones de aprobación y rechazo, solo para lo que está en revisión."""
    st.markdown("#### :material/gavel: Decisión")

    if proceso["estado"] != "en_revision":
        st.caption(f"El proceso está en `{proceso['estado']}`: no admite decisión.")
        if proceso["decision"]:
            st.caption(f"Decisión registrada: **{proceso['decision']}**")
        return

    pendientes = [comando for comando in leer_comandos(proceso["id"]) if not comando["aplicado"]]
    if pendientes:
        st.info(
            f"Ya hay una decisión encolada (**{pendientes[-1]['accion']}**). "
            "El worker la aplicará en el próximo ciclo."
        )

    izquierda, derecha, _ = st.columns([1, 1, 2])
    if izquierda.button(
        "Aprobar",
        type="primary",
        icon=":material/check_circle:",
        key=f"aprobar-{proceso['id']}",
    ):
        encolar_decision(proceso["id"], "aprobado")
    if derecha.button(
        "Rechazar",
        icon=":material/cancel:",
        key=f"rechazar-{proceso['id']}",
    ):
        encolar_decision(proceso["id"], "rechazado")


def _linea_uso(proceso: dict[str, Any]) -> str:
    """Consumo y coste de extracción de una factura.

    Se llama *coste de extracción* a propósito: es lo que cuesta ejecutar el
    agente (tokens del modelo), no el importe de la factura del proveedor.
    Devuelve cadena vacía si la factura no se llegó a extraer.
    """
    tokens_entrada = int(proceso.get("tokens_entrada") or 0)
    tokens_salida = int(proceso.get("tokens_salida") or 0)
    if not tokens_entrada and not tokens_salida:
        return ""

    partes = [
        f"{metrics.formatear_tokens(tokens_entrada + tokens_salida)} tokens",
        (
            "coste de extracción "
            f"{metrics.formatear_coste(float(proceso.get('coste') or 0.0))}"
        ),
    ]
    duracion = int(proceso.get("duracion_ms") or 0)
    if duracion:
        partes.append(metrics.formatear_duracion(duracion))
    return ":material/toll: " + " · ".join(partes)


def _ficha(proceso: dict[str, Any]) -> None:
    """Todo el detalle de una factura, de arriba abajo."""
    estado = leer_estado_grafo(str(proceso["id"]))
    factura = _factura_de(estado)
    resultado = _resultado_de(estado)

    st.subheader(f"{proceso['numero'] or proceso['id']}")
    apariencia = ESTADOS.get(str(proceso["estado"]))
    if apariencia is not None:
        st.badge(apariencia.etiqueta, icon=apariencia.icono, color=apariencia.color)
    st.caption(
        f"{proceso['proveedor'] or 'sin datos'} · fichero `{proceso['fichero']}`"
    )
    linea_uso = _linea_uso(proceso)
    if linea_uso:
        st.caption(linea_uso)

    izquierda, derecha = st.columns(2)
    with izquierda, st.container(border=True):
        _bloque_factura(factura)
    with derecha, st.container(border=True):
        _bloque_albaran(factura)

    with st.container(border=True):
        if factura is not None:
            _bloque_fiscal(factura)
        _bloque_incidencias(resultado)

    with st.container(border=True):
        _bloque_decision(proceso)


# ---------------------------------------------------------------------------
# Vistas
# ---------------------------------------------------------------------------
def _contadores(procesos: list[dict[str, Any]]) -> None:
    """Fila de contadores por estado."""
    conteo: dict[str, int] = {}
    for proceso in procesos:
        conteo[proceso["estado"]] = conteo.get(proceso["estado"], 0) + 1

    columnas = st.columns(len(ESTADOS))
    for columna, (clave, estado) in zip(columnas, ESTADOS.items(), strict=True):
        columna.metric(f"{estado.emoji} {estado.plural}", conteo.get(clave, 0))


@st.fragment(run_every=ajustes.auto_refresco_segundos)
def panel_cola(*, solo_revision: bool) -> None:
    """Cola de facturas con su ficha.

    El fragmento se refresca solo cada pocos segundos: cuando el worker aplica
    una decisión, la factura cambia de estado sin que haya que recargar la
    página a mano (útil en la demo).
    """
    procesos = leer_procesos()
    if not procesos:
        st.info(
            "Todavía no hay procesos. Ejecuta `python worker.py` para procesar "
            "el lote de facturas.",
            icon=":material/inbox:",
        )
        return

    _contadores(procesos)
    uso = metrics.resumir(procesos)
    if uso.facturas:
        st.caption(
            f":material/toll: {uso.facturas} extracciones · "
            f"{metrics.formatear_tokens(uso.tokens)} tokens · "
            f"coste de extracción {metrics.formatear_coste(uso.coste)} · "
            f"{metrics.formatear_duracion(uso.duracion_ms)} en total"
        )

    visibles = (
        [proceso for proceso in procesos if proceso["estado"] == "en_revision"]
        if solo_revision
        else procesos
    )
    if not visibles:
        st.success(
            "No queda ninguna factura pendiente de revisión.",
            icon=":material/check_circle:",
        )
        return

    por_id = {proceso["id"]: proceso for proceso in visibles}
    elegido = st.selectbox(
        "Factura",
        options=list(por_id),
        format_func=lambda identificador: _etiqueta(por_id[identificador]),
        key="selector-factura",
    )
    _ficha(por_id[elegido])


@st.fragment(run_every=ajustes.auto_refresco_segundos)
def panel_vencimientos() -> None:
    """Vencimientos de lo ya registrado, para el discurso de tesorería."""
    registros = leer_registro()
    if not registros:
        st.info(
            "Todavía no hay facturas registradas en el ERP simulado.",
            icon=":material/inbox:",
        )
        return

    hoy = datetime.now().astimezone().date()  # fecha local, no UTC
    total = Decimal(0)
    vencidas = 0
    filas: list[tuple[date, dict[str, Any]]] = []

    for registro in registros:
        vencimiento = date.fromisoformat(registro["fecha_vencimiento"])
        total += Decimal(str(registro["total"]))
        dias = (vencimiento - hoy).days
        if dias < 0:
            vencidas += 1
        filas.append(
            (
                vencimiento,
                {
                    "Vencimiento": vencimiento.strftime("%d/%m/%Y"),
                    "Días": dias,
                    "Factura": registro["numero"],
                    "Proveedor": registro["proveedor"],
                    "Total": _euros(registro["total"]),
                    "Forma de pago": registro.get("forma_pago") or "—",
                    "Pago": registro.get("estado_pago") or "—",
                },
            )
        )

    st.markdown("#### :material/event_upcoming: Vencimientos")
    columnas = st.columns(3)
    columnas[0].metric("Facturas registradas", len(registros))
    columnas[1].metric("Pendiente de pago", _euros(total))
    columnas[2].metric("Vencidas sin pagar", vencidas)

    filas.sort(key=lambda pareja: pareja[0])  # por fecha real, no por el texto
    st.dataframe([fila for _, fila in filas], hide_index=True, width="stretch")
    st.caption(
        ":material/info: El agente no paga: deja cada factura con "
        "`estado_pago = pendiente` y la fecha de vencimiento del plazo pactado. "
        "La remesa la haría tesorería."
    )


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------
def main() -> None:
    """Cabecera, selector de vista y vista elegida."""
    st.set_page_config(
        page_title="Agente de cuentas por pagar",
        page_icon=":material/receipt_long:",
        layout="wide",
    )
    st.title("Agente de cuentas por pagar")
    st.caption(
        ":material/apartment: Logística Ibérica · datos simulados · "
        "el agente registra, no paga"
    )
    # Se inyecta una sola vez: los refrescos del fragmento no reejecutan main().
    st.markdown(ESTILOS_DECISION, unsafe_allow_html=True)

    with st.sidebar:
        st.subheader(":material/tune: Configuración")
        st.caption(f":material/memory: Modelo `{ajustes.modelo}`")
        st.caption(
            f":material/speed: Umbral de aprobación {_euros(ajustes.umbral_aprobacion)}"
        )
        st.divider()
        vista = st.radio(
            "Vista",
            ("Revisión pendiente", "Todas las facturas", "Vencimientos"),
        )
        st.divider()
        st.badge("Solo lectura", icon=":material/lock:", color="gray")
        st.caption(
            "Las decisiones se encolan en `comandos` y las aplica el worker "
            "(`python worker.py --vigilar`)."
        )

    if vista == "Vencimientos":
        panel_vencimientos()
        return

    panel_cola(solo_revision=vista == "Revisión pendiente")


if __name__ == "__main__":
    main()
