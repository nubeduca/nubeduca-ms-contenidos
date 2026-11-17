"""Utilidades de ms-contenidos: claves de la tabla, proyecciones de ms-cursos y permisos.

Quién puede hacer qué con los archivos de un curso se decide con dos proyecciones que
escriben los consumidores (consumidores/cursos.py) a partir de eventos de ms-cursos:

- PROY#COURSE#<courseId>            de course.created: dueño del curso (ownerId).
- PROY#ENROLL#<courseId>#<userId>   de enrollment.created (se borra con enrollment.dropped): rol en el curso.
"""

import re

from lib import tabla as t

COURSE_ID = re.compile(r"^crs_[0-9A-HJKMNP-TV-Z]{26}$")
FILE_ID = re.compile(r"^fil_[0-9A-HJKMNP-TV-Z]{26}$")


def sk_archivo(file_id: str) -> str:
    return f"FILE#{file_id}"


def sk_curso(course_id: str) -> str:
    return f"PROY#COURSE#{course_id}"


def sk_matricula(course_id: str, user_id: str = "") -> str:
    return f"PROY#ENROLL#{course_id}#{user_id}"


def _leer(tenant_id: str, sk: str, consistente: bool = False) -> dict | None:
    return t.tabla().get_item(Key={"tenant_id": tenant_id, "sk": sk}, ConsistentRead=consistente).get("Item")


def matricula(usuario, course_id: str) -> dict | None:
    return _leer(usuario.tenant_id, sk_matricula(course_id, usuario.user_id))


def es_docente_del_curso(usuario, course_id: str) -> bool:
    """Docente dueño del curso o matriculado en él como TEACHER."""
    if usuario.rol != "TEACHER":
        return False
    curso = _leer(usuario.tenant_id, sk_curso(course_id))
    if curso and curso.get("ownerId") == usuario.user_id:
        return True
    m = matricula(usuario, course_id)
    return bool(m) and m.get("role") == "TEACHER"


def es_miembro_del_curso(usuario, course_id: str) -> bool:
    return es_docente_del_curso(usuario, course_id) or matricula(usuario, course_id) is not None


def leer_archivo(usuario, file_id: str) -> dict | None:
    if not FILE_ID.match(file_id or ""):
        return None
    return _leer(usuario.tenant_id, sk_archivo(file_id), consistente=True)


def puede_ver_archivo(usuario, archivo: dict) -> bool:
    """CONTENT: cualquier miembro del curso. SUBMISSION: quien la subió y los docentes del curso."""
    if usuario.rol == "ADMIN" or archivo["ownerId"] == usuario.user_id:
        return True
    if archivo["purpose"] == "CONTENT":
        return es_miembro_del_curso(usuario, archivo["courseId"])
    return es_docente_del_curso(usuario, archivo["courseId"])


def a_archivo(item: dict) -> dict:
    campos = ("fileId", "courseId", "purpose", "fileName", "contentType", "sizeBytes", "status", "ownerId",
              "createdAt")
    archivo = {k: item[k] for k in campos}
    archivo["sizeBytes"] = int(archivo["sizeBytes"])
    return archivo


def path_param(event: dict, nombre: str) -> str:
    return ((event or {}).get("pathParameters") or {}).get(nombre) or ""
