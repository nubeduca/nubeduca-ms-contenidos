"""
Escáner de archivos de CON-01. Es liviano y no tiene dependencias: lee el objeto por bloques
y busca firmas conocidas, así cabe en una Lambda con el LabRole sin capas ni imágenes.

Detecta:
- La firma de prueba EICAR en cualquier parte del archivo (también dentro de un ZIP sin comprimir).
- Archivos cuyo SHA-256 está en la lista de hashes bloqueados (FIRMAS_SHA256).

La interfaz es escanear(bloques) -> Resultado. Para usar ClamAV más adelante basta con otra
función con la misma firma: handler.py no cambia.

La firma EICAR se arma por partes para que el antivirus de la máquina de desarrollo no ponga
en cuarentena este archivo.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

EICAR = b"".join([
    b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$",
    b"EICAR-STANDARD-ANTIVIRUS",
    b"-TEST-FILE!$H+H*",
])

# SHA-256 de archivos bloqueados. El de EICAR sirve de ejemplo y de prueba.
FIRMAS_SHA256 = {
    hashlib.sha256(EICAR).hexdigest(): "EICAR-Test-File",
    hashlib.sha256(EICAR + b"\r\n").hexdigest(): "EICAR-Test-File",
    hashlib.sha256(EICAR + b"\n").hexdigest(): "EICAR-Test-File",
}

FIRMAS_BYTES = {EICAR: "EICAR-Test-File"}
_SOLAPE = max(len(f) for f in FIRMAS_BYTES) - 1


@dataclass(frozen=True)
class Resultado:
    resultado: str            # CLEAN | INFECTED (enum del contrato file.scanned)
    firma: str | None         # qué se encontró, si algo
    bytes_leidos: int
    sha256: str

    @property
    def limpio(self) -> bool:
        return self.resultado == "CLEAN"


def escanear(bloques: Iterable[bytes]) -> Resultado:
    """Recorre el archivo una vez. Guarda el final de cada bloque para encontrar firmas partidas."""
    digest = hashlib.sha256()
    cola = b""
    leidos = 0
    encontrada = None
    for bloque in bloques:
        if not bloque:
            continue
        digest.update(bloque)
        leidos += len(bloque)
        if encontrada is None:
            ventana = cola + bloque
            for firma, nombre in FIRMAS_BYTES.items():
                if firma in ventana:
                    encontrada = nombre
                    break
            cola = ventana[-_SOLAPE:] if _SOLAPE else b""
    sha = digest.hexdigest()
    if encontrada is None:
        encontrada = FIRMAS_SHA256.get(sha)
    return Resultado("INFECTED" if encontrada else "CLEAN", encontrada, leidos, sha)
