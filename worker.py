"""Procesa el lote de facturas con el grafo del agente.

Uso:
    python worker.py                         # procesa todos los PDF de datos/fras
    python worker.py --limite 5              # solo los cinco primeros
    python worker.py --listar                # estado de los procesos
    python worker.py --vigilar               # atiende las decisiones del panel
    python worker.py --reanudar <proceso> --decision aprobado
    python worker.py --reanudar <proceso> --decision rechazado

El worker es el único escritor del estado. Las facturas que necesitan revisión
se quedan pausadas (``interrupt``) y el bucle continúa con la siguiente.

Con ``--vigilar`` se queda en segundo plano comprobando la tabla ``comandos``:
allí deja el panel de Streamlit la decisión de la persona (aprobar o rechazar),
y aquí se reanuda el grafo. Así el panel nunca escribe el estado del agente y
solo hay un proceso que mueve el checkpointer.

Si ``AGENTE_NOTIFICADOR=telegram``, cada factura que queda en revisión (y cada
error) dispara un aviso al móvil. Un fallo al notificar no detiene el lote.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from config import ajustes
from core import store
from core.graph import construir_grafo
from core.ingest import listar_facturas
from core.notify import Aviso, Notificador, crear_notificador, motivos_de
from core.schemas import ResultadoValidacion


def _crear_checkpointer() -> tuple[SqliteSaver, sqlite3.Connection]:
    """Checkpointer SQLite en un fichero aparte del de procesos."""
    ajustes.ruta_checkpoints.parent.mkdir(parents=True, exist_ok=True)
    conexion = sqlite3.connect(str(ajustes.ruta_checkpoints), check_same_thread=False)
    return SqliteSaver(conexion), conexion


def _milisegundos(inicio: float) -> int:
    """Milisegundos transcurridos desde ``inicio`` (``time.perf_counter``)."""
    return int((time.perf_counter() - inicio) * 1000)


def _avisar_revision(
    notificador: Notificador,
    conexion: sqlite3.Connection,
    proceso: str,
    valores: dict[str, Any],
) -> None:
    """Avisa de que la factura espera decisión humana y lo deja anotado.

    ``notificado_en`` solo se escribe si el envío salió bien: así un fallo de red
    no impide reintentar el aviso más adelante.
    """
    resultado: ResultadoValidacion | None = None
    with suppress(ValueError):
        resultado = ResultadoValidacion.model_validate(valores.get("resultado"))

    fila = store.obtener_proceso(conexion, proceso) or {}
    aviso = Aviso(
        proceso=proceso,
        numero=fila.get("numero"),
        proveedor=fila.get("proveedor"),
        importe=fila.get("importe"),
        motivos=motivos_de(resultado),
    )
    if notificador.enviar(aviso):
        store.actualizar_proceso(conexion, proceso, notificado_en=store.ahora())


def _avisar_error(
    notificador: Notificador,
    conexion: sqlite3.Connection,
    proceso: str,
    motivo: str,
) -> None:
    """Avisa de un fallo de procesamiento (distinto de una revisión)."""
    if notificador.enviar(Aviso(proceso=proceso, motivos=motivo, es_error=True)):
        store.actualizar_proceso(conexion, proceso, notificado_en=store.ahora())


def procesar_lote(
    app: object,
    conexion: sqlite3.Connection,
    rutas: list[Path],
    notificador: Notificador | None = None,
) -> list[tuple[str, str]]:
    """Ejecuta el grafo factura a factura y avisa de lo que quede en revisión.

    La duración se mide alrededor del ``invoke``: es el tiempo del agente para
    esa factura, sin contar la espera entre una y otra.
    """
    resultados: list[tuple[str, str]] = []
    avisador = notificador or crear_notificador()

    for indice, ruta in enumerate(rutas, 1):
        proceso = ruta.stem
        config = {"configurable": {"thread_id": proceso}}
        store.crear_proceso(conexion, proceso, ruta.name)
        inicio = time.perf_counter()

        try:
            app.invoke({"proceso": proceso, "fichero": str(ruta)}, config)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - se registra y se sigue
            motivo = f"{type(exc).__name__}: {exc}"[:200]
            store.actualizar_proceso(
                conexion,
                proceso,
                estado="error",
                error=motivo,
                fin=store.ahora(),
                duracion_ms=_milisegundos(inicio),
            )
            store.registrar_evento(conexion, proceso, "error", motivo)
            _avisar_error(avisador, conexion, proceso, motivo)
            resultados.append((proceso, "error"))
            print(f"[{indice:>2}/{len(rutas)}] ERROR  {proceso}: {motivo}")
            continue

        store.actualizar_proceso(
            conexion, proceso, duracion_ms=_milisegundos(inicio)
        )

        estado_grafo = app.get_state(config)  # type: ignore[attr-defined]
        pendiente = bool(estado_grafo.next)
        estado_proceso = store.obtener_proceso(conexion, proceso) or {}
        situacion = "en_revision" if pendiente else str(estado_proceso.get("estado"))
        if pendiente:
            _avisar_revision(avisador, conexion, proceso, dict(estado_grafo.values))
        resultados.append((proceso, situacion))
        print(f"[{indice:>2}/{len(rutas)}] {situacion:<12} {proceso}")

    return resultados


def resumen(conexion: sqlite3.Connection) -> None:
    """Recuento de procesos por estado y lista de los que esperan revisión."""
    conteo = store.contar_por_estado(conexion)
    print("\nResumen")
    print("-" * 40)
    for estado, total in sorted(conteo.items()):
        print(f"  {estado:<12} {total}")

    pendientes = [p for p in store.listar_procesos(conexion) if p["estado"] == "en_revision"]
    if pendientes:
        print("\nPendientes de revisión humana:")
        for proceso in pendientes:
            print(
                f"  {proceso['numero'] or proceso['id']:<16} "
                f"{proceso['proveedor'] or '-':<40} {proceso['importe'] or 0:.2f} €"
            )


def aplicar_comandos(app: Any, conexion: sqlite3.Connection) -> list[tuple[str, str]]:
    """Aplica las decisiones que el panel ha dejado en la tabla ``comandos``.

    Se procesan por orden de llegada y cada una se marca como aplicada, para que
    un ciclo posterior no la repita. Hay tres desenlaces posibles:

    - ``aprobado``: el grafo sale del ``interrupt`` y la factura se registra.
    - ``rechazado``: el grafo sale del ``interrupt`` y la factura se rechaza.
    - ``descartado``: el proceso ya no estaba pausado (por ejemplo, alguien lo
      reanudó a mano desde la consola). Se anota y se sigue: reanudar un grafo
      terminado lanzaría una excepción sin aportar nada.

    Args:
        app: grafo compilado con su checkpointer.
        conexion: base de datos de procesos y comandos.

    Returns:
        Pares ``(proceso, resultado)``, uno por comando atendido.
    """
    atendidos: list[tuple[str, str]] = []

    for comando in store.comandos_pendientes(conexion):
        proceso = str(comando["proceso"])
        accion = str(comando["accion"])
        id_comando = int(comando["id"])
        config: RunnableConfig = {"configurable": {"thread_id": proceso}}

        if not app.get_state(config).next:
            # Llega tarde: la factura ya no espera a nadie.
            store.marcar_comando(conexion, id_comando)
            store.registrar_evento(
                conexion, proceso, "comando", f"descartado: {accion} (no estaba pausado)"
            )
            atendidos.append((proceso, "descartado"))
            continue

        try:
            app.invoke(Command(resume=accion), config)
        except Exception as exc:  # noqa: BLE001 - se registra y se sigue
            motivo = f"{type(exc).__name__}: {exc}"[:200]
            store.registrar_evento(
                conexion, proceso, "comando", f"error al aplicar {accion}: {motivo}"
            )
            # Se marca igualmente: reintentarlo dejaría el comando en bucle.
            store.marcar_comando(conexion, id_comando)
            atendidos.append((proceso, "error"))
            continue

        store.marcar_comando(conexion, id_comando)
        store.registrar_evento(conexion, proceso, "comando", f"aplicado: {accion}")
        situacion = str((store.obtener_proceso(conexion, proceso) or {}).get("estado"))
        atendidos.append((proceso, situacion))

    return atendidos


def vigilar(app: Any, conexion: sqlite3.Connection, intervalo: float) -> int:
    """Bucle de vigilancia: aplica los comandos del panel hasta Ctrl+C.

    Es el modo que se deja abierto en una pestaña mientras se graba la demo. Se
    para con Ctrl+C, que no deja el estado a medias: cada comando se aplica y se
    marca dentro de la misma iteración.
    """
    print(
        f"Vigilando comandos cada {intervalo:g} s sobre {ajustes.ruta_db.name}. "
        "Ctrl+C para salir."
    )
    try:
        while True:
            for proceso, resultado in aplicar_comandos(app, conexion):
                print(f"  {resultado:<12} {proceso}")
            time.sleep(intervalo)
    except KeyboardInterrupt:
        print("\nVigilancia detenida.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Procesa el lote de facturas.")
    parser.add_argument("--limite", type=int, default=0, help="procesa solo las N primeras")
    parser.add_argument("--listar", action="store_true", help="muestra el estado y sale")
    parser.add_argument(
        "--vigilar",
        action="store_true",
        help="aplica los comandos que deja el panel y sigue vigilando",
    )
    parser.add_argument(
        "--intervalo",
        type=float,
        default=ajustes.intervalo_vigilancia,
        metavar="SEGUNDOS",
        help="cada cuánto sondea los comandos con --vigilar",
    )
    parser.add_argument("--reanudar", metavar="PROCESO", help="reanuda una factura pausada")
    parser.add_argument(
        "--decision",
        choices=("aprobado", "rechazado"),
        default="aprobado",
        help="decisión para --reanudar",
    )
    args = parser.parse_args()

    conexion = store.conectar()
    saver, conexion_checkpoints = _crear_checkpointer()
    app = construir_grafo(conexion=conexion).compile(checkpointer=saver)
    notificador = crear_notificador()

    try:
        if args.listar:
            resumen(conexion)
            return 0

        if args.vigilar:
            return vigilar(app, conexion, args.intervalo)

        if args.reanudar:
            # ``RunnableConfig`` es el tipo que espera ``invoke``; sin la anotación
            # el diccionario se infiere como anidado genérico y no encaja.
            config: RunnableConfig = {"configurable": {"thread_id": args.reanudar}}
            app.invoke(Command(resume=args.decision), config)
            proceso = store.obtener_proceso(conexion, args.reanudar) or {}
            print(f"{args.reanudar}: {proceso.get('estado')}")
            return 0

        rutas = listar_facturas(ajustes.directorio_facturas)
        if args.limite > 0:
            rutas = rutas[: args.limite]
        if not rutas:
            print(f"No hay facturas en {ajustes.directorio_facturas}")
            return 1

        print(f"Procesando {len(rutas)} facturas con {ajustes.modelo}...\n")
        procesar_lote(app, conexion, rutas, notificador)
        resumen(conexion)
        return 0
    finally:
        conexion_checkpoints.close()
        conexion.close()


if __name__ == "__main__":
    sys.exit(main())
