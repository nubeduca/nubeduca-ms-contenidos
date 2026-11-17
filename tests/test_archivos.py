"""CON-01: pedir la URL de subida, ver el estado y pedir la URL de descarga."""

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from api import archivos, comun
from consumidores import cursos
from lib import tabla as t
from tests.conftest import (ADMIN, ALUMNO, BUCKET, CURSO, DOCENTE, OTRO_ALUMNO, OTRO_DOCENTE, TENANT, items,
                            mensaje, peticion, token)

FIL = re.compile(r"^fil_[0-9A-HJKMNP-TV-Z]{26}$")
PDF = {"courseId": CURSO, "purpose": "CONTENT", "fileName": "lab4.pdf", "contentType": "application/pdf",
       "sizeBytes": 1234}


@pytest.fixture
def curso(aws):
    """CURSO de DOCENTE (por course.created) con ALUMNO matriculado como STUDENT."""
    lote = {"Records": [
        {"messageId": "1", "body": mensaje("lms.courses.course.created.v1", event_id="01JC1SNDG3G46NZTCR7WN1Q8S5",
                                           ownerId=DOCENTE)},
        {"messageId": "2", "body": mensaje("lms.courses.enrollment.created.v1", event_id="01JCN6M3S82KD5RV1ZK8T8QEKP",
                                           userId=ALUMNO, role="STUDENT")},
    ]}
    assert cursos.lambda_handler(lote, None) == {"batchItemFailures": []}
    return aws


def _subir(body=PDF, headers=None):
    headers = token() if headers is None else headers
    return archivos.lambda_handler(peticion("POST", headers, body, resource="/files"), None)


def _ver(file_id, headers=None, descarga=False):
    headers = token() if headers is None else headers
    recurso = "/files/{fileId}/download" if descarga else "/files/{fileId}"
    return archivos.lambda_handler(peticion("GET", headers, path_params={"fileId": file_id}, resource=recurso), None)


def _cuerpo(r):
    return json.loads(r["body"])


def _marcar(file_id, estado):
    t.tabla().update_item(Key={"tenant_id": TENANT, "sk": comun.sk_archivo(file_id)},
                          UpdateExpression="SET #s = :e", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":e": estado})


def test_docente_pide_subida_y_recibe_url_prefirmada(curso):
    r = _subir()
    assert r["statusCode"] == 201, r["body"]
    ticket = _cuerpo(r)
    assert FIL.match(ticket["fileId"])
    assert ticket["method"] == "PUT"
    assert ticket["headers"] == {"Content-Type": "application/pdf"}
    url = urlparse(ticket["uploadUrl"])
    assert BUCKET in url.netloc + url.path
    assert url.path.endswith(f"archivos/{TENANT}/{ticket['fileId']}")
    assert parse_qs(url.query)["X-Amz-Expires"] == ["900"]

    (item,) = items("FILE#")
    assert item["status"] == "PENDING_UPLOAD" and item["ownerId"] == DOCENTE and item["version"] == 1
    # La API no publica eventos: file.scanned lo publica el antivirus.
    assert items("EVENTO#") == []


def test_el_tenant_sale_del_token(curso):
    r = _subir(PDF | {"tenantId": "upc"})
    assert r["statusCode"] == 201
    assert items("FILE#", tenant="upc") == []


@pytest.mark.parametrize("cambio, campo", [
    ({"courseId": "curso-1"}, "courseId"),
    ({"purpose": "OTRO"}, "purpose"),
    ({"fileName": "../../etc/passwd"}, "fileName"),
    ({"fileName": ""}, "fileName"),
    ({"contentType": "no es mime"}, "contentType"),
    ({"sizeBytes": 0}, "sizeBytes"),
    ({"sizeBytes": 52428801}, "sizeBytes"),
    ({"sizeBytes": "10"}, "sizeBytes"),
])
def test_valida_el_body(curso, cambio, campo):
    r = _subir(PDF | cambio)
    assert r["statusCode"] == 400
    assert campo in _cuerpo(r)["message"]


def test_permisos_de_subida(curso):
    # El alumno no sube CONTENT, pero sí sus entregas.
    assert _subir(headers=token("STUDENT", user=ALUMNO))["statusCode"] == 403
    assert _subir(PDF | {"purpose": "SUBMISSION"}, token("STUDENT", user=ALUMNO))["statusCode"] == 201
    # Alumno no matriculado y docente de otro curso: 403.
    assert _subir(PDF | {"purpose": "SUBMISSION"}, token("STUDENT", user=OTRO_ALUMNO))["statusCode"] == 403
    assert _subir(headers=token(user=OTRO_DOCENTE))["statusCode"] == 403
    # El ADMIN del tenant puede.
    assert _subir(headers=token("ADMIN", user=ADMIN))["statusCode"] == 201
    # Sin token: 401.
    assert _subir(headers={})["statusCode"] == 401


def test_docente_matriculado_como_teacher_puede_subir(curso):
    cursos.lambda_handler({"Records": [{"messageId": "3", "body": mensaje(
        "lms.courses.enrollment.created.v1", event_id="01JCN6M3S82KD5RV1ZK8T8QEKQ",
        aggregate_id="enr_01JC5F0KPBVF7HK1VV8H23JGMX", userId=OTRO_DOCENTE, role="TEACHER",
        enrollmentId="enr_01JC5F0KPBVF7HK1VV8H23JGMX")}]}, None)
    assert _subir(headers=token(user=OTRO_DOCENTE))["statusCode"] == 201


def test_ver_estado_del_archivo(curso):
    file_id = _cuerpo(_subir())["fileId"]
    r = _ver(file_id, token("STUDENT", user=ALUMNO))
    assert r["statusCode"] == 200
    assert _cuerpo(r) == {"fileId": file_id, "courseId": CURSO, "purpose": "CONTENT", "fileName": "lab4.pdf",
                          "contentType": "application/pdf", "sizeBytes": 1234, "status": "PENDING_UPLOAD",
                          "ownerId": DOCENTE, "createdAt": _cuerpo(r)["createdAt"]}


def test_entrega_solo_la_ven_su_dueno_y_los_docentes(curso):
    file_id = _cuerpo(_subir(PDF | {"purpose": "SUBMISSION"}, token("STUDENT", user=ALUMNO)))["fileId"]
    assert _ver(file_id, token("STUDENT", user=ALUMNO))["statusCode"] == 200
    assert _ver(file_id, token())["statusCode"] == 200
    assert _ver(file_id, token("ADMIN", user=ADMIN))["statusCode"] == 200
    assert _ver(file_id, token("STUDENT", user=OTRO_ALUMNO))["statusCode"] == 404
    assert _ver(file_id, token(user=OTRO_DOCENTE))["statusCode"] == 403


def test_no_existe_o_es_de_otro_tenant(curso):
    file_id = _cuerpo(_subir())["fileId"]
    assert _ver("fil_01JCQRZHQT9F4WY2JYPDV546K7")["statusCode"] == 404
    assert _ver("no-es-un-id")["statusCode"] == 404
    assert _ver(file_id, token(tenant="upc"))["statusCode"] == 404


@pytest.mark.parametrize("estado", ["PENDING_UPLOAD", "PENDING_SCAN", "INFECTED"])
def test_descarga_solo_si_esta_clean(curso, estado):
    file_id = _cuerpo(_subir())["fileId"]
    _marcar(file_id, estado)
    r = _ver(file_id, token("STUDENT", user=ALUMNO), descarga=True)
    assert r["statusCode"] == 409
    assert _cuerpo(r)["code"] == "ARCHIVO_NO_DISPONIBLE"


def test_descarga_de_un_archivo_clean(curso):
    file_id = _cuerpo(_subir(PDF | {"fileName": 'lab "4".pdf'}))["fileId"]
    _marcar(file_id, "CLEAN")
    r = _ver(file_id, token("STUDENT", user=ALUMNO), descarga=True)
    assert r["statusCode"] == 200
    link = _cuerpo(r)
    url = urlparse(link["downloadUrl"])
    query = parse_qs(url.query)
    assert query["X-Amz-Expires"] == ["300"]
    assert query["response-content-disposition"] == ['attachment; filename="lab _4_.pdf"']
    assert link["expiresAt"].endswith("Z")
    # Quien no puede ver el archivo tampoco lo descarga.
    assert _ver(file_id, token("STUDENT", user=OTRO_ALUMNO), descarga=True)["statusCode"] == 404
