"""
Contenido y módulos (CON-02), según apis/ms-contenidos.openapi.yaml de nubeduca-contratos.

GET   /courses/{courseId}/modules                              miembros del curso; STUDENT solo ve PUBLISHED
POST  /courses/{courseId}/modules             {title, position} docente del curso; queda en DRAFT
GET   /courses/{courseId}/modules/{moduleId}                   un módulo con sus ítems
PATCH /courses/{courseId}/modules/{moduleId}  {title?, position?}
POST  /courses/{courseId}/modules/{moduleId}/items             PAGE {title, body} · FILE {title, fileId} · LINK {title, url}
                                                               se agrega al final; 409 si el archivo no está CLEAN
POST  /courses/{courseId}/modules/{moduleId}/publish           409 si ya está publicado o no tiene ítems.
                                                               Publica lms.content.module.published.v1

Ítems de la tabla:
- MODULE#<courseId>#<moduleId>                     el módulo (status, position, itemCount, version)
- MODULE#<courseId>#<moduleId>#ITEM#<itemId>       sus ítems, en orden de llegada (itemId es un ULID)

Como los ítems van debajo de su módulo, una sola consulta por prefijo trae el curso o el módulo
completo. "Docente del curso" = dueño (course.created) o matriculado como TEACHER.
"""

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from api import comun
from lib import auth, http, ids, outbox
from lib import tabla as t

PUBLICADO = "lms.content.module.published.v1"
TIPOS = ("PAGE", "FILE", "LINK")
CAMPOS_POR_TIPO = {"PAGE": "body", "FILE": "fileId", "LINK": "url"}


# --- Lectura -----------------------------------------------------------------------------

def _consultar(tenant_id: str, prefijo: str) -> list[dict]:
    consulta = {"KeyConditionExpression": Key("tenant_id").eq(tenant_id) & Key("sk").begins_with(prefijo),
                "ConsistentRead": True}
    salida = []
    while True:
        resultado = t.tabla().query(**consulta)
        salida.extend(resultado.get("Items", []))
        if "LastEvaluatedKey" not in resultado:
            return salida
        consulta["ExclusiveStartKey"] = resultado["LastEvaluatedKey"]


def a_item(item: dict) -> dict:
    salida = {"itemId": item["itemId"], "type": item["type"], "title": item["title"],
              "position": int(item["position"])}
    campo = CAMPOS_POR_TIPO[item["type"]]
    salida[campo] = item[campo]
    return salida


def a_modulo(item: dict, items: list[dict]) -> dict:
    return {
        "moduleId": item["moduleId"],
        "title": item["title"],
        "position": int(item["position"]),
        "status": item["status"],
        "items": [a_item(i) for i in sorted(items, key=lambda i: int(i["position"]))],
    }


def _modulos(tenant_id: str, course_id: str, module_id: str = "") -> list[dict]:
    """Módulos (con sus ítems) del curso, o solo el módulo pedido. Ordenados por posición."""
    filas = _consultar(tenant_id, comun.sk_modulo(course_id, module_id))
    cabeceras = {f["moduleId"]: f for f in filas if f.get("entidad") == "MODULE"}
    hijos: dict[str, list] = {mid: [] for mid in cabeceras}
    for fila in filas:
        if fila.get("entidad") == "MODULE_ITEM" and fila["moduleId"] in hijos:
            hijos[fila["moduleId"]].append(fila)
    modulos = [a_modulo(cab, hijos[mid]) for mid, cab in cabeceras.items()]
    return sorted(modulos, key=lambda m: (m["position"], m["moduleId"]))


def _leer_modulo(tenant_id: str, course_id: str, module_id: str) -> dict | None:
    if not comun.MODULE_ID.match(module_id or ""):
        return None
    clave = {"tenant_id": tenant_id, "sk": comun.sk_modulo(course_id, module_id)}
    return t.tabla().get_item(Key=clave, ConsistentRead=True).get("Item")


# --- Validación ----------------------------------------------------------------------------

def _titulo(valor) -> bool:
    return isinstance(valor, str) and bool(valor.strip()) and len(valor) <= 200


def _posicion(valor) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool) and valor >= 1


def _validar_item(cuerpo) -> str | None:
    if not isinstance(cuerpo, dict):
        return "El body debe ser un objeto JSON."
    tipo = cuerpo.get("type")
    if tipo not in TIPOS:
        return "type debe ser PAGE, FILE o LINK."
    if not _titulo(cuerpo.get("title")):
        return "title es obligatorio y tiene hasta 200 caracteres."
    propio = CAMPOS_POR_TIPO[tipo]
    ajenos = [c for c in CAMPOS_POR_TIPO.values() if c != propio and cuerpo.get(c) is not None]
    if ajenos:
        return f"Un ítem {tipo} no lleva {', '.join(ajenos)}."
    valor = cuerpo.get(propio)
    if tipo == "PAGE" and not (isinstance(valor, str) and valor.strip() and len(valor) <= 50000):
        return "body es obligatorio en PAGE y tiene hasta 50.000 caracteres."
    if tipo == "FILE" and not comun.FILE_ID.match(str(valor or "")):
        return "fileId es obligatorio en FILE y debe ser un id de archivo válido."
    if tipo == "LINK" and not (isinstance(valor, str) and len(valor) <= 2000
                               and valor.startswith(("https://", "http://")) and " " not in valor):
        return "url es obligatoria en LINK y debe empezar con http:// o https://."
    return None


# --- Errores -----------------------------------------------------------------------------

def _curso_no_encontrado(event):
    return http.error(event, 404, "CURSO_NO_ENCONTRADO", "No existe el curso en este tenant.")


def _modulo_no_encontrado(event):
    return http.error(event, 404, "MODULO_NO_ENCONTRADO", "No existe el módulo en este curso.")


def _solo_docentes(event):
    return http.error(event, 403, "SIN_PERMISO", "Solo un docente del curso puede editar sus módulos.")


# --- Operaciones -----------------------------------------------------------------------------

def listar(event, usuario, course_id):
    modulos = _modulos(usuario.tenant_id, course_id)
    if usuario.rol == "STUDENT":
        modulos = [m for m in modulos if m["status"] == "PUBLISHED"]
    return http.respuesta(200, {"items": modulos})


def ver(event, usuario, course_id, module_id):
    modulos = _modulos(usuario.tenant_id, course_id, module_id) if comun.MODULE_ID.match(module_id) else []
    if not modulos or (usuario.rol == "STUDENT" and modulos[0]["status"] != "PUBLISHED"):
        return _modulo_no_encontrado(event)
    return http.respuesta(200, modulos[0])


def crear(event, usuario, course_id):
    cuerpo = http.leer_json(event)
    if not isinstance(cuerpo, dict) or not _titulo(cuerpo.get("title")):
        return http.datos_invalidos(event, "title es obligatorio y tiene hasta 200 caracteres.")
    if not _posicion(cuerpo.get("position")):
        return http.datos_invalidos(event, "position es obligatoria y es un entero desde 1.")
    module_id = ids.nuevo("mod")
    item = {
        "tenant_id": usuario.tenant_id, "sk": comun.sk_modulo(course_id, module_id), "entidad": "MODULE",
        "version": 1, "moduleId": module_id, "courseId": course_id, "title": cuerpo["title"].strip(),
        "position": cuerpo["position"], "status": "DRAFT", "itemCount": 0, "createdBy": usuario.user_id,
        "createdAt": outbox.ahora_iso(),
    }
    t.tabla().put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
    return http.respuesta(201, a_modulo(item, []))


def editar(event, usuario, course_id, module_id):
    cuerpo = http.leer_json(event)
    if not isinstance(cuerpo, dict) or not ({"title", "position"} & set(cuerpo)):
        return http.datos_invalidos(event, "Envía title, position o ambos.")
    if "title" in cuerpo and not _titulo(cuerpo["title"]):
        return http.datos_invalidos(event, "title tiene entre 1 y 200 caracteres.")
    if "position" in cuerpo and not _posicion(cuerpo["position"]):
        return http.datos_invalidos(event, "position es un entero desde 1.")
    if not comun.MODULE_ID.match(module_id):
        return _modulo_no_encontrado(event)
    sets, nombres, valores = ["version = version + :uno"], {}, {":uno": 1}
    if "title" in cuerpo:
        sets.append("#t = :t")
        nombres["#t"] = "title"
        valores[":t"] = cuerpo["title"].strip()
    if "position" in cuerpo:
        sets.append("#p = :p")  # nombres con # por si son palabras reservadas de DynamoDB
        nombres["#p"] = "position"
        valores[":p"] = cuerpo["position"]
    actualizacion = {
        "Key": {"tenant_id": usuario.tenant_id, "sk": comun.sk_modulo(course_id, module_id)},
        "UpdateExpression": "SET " + ", ".join(sets),
        "ConditionExpression": "attribute_exists(sk)",
        "ExpressionAttributeValues": valores,
    }
    if nombres:
        actualizacion["ExpressionAttributeNames"] = nombres
    try:
        t.tabla().update_item(**actualizacion)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return _modulo_no_encontrado(event)
        raise
    return http.respuesta(200, _modulos(usuario.tenant_id, course_id, module_id)[0])


def agregar_item(event, usuario, course_id, module_id):
    cuerpo = http.leer_json(event)
    problema = _validar_item(cuerpo)
    if problema:
        return http.datos_invalidos(event, problema)
    modulo = _leer_modulo(usuario.tenant_id, course_id, module_id)
    if modulo is None:
        return _modulo_no_encontrado(event)

    cambios = []
    if cuerpo["type"] == "FILE":
        archivo = comun.leer_archivo(usuario, cuerpo["fileId"])
        if archivo is None or archivo["courseId"] != course_id or archivo["purpose"] != "CONTENT":
            return http.datos_invalidos(event, "fileId no es un archivo de contenido de este curso.")
        if archivo["status"] != "CLEAN":
            return http.error(event, 409, "ARCHIVO_NO_DISPONIBLE",
                              f"El archivo está en {archivo['status']}: solo se agregan archivos CLEAN.")
        # El archivo debe seguir CLEAN en el momento de escribir (un re-escaneo podría cambiarlo).
        cambios.append({"ConditionCheck": {
            "Key": {"tenant_id": usuario.tenant_id, "sk": comun.sk_archivo(cuerpo["fileId"])},
            "ConditionExpression": "#s = :clean",
            "ExpressionAttributeNames": {"#s": "status"},
            "ExpressionAttributeValues": {":clean": "CLEAN"},
        }})

    cantidad = int(modulo["itemCount"])
    campo = CAMPOS_POR_TIPO[cuerpo["type"]]
    item = {
        "itemId": ids.nuevo("itm"), "type": cuerpo["type"], "title": cuerpo["title"].strip(),
        "position": cantidad + 1, campo: cuerpo[campo],
    }
    cambios += [
        # itemCount = :n evita que dos ítems simultáneos queden con la misma posición.
        {"Update": {
            "Key": {"tenant_id": usuario.tenant_id, "sk": modulo["sk"]},
            "UpdateExpression": "SET itemCount = :n1, version = version + :uno",
            "ConditionExpression": "itemCount = :n",
            "ExpressionAttributeValues": {":n": cantidad, ":n1": cantidad + 1, ":uno": 1},
        }},
        {"Put": {
            "Item": {"tenant_id": usuario.tenant_id, "sk": comun.sk_item(course_id, module_id, item["itemId"]),
                     "entidad": "MODULE_ITEM", "moduleId": module_id, "courseId": course_id,
                     "createdBy": usuario.user_id, "createdAt": outbox.ahora_iso(), **item},
            "ConditionExpression": "attribute_not_exists(sk)",
        }},
    ]
    try:
        outbox.guardar(cambios=cambios, eventos=[])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            return http.error(event, 409, "MODULO_CAMBIO", "El módulo o el archivo cambió; vuelve a intentarlo.")
        raise
    return http.respuesta(201, a_item(item))


def publicar(event, usuario, course_id, module_id):
    modulo = _leer_modulo(usuario.tenant_id, course_id, module_id)
    if modulo is None:
        return _modulo_no_encontrado(event)
    if modulo["status"] == "PUBLISHED":
        return http.error(event, 409, "MODULO_YA_PUBLICADO", "El módulo ya está publicado.")
    if int(modulo["itemCount"]) == 0:
        return http.error(event, 409, "MODULO_SIN_ITEMS", "Agrega al menos un ítem antes de publicar.")

    version = int(modulo["version"]) + 1
    evento = outbox.nuevo_evento(
        PUBLICADO,
        tenant_id=usuario.tenant_id,  # del token, nunca del body
        aggregate_type="module",
        aggregate_id=module_id,
        aggregate_version=version,
        actor_id=usuario.user_id,
        course_id=course_id,
        data={"moduleId": module_id, "title": modulo["title"], "position": int(modulo["position"]),
              "itemCount": int(modulo["itemCount"])},
    )
    cambio = {"Update": {
        "Key": {"tenant_id": usuario.tenant_id, "sk": modulo["sk"]},
        "UpdateExpression": "SET #s = :pub, version = :nv, publishedAt = :en, publishedBy = :quien",
        "ConditionExpression": "version = :v AND #s = :draft",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":pub": "PUBLISHED", ":draft": "DRAFT", ":nv": version,
                                      ":v": modulo["version"], ":en": outbox.ahora_iso(),
                                      ":quien": usuario.user_id},
    }}
    try:
        outbox.guardar(cambios=[cambio], eventos=[evento])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            return http.error(event, 409, "MODULO_CAMBIO", "El módulo cambió mientras se publicaba; vuelve a intentarlo.")
        raise
    return http.respuesta(200, _modulos(usuario.tenant_id, course_id, module_id)[0])


# --- Handler -----------------------------------------------------------------------------

def lambda_handler(event, context):
    usuario = auth.usuario(event)
    if usuario is None:
        return http.no_autenticado(event)
    event = event or {}
    course_id = comun.path_param(event, "courseId")
    module_id = comun.path_param(event, "moduleId")
    metodo = event.get("httpMethod")
    recurso = event.get("resource") or ""
    if not comun.curso_conocido(usuario, course_id):
        return _curso_no_encontrado(event)

    if metodo == "GET":
        if usuario.rol != "ADMIN" and not comun.es_miembro_del_curso(usuario, course_id):
            # Al alumno ajeno no se le revela que el curso existe.
            return _curso_no_encontrado(event) if usuario.rol == "STUDENT" else http.sin_permiso(event)
        return ver(event, usuario, course_id, module_id) if module_id else listar(event, usuario, course_id)

    if not comun.es_docente_del_curso(usuario, course_id):
        return _solo_docentes(event)
    if metodo == "PATCH":
        return editar(event, usuario, course_id, module_id)
    if recurso.endswith("/items"):
        return agregar_item(event, usuario, course_id, module_id)
    if recurso.endswith("/publish"):
        return publicar(event, usuario, course_id, module_id)
    return crear(event, usuario, course_id)
