"""Modelos de datos compartidos por el agente contable.

Aquí se definen los contratos, no la lógica:

- Lo que el LLM debe devolver al extraer una factura (``Factura``), con
  ``extra="forbid"`` para que un campo inventado por el modelo no pase
  desapercibido.
- Lo que hay en el "ERP" simulado: maestro de proveedores y albaranes.
- Las incidencias detectadas y su catálogo de códigos.
- El estado de un proceso en el panel.

Todos los importes son ``Decimal``: nada de ``float`` en contabilidad. Las
fechas son ``date`` y se validan en formato ISO (``AAAA-MM-DD``).

Uso:
    python -m core.schemas        # (desde la carpeta del proyecto)
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def redondear_euros(valor: Decimal) -> Decimal:
    """Redondea a céntimos con el criterio comercial (mitad hacia arriba)."""
    return valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def normalizar_importe(valor: Any) -> Any:
    """Convierte a número los importes que llegan como texto en formato español.

    Los modelos de lenguaje tienden a copiar los importes tal y como están
    impresos (``"1.234,56 €"``). Se limpian el símbolo de euro, los espacios y
    los separadores de miles, respetando el separador decimal que aparezca más
    a la derecha.

    Ejemplos: ``"1.234,56 €"`` -> ``"1234.56"``; ``"91,76"`` -> ``"91.76"``;
    ``"1,234.56"`` -> ``"1234.56"``; ``500`` -> ``500``.
    """
    if not isinstance(valor, str):
        return valor

    limpio = (
        valor.replace("€", "")
        .replace("EUR", "")
        .replace("%", "")
        .replace("\u00a0", "")
        .replace(" ", "")
        .strip()
    )
    if not limpio:
        return limpio

    if "," in limpio and "." in limpio:
        if limpio.rfind(",") > limpio.rfind("."):
            limpio = limpio.replace(".", "").replace(",", ".")
        else:
            limpio = limpio.replace(",", "")
    elif "," in limpio:
        limpio = limpio.replace(",", ".")

    return limpio


# Tipo reutilizable: un Decimal que también acepta importes en texto español.
Importe = Annotated[Decimal, BeforeValidator(normalizar_importe)]


def normalizar_cif(valor: str) -> str:
    """Deja el CIF en mayúsculas y sin separadores (espacios, guiones, puntos).

    Se usa como clave de comparación, para que ``B-987 654 32`` y
    ``B98765432`` se consideren el mismo identificador.
    """
    return "".join(c for c in valor if c.isalnum()).upper()


def normalizar_nombre(valor: str) -> str:
    """Normaliza una razón social para compararla (espacios y mayúsculas)."""
    return " ".join(valor.split()).casefold()


# ---------------------------------------------------------------------------
# Catálogos
# ---------------------------------------------------------------------------
class TipoIncidencia(StrEnum):
    """Motivos por los que una factura puede requerir revisión humana."""

    CIF_DESCONOCIDO = "cif_desconocido"        # el proveedor no está en el maestro
    CIF_NO_COINCIDE = "cif_no_coincide"        # el CIF de la factura no cuadra
    NOMBRE_NO_COINCIDE = "nombre_no_coincide"  # razón social distinta
    IBAN_NO_COINCIDE = "iban_no_coincide"      # cuenta bancaria distinta (fraude)
    FORMA_PAGO_NO_COINCIDE = "forma_pago_no_coincide"  # forma de pago distinta
    PLAZO_NO_COINCIDE = "plazo_no_coincide"    # vencimiento distinto al pactado
    IMPORTE_NO_CUADRA = "importe_no_cuadra"    # base + IVA - IRPF != total
    ALBARAN_NO_ENCONTRADO = "albaran_no_encontrado"
    ALBARAN_CANTIDAD = "albaran_cantidad"            # unidades distintas
    ALBARAN_CONCEPTO = "albaran_concepto"            # mercancía distinta
    ALBARAN_LINEA_FALTANTE = "albaran_linea_faltante"  # facturado y no entregado
    ALBARAN_LINEA_EXTRA = "albaran_linea_extra"      # entregado y no facturado
    SUPERA_UMBRAL = "supera_umbral"            # importe > umbral de aprobación
    DUPLICADO = "duplicado"                    # NIF + número + año ya registrado


class EstadoProceso(StrEnum):
    """Ciclo de vida de una factura dentro del agente."""

    ENCOLADO = "encolado"
    EXTRAYENDO = "extrayendo"
    VALIDANDO = "validando"
    EN_REVISION = "en_revision"
    REGISTRADO = "registrado"
    RECHAZADO = "rechazado"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Datos fiscales
# ---------------------------------------------------------------------------
class DatosFiscales(BaseModel):
    """Identificación fiscal de una de las partes de la factura."""

    model_config = ConfigDict(str_strip_whitespace=True)

    nombre: str
    cif: str
    direccion: str | None = None
    codigo_postal: str | None = None
    ciudad: str | None = None

    @property
    def cif_normalizado(self) -> str:
        """CIF listo para comparar contra el maestro."""
        return normalizar_cif(self.cif)


# ---------------------------------------------------------------------------
# Factura extraída del PDF
# ---------------------------------------------------------------------------
class LineaFactura(BaseModel):
    """Una línea de detalle tal y como viene impresa en la factura."""

    # extra="forbid": si el modelo añade un campo que no existe, salta el error
    # y se puede reintentar la extracción en lugar de aceptar basura.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    concepto: str
    cantidad: Importe
    precio_unitario: Importe
    importe: Importe

    @property
    def importe_calculado(self) -> Decimal:
        """Importe que debería figurar según cantidad y precio."""
        return redondear_euros(self.cantidad * self.precio_unitario)


class Factura(BaseModel):
    """Factura de proveedor ya normalizada (salida de la extracción)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    numero: str
    fecha_expedicion: date
    fecha_operacion: date | None = None
    referencia_albaran: str  # dato clave para el cruce documental

    proveedor: DatosFiscales
    cliente: DatosFiscales

    lineas: list[LineaFactura] = Field(min_length=1)

    base_imponible: Importe
    tipo_iva: Importe = Decimal(21)
    cuota_iva: Importe
    # Porcentaje de retención (0 si no aplica). El importe se calcula.
    tipo_retencion_irpf: Importe = Decimal(0)
    total: Importe

    forma_pago: str | None = None
    iban: str | None = None

    @property
    def anio(self) -> int:
        """Año fiscal de la factura (el de la fecha de expedición)."""
        return self.fecha_expedicion.year

    @property
    def identificador(self) -> str:
        """Clave única de la factura: se usa como ``thread_id`` del grafo."""
        return f"{self.proveedor.cif_normalizado}-{self.numero}-{self.anio}"

    @property
    def cuota_retencion(self) -> Decimal:
        """Importe de la retención de IRPF, calculado sobre la base."""
        return redondear_euros(self.base_imponible * self.tipo_retencion_irpf / 100)

    @property
    def total_esperado(self) -> Decimal:
        """Total que debería figurar: base + IVA - retención."""
        return redondear_euros(self.base_imponible + self.cuota_iva - self.cuota_retencion)


# ---------------------------------------------------------------------------
# ERP simulado: maestro de proveedores
# ---------------------------------------------------------------------------
class Proveedor(BaseModel):
    """Ficha de proveedor tal y como estaría en el ERP."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str
    nombre: str
    cif: str
    direccion: str
    codigo_postal: str
    ciudad: str
    pais: str = "España"
    iban: str
    forma_pago: str
    dias_vencimiento: int = 30
    retencion_irpf: Decimal = Decimal(0)
    activo: bool = True


class MaestroProveedores(BaseModel):
    """Contenido de ``datos/maestro_proveedores.json``."""

    model_config = ConfigDict(extra="forbid")

    version: int
    descripcion: str | None = None
    moneda: str = "EUR"
    proveedores: list[Proveedor] = Field(min_length=1)

    def indice_por_cif(self) -> dict[str, Proveedor]:
        """Indexa los proveedores por CIF normalizado."""
        return {normalizar_cif(proveedor.cif): proveedor for proveedor in self.proveedores}

    def buscar(self, cif: str) -> Proveedor | None:
        """Devuelve el proveedor con ese CIF, o ``None`` si no está en el ERP."""
        return self.indice_por_cif().get(normalizar_cif(cif))

    def buscar_por_nombre(self, nombre: str) -> Proveedor | None:
        """Busca por razón social, para detectar fichas con el CIF mal tecleado."""
        buscado = normalizar_nombre(nombre)
        for proveedor in self.proveedores:
            if normalizar_nombre(proveedor.nombre) == buscado:
                return proveedor
        return None


# ---------------------------------------------------------------------------
# ERP simulado: albaranes
# ---------------------------------------------------------------------------
class LineaAlbaran(BaseModel):
    """Mercancía o servicio realmente entregado."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    concepto: str
    cantidad: Importe


class Albaran(BaseModel):
    """Albarán de entrega tal y como estaría en el ERP."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    numero: str
    fecha: date
    proveedor: DatosFiscales
    destinatario: DatosFiscales
    direccion_entrega: str | None = None
    lineas: list[LineaAlbaran] = Field(min_length=1)


class Albaranes(BaseModel):
    """Contenido de ``datos/albaranes/albaranes.json``."""

    model_config = ConfigDict(extra="forbid")

    version: int
    descripcion: str | None = None
    albaranes: list[Albaran] = Field(min_length=1)

    def indice_por_numero(self) -> dict[str, Albaran]:
        """Indexa los albaranes por número (tal cual viene impreso)."""
        return {albaran.numero: albaran for albaran in self.albaranes}

    def buscar(self, numero: str) -> Albaran | None:
        """Devuelve el albarán con ese número, o ``None`` si no existe."""
        return self.indice_por_numero().get(numero.strip().upper())


# ---------------------------------------------------------------------------
# Resultado de la validación
# ---------------------------------------------------------------------------
class Incidencia(BaseModel):
    """Un problema concreto detectado al validar una factura."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    codigo: TipoIncidencia
    mensaje: str
    detalle: dict[str, str | int] | None = None


class ResultadoValidacion(BaseModel):
    """Conjunto de incidencias de una factura y decisión derivada."""

    model_config = ConfigDict(extra="forbid")

    factura_id: str
    incidencias: list[Incidencia] = Field(default_factory=list)
    # Se rellena cuando el importe supera el umbral, para poder mostrarlo en el
    # panel aunque no haya ninguna otra incidencia.
    supera_umbral: bool = False

    @property
    def tiene_incidencias(self) -> bool:
        """True si se detectó algún problema documental o fiscal."""
        return bool(self.incidencias)

    @property
    def requiere_revision(self) -> bool:
        """True si la factura no puede registrarse automáticamente."""
        return self.tiene_incidencias or self.supera_umbral

    @property
    def codigos(self) -> list[TipoIncidencia]:
        """Códigos de las incidencias, útil para tests y métricas."""
        return [incidencia.codigo for incidencia in self.incidencias]


if __name__ == "__main__":
    # Comprobación de humo: carga el ERP simulado y demuestra que un JSON
    # mal formado se rechaza.
    from pydantic import ValidationError

    from config import ajustes

    maestro = MaestroProveedores.model_validate_json(
        ajustes.ruta_maestro.read_text(encoding="utf-8")
    )
    albaranes = Albaranes.model_validate_json(
        ajustes.ruta_albaranes.read_text(encoding="utf-8")
    )
    print(f"proveedores cargados: {len(maestro.proveedores)}")
    print(f"albaranes cargados:   {len(albaranes.albaranes)}")
    print(f"primer CIF indexado:  {next(iter(maestro.indice_por_cif()))}")

    rota = '{"version": 1, "proveedores": [{"id": "PRV-X"}]}'
    try:
        MaestroProveedores.model_validate_json(rota)
    except ValidationError as exc:
        print(f"JSON mal formado rechazado correctamente ({exc.error_count()} errores)")
    else:
        raise SystemExit("ERROR: el maestro incompleto debería haber fallado")
