"""Configuración del servicio, leída de las variables de entorno que pone serverless.yml."""

import os

TENANTS = ("utec", "upc", "pucp")
ROLES = ("STUDENT", "TEACHER", "ADMIN")

# Cambia estos dos valores al copiar la plantilla (en este servicio: "ms-contenidos" y "lms.content").
SERVICIO = os.environ.get("SERVICIO", "ms-contenidos")
SOURCE = os.environ.get("EVENT_SOURCE", "lms.content")


def tabla_nombre() -> str:
    return os.environ.get("TABLA", "dev-t_contenidos")


def bus_nombre() -> str:
    return os.environ.get("EVENT_BUS", "lms")


def auth_mode() -> str:
    """cognito: claims del authorizer de API Gateway · mock: token de demo del frontend."""
    return os.environ.get("AUTH_MODE", "cognito").lower()


def bucket_nombre() -> str:
    """Bucket de archivos de ms-contenidos (CON-01)."""
    return os.environ.get("BUCKET", "dev-contenidos")
