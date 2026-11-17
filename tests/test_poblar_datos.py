"""Seed de CON-02: 30.000 módulos (UTEC 15.000, UPC 10.000, PUCP 5.000) sin inundar el bus."""

import json

from api import comun, modulos
from lib import esquemas
from scripts import poblar_datos
from tests.conftest import items, peticion, token


def test_reparto_oficial():
    assert poblar_datos.REPARTO == {"utec": 15000, "upc": 10000, "pucp": 5000}
    assert sum(poblar_datos.REPARTO.values()) == 30000


def test_carga_el_reparto_sin_eventos_y_es_idempotente(aws):
    reparto = {"utec": 30, "upc": 20, "pucp": 10}
    assert poblar_datos.poblar(reparto) == reparto
    assert poblar_datos.poblar(reparto) == reparto  # correrlo de nuevo no duplica
    for tenant, cantidad in reparto.items():
        filas = items("MODULE#", tenant)
        assert len([f for f in filas if f["entidad"] == "MODULE"]) == cantidad
        assert len([f for f in filas if f["entidad"] == "MODULE_ITEM"]) == cantidad
        assert len(items("PROY#COURSE#", tenant)) == cantidad
        assert items("EVENTO#", tenant) == []  # el seed no publica al bus


def test_los_modulos_del_seed_cumplen_el_contrato(aws):
    poblar_datos.poblar({"utec": 10, "upc": 0, "pucp": 0})
    cabeceras = [f for f in items("MODULE#") if f["entidad"] == "MODULE"]
    assert {m["status"] for m in cabeceras} == {"DRAFT", "PUBLISHED"}
    for m in cabeceras:
        assert comun.MODULE_ID.match(m["moduleId"]) and comun.COURSE_ID.match(m["courseId"])
        meta = {"eventId": "01JCB7FV64BFKS3ZX0NJD0BXNJ", "tenantId": "utec", "occurredAt": m["createdAt"],
                "aggregateType": "module", "aggregateId": m["moduleId"], "aggregateVersion": int(m["version"]),
                "actorId": m["createdBy"], "courseId": m["courseId"]}
        data = {"moduleId": m["moduleId"], "title": m["title"], "position": int(m["position"]),
                "itemCount": int(m["itemCount"])}
        assert esquemas.errores("lms.content.module.published.v1", {"metadata": meta, "data": data}) == []


def test_el_docente_de_seed_ve_sus_modulos_por_la_api(aws):
    """Mismo docente que el seed de ms-cursos: es dueño del curso sin esperar course.created."""
    poblar_datos.poblar({"utec": 3, "upc": 0, "pucp": 0})
    curso, docente = poblar_datos._course_id(1), poblar_datos._docente("utec", 1)
    r = modulos.lambda_handler(peticion("GET", token(user=docente), path_params={"courseId": curso},
                                        resource="/courses/{courseId}/modules"), None)
    assert r["statusCode"] == 200, r["body"]
    (modulo,) = json.loads(r["body"])["items"]
    assert modulo["items"][0]["type"] == "PAGE"
