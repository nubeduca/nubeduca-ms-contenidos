import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import almacen  # noqa: E402
from lib import tabla as t  # noqa: E402
from relay import stream_a_bus  # noqa: E402

TABLA = "test-t_contenidos"
BUCKET = "test-contenidos"
BUS = "lms"
TENANT = "utec"
ADMIN = "16248d98-4e7a-41b9-a08b-4929177d2e2d"
DOCENTE = "c62d9930-b473-4b1d-adec-72fdda7cf599"
OTRO_DOCENTE = "0b1e3f52-7c2d-4c8e-9a51-2f6d3e8b9c10"
ALUMNO = "a18577e2-e005-4a49-a29e-755dbf08f5b6"
OTRO_ALUMNO = "5d0c7a8e-2f41-4b6a-9e3d-1c8b7a6f5e42"
CURSO = "crs_01JCQRZHQT9F4WY2JYPDV546K7"
CONSUMIDOS = Path(__file__).parent / "consumidos"


def token(rol="TEACHER", tenant=TENANT, user=DOCENTE):
    return {"Authorization": f"Bearer mock.{tenant}.{rol}.{user}"}


def peticion(metodo, headers, body=None, path_params=None, query=None, resource=None):
    return {
        "httpMethod": metodo,
        "resource": resource,
        "headers": headers,
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": path_params,
        "queryStringParameters": query,
        "requestContext": {"requestId": "req-test"},
    }


def items(prefijo, tenant=TENANT):
    resp = t.tabla().query(
        KeyConditionExpression="tenant_id = :t AND begins_with(sk, :p)",
        ExpressionAttributeValues={":t": tenant, ":p": prefijo},
    )
    return resp["Items"]


def eventos(tenant=TENANT):
    return [json.loads(i["detail"]) | {"_detailType": i["detailType"], "_source": i["source"]}
            for i in items("EVENTO#", tenant)]


def mensaje(detail_type, *, tenant=TENANT, event_id, version=1, course_id=CURSO, aggregate_id=None, **data):
    """Body de SQS con un evento de ms-cursos, a partir del ejemplo oficial del catálogo."""
    evento = json.loads((CONSUMIDOS / f"{detail_type}.json").read_text(encoding="utf-8"))
    meta = evento["detail"]["metadata"]
    meta.update(eventId=event_id, tenantId=tenant, courseId=course_id, aggregateVersion=version)
    if aggregate_id:
        meta["aggregateId"] = aggregate_id
    if detail_type == "lms.courses.course.created.v1":
        meta["aggregateId"] = course_id
    evento["detail"]["data"].update(data)
    return json.dumps(evento)


def registros_stream():
    ddb = boto3.client("dynamodb")
    streams = boto3.client("dynamodbstreams")
    arn = ddb.describe_table(TableName=TABLA)["Table"]["LatestStreamArn"]
    shard = streams.describe_stream(StreamArn=arn)["StreamDescription"]["Shards"][0]["ShardId"]
    it = streams.get_shard_iterator(StreamArn=arn, ShardId=shard, ShardIteratorType="TRIM_HORIZON")["ShardIterator"]
    return streams.get_records(ShardIterator=it)["Records"]


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("TABLA", TABLA)
    monkeypatch.setenv("BUCKET", BUCKET)
    monkeypatch.setenv("EVENT_BUS", BUS)
    monkeypatch.setenv("EVENT_SOURCE", "lms.content")
    monkeypatch.setenv("AUTH_MODE", "mock")
    with mock_aws():
        t.reiniciar()
        almacen.reiniciar()
        stream_a_bus.reiniciar()
        ddb = boto3.client("dynamodb")
        ddb.create_table(
            TableName=TABLA,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "tenant_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "tenant_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
        )
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket=BUCKET)
        # Cola que hace de ms-evaluaciones: recibe file.scanned desde el bus.
        sqs = boto3.client("sqs")
        cola = sqs.create_queue(QueueName="evaluaciones-eventos")["QueueUrl"]
        cola_arn = sqs.get_queue_attributes(QueueUrl=cola, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        events = boto3.client("events")
        events.create_event_bus(Name=BUS)
        events.put_rule(
            Name="evaluaciones-archivos",
            EventBusName=BUS,
            EventPattern='{"source":["lms.content"],"detail-type":["lms.content.file.scanned.v1"]}',
        )
        events.put_targets(Rule="evaluaciones-archivos", EventBusName=BUS, Targets=[{"Id": "1", "Arn": cola_arn}])
        yield {"cola": cola, "sqs": sqs, "ddb": ddb, "s3": s3}
