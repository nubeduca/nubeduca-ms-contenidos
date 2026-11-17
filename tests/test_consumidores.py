"""Proyecciones de ms-cursos: dueño del curso y matrículas, sin duplicados ni eventos viejos."""

from consumidores import cursos
from tests.conftest import ALUMNO, CURSO, DOCENTE, items, mensaje

ENROLL = "enr_01JC5F0KPBVF7HK1VV8H23JGMW"


def _lote(*cuerpos):
    return {"Records": [{"messageId": f"m{i}", "body": c} for i, c in enumerate(cuerpos)]}


def test_course_created_guarda_el_dueno(aws):
    r = cursos.lambda_handler(_lote(mensaje("lms.courses.course.created.v1", event_id="01JC1SNDG3G46NZTCR7WN1Q8S5",
                                            ownerId=DOCENTE)), None)
    assert r == {"batchItemFailures": []}
    (curso,) = items("PROY#COURSE#")
    assert curso["courseId"] == CURSO and curso["ownerId"] == DOCENTE


def test_matricula_y_retiro(aws):
    creado = mensaje("lms.courses.enrollment.created.v1", event_id="01JCN6M3S82KD5RV1ZK8T8QEKP", version=1,
                     userId=ALUMNO, role="STUDENT", enrollmentId=ENROLL)
    cursos.lambda_handler(_lote(creado), None)
    (m,) = items("PROY#ENROLL#")
    assert m["sk"] == f"PROY#ENROLL#{CURSO}#{ALUMNO}" and m["role"] == "STUDENT"

    retiro = mensaje("lms.courses.enrollment.dropped.v1", event_id="01JCSEEPA7P5YD57NDGV8YPHKT", version=2,
                     userId=ALUMNO)
    cursos.lambda_handler(_lote(retiro), None)
    assert items("PROY#ENROLL#") == []


def test_duplicado_y_viejo_se_descartan(aws):
    creado = mensaje("lms.courses.enrollment.created.v1", event_id="01JCN6M3S82KD5RV1ZK8T8QEKP", version=1,
                     userId=ALUMNO, role="STUDENT", enrollmentId=ENROLL)
    retiro = mensaje("lms.courses.enrollment.dropped.v1", event_id="01JCSEEPA7P5YD57NDGV8YPHKT", version=2,
                     userId=ALUMNO)
    # Llegan en desorden: primero el retiro, luego la matrícula (vieja) y otra vez el retiro (duplicado).
    cursos.lambda_handler(_lote(retiro, creado, retiro), None)
    assert items("PROY#ENROLL#") == []
    assert len(items("INBOX#")) == 1
