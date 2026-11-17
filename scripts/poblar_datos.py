"""
Seed de ms-contenidos (CON-02): 30.000 módulos en la tabla principal, UTEC 15.000, UPC 10.000 y PUCP 5.000.

    python scripts/poblar_datos.py --stage dev                 # tabla dev-t_contenidos
    python scripts/poblar_datos.py --stage dev --escala 0.01   # 1 %: 150/100/50 para probar
    TABLA=otra-tabla python scripts/poblar_datos.py

Usa las credenciales de AWS del entorno (en nubeduca-backend: source scripts/cuenta.sh).

- NO escribe ítems EVENTO#: cargar 30.000 módulos no debe inundar el bus.
- Es idempotente: los ids son fijos, así que correrlo otra vez reescribe los mismos ítems.
- Encaja con el seed de ms-cursos: el módulo n de cada tenant es del curso crs_<n> de ese seed, y
  la proyección PROY#COURSE# lleva el mismo docente dueño (uuid5 con el mismo espacio). Así un docente
  de seed puede ver y editar los módulos de sus cursos sin esperar los eventos course.created.
- Cada módulo trae una página (PAGE). 70 % PUBLISHED y 30 % DRAFT.
"""

import argparse
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import comun  # noqa: E402
from lib import tabla as t  # noqa: E402

REPARTO = {"utec": 15000, "upc": 10000, "pucp": 5000}
CREADO_EN = "2026-08-17T13:00:00Z"
# Mismo espacio y fórmula que nubeduca-ms-cursos/scripts/poblar_datos.py (_docente).
ESPACIO_CURSOS = uuid.UUID("6f1c2e8a-3b7d-4c5e-9a10-5eed00000000")
TEMAS = ("Introducción", "Fundamentos", "Laboratorio", "Caso de estudio", "Repaso", "Proyecto")


def _docente(tenant: str, n: int) -> str:
    return str(uuid.uuid5(ESPACIO_CURSOS, f"{tenant}-docente-{n % 20}"))


def _course_id(n: int) -> str:
    return f"crs_{n:026d}"


def _module_id(n: int) -> str:
    return f"mod_{n:022d}SEED"


def _item_id(n: int) -> str:
    return f"itm_{n:022d}SEED"


def _estado(n: int) -> str:
    return "DRAFT" if n % 10 >= 7 else "PUBLISHED"


def _registros(tenant: str, n: int) -> list[dict]:
    course_id, module_id, docente = _course_id(n), _module_id(n), _docente(tenant, n)
    estado = _estado(n)
    semana = n % 16 + 1
    modulo = {
        "tenant_id": tenant, "sk": comun.sk_modulo(course_id, module_id), "entidad": "MODULE",
        "version": 3 if estado == "PUBLISHED" else 2, "moduleId": module_id, "courseId": course_id,
        "title": f"Semana {semana}: {TEMAS[n % len(TEMAS)]}", "position": semana, "status": estado,
        "itemCount": 1, "createdBy": docente, "createdAt": CREADO_EN, "seed": True,
    }
    if estado == "PUBLISHED":
        modulo |= {"publishedAt": CREADO_EN, "publishedBy": docente}
    pagina = {
        "tenant_id": tenant, "sk": comun.sk_item(course_id, module_id, _item_id(n)), "entidad": "MODULE_ITEM",
        "moduleId": module_id, "courseId": course_id, "itemId": _item_id(n), "type": "PAGE",
        "title": "Material de la semana", "body": f"Contenido de seed de la semana {semana}.", "position": 1,
        "createdBy": docente, "createdAt": CREADO_EN, "seed": True,
    }
    curso = {"tenant_id": tenant, "sk": comun.sk_curso(course_id), "entidad": "PROYECCION",
             "courseId": course_id, "ownerId": docente, "seed": True}
    return [modulo, pagina, curso]


def poblar(reparto: dict[str, int] = REPARTO) -> dict[str, int]:
    """Devuelve {tenant: módulos escritos}."""
    escritos = {}
    with t.tabla().batch_writer(overwrite_by_pkeys=["tenant_id", "sk"]) as lote:
        for tenant, cantidad in reparto.items():
            for n in range(1, cantidad + 1):
                for item in _registros(tenant, n):
                    lote.put_item(Item=item)
            escritos[tenant] = cantidad
    return escritos


def main():
    parser = argparse.ArgumentParser(description="Seed de ms-contenidos")
    parser.add_argument("--stage", help="usa la tabla <stage>-t_contenidos")
    parser.add_argument("--escala", type=float, default=1.0, help="fracción del reparto (1 = 30.000)")
    args = parser.parse_args()
    if args.stage:
        os.environ["TABLA"] = f"{args.stage}-t_contenidos"
    if not os.environ.get("TABLA"):
        raise SystemExit("Pasa --stage o define TABLA.")
    reparto = {k: max(1, int(v * args.escala)) for k, v in REPARTO.items()}
    for tenant, cantidad in poblar(reparto).items():
        print(f"{tenant}: {cantidad} módulos")
    print(f"Total: {sum(reparto.values())} módulos en {os.environ['TABLA']}")


if __name__ == "__main__":
    main()
