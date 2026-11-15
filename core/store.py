"""Persistencia del estado del agente: procesos, eventos y comandos.

Tablas SQLite que sirven de "proyección" para el panel. El checkpointer de
LangGraph guarda el estado interno del grafo (que no se consulta desde la UI);
estas tablas son la vista legible: qué se está procesando, en qué estado y qué
ha pasado.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from config import ajustes

ESQUEMA = """
CREATE TABLE IF NOT EXISTS procesos (
    id          TEXT PRIMARY KEY,   -- thread_id (nombre del fichero)
    factura_id  TEXT,               -- CIF-número-año, se conoce tras extraer
    fichero     TEXT,
    numero      TEXT,
    proveedor   TEXT,
    estado      TEXT NOT NULL,
    importe     REAL,
    incidencias INTEGER DEFAULT 0,
    decision    TEXT,
    inicio      TEXT,
    fin         TEXT,
    heartbeat   TEXT,
    error       TEXT,
    notificado_en  TEXT,            -- cuándo se avisó (Telegram o consola)
    tokens_entrada INTEGER,         -- consumo del modelo, sumando reintentos
    tokens_salida  INTEGER,
    coste          REAL,            -- coste de extracción estimado, en dólares
    duracion_ms    INTEGER
);

CREATE TABLE IF NOT EXISTS eventos (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    proceso TEXT,
    nodo    TEXT,
    ts      TEXT,
    detalle TEXT
);

CREATE TABLE IF NOT EXISTS comandos (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    proceso TEXT NOT NULL,
    accion  TEXT NOT NULL,
    ts      TEXT,
    aplicado INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_procesos_estado ON procesos(estado);
CREATE INDEX IF NOT EXISTS idx_eventos_proceso ON eventos(proceso);
"""

# Columnas que se pueden actualizar de un proceso.
COLUMNAS = {
    "factura_id",
    "fichero",
    "numero",
    "proveedor",
    "estado",
    "importe",
    "incidencias",
    "decision",
    "inicio",
    "fin",
    "heartbeat",
    "error",
    "notificado_en",
    "tokens_entrada",
    "tokens_salida",
    "coste",
    "duracion_ms",
}

# Columnas añadidas después de la primera versión del esquema. ``CREATE TABLE IF
# NOT EXISTS`` no toca una tabla que ya existe, así que hay que migrarla a mano:
# si no, la base de datos de una demo anterior se quedaría sin las métricas.
COLUMNAS_NUEVAS: tuple[tuple[str, str], ...] = (
    ("notificado_en", "TEXT"),
    ("tokens_entrada", "INTEGER"),
    ("tokens_salida", "INTEGER"),
    ("coste", "REAL"),
    ("duracion_ms", "INTEGER"),
)


def ahora() -> str:
    """Marca temporal local en ISO, con segundos (y desfase horario)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _migrar(conexion: sqlite3.Connection) -> None:
    """Añade a ``procesos`` las columnas que falten, sin tocar los datos.

    Es idempotente y barata: se ejecuta en cada conexión, lee el esquema actual
    con ``PRAGMA table_info`` y solo altera la tabla si hay algo nuevo.
    """
    existentes = {fila["name"] for fila in conexion.execute("PRAGMA table_info(procesos)")}
    for nombre, tipo in COLUMNAS_NUEVAS:
        if nombre not in existentes:
            conexion.execute(f"ALTER TABLE procesos ADD COLUMN {nombre} {tipo}")
    conexion.commit()


def conectar(ruta: Path | str | None = None) -> sqlite3.Connection:
    """Abre la base de datos, garantiza el esquema y migra si hace falta."""
    destino = str(ruta or ajustes.ruta_db)
    if destino != ":memory:":
        Path(destino).parent.mkdir(parents=True, exist_ok=True)
    conexion = sqlite3.connect(destino)
    conexion.row_factory = sqlite3.Row
    conexion.executescript(ESQUEMA)
    _migrar(conexion)
    return conexion


def crear_proceso(conexion: sqlite3.Connection, proceso: str, fichero: str) -> None:
    """Da de alta (o reinicia) un proceso al empezar a trabajar con él."""
    conexion.execute(
        """
        INSERT INTO procesos (id, fichero, estado, inicio, heartbeat)
        VALUES (?, ?, 'encolado', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            fichero = excluded.fichero,
            estado = 'encolado',
            inicio = excluded.inicio,
            heartbeat = excluded.heartbeat,
            fin = NULL,
            decision = NULL,
            error = NULL
        """,
        (proceso, fichero, ahora(), ahora()),
    )
    conexion.commit()


def actualizar_proceso(
    conexion: sqlite3.Connection, proceso: str, **campos: Any
) -> None:
    """Actualiza las columnas indicadas, refrescando el latido."""
    desconocidas = set(campos) - COLUMNAS
    if desconocidas:
        raise ValueError(f"columnas no válidas: {sorted(desconocidas)}")

    campos.setdefault("heartbeat", ahora())
    asignaciones = ", ".join(f"{columna} = ?" for columna in campos)
    conexion.execute(
        f"UPDATE procesos SET {asignaciones} WHERE id = ?",
        (*campos.values(), proceso),
    )
    conexion.commit()


def registrar_evento(
    conexion: sqlite3.Connection, proceso: str, nodo: str, detalle: str = ""
) -> None:
    """Añade una línea a la traza de un proceso."""
    conexion.execute(
        "INSERT INTO eventos (proceso, nodo, ts, detalle) VALUES (?, ?, ?, ?)",
        (proceso, nodo, ahora(), detalle),
    )
    conexion.commit()


def obtener_proceso(conexion: sqlite3.Connection, proceso: str) -> dict[str, Any] | None:
    """Devuelve un proceso como diccionario, o ``None`` si no existe."""
    fila = conexion.execute("SELECT * FROM procesos WHERE id = ?", (proceso,)).fetchone()
    return dict(fila) if fila else None


def listar_procesos(conexion: sqlite3.Connection) -> list[dict[str, Any]]:
    """Lista todos los procesos, para el panel y los resúmenes."""
    filas = conexion.execute("SELECT * FROM procesos ORDER BY inicio").fetchall()
    return [dict(fila) for fila in filas]


def contar_por_estado(conexion: sqlite3.Connection) -> dict[str, int]:
    """Recuento de procesos por estado."""
    filas = conexion.execute(
        "SELECT estado, COUNT(*) AS total FROM procesos GROUP BY estado"
    ).fetchall()
    return {fila["estado"]: fila["total"] for fila in filas}


def existe_registrada(
    conexion: sqlite3.Connection, factura_id: str, excluir: str | None = None
) -> bool:
    """Indica si esa factura ya está registrada (regla de duplicados)."""
    sql = "SELECT 1 FROM procesos WHERE factura_id = ? AND estado = 'registrado'"
    parametros: list[Any] = [factura_id]
    if excluir:
        sql += " AND id <> ?"
        parametros.append(excluir)
    return conexion.execute(sql, parametros).fetchone() is not None


# ---------------------------------------------------------------------------
# Comandos: el canal por el que el panel habla con el worker
# ---------------------------------------------------------------------------
# El panel no reanuda el grafo (sería un segundo escritor del checkpointer).
# Deja la orden en esta tabla y el worker, que es el único que mueve el grafo,
# la recoge cuando sondea. Las acciones son las mismas que entiende el nodo de
# revisión: ver ``Command(resume=...)`` en el grafo.
ACCIONES = ("aprobado", "rechazado")


def enviar_comando(conexion: sqlite3.Connection, proceso: str, accion: str) -> int:
    """Encola una decisión humana sobre un proceso.

    Args:
        conexion: base de datos de procesos.
        proceso: ``thread_id`` del grafo (nombre del fichero de la factura).
        accion: ``aprobado`` o ``rechazado``.

    Returns:
        Identificador del comando encolado.

    Raises:
        ValueError: si la acción no es una de las admitidas.
    """
    if accion not in ACCIONES:
        raise ValueError(f"acción no válida: {accion!r}; se admite {ACCIONES}")

    cursor = conexion.execute(
        "INSERT INTO comandos (proceso, accion, ts, aplicado) VALUES (?, ?, ?, 0)",
        (proceso, accion, ahora()),
    )
    conexion.commit()
    return int(cursor.lastrowid or 0)


def comandos_pendientes(conexion: sqlite3.Connection) -> list[dict[str, Any]]:
    """Comandos que el worker todavía no ha aplicado, del más antiguo al más nuevo.

    Se ordena por ``id`` para que dos decisiones sobre la misma factura se
    apliquen en el orden en que se pulsaron los botones.
    """
    filas = conexion.execute(
        "SELECT * FROM comandos WHERE aplicado = 0 ORDER BY id"
    ).fetchall()
    return [dict(fila) for fila in filas]


def comandos_del_proceso(
    conexion: sqlite3.Connection, proceso: str
) -> list[dict[str, Any]]:
    """Historial de comandos de un proceso (el panel lo usa para avisar)."""
    filas = conexion.execute(
        "SELECT * FROM comandos WHERE proceso = ? ORDER BY id", (proceso,)
    ).fetchall()
    return [dict(fila) for fila in filas]


def marcar_comando(conexion: sqlite3.Connection, id_comando: int) -> None:
    """Da un comando por aplicado, para que el worker no lo repita."""
    conexion.execute("UPDATE comandos SET aplicado = 1 WHERE id = ?", (id_comando,))
    conexion.commit()
