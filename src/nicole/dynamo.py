"""Shared domain records for replaceable Workers; JSON stays byte-comparable."""

import json
import os
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError


def client():
    return boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION"))


def key(pk, sk):
    return {"PK": {"S": pk}, "SK": {"S": sk}}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def item(pk, sk, value):
    return {**key(pk, sk), "content": {"S": encoded(value)}}


def get(pk, sk):
    row = (
        client()
        .get_item(
            TableName=os.environ["NICOLE_DYNAMODB_TABLE"],
            Key=key(pk, sk),
            ConsistentRead=True,
        )
        .get("Item")
    )
    return json.loads(row["content"]["S"]) if row else None


def put_once(pk, sk, value):
    try:
        client().put_item(
            TableName=os.environ["NICOLE_DYNAMODB_TABLE"],
            Item=item(pk, sk, value),
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        if get(pk, sk) != value:
            raise ValueError("This operation already owns a different record") from None


def rows(pk, prefix):
    request = dict(
        TableName=os.environ["NICOLE_DYNAMODB_TABLE"],
        KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
        ExpressionAttributeValues={":pk": {"S": pk}, ":prefix": {"S": prefix}},
        ConsistentRead=True,
    )
    while True:
        result = client().query(**request)
        yield from (json.loads(r["content"]["S"]) for r in result.get("Items", []))
        if not result.get("LastEvaluatedKey"):
            break
        request["ExclusiveStartKey"] = result["LastEvaluatedKey"]


def memories(actor):
    return list(rows("ACTOR#" + actor, "MEMORY#"))[:12]


def record_turn(data):
    table = os.environ["NICOLE_DYNAMODB_TABLE"]
    turn = {k: data[k] for k in ("id", "actor", "session", "text", "reply")}
    pk = "TURN#" + data["id"]
    writes = [
        {
            "Put": {
                "TableName": table,
                "Item": item(pk, "TURN", turn),
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }
    ]
    if data["backend"] == "local":
        # One atomic turn receipt plus its preference updates, safe across retries.
        for memory in {m["key"]: m for m in data["memories"]}.values():
            writes.append(
                {
                    "Put": {
                        "TableName": table,
                        "Item": item(
                            "ACTOR#" + data["actor"],
                            "MEMORY#" + memory["key"],
                            {
                                **memory,
                                "source_session": data["session"],
                            },
                        ),
                    }
                }
            )
    try:
        client().transact_write_items(TransactItems=writes)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        existing = get(pk, "TURN")
        if existing is None:
            raise
        if existing != turn:
            raise ValueError("This turn already owns a different record") from None


def save_pick(request_id, actor, movie):
    put_once("REQUEST#" + request_id, "PICK", {"actor": actor, "movie": movie})
    return movie


def saved_pick(request_id, actor):
    record = get("REQUEST#" + request_id, "PICK")
    return record["movie"] if record and record["actor"] == actor else None


def count_call(operation, request_id):
    put_once("REQUEST#" + request_id, "CALL#" + uuid4().hex, {"operation": operation})


def call_counts(request_id):
    counts = {}
    for record in rows("REQUEST#" + request_id, "CALL#"):
        op = record["operation"]
        counts[op] = counts.get(op, 0) + 1
    return counts
