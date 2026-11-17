"""
Archivos y media (CON-01), según apis/ms-contenidos.openapi.yaml de nubeduca-contratos.

POST /files                      {courseId, purpose, fileName, contentType, sizeBytes}
                                 -> 201 {fileId, uploadUrl, method: PUT, headers, expiresAt}
                                 CONTENT: solo docentes del curso (o ADMIN). SUBMISSION: miembros del curso.
GET  /files/{fileId}             estado del archivo (PENDING_UPLOAD, PENDING_SCAN, CLEAN, INFECTED)
GET  /files/{fileId}/download    URL prefirmada de 5 minutos; 409 si el archivo no está CLEAN

El binario nunca pasa por la Lambda: el navegador hace PUT directo a S3 con la URL prefirmada
(15 minutos). La Lambda de antivirus (antivirus/handler.py) se dispara con el evento de S3,
escanea el objeto y publica lms.content.file.scanned.v1. Esta API no publica eventos.

Ítem: FILE#<fileId>. Objeto en S3: archivos/<tenantId>/<fileId>.
"""

import re
from datetime import datetime, timedelta, timezone

from api import comun
from lib import almacen, auth, http, ids, outbox
from lib import tabla as t

PROPOSITOS = ("CONTENT", "SUBMISSION")
TAMANO_MAXIMO = 52428800  # 50 MB, como en el contrato
SUBIDA_SEGUNDOS = 15 * 60
DESCARGA_SEGUNDOS = 5 * 60
CONTENT_TYPE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


def clave_s3(tenant_id: str, file_id: str) -> str:
    return f"archivos/{tenant_id}/{file_id}"


def _expira(segundos: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=segundos)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validar_subida(cuerpo) -> str | None:
    """Mensaje de error si el body no cumple UploadRequest; None si está bien."""
    if not isinstance(cuerpo, dict):
        return "El body debe ser un objeto JSON."
    if not comun.COURSE_ID.match(str(cuerpo.get("courseId") or "")):
        return "courseId no es válido."
    if cuerpo.get("purpose") not in PROPOSITOS:
        return "purpose debe ser CONTENT o SUBMISSION."
    nombre = cuerpo.get("fileName")
    if not isinstance(nombre, str) or not nombre.strip() or len(nombre) > 200 or "/" in nombre or "\\" in nombre:
        return "fileName es obligatorio, tiene hasta 200 caracteres y no lleva rutas."
    tipo = cuerpo.get("contentType")
    if not isinstance(tipo, str) or len(tipo) > 100 or not CONTENT_TYPE.match(tipo):
        return "contentType no es un tipo MIME válido."
    tamano = cuerpo.get("sizeBytes")
    if not isinstance(tamano, int) or isinstance(tamano, bool) or not 1 <= tamano <= TAMANO_MAXIMO:
        return "sizeBytes debe ser un entero entre 1 y 52428800 (50 MB)."
    return None


def _puede_subir(usuario, course_id: str, proposito: str) -> bool:
    if usuario.rol == "ADMIN":
        return True
    if proposito == "CONTENT":
        return comun.es_docente_del_curso(usuario, course_id)
    return comun.es_miembro_del_curso(usuario, course_id)


def pedir_subida(event, usuario):
    cuerpo = http.leer_json(event)
    problema = _validar_subida(cuerpo)
    if problema:
        return http.datos_invalidos(event, problema)
    course_id, proposito = cuerpo["courseId"], cuerpo["purpose"]
    if not _puede_subir(usuario, course_id, proposito):
        mensaje = ("Solo un docente del curso puede subir contenido." if proposito == "CONTENT"
                   else "No estás matriculado en este curso.")
        return http.error(event, 403, "SIN_PERMISO", mensaje)

    file_id = ids.nuevo("fil")
    clave = clave_s3(usuario.tenant_id, file_id)
    item = {
        "tenant_id": usuario.tenant_id,  # del token, nunca del body
        "sk": comun.sk_archivo(file_id),
        "entidad": "FILE",
        "version": 1,
        "fileId": file_id,
        "courseId": course_id,
        "purpose": proposito,
        "fileName": cuerpo["fileName"].strip(),
        "contentType": cuerpo["contentType"],
        "sizeBytes": cuerpo["sizeBytes"],
        "status": "PENDING_UPLOAD",
        "ownerId": usuario.user_id,
        "createdAt": outbox.ahora_iso(),
        "s3Key": clave,
    }
    t.tabla().put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
    url = almacen.cliente().generate_presigned_url(
        "put_object",
        Params={"Bucket": almacen.bucket(), "Key": clave, "ContentType": item["contentType"]},
        ExpiresIn=SUBIDA_SEGUNDOS,
    )
    return http.respuesta(201, {
        "fileId": file_id,
        "uploadUrl": url,
        "method": "PUT",
        "headers": {"Content-Type": item["contentType"]},
        "expiresAt": _expira(SUBIDA_SEGUNDOS),
    })


def _nombre_seguro(nombre: str) -> str:
    """Para Content-Disposition: sin comillas ni saltos de línea."""
    return re.sub(r'["\r\n\\]', "_", nombre)


def link_descarga(event, usuario, archivo):
    if archivo["status"] != "CLEAN":
        return http.error(event, 409, "ARCHIVO_NO_DISPONIBLE",
                          f"El archivo está en {archivo['status']}: solo se descargan archivos CLEAN.")
    url = almacen.cliente().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": almacen.bucket(),
            "Key": archivo["s3Key"],
            "ResponseContentType": archivo["contentType"],
            "ResponseContentDisposition": f'attachment; filename="{_nombre_seguro(archivo["fileName"])}"',
        },
        ExpiresIn=DESCARGA_SEGUNDOS,
    )
    return http.respuesta(200, {"downloadUrl": url, "expiresAt": _expira(DESCARGA_SEGUNDOS)})


def _no_encontrado(event):
    return http.error(event, 404, "ARCHIVO_NO_ENCONTRADO", "No existe el archivo en este tenant.")


def lambda_handler(event, context):
    usuario = auth.usuario(event)
    if usuario is None:
        return http.no_autenticado(event)
    event = event or {}
    if event.get("httpMethod") == "POST":
        return pedir_subida(event, usuario)

    archivo = comun.leer_archivo(usuario, comun.path_param(event, "fileId"))
    if archivo is None:
        return _no_encontrado(event)
    if not comun.puede_ver_archivo(usuario, archivo):
        # Al alumno no se le revela que el archivo existe; al docente sin acceso, 403.
        return _no_encontrado(event) if usuario.rol == "STUDENT" else http.sin_permiso(event)
    if (event.get("resource") or "").endswith("/download"):
        return link_descarga(event, usuario, archivo)
    return http.respuesta(200, comun.a_archivo(archivo))
