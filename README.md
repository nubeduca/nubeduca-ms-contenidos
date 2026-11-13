# nubeduca-ms-contenidos

Microservicio de Nubeduca: Contenido del curso: módulos, páginas e ítems, y archivos en S3 con antivirus.

Contrato: [`apis/ms-contenidos.openapi.yaml`](https://github.com/nubeduca/nubeduca-contratos/blob/main/apis/ms-contenidos.openapi.yaml) y el [catálogo de eventos](https://github.com/nubeduca/nubeduca-contratos/blob/main/eventos/catalogo.yaml) de `nubeduca-contratos`. **No se publica un evento ni se expone una ruta que no esté ahí.**

## Tareas

| Tarea | Responsable | Listo cuando |
|---|---|---|
| [CON-01 · Archivos y Media](https://github.com/nubeduca/nubeduca-backlog/issues/11) | Dev 5 @AIexander-lx | Subida a S3 con URL prefirmada y antivirus en Lambda; publica `file.scanned` y rechaza EICAR |
| [CON-02 · Contenido y Módulos](https://github.com/nubeduca/nubeduca-backlog/issues/20) | Dev 5 @AIexander-lx | Módulos, páginas e ítems con archivos de CON-01; publica `module.published` |

**Estado:** ⚪ Solo README. CON-01 está lista para empezar (su única dependencia, LIB-01, está cerrada); CON-02 espera a CON-01.

## Eventos

| | |
|---|---|
| `source` | `lms.content` |
| Publica | `lms.content.file.scanned.v1` (→ ms-evaluaciones), `lms.content.module.published.v1` (→ ms-notificaciones) |
| Consume | `lms.courses.course.created.v1`, `lms.courses.enrollment.created.v1`, `lms.courses.enrollment.dropped.v1` |

## Cómo empezar

1. Copia [`nubeduca-plantilla-servicio`](https://github.com/nubeduca/nubeduca-plantilla-servicio) a este repo (D1: un repo por microservicio). Ejemplos ya hechos: [`nubeduca-ms-usuarios`](https://github.com/nubeduca/nubeduca-ms-usuarios) y [`nubeduca-ms-cursos`](https://github.com/nubeduca/nubeduca-ms-cursos).
2. En `serverless.yml`: `service: nubeduca-ms-contenidos`, `SERVICIO: ms-contenidos`, `EVENT_SOURCE: lms.content`, tabla `${sls:stage}-t_contenidos`, y una regla, cola, DLQ y alarma para los eventos que consume. Sin línea `org:`.
3. Copia tus esquemas: `python scripts/copiar_esquemas.py --solo lms.content`.
4. Implementa las rutas del OpenAPI con pruebas (moto, sin AWS) y CI (ruff, yamllint y pytest).
5. Agrega `scripts/poblar_datos.py --stage <stage>`: 30.000 registros en la tabla principal, **UTEC 15.000, UPC 10.000 y PUCP 5.000**, sin ítems `EVENTO#`.
6. Documenta en este README la **estructura JSON de tus tablas** (lo pide la rúbrica) y publica la URL de tu API en SSM (`/nubeduca/<stage>/api/contenidos/url`).

## Desplegar

En la cuenta de integración (D2) se despliega con todo lo demás desde la VM `nubeduca-despliegue`: `scripts/desplegar.sh staging --con-seed` de [`nubeduca-backend`](https://github.com/nubeduca/nubeduca-backend), o solo este servicio con `--solo contenidos`. Mientras este repo no tenga `serverless.yml`, `desplegar.sh` lo marca como **PENDIENTE**.

Reglas del equipo: Python 3.13, Serverless Framework v4, rol `LabRole`, `tenant_id` y rol solo desde el token, ramas `<tipo>/<ID>-descripcion`, commits `<ID>: mensaje` y PRs a `main` con 1 aprobación y la CI en verde.
