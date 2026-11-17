"""CON-02: un docente publica un módulo con archivos y se emite lms.content.module.published.v1."""

import json
import re

import pytest

from api import archivos, comun, modulos
from consumidores import cursos
from lib import esquemas
from lib import tabla as t
from relay import stream_a_bus
from tests.conftest import (ADMIN, ALUMNO, CURSO, DOCENTE, OTRO_ALUMNO, OTRO_DOCENTE, TENANT, eventos, items,
                            mensaje, peticion, registros_stream, token)

MOD = re.compile(r"^mod_[0-9A-HJKMNP-TV-Z]{26}$")
ITM = re.compile(r"^itm_[0-9A-HJKMNP-TV-Z]{26}$")
PUBLICADO = "lms.content.module.published.v1"
BASE = "/courses/{courseId}/modules"


@pytest.fixture
def curso(aws):
    """CURSO de DOCENTE (course.created) con ALUMNO matriculado como STUDENT."""
    lote = {"Records": [
        {"messageId": "1", "body": mensaje("lms.courses.course.created.v1", event_id="01JC1SNDG3G46NZTCR7WN1Q8S5",
                                           ownerId=DOCENTE)},
        {"messageId": "2", "body": mensaje("lms.courses.enrollment.created.v1", event_id="01JCN6M3S82KD5RV1ZK8T8QEKP",
                                           userId=ALUMNO, role="STUDENT")},
    ]}
    assert cursos.lambda_handler(lote, None) == {"batchItemFailures": []}
    return aws


def _llamar(metodo, recurso, headers=None, body=None, module_id=None, course_id=CURSO):
    headers = token() if headers is None else headers
    params = {"courseId": course_id} | ({"moduleId": module_id} if module_id else {})
    return modulos.lambda_handler(peticion(metodo, headers, body, params, resource=recurso), None)


def _cuerpo(r):
    return json.loads(r["body"])


def _crear(title="Semana 3: Serverless", position=3, headers=None):
    r = _llamar("POST", BASE, headers, {"title": title, "position": position})
    assert r["statusCode"] == 201, r["body"]
    return _cuerpo(r)["moduleId"]


def _item(module_id, body, headers=None):
    return _llamar("POST", BASE + "/{moduleId}/items", headers, body, module_id)


def _publicar(module_id, headers=None):
    return _llamar("POST", BASE + "/{moduleId}/publish", headers, module_id=module_id)


def _archivo_clean(purpose="CONTENT", course_id=CURSO, estado="CLEAN"):
    headers = token() if purpose == "CONTENT" else token("STUDENT", user=ALUMNO)
    r = archivos.lambda_handler(peticion("POST", headers, {"courseId": course_id, "purpose": purpose,
                                                           "fileName": "lab4.pdf", "contentType": "application/pdf",
                                                           "sizeBytes": 10}, resource="/files"), None)
    file_id = _cuerpo(r)["fileId"]
    t.tabla().update_item(Key={"tenant_id": TENANT, "sk": comun.sk_archivo(file_id)},
                          UpdateExpression="SET #s = :e", ExpressionAttributeNames={"#s": "status"},
                          ExpressionAttributeValues={":e": estado})
    return file_id


def _publicados():
    return [e for e in eventos() if e["_detailType"] == PUBLICADO]


def test_docente_publica_un_modulo_con_archivos_y_emite_module_published(curso):
    mid = _crear()
    assert MOD.match(mid)
    file_id = _archivo_clean()
    paginas = [
        {"type": "PAGE", "title": "Introducción", "body": "# Serverless\nLambda + API Gateway"},
        {"type": "FILE", "title": "Laboratorio 4", "fileId": file_id},
        {"type": "LINK", "title": "Documentación", "url": "https://docs.aws.amazon.com/lambda/"},
    ]
    creados = []
    for posicion, body in enumerate(paginas, start=1):
        r = _item(mid, body)
        assert r["statusCode"] == 201, r["body"]
        creados.append(_cuerpo(r))
        assert ITM.match(creados[-1]["itemId"]) and creados[-1]["position"] == posicion
    assert _publicados() == []  # agregar ítems no publica nada

    r = _publicar(mid)
    assert r["statusCode"] == 200, r["body"]
    modulo = _cuerpo(r)
    assert modulo["status"] == "PUBLISHED"
    assert [i["type"] for i in modulo["items"]] == ["PAGE", "FILE", "LINK"]
    assert modulo["items"][1] == {"itemId": creados[1]["itemId"], "type": "FILE", "title": "Laboratorio 4",
                                  "position": 2, "fileId": file_id}

    (ev,) = _publicados()
    assert ev["_source"] == "lms.content"
    assert ev["metadata"]["aggregateType"] == "module" and ev["metadata"]["aggregateId"] == mid
    assert ev["metadata"]["courseId"] == CURSO and ev["metadata"]["actorId"] == DOCENTE
    assert ev["metadata"]["aggregateVersion"] == 5  # creado (1) + 3 ítems + publicado
    assert ev["data"] == {"moduleId": mid, "title": "Semana 3: Serverless", "position": 3, "itemCount": 3}
    assert esquemas.errores(PUBLICADO, {"metadata": ev["metadata"], "data": ev["data"]}) == []


def test_module_published_viaja_por_el_bus(curso):
    """Outbox -> Stream -> relay -> bus. La cola de prueba solo escucha file.scanned, así que se mira el envío."""
    mid = _crear()
    _item(mid, {"type": "PAGE", "title": "Intro", "body": "hola"})
    _publicar(mid)
    assert stream_a_bus.publicar(registros_stream()) == 1


def test_publicar_dos_veces_o_sin_items_da_409(curso):
    mid = _crear()
    r = _publicar(mid)
    assert r["statusCode"] == 409 and _cuerpo(r)["code"] == "MODULO_SIN_ITEMS"
    _item(mid, {"type": "PAGE", "title": "Intro", "body": "hola"})
    assert _publicar(mid)["statusCode"] == 200
    r = _publicar(mid)
    assert r["statusCode"] == 409 and _cuerpo(r)["code"] == "MODULO_YA_PUBLICADO"
    assert len(_publicados()) == 1


@pytest.mark.parametrize("estado", ["PENDING_UPLOAD", "PENDING_SCAN", "INFECTED"])
def test_archivo_que_no_esta_clean_da_409(curso, estado):
    mid = _crear()
    r = _item(mid, {"type": "FILE", "title": "Lab", "fileId": _archivo_clean(estado=estado)})
    assert r["statusCode"] == 409 and _cuerpo(r)["code"] == "ARCHIVO_NO_DISPONIBLE"
    assert items(f"MODULE#{CURSO}#{mid}#ITEM#") == []


def test_archivo_de_otro_curso_o_entrega_no_se_agrega(curso):
    mid = _crear()
    entrega = _archivo_clean(purpose="SUBMISSION")
    assert _item(mid, {"type": "FILE", "title": "Lab", "fileId": entrega})["statusCode"] == 400
    assert _item(mid, {"type": "FILE", "title": "Lab", "fileId": "fil_01JCQRZHQT9F4WY2JYPDV546K7"})["statusCode"] == 400


@pytest.mark.parametrize("body, campo", [
    ({"type": "VIDEO", "title": "x"}, "type"),
    ({"type": "PAGE", "title": ""}, "title"),
    ({"type": "PAGE", "title": "x"}, "body"),
    ({"type": "PAGE", "title": "x", "body": "y", "url": "https://a.b"}, "url"),
    ({"type": "FILE", "title": "x", "fileId": "no-es-id"}, "fileId"),
    ({"type": "LINK", "title": "x", "url": "javascript:alert(1)"}, "url"),
    ({"type": "LINK", "title": "x", "url": "https://a.b", "body": "y"}, "body"),
])
def test_valida_los_items(curso, body, campo):
    mid = _crear()
    r = _item(mid, body)
    assert r["statusCode"] == 400
    assert campo in _cuerpo(r)["message"]


@pytest.mark.parametrize("body", [{"position": 1}, {"title": "x", "position": 0}, {"title": "x", "position": "1"},
                                  {"title": "x" * 201, "position": 1}])
def test_valida_el_modulo(curso, body):
    assert _llamar("POST", BASE, body=body)["statusCode"] == 400


def test_alumno_solo_ve_modulos_publicados_y_en_orden(curso):
    borrador = _crear("Semana 2", 2)
    publicado = _crear("Semana 1", 1)
    _item(publicado, {"type": "PAGE", "title": "Intro", "body": "hola"})
    _publicar(publicado)

    alumno = token("STUDENT", user=ALUMNO)
    r = _llamar("GET", BASE, alumno)
    assert r["statusCode"] == 200
    assert [m["moduleId"] for m in _cuerpo(r)["items"]] == [publicado]
    assert _llamar("GET", BASE + "/{moduleId}", alumno, module_id=borrador)["statusCode"] == 404
    assert _llamar("GET", BASE + "/{moduleId}", alumno, module_id=publicado)["statusCode"] == 200

    r = _llamar("GET", BASE)  # el docente ve todos, ordenados por posición
    assert [m["title"] for m in _cuerpo(r)["items"]] == ["Semana 1", "Semana 2"]


def test_editar_titulo_y_posicion(curso):
    mid = _crear("Semana 3", 3)
    r = _llamar("PATCH", BASE + "/{moduleId}", body={"title": "Semana 3: Lambdas", "position": 4}, module_id=mid)
    assert r["statusCode"] == 200, r["body"]
    assert _cuerpo(r) | {"moduleId": "x"} == {"moduleId": "x", "title": "Semana 3: Lambdas", "position": 4,
                                              "status": "DRAFT", "items": []}
    assert _llamar("PATCH", BASE + "/{moduleId}", body={}, module_id=mid)["statusCode"] == 400
    assert _llamar("PATCH", BASE + "/{moduleId}", body={"title": "x"},
                   module_id="mod_01JCQRZHQT9F4WY2JYPDV546K7")["statusCode"] == 404


def test_permisos(curso):
    mid = _crear()
    alumno, ajeno = token("STUDENT", user=ALUMNO), token("STUDENT", user=OTRO_ALUMNO)
    # Solo el docente del curso escribe (el ADMIN tampoco: x-roles es TEACHER).
    for headers in (alumno, token(user=OTRO_DOCENTE), token("ADMIN", user=ADMIN)):
        assert _llamar("POST", BASE, headers, {"title": "x", "position": 1})["statusCode"] == 403
        assert _item(mid, {"type": "PAGE", "title": "x", "body": "y"}, headers)["statusCode"] == 403
        assert _publicar(mid, headers)["statusCode"] == 403
    # Leer: miembros y ADMIN. Alumno ajeno: 404; docente ajeno: 403.
    assert _llamar("GET", BASE, token("ADMIN", user=ADMIN))["statusCode"] == 200
    assert _llamar("GET", BASE, ajeno)["statusCode"] == 404
    assert _llamar("GET", BASE, token(user=OTRO_DOCENTE))["statusCode"] == 403
    assert _llamar("GET", BASE, {})["statusCode"] == 401


def test_docente_matriculado_como_teacher_puede_editar(curso):
    cursos.lambda_handler({"Records": [{"messageId": "3", "body": mensaje(
        "lms.courses.enrollment.created.v1", event_id="01JCN6M3S82KD5RV1ZK8T8QEKQ",
        aggregate_id="enr_01JC5F0KPBVF7HK1VV8H23JGMX", userId=OTRO_DOCENTE, role="TEACHER",
        enrollmentId="enr_01JC5F0KPBVF7HK1VV8H23JGMX")}]}, None)
    _crear(headers=token(user=OTRO_DOCENTE))


def test_curso_desconocido_o_de_otro_tenant(curso):
    assert _llamar("GET", BASE, course_id="crs_01JCQRZHQT9F4WY2JYPDV546K8")["statusCode"] == 404
    assert _llamar("GET", BASE, course_id="curso-1")["statusCode"] == 404
    assert _llamar("GET", BASE, token(tenant="upc"))["statusCode"] == 404
