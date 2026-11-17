"""
Lambda de antivirus (CON-01). Se dispara con s3:ObjectCreated en archivos/<tenantId>/<fileId>.

Por cada objeto:
1. Lee el ítem FILE#<fileId>. Si el mismo contenido (ETag) ya se escaneó, no hace nada (S3 puede
   entregar el evento más de una vez).
2. Marca el archivo PENDING_SCAN y lo escanea por bloques (antivirus/escaner.py). Un archivo cuyo
   tamaño no coincide con el que se pidió, o que pasa de 50 MB, se rechaza igual que uno infectado.
3. Si está infectado, borra el objeto de S3 para que nadie lo pueda descargar.
4. Guarda el resultado y lms.content.file.scanned.v1 en la misma transacción (outbox), con la
   versión del archivo +1. Si alguien cambió el ítem mientras tanto, no guarda nada.

Si el mismo fileId se vuelve a subir con la URL todavía vigente, el ETag cambia y se escanea de
nuevo (nueva versión y nuevo file.scanned): lo que se descarga siempre es lo que se escaneó.

Si algo falla se lanza la excepción y Lambda reintenta (invocación asíncrona de S3).
"""

import re
from urllib.parse import unquote_plus

from botocore.exceptions import ClientError

from antivirus import escaner
from api import comun
from lib import almacen, logs, outbox
from lib import tabla as t

ESCANEADO = "lms.content.file.scanned.v1"
CLAVE = re.compile(r"^archivos/([a-z0-9-]{2,32})/(fil_[0-9A-HJKMNP-TV-Z]{26})$")
TAMANO_MAXIMO = 52428800
BLOQUE = 1024 * 1024
FINALES = ("CLEAN", "INFECTED")


def _leer_archivo(tenant_id: str, file_id: str) -> dict | None:
    clave = {"tenant_id": tenant_id, "sk": comun.sk_archivo(file_id)}
    return t.tabla().get_item(Key=clave, ConsistentRead=True).get("Item")


def _marcar_en_escaneo(item: dict) -> None:
    t.tabla().update_item(
        Key={"tenant_id": item["tenant_id"], "sk": item["sk"]},
        UpdateExpression="SET #s = :p",
        ConditionExpression="version = :v",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":p": "PENDING_SCAN", ":v": item["version"]},
    )


def _escanear_objeto(s3, bucket: str, clave: str, declarado: int, tamano: int) -> escaner.Resultado:
    if tamano > TAMANO_MAXIMO or tamano != declarado:
        return escaner.Resultado("INFECTED", "TAMANO_NO_COINCIDE", 0, "")
    cuerpo = s3.get_object(Bucket=bucket, Key=clave)["Body"]
    return escaner.escanear(cuerpo.iter_chunks(BLOQUE))


def procesar_objeto(bucket: str, clave: str) -> str:
    """Devuelve qué pasó: CLEAN, INFECTED, DUPLICADO, IGNORADO o CARRERA (para logs y pruebas)."""
    coincide = CLAVE.match(clave)
    if not coincide:
        logs.info(mensaje="Objeto fuera de archivos/<tenant>/<fileId>", clave=clave)
        return "IGNORADO"
    tenant_id, file_id = coincide.groups()
    s3 = almacen.cliente()
    item = _leer_archivo(tenant_id, file_id)
    if item is None:
        # Nadie pidió esta subida: no se deja el objeto en el bucket.
        s3.delete_object(Bucket=bucket, Key=clave)
        logs.error(mensaje="Objeto sin ítem FILE#, borrado", clave=clave)
        return "IGNORADO"

    cabecera = s3.head_object(Bucket=bucket, Key=clave)
    etag = cabecera["ETag"]
    if item["status"] in FINALES and item.get("scannedEtag") == etag:
        logs.info(mensaje="Archivo ya escaneado", fileId=file_id, tenantId=tenant_id)
        return "DUPLICADO"

    try:
        _marcar_en_escaneo(item)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logs.info(mensaje="Otro escaneo ganó la carrera", fileId=file_id)
            return "CARRERA"
        raise

    resultado = _escanear_objeto(s3, bucket, clave, int(item["sizeBytes"]), int(cabecera["ContentLength"]))
    if not resultado.limpio:
        s3.delete_object(Bucket=bucket, Key=clave)

    version = int(item["version"]) + 1
    evento = outbox.nuevo_evento(
        ESCANEADO,
        tenant_id=tenant_id,
        aggregate_type="file",
        aggregate_id=file_id,
        aggregate_version=version,
        actor_id="system",
        course_id=item["courseId"],
        data={
            "fileId": file_id,
            "purpose": item["purpose"],
            "result": resultado.resultado,
            "contentType": item["contentType"],
            "sizeBytes": int(item["sizeBytes"]),
            "ownerId": item["ownerId"],
        },
    )
    cambio = {
        "Update": {
            "Key": {"tenant_id": tenant_id, "sk": item["sk"]},
            "UpdateExpression": ("SET #s = :r, version = :nv, scannedAt = :en, scannedEtag = :etag, "
                                 "scannedBytes = :b, scanSignature = :f"),
            "ConditionExpression": "version = :v",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {
                ":r": resultado.resultado, ":nv": version, ":v": item["version"], ":en": outbox.ahora_iso(),
                ":etag": etag, ":b": int(cabecera["ContentLength"]), ":f": resultado.firma or "-",
            },
        }
    }
    try:
        outbox.guardar(cambios=[cambio], eventos=[evento])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            logs.info(mensaje="El archivo cambió durante el escaneo", fileId=file_id)
            return "CARRERA"
        raise
    logs.info(mensaje="Archivo escaneado", fileId=file_id, tenantId=tenant_id, resultado=resultado.resultado,
              firma=resultado.firma)
    return resultado.resultado


def lambda_handler(event, context):
    resultados = []
    for record in (event or {}).get("Records", []):
        s3 = record["s3"]
        resultados.append(procesar_objeto(s3["bucket"]["name"], unquote_plus(s3["object"]["key"])))
    return {"resultados": resultados}
