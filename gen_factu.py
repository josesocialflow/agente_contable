"""Generador de facturas de proveedor en PDF para Logística Ibérica S.L.

Genera un lote de facturas sintéticas con formato fiscal español que incluyen,
además de los datos habituales (emisor, receptor, líneas, base, IVA y total),
la **referencia del albarán** que se factura. Eso permite probar el cruce
factura <-> albarán (3-way match) del agente de cuentas por pagar.

Además se emiten los **albaranes de entrega** de cada factura. Por defecto se
escriben todos juntos en un único JSON (``datos/albaranes/albaranes.json``), que
hace de tabla de albaranes del ERP simulado; con ``--formato-albaran pdf`` se
genera un PDF por albarán, como antes.

De los 20 albaranes, 15 coinciden con lo facturado y 5 contienen una
discrepancia deliberada (cantidad o mercancía) para que el agente tenga algo
que detectar:

    cantidad_menor      el albarán entrega menos unidades de las facturadas
    cantidad_mayor      el albarán entrega más unidades de las facturadas
    mercancia_distinta  una línea entrega un artículo distinto al facturado
    linea_omitida       falta una línea facturada (no se entregó)
    linea_extra         se entrega un artículo que no figura en la factura

El JSON NO incluye qué albarán está mal ni de qué tipo: es la fuente de datos
que consulta el agente, así que solo contiene información de entrega. Las
discrepancias se muestran por consola al terminar.

Los albaranes usan un generador aleatorio propio, de modo que añadirlos no
altera la secuencia de las facturas: un lote ya generado se reproduce igual.

Perfil de importes
------------------
Las cantidades se escalan (``ESCALA_CANTIDADES``) y tres facturas del lote se
generan con cantidades multiplicadas (``INDICES_IMPORTE_ALTO``). Con eso la
mayoría de las facturas queda por debajo del umbral de aprobación y
exactamente tres lo superan, de forma predecible: la regla "importe > umbral"
añade tres revisiones limpias y no tapa las incidencias documentales. La escala
se aplica después de sortear la cantidad, así que no altera el flujo aleatorio.

Campos fiscales que incluye cada factura (RD 1619/2012, arts. 6 y 7):
    - Número de factura correlativo y fecha de expedición.
    - Nombre/razón social, NIF y domicilio del emisor y del destinatario.
    - Descripción de la operación (líneas con cantidad y precio unitario).
    - Base imponible, tipo de IVA, cuota y total.
    - Fecha de operación (la del albarán), distinta de la de expedición.
    - Retención de IRPF en los servicios profesionales.
    - Forma de pago y vencimiento.

Uso:
    python gen_factu.py                       # 20 facturas + albaranes.json
    python gen_factu.py --fecha-base 2026-11-14  # fechas relativas a ese día
    python gen_factu.py --cantidad 5 --salida /tmp/prueba
    python gen_factu.py --semilla 7           # lote reproducible distinto
    python gen_factu.py --errores 0           # todos los albaranes correctos
    python gen_factu.py --formato-albaran pdf # un PDF por albarán

``--fecha-base`` reparte las fechas del lote alrededor de ese día: ~25 % de
facturas ya vencidas, ~25 % que vencen en la semana siguiente y ~50 % con
vencimiento futuro. Sirve para que la vista de vencimientos y la remesa
simulada tengan sentido en la demo.
"""

from __future__ import annotations

import argparse
import json
import random
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------------------
# Rutas y constantes generales
# ---------------------------------------------------------------------------
DIRECTORIO_SCRIPT = Path(__file__).resolve().parent
SALIDA_POR_DEFECTO = DIRECTORIO_SCRIPT / "datos" / "fras"
SALIDA_ALBARANES_POR_DEFECTO = DIRECTORIO_SCRIPT / "datos" / "albaranes"
# Nombre del fichero único que sustituye a la tabla de albaranes del ERP.
NOMBRE_ALBARANES_JSON = "albaranes.json"

# Las 20 facturas existentes en datos/ usan los números 001-020, así que este
# lote continúa la serie a partir de 021 para que el agente no las detecte como
# duplicadas (NIF + número + año). El año sale de --fecha-base.
NUMERO_INICIAL = 21
TIPO_IVA = Decimal(21)  # IVA general aplicado por los proveedores del caso
DIAS_VENCIMIENTO = 30
MENSAJE_PIE = "Documento generado para simulación de extracción documental con Agente de IA."

# Reparto del lote de albaranes: por defecto 5 de cada 20 llevan una
# discrepancia respecto a la factura. Los tipos se reparten en orden para que
# un lote de 5 errores cubra exactamente las cinco clases posibles.
ERRORES_POR_DEFECTO = 5
TIPOS_ERROR = (
    "cantidad_menor",
    "cantidad_mayor",
    "mercancia_distinta",
    "linea_omitida",
    "linea_extra",
)

# --- Perfil de importes ----------------------------------------------------
# La demo necesita un reparto predecible: casi todas las facturas por debajo
# del umbral de aprobación y unas pocas claramente por encima, para que la
# regla "importe > umbral" no eclipse las incidencias documentales.
#
# Se aplica una escala a las cantidades (los precios unitarios se mantienen
# realistas) en lugar de tocar cada catálogo. Al escalar después de generar las
# cantidades, el flujo aleatorio no cambia: las líneas son las mismas que sin
# escala.
ESCALA_CANTIDADES = 0.6
# Facturas con perfil "grande" (índices 0-based del lote): representan
# servicios trimestrales y superan el umbral con margen. Se eligen índices que
# no coinciden con ninguna incidencia deliberada, para que las revisiones por
# importe sean adicionales a las 7 documentales.
INDICES_IMPORTE_ALTO = (7, 12, 18)
FACTOR_IMPORTE_ALTO = 4.0

# Destinatario de todas las facturas (la empresa del caso de estudio).
RECEPTOR = {
    "nombre": "Logística Ibérica S.L.",
    "cif": "B80000000",
    "direccion": "Pol. Ind. Los Olivos, C/ Principal 8",
    "cp_ciudad": "28000 Madrid",
}

# Paleta de color discreta, en la línea visual de los PDF del caso.
TINTA = colors.HexColor("#1f2a37")
SUAVE = colors.HexColor("#5b6472")
LINEA = colors.HexColor("#c9cfd8")
FONDO = colors.HexColor("#eef1f5")


# ---------------------------------------------------------------------------
# Modelo de datos
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Articulo:
    """Artículo o servicio facturable con su rango de precio y cantidad."""

    concepto: str
    precio_min: int  # en euros, sin decimales
    precio_max: int
    cantidad_min: int
    cantidad_max: int


@dataclass(frozen=True)
class Proveedor:
    """Datos fiscales del emisor y su catálogo de artículos."""

    nombre: str
    cif: str
    direccion: str
    cp_ciudad: str
    iban: str
    serie: str  # prefijo del número de factura y de albarán
    articulos: tuple[Articulo, ...]
    retencion_irpf: Decimal = Decimal(0)
    # Artículos que el proveedor no suele facturar pero que sí podría entregar
    # por error (referencia equivocada, modelo distinto...). Se usan SOLO para
    # generar discrepancias en los albaranes: no entran en el catálogo
    # facturable, de modo que la secuencia aleatoria de las facturas no cambia.
    articulos_error: tuple[Articulo, ...] = ()


# Las diez empresas proveedoras del caso de estudio, con los mismos datos
# fiscales que aparecen en los PDF de datos/ para que el maestro de proveedores
# y el IBAN cuadren con el histórico.
PROVEEDORES: tuple[Proveedor, ...] = (
    Proveedor(
        nombre="Suministros de Oficina Global S.A.",
        cif="A87654321",
        direccion="Av. Diagonal 458",
        cp_ciudad="08006 Barcelona",
        iban="ES91 0049 1500 2222 3333 4444",
        serie="FAC",
        articulos=(
            Articulo("Papel A4 80 g (caja de 5 paquetes)", 22, 27, 3, 12),
            Articulo("Tóner láser negro HP", 105, 125, 1, 4),
            Articulo("Archivadores palanca lomo ancho", 17, 22, 5, 20),
            Articulo("Bolígrafos azules (caja de 50)", 11, 16, 2, 10),
            Articulo("Grapadoras metálicas", 7, 12, 2, 10),
        ),
        articulos_error=(
            Articulo("Tóner láser color HP", 140, 170, 1, 5),
            Articulo("Papel A3 80 g (caja de 5 paquetes)", 34, 42, 2, 15),
        ),
    ),
    Proveedor(
        nombre="Consultoría y Marketing Digital S.L.",
        cif="B98765432",
        direccion="Plaza de España 3",
        cp_ciudad="41013 Sevilla",
        iban="ES12 0081 5000 5555 6666 7777",
        serie="MD",
        retencion_irpf=Decimal(15),
        articulos=(
            Articulo("Gestión de campañas SEM y LinkedIn Ads", 350, 500, 1, 1),
            Articulo("Auditoría SEO técnico de la web corporativa", 200, 280, 1, 1),
            Articulo("Informe mensual de analítica y KPIs", 90, 150, 1, 1),
            Articulo("Copywriting para landing pages", 80, 120, 1, 2),
        ),
        articulos_error=(
            Articulo("Gestión de campañas en Meta Ads", 900, 1300, 1, 2),
            Articulo("Auditoría de accesibilidad web", 450, 650, 1, 1),
        ),
    ),
    Proveedor(
        nombre="Limpiezas y Mantenimiento Integral S.L.",
        cif="B11223344",
        direccion="Calle Gran Vía 78",
        cp_ciudad="28013 Madrid",
        iban="ES56 2080 0300 8888 9999 0000",
        serie="LIM",
        articulos=(
            Articulo("Limpieza de oficinas (hora de servicio)", 16, 19, 8, 28),
            Articulo("Desinfección integral de instalaciones", 120, 180, 1, 2),
            Articulo("Mantenimiento de zonas ajardinadas", 60, 90, 1, 2),
            Articulo("Suministro de consumibles de limpieza", 80, 120, 1, 2),
        ),
        articulos_error=(
            Articulo("Limpieza industrial de naves", 220, 320, 1, 3),
            Articulo("Desinsectación y desratización", 180, 260, 1, 2),
        ),
    ),
    Proveedor(
        nombre="Comunicaciones y Redes Biz S.L.",
        cif="B99887766",
        direccion="Calle Larios 22",
        cp_ciudad="29005 Málaga",
        iban="ES45 0075 1111 7777 8888 9999",
        serie="CRB",
        articulos=(
            Articulo("Fibra simétrica 1 Gbps con IP fija", 170, 190, 1, 1),
            Articulo("Centralita virtual 25 extensiones", 250, 320, 1, 1),
            Articulo("Línea móvil de flota", 20, 30, 5, 15),
        ),
        articulos_error=(
            Articulo("Teléfono IP de sobremesa", 85, 120, 1, 10),
            Articulo("Centralita virtual 12 extensiones", 380, 460, 1, 1),
        ),
    ),
    Proveedor(
        nombre="Servicios de Imprenta y Publicidad S.A.",
        cif="A66778899",
        direccion="Calle Alcalá 45",
        cp_ciudad="28014 Madrid",
        iban="ES89 0049 6666 3333 4444 5555",
        serie="IMP",
        articulos=(
            Articulo("Folletos A4 cuatricromía (millar)", 100, 150, 1, 2),
            Articulo("Carteles plastificados", 3, 6, 10, 40),
            Articulo("Vinilos decorativos para flota", 25, 40, 2, 5),
            Articulo("Diseño gráfico de campaña", 100, 150, 1, 2),
        ),
        articulos_error=(
            Articulo("Lonas publicitarias 3x1 m", 90, 150, 1, 6),
            Articulo("Tarjetas de visita (millar)", 45, 70, 1, 10),
        ),
    ),
    Proveedor(
        nombre="Mobiliario de Oficina Moderno S.L.",
        cif="B44556677",
        direccion="Calle Sierpes 14",
        cp_ciudad="41004 Sevilla",
        iban="ES67 0128 5555 2222 3333 4444",
        serie="MOM",
        articulos=(
            Articulo("Silla ergonómica con reposabrazos", 120, 180, 2, 4),
            Articulo("Mesa operativa 160x80", 180, 260, 1, 2),
            Articulo("Armario metálico dos puertas", 220, 300, 1, 1),
            Articulo("Cajonera rodante", 60, 90, 2, 4),
        ),
        articulos_error=(
            Articulo("Estantería metálica de carga", 150, 220, 2, 12),
            Articulo("Biombo separador de oficina", 190, 280, 1, 6),
        ),
    ),
    Proveedor(
        nombre="Electricidad y Fontanería del Norte S.A.",
        cif="A33445566",
        direccion="Paseo de Zorrilla 15",
        cp_ciudad="47007 Valladolid",
        iban="ES78 2100 8888 1111 2222 3333",
        serie="EFN",
        articulos=(
            Articulo("Revisión y certificación del cuadro eléctrico", 180, 260, 1, 2),
            Articulo("Sustitución de luminarias LED", 45, 70, 4, 10),
            Articulo("Mano de obra de mantenimiento (hora)", 28, 45, 2, 8),
        ),
        articulos_error=(
            Articulo("Instalación de punto de recarga eléctrico", 450, 700, 1, 2),
            Articulo("Revisión de grupo electrógeno", 220, 340, 1, 2),
        ),
    ),
    Proveedor(
        nombre="Logística y Almacenaje Express S.L.",
        cif="B55667788",
        direccion="Pol. Ind. Sur, Nave 4",
        cp_ciudad="46013 Valencia",
        iban="ES90 0182 3333 4444 5555 6666",
        serie="LAE",
        articulos=(
            Articulo("Almacenaje de palet europeo (30 días)", 14, 16, 8, 36),
            Articulo("Picking y preparación de pedidos", 4, 6, 20, 85),
            Articulo("Transporte nacional de mercancía", 120, 260, 1, 2),
        ),
        articulos_error=(
            Articulo("Descarga de contenedor", 180, 260, 1, 4),
            Articulo("Etiquetado y precintado de palets", 3, 6, 50, 400),
        ),
    ),
    Proveedor(
        nombre="Tecnología Avanzada S.L.",
        cif="B12345678",
        direccion="Calle Mayor 12",
        cp_ciudad="28013 Madrid",
        iban="ES34 2100 0000 1234 5678 9012",
        serie="TEC",
        articulos=(
            Articulo("Licencia ERP Cloud (usuario/mes)", 35, 45, 5, 12),
            Articulo("Soporte técnico 24/7 y copias de seguridad", 250, 320, 1, 1),
            Articulo("Servidor cloud dedicado", 120, 180, 1, 2),
        ),
        articulos_error=(
            Articulo("Firewall perimetral gestionado", 300, 450, 1, 2),
            Articulo("Licencia de ofimática (usuario/mes)", 8, 14, 10, 40),
        ),
    ),
    Proveedor(
        nombre="Seguridad y Protección Activa S.A.",
        cif="A22334455",
        direccion="Av. de la Constitución 9",
        cp_ciudad="48009 Bilbao",
        iban="ES23 2038 9999 0000 1111 2222",
        serie="SPA",
        articulos=(
            Articulo("Vigilancia y control de accesos (hora)", 20, 24, 10, 30),
            Articulo("Mantenimiento de sistema de alarmas", 250, 320, 1, 1),
            Articulo("Revisión de extintores y BIEs", 25, 40, 2, 8),
        ),
        articulos_error=(
            Articulo("Instalación de videovigilancia CCTV", 600, 900, 1, 2),
            Articulo("Control de rondas con lector RFID", 120, 180, 1, 6),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Utilidades de formato
# ---------------------------------------------------------------------------
def _escapar(texto: str) -> str:
    """Escapa los caracteres especiales de XML que interpreta Paragraph."""
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _dos_decimales(valor: Decimal) -> Decimal:
    """Redondea a céntimos con el criterio comercial (mitad hacia arriba)."""
    return valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _euros(valor: Decimal) -> str:
    """Formatea un importe al estilo español: 1.845,50 €."""
    texto = f"{abs(valor):,.2f}"  # 1,845.50
    texto = texto.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    signo = "-" if valor < 0 else ""
    return f"{signo}{texto} €"


def _fecha(valor: date) -> str:
    """Formatea una fecha como dd/mm/aaaa (formato habitual en facturación)."""
    return valor.strftime("%d/%m/%Y")


def _fecha_iso(texto: str) -> date:
    """Convierte ``AAAA-MM-DD`` en ``date``, con error legible para argparse."""
    try:
        return date.fromisoformat(texto)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"fecha no válida: {texto!r} (formato esperado AAAA-MM-DD)"
        ) from exc


def _slug(texto: str) -> str:
    """Convierte un nombre en un fragmento válido para un nombre de fichero."""
    normalizado = unicodedata.normalize("NFKD", texto)
    sin_acentos = "".join(c for c in normalizado if not unicodedata.combining(c))
    limpio = "".join(c if c.isalnum() else "_" for c in sin_acentos)
    return "_".join(parte for parte in limpio.split("_") if parte)


# ---------------------------------------------------------------------------
# Generación de los datos de cada factura
# ---------------------------------------------------------------------------
def construir_factura(
    rng: random.Random, proveedor: Proveedor, indice: int, fecha_base: date
) -> dict:
    """Crea todos los datos (no solo el PDF) de una factura.

    Args:
        rng: generador aleatorio ya sembrado, para que el lote sea reproducible.
        proveedor: emisor de la factura.
        indice: posición dentro del lote (0-based); define número y fichero.
        fecha_base: día de referencia del lote (normalmente el de la demo).
            Las fechas se reparten alrededor de él para que convivan
            vencimientos pasados, próximos y futuros.

    Returns:
        Diccionario con fechas, referencias, líneas y totales calculados.
    """
    anio = fecha_base.year
    # Referencias correlativas: el albarán y la factura comparten el índice del
    # lote, así que cada factura remite a un albarán distinto y trazable.
    numero = f"{proveedor.serie}-{anio}-{NUMERO_INICIAL + indice:03d}"
    albaran = f"ALB-{proveedor.serie}-{anio}-{NUMERO_INICIAL + indice:03d}"

    # Reparto de fechas alrededor de --fecha-base. El bucket se decide por la
    # posición en el lote (no por el azar) para que las proporciones sean
    # siempre 25 % / 25 % / 50 %:
    #   bucket 0  ya vencidas:      vencimiento entre 10 y 20 días atrás
    #   bucket 1  vencen pronto:    vencimiento entre hoy y 7 días vista
    #   resto     futuras:          vencimiento entre 8 y 30 días vista
    #
    # Se conservan las dos llamadas aleatorias originales (rangos 0-89 y 1-10)
    # para que mover las fechas no altere el resto del lote: el reparto de
    # líneas, importes y discrepancias de albarán sale idéntico al anterior.
    selector = rng.randint(0, 89)
    desfase = rng.randint(1, 10)

    bucket = indice % 4
    if bucket == 0:  # vencidas hace 10-20 días
        dias_factura = 40 + selector % 11
    elif bucket == 1:  # vencen entre hoy y 7 días vista
        dias_factura = 23 + selector % 8
    else:  # futuras: entre 8 y 30 días vista
        dias_factura = selector % 23

    # El albarán siempre es anterior a la factura (1-10 días), y la factura se
    # emitió 'dias_factura' días antes de la fecha base.
    fecha_factura = fecha_base - timedelta(days=dias_factura)
    fecha_albaran = fecha_factura - timedelta(days=desfase)
    fecha_vencimiento = fecha_factura + timedelta(days=DIAS_VENCIMIENTO)

    # Entre 1 y 4 líneas distintas, sin repetir artículo.
    maximo_lineas = min(4, len(proveedor.articulos))
    elegidos = rng.sample(proveedor.articulos, k=rng.randint(1, maximo_lineas))

    lineas = []
    escala_cantidad = (
        FACTOR_IMPORTE_ALTO if indice in INDICES_IMPORTE_ALTO else ESCALA_CANTIDADES
    )
    for articulo in elegidos:
        cantidad_base = rng.randint(articulo.cantidad_min, articulo.cantidad_max)
        # La escala se aplica DESPUÉS del sorteo: el consumo del generador
        # aleatorio es idéntico al de un lote sin escalar, así que las líneas
        # (qué artículo y en qué orden) no cambian.
        cantidad = max(1, round(cantidad_base * escala_cantidad))
        # Precio en céntimos para trabajar con Decimal exacto (sin float).
        precio = Decimal(rng.randint(articulo.precio_min * 100, articulo.precio_max * 100)) / 100
        importe = _dos_decimales(precio * cantidad)
        lineas.append(
            {
                "concepto": articulo.concepto,
                "cantidad": cantidad,
                "precio": precio,
                "importe": importe,
            }
        )

    base = _dos_decimales(sum((linea["importe"] for linea in lineas), Decimal(0)))
    cuota_iva = _dos_decimales(base * TIPO_IVA / 100)
    retencion = _dos_decimales(base * proveedor.retencion_irpf / 100)
    total = _dos_decimales(base + cuota_iva - retencion)

    return {
        "proveedor": proveedor,
        "numero": numero,
        "albaran": albaran,
        "fecha_albaran": fecha_albaran,
        "fecha_factura": fecha_factura,
        "fecha_vencimiento": fecha_vencimiento,
        "lineas": lineas,
        "base": base,
        "cuota_iva": cuota_iva,
        "retencion": retencion,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Generación de los datos de cada albarán
# ---------------------------------------------------------------------------
def construir_albaran(
    rng: random.Random, factura: dict, tipo_error: str | None = None
) -> dict:
    """Crea el albarán correspondiente a una factura ya calculada.

    Sin ``tipo_error``, el albarán reproduce exactamente las líneas facturadas
    (mismo concepto y misma cantidad). Con ``tipo_error`` se introduce una
    discrepancia controlada para que el cruce documental tenga algo que
    detectar; ver ``TIPOS_ERROR`` para el catálogo.

    Si el tipo pedido no se puede aplicar en esa factura concreta (por ejemplo,
    ``linea_omitida`` cuando solo hay una línea, o ``mercancia_distinta``
    cuando el catálogo del proveedor no ofrece otro artículo), se degrada a
    ``cantidad_mayor``, que siempre es aplicable. Así nunca se devuelve por
    error un albarán "correcto" cuando se pidió uno con discrepancia.

    Args:
        rng: generador aleatorio propio de los albaranes.
        factura: datos de la factura a la que corresponde la entrega.
        tipo_error: discrepancia a introducir, o ``None`` para un albarán correcto.

    Returns:
        Diccionario con número, fecha, líneas y descripción de la discrepancia.
    """
    proveedor: Proveedor = factura["proveedor"]
    lineas = [
        {"concepto": linea["concepto"], "cantidad": linea["cantidad"]}
        for linea in factura["lineas"]
    ]

    def articulo_no_facturado() -> Articulo | None:
        """Devuelve un artículo distinto de los ya presentes en el albarán.

        Se prefieren los artículos de ``articulos_error`` (entregas plausibles
        pero no contratadas); si no quedan, se recurre al catálogo facturable.
        """
        facturados = {linea["concepto"] for linea in lineas}
        for catalogo in (proveedor.articulos_error, proveedor.articulos):
            disponibles = [a for a in catalogo if a.concepto not in facturados]
            if disponibles:
                return rng.choice(disponibles)
        return None

    aplicado: str | None = None
    detalle = ""

    if tipo_error == "cantidad_menor":
        # Solo tiene sentido si hay alguna línea con más de una unidad.
        candidatas = [linea for linea in lineas if linea["cantidad"] > 1]
        if candidatas:
            linea = rng.choice(candidatas)
            original = linea["cantidad"]
            linea["cantidad"] = max(1, original - max(1, original // 4))
            if linea["cantidad"] < original:
                aplicado = "cantidad_menor"
                detalle = (
                    f"{linea['concepto']}: facturadas {original}, "
                    f"entregadas {linea['cantidad']}"
                )

    elif tipo_error == "cantidad_mayor":
        linea = rng.choice(lineas)
        original = linea["cantidad"]
        linea["cantidad"] = original + max(1, original // 5)
        aplicado = "cantidad_mayor"
        detalle = (
            f"{linea['concepto']}: facturadas {original}, "
            f"entregadas {linea['cantidad']}"
        )

    elif tipo_error == "mercancia_distinta":
        sustituto = articulo_no_facturado()
        if sustituto is not None:
            linea = rng.choice(lineas)
            detalle = (
                f"facturado '{linea['concepto']}', "
                f"entregado '{sustituto.concepto}'"
            )
            linea["concepto"] = sustituto.concepto
            aplicado = "mercancia_distinta"

    elif tipo_error == "linea_omitida" and len(lineas) > 1:
        linea = lineas.pop(rng.randrange(len(lineas)))
        aplicado = "linea_omitida"
        detalle = f"no se entregó '{linea['concepto']}' ({linea['cantidad']} ud.)"

    elif tipo_error == "linea_extra":
        extra = articulo_no_facturado()
        if extra is not None:
            cantidad = rng.randint(extra.cantidad_min, extra.cantidad_max)
            lineas.append({"concepto": extra.concepto, "cantidad": cantidad})
            aplicado = "linea_extra"
            detalle = (
                f"entregado sin facturar '{extra.concepto}' ({cantidad} ud.)"
            )

    # Degradación segura: se pidió discrepancia pero el tipo no era aplicable.
    if aplicado is None and tipo_error is not None:
        linea = rng.choice(lineas)
        original = linea["cantidad"]
        linea["cantidad"] = original + max(1, original // 5)
        aplicado = "cantidad_mayor"
        detalle = (
            f"{linea['concepto']}: facturadas {original}, "
            f"entregadas {linea['cantidad']} (degradado desde '{tipo_error}')"
        )

    return {
        "proveedor": proveedor,
        "numero": factura["albaran"],
        "fecha": factura["fecha_albaran"],
        "lineas": lineas,
        "tipo_error": aplicado,
        "detalle": detalle,
    }


# ---------------------------------------------------------------------------
# Composición del PDF
# ---------------------------------------------------------------------------
def _estilos() -> dict[str, ParagraphStyle]:
    """Define los estilos de texto usados en el documento."""
    base = getSampleStyleSheet()["Normal"]
    return {
        "empresa": ParagraphStyle(
            "empresa", parent=base, fontName="Helvetica-Bold", fontSize=14,
            leading=17, textColor=TINTA,
        ),
        "titulo": ParagraphStyle(
            "titulo", parent=base, fontName="Helvetica-Bold", fontSize=22,
            leading=24, alignment=2, textColor=TINTA,
        ),
        "normal": ParagraphStyle("normal", parent=base, fontSize=9, leading=12, textColor=TINTA),
        "pequeno": ParagraphStyle(
            "pequeno", parent=base, fontSize=8, leading=11, textColor=SUAVE,
        ),
        "pequeno_der": ParagraphStyle(
            "pequeno_der", parent=base, fontSize=8.5, leading=12, alignment=2, textColor=TINTA,
        ),
        "etiqueta": ParagraphStyle(
            "etiqueta", parent=base, fontName="Helvetica-Bold", fontSize=7.5,
            leading=10, textColor=SUAVE,
        ),
        "celda": ParagraphStyle("celda", parent=base, fontSize=8.5, leading=11, textColor=TINTA),
        "celda_der": ParagraphStyle(
            "celda_der", parent=base, fontSize=8.5, leading=11, alignment=2, textColor=TINTA,
        ),
        "cabecera_tabla": ParagraphStyle(
            "cabecera_tabla", parent=base, fontName="Helvetica-Bold", fontSize=8,
            leading=10, textColor=colors.white,
        ),
        "cabecera_tabla_der": ParagraphStyle(
            "cabecera_tabla_der", parent=base, fontName="Helvetica-Bold", fontSize=8,
            leading=10, alignment=2, textColor=colors.white,
        ),
        "total": ParagraphStyle(
            "total", parent=base, fontName="Helvetica-Bold", fontSize=10.5,
            leading=13, alignment=2, textColor=TINTA,
        ),
    }


def _pie_de_pagina(canvas, documento) -> None:
    """Dibuja la línea y el texto legal al pie de cada página."""
    canvas.saveState()
    ancho, _ = A4
    margen = 18 * mm
    canvas.setStrokeColor(LINEA)
    canvas.setLineWidth(0.5)
    canvas.line(margen, 16 * mm, ancho - margen, 16 * mm)
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(SUAVE)
    canvas.drawString(margen, 11.5 * mm, MENSAJE_PIE)
    canvas.drawRightString(ancho - margen, 11.5 * mm, f"Página {canvas.getPageNumber()}")
    canvas.restoreState()


def generar_pdf(factura: dict, ruta: Path) -> None:
    """Escribe la factura en un PDF con maquetación de factura española."""
    estilos = _estilos()
    proveedor: Proveedor = factura["proveedor"]
    ancho_util = A4[0] - 36 * mm

    documento = SimpleDocTemplate(
        str(ruta),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=24 * mm,
        title=f"Factura {factura['numero']}",
        author=proveedor.nombre,
        subject=f"Factura {factura['numero']} - albarán {factura['albaran']}",
    )

    # --- Cabecera: emisor a la izquierda, bloque de factura a la derecha -----
    emisor = [
        Paragraph(_escapar(proveedor.nombre), estilos["empresa"]),
        Spacer(1, 2),
        Paragraph(
            f"CIF: {proveedor.cif}<br/>{_escapar(proveedor.direccion)}<br/>{_escapar(proveedor.cp_ciudad)}"
            f"<br/>IBAN: {proveedor.iban}",
            estilos["pequeno"],
        ),
    ]
    bloque_factura = [
        Paragraph("FACTURA", estilos["titulo"]),
        Spacer(1, 4),
        Paragraph(
            f"Nº: <b>{factura['numero']}</b><br/>"
            f"Fecha de expedición: <b>{_fecha(factura['fecha_factura'])}</b><br/>"
            f"Fecha de operación: <b>{_fecha(factura['fecha_albaran'])}</b>",
            estilos["pequeno_der"],
        ),
    ]
    cabecera = Table(
        [[emisor, bloque_factura]],
        colWidths=[ancho_util * 0.58, ancho_util * 0.42],
    )
    cabecera.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )

    # --- Bloque del receptor ------------------------------------------------
    receptor = Table(
        [[
            Paragraph("CLIENTE / RECEPTOR", estilos["etiqueta"]),
            Paragraph(
                f"<b>{RECEPTOR['nombre']}</b> — CIF: {RECEPTOR['cif']}<br/>"
                f"{RECEPTOR['direccion']}, {RECEPTOR['cp_ciudad']}",
                estilos["normal"],
            ),
        ]],
        colWidths=[ancho_util * 0.28, ancho_util * 0.72],
    )
    receptor.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), FONDO),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])
    )

    # --- Referencia al albarán: el dato que pide el cruce documental --------
    albaran = Table(
        [[
            Paragraph(
                "ALBARÁN FACTURADO", estilos["etiqueta"]
            ),
            Paragraph(
                f"Nº <b>{factura['albaran']}</b> de <b>{_fecha(factura['fecha_albaran'])}</b> — "
                "conforme a la entrega de mercancía/servicio realizada en dicha fecha.",
                estilos["normal"],
            ),
        ]],
        colWidths=[ancho_util * 0.22, ancho_util * 0.78],
    )
    albaran.setStyle(
        TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.75, TINTA),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])
    )

    # --- Tabla de líneas ----------------------------------------------------
    filas = [[
        Paragraph("CONCEPTO / DESCRIPCIÓN", estilos["cabecera_tabla"]),
        Paragraph("CANT.", estilos["cabecera_tabla_der"]),
        Paragraph("PRECIO UNIT.", estilos["cabecera_tabla_der"]),
        Paragraph("IMPORTE", estilos["cabecera_tabla_der"]),
    ]]
    for linea in factura["lineas"]:
        filas.append([
            Paragraph(_escapar(linea["concepto"]), estilos["celda"]),
            Paragraph(str(linea["cantidad"]), estilos["celda_der"]),
            Paragraph(_euros(linea["precio"]), estilos["celda_der"]),
            Paragraph(_euros(linea["importe"]), estilos["celda_der"]),
        ])

    tabla_lineas = Table(
        filas,
        colWidths=[ancho_util * 0.58, ancho_util * 0.1, ancho_util * 0.16, ancho_util * 0.16],
        repeatRows=1,
    )
    tabla_lineas.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), TINTA),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINEA),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])
    )

    # --- Totales (alineados a la derecha) -----------------------------------
    filas_totales = [
        [Paragraph("Base imponible", estilos["celda"]), Paragraph(_euros(factura["base"]), estilos["celda_der"])],
        [
            Paragraph(f"IVA ({TIPO_IVA}%)", estilos["celda"]),
            Paragraph(_euros(factura["cuota_iva"]), estilos["celda_der"]),
        ],
    ]
    if factura["retencion"]:
        filas_totales.append([
            Paragraph(
                f"Retención IRPF (-{proveedor.retencion_irpf}%)", estilos["celda"]
            ),
            Paragraph(_euros(-factura["retencion"]), estilos["celda_der"]),
        ])
    filas_totales.append([
        Paragraph("TOTAL FACTURA", estilos["total"]),
        Paragraph(_euros(factura["total"]), estilos["total"]),
    ])

    tabla_totales = Table(filas_totales, colWidths=[ancho_util * 0.28, ancho_util * 0.17])
    tabla_totales.setStyle(
        TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
            ("LINEABOVE", (0, 0), (-1, 0), 0.4, LINEA),
            ("LINEABOVE", (0, -1), (-1, -1), 0.75, TINTA),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
    )
    totales = Table([["", tabla_totales]], colWidths=[ancho_util * 0.55, ancho_util * 0.45])
    totales.setStyle(
        TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )

    # --- Forma de pago ------------------------------------------------------
    pago = Paragraph(
        f"<b>Forma de pago:</b> Transferencia bancaria a {proveedor.iban} — "
        f"<b>Vencimiento:</b> {DIAS_VENCIMIENTO} días fecha factura ({_fecha(factura['fecha_vencimiento'])}).",
        estilos["normal"],
    )

    historia = [
        cabecera,
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=0.75, color=TINTA, spaceBefore=0, spaceAfter=10),
        receptor,
        Spacer(1, 8),
        albaran,
        Spacer(1, 12),
        tabla_lineas,
        Spacer(1, 10),
        totales,
        Spacer(1, 14),
        KeepTogether([pago]),
    ]

    documento.build(historia, onFirstPage=_pie_de_pagina, onLaterPages=_pie_de_pagina)


def generar_pdf_albaran(albaran: dict, ruta: Path) -> None:
    """Escribe el albarán de entrega en PDF.

    Un albarán no es un documento fiscal: no lleva importes ni IVA (se emite
    "sin valorar"), sino la relación de mercancía entregada y el espacio para
    la firma de conformidad del cliente. Por eso las columnas son descripción,
    cantidad y recibido, en lugar de precio e importe.
    """
    estilos = _estilos()
    proveedor: Proveedor = albaran["proveedor"]
    ancho_util = A4[0] - 36 * mm

    documento = SimpleDocTemplate(
        str(ruta),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=24 * mm,
        title=f"Albarán {albaran['numero']}",
        author=proveedor.nombre,
        subject=f"Albarán de entrega {albaran['numero']}",
    )

    # --- Cabecera -----------------------------------------------------------
    emisor = [
        Paragraph(_escapar(proveedor.nombre), estilos["empresa"]),
        Spacer(1, 2),
        Paragraph(
            f"CIF: {proveedor.cif}<br/>{_escapar(proveedor.direccion)}<br/>{_escapar(proveedor.cp_ciudad)}",
            estilos["pequeno"],
        ),
    ]
    bloque_albaran = [
        Paragraph("ALBARÁN", estilos["titulo"]),
        Spacer(1, 4),
        Paragraph(
            f"Nº: <b>{albaran['numero']}</b><br/>"
            f"Fecha de entrega: <b>{_fecha(albaran['fecha'])}</b><br/>"
            f"<i>Documento no válido como factura</i>",
            estilos["pequeno_der"],
        ),
    ]
    cabecera = Table(
        [[emisor, bloque_albaran]],
        colWidths=[ancho_util * 0.58, ancho_util * 0.42],
    )
    cabecera.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ])
    )

    # --- Destinatario y lugar de entrega ------------------------------------
    destinatario = Table(
        [[
            Paragraph("DESTINATARIO / LUGAR DE ENTREGA", estilos["etiqueta"]),
            Paragraph(
                f"<b>{RECEPTOR['nombre']}</b> — CIF: {RECEPTOR['cif']}<br/>"
                f"{RECEPTOR['direccion']}, {RECEPTOR['cp_ciudad']}",
                estilos["normal"],
            ),
        ]],
        colWidths=[ancho_util * 0.32, ancho_util * 0.68],
    )
    destinatario.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), FONDO),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ])
    )

    # --- Líneas entregadas --------------------------------------------------
    filas = [[
        Paragraph("DESCRIPCIÓN DE LA MERCANCÍA / SERVICIO", estilos["cabecera_tabla"]),
        Paragraph("CANT.", estilos["cabecera_tabla_der"]),
        Paragraph("RECIBIDO", estilos["cabecera_tabla_der"]),
    ]]
    for linea in albaran["lineas"]:
        filas.append([
            Paragraph(_escapar(linea["concepto"]), estilos["celda"]),
            Paragraph(str(linea["cantidad"]), estilos["celda_der"]),
            Paragraph("", estilos["celda_der"]),
        ])

    tabla_lineas = Table(
        filas,
        colWidths=[ancho_util * 0.68, ancho_util * 0.16, ancho_util * 0.16],
        repeatRows=1,
    )
    tabla_lineas.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), TINTA),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINEA),
            ("BOX", (1, 0), (-1, -1), 0.4, LINEA),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])
    )

    observaciones = Paragraph(
        "<b>Observaciones:</b> mercancía entregada en las instalaciones del cliente. "
        "La firma de este documento acredita la recepción conforme, sin perjuicio "
        "de los defectos no apreciables en el acto de la entrega.",
        estilos["pequeno"],
    )

    # --- Firma de conformidad ----------------------------------------------
    # Tres columnas: fecha, separación y firma, para que las líneas no se
    # toquen y se vean como dos casillas independientes.
    firmas = Table(
        [
            [
                Paragraph("Fecha de recepción", estilos["pequeno"]),
                "",
                Paragraph("Firma y sello del cliente", estilos["pequeno"]),
            ],
            ["", "", ""],
        ],
        colWidths=[ancho_util * 0.32, ancho_util * 0.06, ancho_util * 0.5],
        rowHeights=[None, 20 * mm],
    )
    firmas.setStyle(
        TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LINEBELOW", (0, 1), (0, 1), 0.5, SUAVE),
            ("LINEBELOW", (2, 1), (2, 1), 0.5, SUAVE),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ])
    )

    historia = [
        cabecera,
        Spacer(1, 8),
        HRFlowable(width="100%", thickness=0.75, color=TINTA, spaceBefore=0, spaceAfter=10),
        destinatario,
        Spacer(1, 12),
        tabla_lineas,
        Spacer(1, 14),
        observaciones,
        Spacer(1, 26),
        firmas,
    ]

    documento.build(historia, onFirstPage=_pie_de_pagina, onLaterPages=_pie_de_pagina)


def exportar_albaranes_json(albaranes: list[dict], ruta: Path) -> None:
    """Escribe todos los albaranes del lote en un único fichero JSON.

    El JSON hace de tabla de albaranes del ERP simulado, así que describe solo
    la entrega: número, fecha, proveedor, destinatario y líneas entregadas. No
    incluye qué albarán es discrepante ni de qué tipo, porque esa es la
    respuesta que debe deducir el agente al cruzar con la factura.

    Las fechas se escriben en ISO 8601 (``2026-02-25``): es el formato natural
    para un consumidor automático. Los PDF de factura mantienen ``dd/mm/aaaa``.

    Args:
        albaranes: datos de entrega generados por ``construir_albaran``.
        ruta: fichero de destino.
    """
    documento = {
        "version": 1,
        "descripcion": (
            "Albaranes de entrega simulados: sustituyen a la tabla de albaranes "
            "del ERP para el agente de cuentas por pagar."
        ),
        "albaranes": [
            {
                "numero": albaran["numero"],
                "fecha": albaran["fecha"].isoformat(),
                "proveedor": {
                    "nombre": albaran["proveedor"].nombre,
                    "cif": albaran["proveedor"].cif,
                },
                "destinatario": {
                    "nombre": RECEPTOR["nombre"],
                    "cif": RECEPTOR["cif"],
                },
                "direccion_entrega": (
                    f"{RECEPTOR['direccion']}, {RECEPTOR['cp_ciudad']}"
                ),
                "lineas": [
                    {
                        "concepto": linea["concepto"],
                        "cantidad": linea["cantidad"],
                    }
                    for linea in albaran["lineas"]
                ],
            }
            for albaran in albaranes
        ],
    }
    ruta.write_text(
        json.dumps(documento, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------
def _tipo_error_aplicable(tipo: str, factura: dict) -> bool:
    """Indica si una discrepancia concreta tiene sentido en esta factura."""
    lineas = factura["lineas"]
    proveedor: Proveedor = factura["proveedor"]
    if tipo == "cantidad_menor":
        return any(linea["cantidad"] > 1 for linea in lineas)
    if tipo == "cantidad_mayor":
        return True
    if tipo == "linea_omitida":
        return len(lineas) > 1
    # mercancia_distinta y linea_extra necesitan algún artículo alternativo.
    facturados = {linea["concepto"] for linea in lineas}
    return any(
        articulo.concepto not in facturados
        for articulo in (*proveedor.articulos_error, *proveedor.articulos)
    )


def _elegir_tipo_error(factura: dict, posicion: int, usados: set[str]) -> str:
    """Elige una discrepancia aplicable, evitando repetir las ya usadas.

    Se recorre ``TIPOS_ERROR`` desde un punto de partida rotatorio y se descartan
    los tipos que ya han aparecido en el lote, para que los cinco errores cubran
    las cinco clases siempre que sea posible. Si ninguno quedara libre (por
    ejemplo, porque ninguna factura admita ``linea_omitida``), se permite
    repetir en una segunda pasada.
    """
    orden = [(posicion + salto) % len(TIPOS_ERROR) for salto in range(len(TIPOS_ERROR))]

    for indice in orden:
        tipo = TIPOS_ERROR[indice]
        if tipo not in usados and _tipo_error_aplicable(tipo, factura):
            return tipo

    for indice in orden:
        tipo = TIPOS_ERROR[indice]
        if _tipo_error_aplicable(tipo, factura):
            return tipo

    return "cantidad_mayor"


def generar_lote(
    cantidad: int,
    salida_facturas: Path,
    salida_albaranes: Path,
    semilla: int,
    errores: int,
    fecha_base: date,
    formato_albaran: str = "json",
) -> tuple[list[Path], list[Path]]:
    """Genera las facturas y los albaranes que les corresponden.

    Args:
        cantidad: número de facturas (y de albaranes) del lote.
        salida_facturas: carpeta de destino de los PDF de factura.
        salida_albaranes: carpeta de destino de los albaranes; con formato
            ``json`` se escribe dentro un único ``albaranes.json``.
        semilla: semilla del generador de facturas.
        errores: cuántos albaranes llevarán una discrepancia deliberada.
        fecha_base: día de referencia para el reparto de fechas del lote.
        formato_albaran: ``json`` (un fichero con todos) o ``pdf`` (uno por
            albarán).

    Returns:
        Dos listas con las rutas creadas: facturas y albaranes. En formato
        ``json`` la segunda lista contiene un único elemento.
    """
    salida_facturas.mkdir(parents=True, exist_ok=True)
    salida_albaranes.mkdir(parents=True, exist_ok=True)

    rng_facturas = random.Random(semilla)
    # Generador independiente para los albaranes: así añadir discrepancias no
    # consume números aleatorios de las facturas y el lote de facturas sigue
    # siendo idéntico al de ejecuciones anteriores con la misma semilla.
    rng_albaranes = random.Random(semilla + 1)

    # Se eligen qué facturas tendrán un albarán discrepante y se reparten los
    # tipos en orden, de modo que un lote de 5 errores cubra las 5 clases.
    cantidad_errores = max(0, min(errores, cantidad))
    indices_error = sorted(rng_albaranes.sample(range(cantidad), k=cantidad_errores))
    orden_error = {indice: pos for pos, indice in enumerate(indices_error)}

    rutas_facturas: list[Path] = []
    rutas_albaranes: list[Path] = []
    albaranes: list[dict] = []
    tipos_usados: set[str] = set()

    for indice in range(cantidad):
        # Se reparten por turnos para que todas las empresas tengan facturas.
        proveedor = PROVEEDORES[indice % len(PROVEEDORES)]
        factura = construir_factura(rng_facturas, proveedor, indice, fecha_base)
        sufijo = f"{NUMERO_INICIAL + indice:02d}_{_slug(proveedor.nombre)}"

        ruta_factura = salida_facturas / f"Factura_{sufijo}.pdf"
        generar_pdf(factura, ruta_factura)
        rutas_facturas.append(ruta_factura)

        # El tipo se decide aquí, cuando ya se conocen las líneas facturadas:
        # así se elige uno aplicable (una factura de una línea no admite
        # "linea_omitida") y se mantiene la variedad del lote.
        tipo_error = (
            _elegir_tipo_error(factura, orden_error[indice], tipos_usados)
            if indice in orden_error
            else None
        )
        if tipo_error is not None:
            tipos_usados.add(tipo_error)
        albaran = construir_albaran(rng_albaranes, factura, tipo_error)
        albaranes.append(albaran)

        # La discrepancia se informa SOLO por consola: el JSON de albaranes
        # debe parecer la tabla de un ERP, sin revelar la respuesta.
        estado = (
            "correcto"
            if albaran["tipo_error"] is None
            else f"DISCREPANCIA ({albaran['tipo_error']}): {albaran['detalle']}"
        )
        print(
            f"  {factura['numero']:>14}  total {_euros(factura['total']):>12}  "
            f"albarán {albaran['numero']:<18} {estado}"
        )

    if formato_albaran == "pdf":
        for indice, albaran in enumerate(albaranes):
            proveedor = albaran["proveedor"]
            sufijo = f"{NUMERO_INICIAL + indice:02d}_{_slug(proveedor.nombre)}"
            ruta = salida_albaranes / f"Albaran_{sufijo}.pdf"
            generar_pdf_albaran(albaran, ruta)
            rutas_albaranes.append(ruta)
    else:
        ruta_json = salida_albaranes / NOMBRE_ALBARANES_JSON
        exportar_albaranes_json(albaranes, ruta_json)
        rutas_albaranes.append(ruta_json)

    return rutas_facturas, rutas_albaranes


def main() -> None:
    """Lee los argumentos de línea de órdenes y lanza la generación."""
    parser = argparse.ArgumentParser(
        description=(
            "Genera facturas de proveedor en PDF con referencia de albarán y los "
            "albaranes de entrega correspondientes."
        )
    )
    parser.add_argument(
        "--cantidad", type=int, default=20, help="número de facturas a generar (20)"
    )
    parser.add_argument(
        "--salida",
        type=Path,
        default=SALIDA_POR_DEFECTO,
        help=f"carpeta de las facturas ({SALIDA_POR_DEFECTO})",
    )
    parser.add_argument(
        "--salida-albaranes",
        type=Path,
        default=SALIDA_ALBARANES_POR_DEFECTO,
        help=(
            "carpeta de los albaranes "
            f"({SALIDA_ALBARANES_POR_DEFECTO}); en formato json se escribe "
            f"dentro {NOMBRE_ALBARANES_JSON}"
        ),
    )
    parser.add_argument(
        "--formato-albaran",
        choices=("json", "pdf"),
        default="json",
        help="json: un único fichero con todos los albaranes (por defecto); pdf: uno por albarán",
    )
    parser.add_argument(
        "--semilla", type=int, default=2026, help="semilla aleatoria para lotes reproducibles"
    )
    parser.add_argument(
        "--fecha-base",
        type=_fecha_iso,
        # El lote se ancla al día natural del equipo, no a UTC: la demo se graba
        # en horario local y las fechas de factura deben coincidir con el día.
        default=date.today(),  # noqa: DTZ011
        metavar="AAAA-MM-DD",
        help=(
            "día de referencia del lote: las fechas se reparten alrededor de él "
            "(por defecto, hoy)"
        ),
    )
    parser.add_argument(
        "--errores",
        type=int,
        default=ERRORES_POR_DEFECTO,
        help=(
            "cuántos albaranes llevan una discrepancia deliberada "
            f"({ERRORES_POR_DEFECTO} por defecto)"
        ),
    )
    args = parser.parse_args()

    if args.cantidad <= 0:
        parser.error("--cantidad debe ser mayor que cero")
    if args.errores < 0:
        parser.error("--errores no puede ser negativo")

    print(
        f"Generando {args.cantidad} facturas en {args.salida} y sus albaranes "
        f"({args.formato_albaran}) en {args.salida_albaranes} "
        f"(semilla {args.semilla}, base {args.fecha_base:%d/%m/%Y}, "
        f"{args.errores} con discrepancia)..."
    )
    rutas_facturas, rutas_albaranes = generar_lote(
        args.cantidad,
        args.salida,
        args.salida_albaranes,
        args.semilla,
        args.errores,
        args.fecha_base,
        args.formato_albaran,
    )
    total_errores = min(args.errores, args.cantidad)
    correctos = args.cantidad - total_errores
    destino = (
        f"{rutas_albaranes[0]} ({args.cantidad} albaranes en un único fichero)"
        if args.formato_albaran == "json"
        else f"{args.cantidad} PDF en {args.salida_albaranes}"
    )
    print(
        f"\nHecho: {len(rutas_facturas)} facturas. Albaranes en {destino}: "
        f"{correctos} correctos, {total_errores} con discrepancia."
    )


if __name__ == "__main__":
    main()
