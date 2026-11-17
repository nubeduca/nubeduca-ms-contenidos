"""
Proyecciones de ms-cursos que ms-contenidos usa para los permisos (CON-01 y CON-02).

- lms.courses.course.created.v1      -> PROY#COURSE#<courseId>           {ownerId}
- lms.courses.enrollment.created.v1  -> PROY#ENROLL#<courseId>#<userId>  {enrollmentId, role}
- lms.courses.enrollment.dropped.v1  -> borra PROY#ENROLL#<courseId>#<userId>

Lector tolerante: solo se leen los campos que se usan. El inbox descarta duplicados y eventos
viejos (enrollment.dropped llega con aggregateVersion mayor que su enrollment.created).
"""

from api import comun
from lib import inbox


def guardar_curso(evento: inbox.Evento) -> list:
    return [{"Put": {"Item": {
        "tenant_id": evento.tenant_id,
        "sk": comun.sk_curso(evento.course_id),
        "entidad": "PROYECCION",
        "courseId": evento.course_id,
        "ownerId": evento.data["ownerId"],
    }}}]


def guardar_matricula(evento: inbox.Evento) -> list:
    return [{"Put": {"Item": {
        "tenant_id": evento.tenant_id,
        "sk": comun.sk_matricula(evento.course_id, evento.data["userId"]),
        "entidad": "PROYECCION",
        "courseId": evento.course_id,
        "userId": evento.data["userId"],
        "enrollmentId": evento.data["enrollmentId"],
        "role": evento.data["role"],
    }}}]


def borrar_matricula(evento: inbox.Evento) -> list:
    return [{"Delete": {"Key": {
        "tenant_id": evento.tenant_id,
        "sk": comun.sk_matricula(evento.course_id, evento.data["userId"]),
    }}}]


REACCIONES = {
    "lms.courses.course.created.v1": guardar_curso,
    "lms.courses.enrollment.created.v1": guardar_matricula,
    "lms.courses.enrollment.dropped.v1": borrar_matricula,
}


def lambda_handler(event, context):
    return inbox.manejar_lote(event, REACCIONES)
