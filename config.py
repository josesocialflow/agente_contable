"""Configuración central del agente de cuentas por pagar.

Concentra todos los valores ajustables: rutas del "ERP" simulado, modelo de
lenguaje, reglas de negocio y credenciales de notificación. Ningún otro módulo
debería llevar rutas o umbrales escritos a mano.

Los valores se pueden sobrescribir con variables de entorno con el prefijo
``AGENTE_`` o con un fichero ``.env`` (se leen, por orden, el de esta carpeta y
el de la raíz del repositorio):

    AGENTE_MODELO=gpt-4o-mini
    AGENTE_UMBRAL_APROBACION=1000
    AGENTE_NOTIFICADOR=telegram
    AGENTE_TELEGRAM_TOKEN=...
    AGENTE_TELEGRAM_CHAT_ID=...

Uso:
    from config import ajustes
    print(ajustes.directorio_facturas)
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Rutas de referencia, calculadas a partir de la ubicación de este fichero para
# que el proyecto funcione sin importar desde dónde se ejecute.
DIRECTORIO_BASE = Path(__file__).resolve().parent
DIRECTORIO_PROYECTO = DIRECTORIO_BASE.parent  # raíz del repo: allí vive el .env compartido
DIRECTORIO_DATOS = DIRECTORIO_BASE / "datos"

# Además de para esta configuración, el .env se carga en os.environ porque hay
# librerías que no lo leen de aquí: ChatOpenAI toma OPENAI_API_KEY del entorno.
# Se carga primero la raíz y después el proyecto sin `override`, de modo que
# gana el primero que defina la variable: el mismo criterio que en Configuracion.
load_dotenv(DIRECTORIO_PROYECTO / ".env")
load_dotenv(DIRECTORIO_BASE / ".env")


class Configuracion(BaseSettings):
    """Ajustes del agente, con valores por defecto listos para la demo."""

    model_config = SettingsConfigDict(
        env_prefix="AGENTE_",
        # Se admiten dos .env: el del proyecto y el de la raíz del repo. El
        # segundo solo se usa mientras el proyecto no sea autocontenido.
        env_file=(DIRECTORIO_BASE / ".env", DIRECTORIO_PROYECTO / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Rutas del ERP simulado -------------------------------------------
    directorio_datos: Path = DIRECTORIO_DATOS
    directorio_facturas: Path = DIRECTORIO_DATOS / "fras"
    ruta_albaranes: Path = DIRECTORIO_DATOS / "albaranes" / "albaranes.json"
    ruta_maestro: Path = DIRECTORIO_DATOS / "maestro_proveedores.json"
    directorio_procesados: Path = DIRECTORIO_DATOS / "procesados"

    # --- Estado del agente ------------------------------------------------
    ruta_db: Path = DIRECTORIO_DATOS / "agente.db"
    ruta_checkpoints: Path = DIRECTORIO_DATOS / "checkpoints.sqlite"
    ruta_pagos: Path = DIRECTORIO_DATOS / "pagos.json"
    ruta_registro: Path = DIRECTORIO_DATOS / "registro_facturas.json"

    # Cada cuántos segundos sondea el worker la tabla de comandos cuando se
    # ejecuta con --vigilar: el panel deja ahí las decisiones humanas y el
    # worker las aplica. Dos segundos dan la sensación de tiempo real en el
    # vídeo sin generar lecturas constantes de la base de datos.
    intervalo_vigilancia: float = Field(default=2.0, gt=0)

    # --- Modelo de lenguaje ----------------------------------------------
    modelo: str = "gpt-4o-mini"
    temperatura: float = Field(default=0.0, ge=0.0, le=2.0)
    # Reintentos de extracción cuando el modelo devuelve un JSON que no valida.
    reintentos_extraccion: int = Field(default=2, ge=0, le=5)

    # --- Coste de la extracción -------------------------------------------
    # Tarifa pública de gpt-4o-mini en dólares por millón de tokens. Con esto se
    # estima el "coste de extracción" del panel: lo que cuesta *ejecutar el
    # agente*, que no tiene nada que ver con el importe de las facturas. Si
    # cambia la tarifa, se cambia aquí y no en el código.
    precio_entrada_por_millon: float = Field(default=0.15, ge=0)
    precio_salida_por_millon: float = Field(default=0.60, ge=0)

    # --- Reglas de negocio ------------------------------------------------
    # Por encima de este importe la factura va a revisión humana aunque no
    # tenga incidencias (regla del caso de estudio).
    umbral_aprobacion: Decimal = Decimal("1000.00")
    # Plazo por defecto si el maestro de proveedores no lo indica.
    dias_vencimiento_por_defecto: int = 30

    # --- Notificaciones ---------------------------------------------------
    notificador: Literal["consola", "telegram"] = "consola"
    telegram_token: str | None = None
    telegram_chat_id: str | None = None

    # --- Panel Streamlit --------------------------------------------------
    auto_refresco_segundos: float = Field(default=3.0, gt=0)

    @property
    def rutas_datos(self) -> dict[str, Path]:
        """Rutas principales, útil para diagnósticos y mensajes de error."""
        return {
            "facturas": self.directorio_facturas,
            "albaranes": self.ruta_albaranes,
            "maestro": self.ruta_maestro,
            "db": self.ruta_db,
            "checkpoints": self.ruta_checkpoints,
            "pagos": self.ruta_pagos,
            "registro": self.ruta_registro,
        }

    def comprobar_rutas(self) -> list[str]:
        """Devuelve las rutas de datos que faltan (lista vacía si están todas).

        Se usa al arrancar el worker para fallar pronto y con un mensaje claro,
        en lugar de estrellarse a mitad del procesamiento.
        """
        obligatorias = {
            "directorio_facturas": self.directorio_facturas,
            "ruta_albaranes": self.ruta_albaranes,
            "ruta_maestro": self.ruta_maestro,
        }
        return [nombre for nombre, ruta in obligatorias.items() if not ruta.exists()]


# Instancia única, importada por el resto de módulos.
ajustes = Configuracion()


if __name__ == "__main__":
    print("Configuración efectiva del agente")
    print("-" * 40)
    for campo, valor in ajustes.model_dump().items():
        print(f"{campo:<32} {valor}")
    faltan = ajustes.comprobar_rutas()
    print("-" * 40)
    print("rutas de datos completas" if not faltan else f"faltan: {', '.join(faltan)}")
