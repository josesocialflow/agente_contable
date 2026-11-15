"""Ajustes comunes de las pruebas.

Añade la carpeta del proyecto a ``sys.path`` para que los tests puedan
importar ``config`` y ``core`` con independencia del directorio desde el que se
lance pytest.
"""

import sys
from pathlib import Path

RAIZ_PROYECTO = Path(__file__).resolve().parents[1]
if str(RAIZ_PROYECTO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROYECTO))
