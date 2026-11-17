"""Cliente de S3 del bucket de archivos (CON-01). Firma con SigV4 para las URLs prefirmadas."""

import boto3
from botocore.config import Config

from lib import config

_cliente = None


def cliente():
    global _cliente
    if _cliente is None:
        _cliente = boto3.client("s3", config=Config(signature_version="s3v4"))
    return _cliente


def bucket() -> str:
    return config.bucket_nombre()


def reiniciar() -> None:
    """Solo para pruebas: fuerza a crear el cliente de nuevo."""
    global _cliente
    _cliente = None
