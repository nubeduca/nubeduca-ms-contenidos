"""CON-01, criterio "Listo cuando": publica file.scanned y rechaza el archivo de prueba EICAR."""

import json

import pytest

from antivirus import escaner, handler
from api import archivos
from consumidores import cursos
from lib import esquemas
from relay import stream_a_bus
from tests.conftest import (ALUMNO, BUCKET, CURSO, DOCENTE, TENANT, eventos, items, mensaje, peticion,
                            registros_stream, token)

ESCANEADO = "lms.content.file.scanned.v1"
PDF = b"%PDF-1.7\n" + b"contenido del laboratorio 4\n" * 200


@pytest.fixture
def curso(aws):
    lote = {"Records": [
        {"messageId": "1", "body": mensaje("lms.courses.course.created.v1", event_id="01JC1SNDG3G46NZTCR7WN1Q8S5",
                                           ownerId=DOCENTE)},
        {"messageId": "2", "body": mensaje("lms.courses.enrollment.created.v1", event_id="01JCN6M3S82KD5RV1ZK8T8QEKP",
                                           userId=ALUMNO, role="STUDENT")},
    ]}
    cursos.lambda_handler(lote, None)
    return aws


def _pedir(contenido: bytes, purpose="SUBMISSION", headers=None, size=None):
    headers = token("STUDENT", user=ALUMNO) if headers is None else headers
    body = {"courseId": CURSO, "purpose": purpose, "fileName": "entrega.pdf", "contentType": "application/pdf",
            "sizeBytes": len(contenido) if size is None else size}
    r = archivos.lambda_handler(peticion("POST", headers, body, resource="/files"), None)
    assert r["statusCode"] == 201, r["body"]
    return json.loads(r["body"])["fileId"]


def _subir_y_escanear(aws, file_id: str, contenido: bytes):
    """Lo que hace el navegador (PUT a S3) y lo que S3 dispara (la Lambda de antivirus)."""
    clave = f"archivos/{TENANT}/{file_id}"
    aws["s3"].put_object(Bucket=BUCKET, Key=clave, Body=contenido, ContentType="application/pdf")
    evento_s3 = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": clave, "size": len(contenido)}}}]}
    return handler.lambda_handler(evento_s3, None)["resultados"]


def _estado(file_id):
    r = archivos.lambda_handler(peticion("GET", token(), path_params={"fileId": file_id}, resource="/files/{fileId}"),
                                None)
    return json.loads(r["body"])["status"]


def _escaneados():
    return [e for e in eventos() if e["_detailType"] == ESCANEADO]


def _existe(aws, file_id):
    lista = aws["s3"].list_objects_v2(Bucket=BUCKET, Prefix=f"archivos/{TENANT}/{file_id}")
    return lista.get("KeyCount", 0) > 0


# --- Escáner ---------------------------------------------------------------------------

def test_escaner_detecta_eicar_y_deja_pasar_lo_limpio():
    assert escaner.escanear([PDF]).resultado == "CLEAN"
    sucio = escaner.escanear([escaner.EICAR])
    assert sucio.resultado == "INFECTED" and sucio.firma == "EICAR-Test-File"


def test_escaner_encuentra_la_firma_partida_entre_bloques():
    datos = b"A" * 1000 + escaner.EICAR + b"B" * 1000
    bloques = [datos[i:i + 7] for i in range(0, len(datos), 7)]
    resultado = escaner.escanear(bloques)
    assert resultado.resultado == "INFECTED"
    assert resultado.bytes_leidos == len(datos)


# --- Lambda de antivirus ---------------------------------------------------------------

def test_archivo_limpio_queda_clean_y_publica_file_scanned(curso):
    file_id = _pedir(PDF)
    assert _subir_y_escanear(curso, file_id, PDF) == ["CLEAN"]
    assert _estado(file_id) == "CLEAN"

    (ev,) = _escaneados()
    assert ev["_source"] == "lms.content"
    assert ev["metadata"] | {"eventId": "x", "occurredAt": "t"} == {
        "eventId": "x", "occurredAt": "t", "tenantId": TENANT, "aggregateType": "file", "aggregateId": file_id,
        "aggregateVersion": 2, "actorId": "system", "courseId": CURSO}
    assert ev["data"] == {"fileId": file_id, "purpose": "SUBMISSION", "result": "CLEAN",
                          "contentType": "application/pdf", "sizeBytes": len(PDF), "ownerId": ALUMNO}
    assert esquemas.errores(ESCANEADO, {"metadata": ev["metadata"], "data": ev["data"]}) == []
    assert _existe(curso, file_id)


def test_rechaza_el_archivo_de_prueba_eicar(curso):
    file_id = _pedir(escaner.EICAR)
    assert _subir_y_escanear(curso, file_id, escaner.EICAR) == ["INFECTED"]
    assert _estado(file_id) == "INFECTED"
    (ev,) = _escaneados()
    assert ev["data"]["result"] == "INFECTED"
    # Se borra del bucket y no se puede descargar.
    assert not _existe(curso, file_id)
    r = archivos.lambda_handler(peticion("GET", token(), path_params={"fileId": file_id},
                                         resource="/files/{fileId}/download"), None)
    assert r["statusCode"] == 409


def test_rechaza_si_el_tamano_no_coincide_con_lo_pedido(curso):
    file_id = _pedir(PDF, size=10)
    assert _subir_y_escanear(curso, file_id, PDF) == ["INFECTED"]
    assert not _existe(curso, file_id)
    assert items("FILE#")[0]["scanSignature"] == "TAMANO_NO_COINCIDE"


def test_evento_de_s3_repetido_no_publica_dos_veces(curso):
    file_id = _pedir(PDF)
    _subir_y_escanear(curso, file_id, PDF)
    clave = f"archivos/{TENANT}/{file_id}"
    repetido = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": clave}}}]}
    assert handler.lambda_handler(repetido, None)["resultados"] == ["DUPLICADO"]
    assert len(_escaneados()) == 1


def test_resubir_con_la_url_vigente_vuelve_a_escanear(curso):
    """Un CLEAN no se puede reemplazar por un archivo infectado con la misma URL."""
    file_id = _pedir(PDF)
    _subir_y_escanear(curso, file_id, PDF)
    sucio = escaner.EICAR + b" " * (len(PDF) - len(escaner.EICAR))
    assert _subir_y_escanear(curso, file_id, sucio) == ["INFECTED"]
    assert _estado(file_id) == "INFECTED"
    versiones = sorted((e["metadata"]["aggregateVersion"], e["data"]["result"]) for e in _escaneados())
    assert versiones == [(2, "CLEAN"), (3, "INFECTED")]


def test_objeto_sin_ficha_se_borra_y_clave_ajena_se_ignora(curso):
    huerfano = "fil_01JCQRZHQT9F4WY2JYPDV546K7"
    assert _subir_y_escanear(curso, huerfano, PDF) == ["IGNORADO"]
    assert not _existe(curso, huerfano)
    ajeno = {"Records": [{"s3": {"bucket": {"name": BUCKET}, "object": {"key": "otra/cosa.txt"}}}]}
    assert handler.lambda_handler(ajeno, None)["resultados"] == ["IGNORADO"]
    assert _escaneados() == []


def test_file_scanned_viaja_por_el_bus_hasta_ms_evaluaciones(curso):
    """Outbox -> Stream -> relay -> bus -> regla de ms-evaluaciones -> su cola."""
    file_id = _pedir(PDF)
    _subir_y_escanear(curso, file_id, PDF)
    assert stream_a_bus.publicar(registros_stream()) == 1
    recibidos = curso["sqs"].receive_message(QueueUrl=curso["cola"], MaxNumberOfMessages=10)["Messages"]
    (cuerpo,) = [json.loads(m["Body"]) for m in recibidos]
    assert cuerpo["detail-type"] == ESCANEADO and cuerpo["source"] == "lms.content"
    assert cuerpo["detail"]["data"]["fileId"] == file_id
