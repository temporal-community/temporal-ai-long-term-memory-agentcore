"""Optional SDK claim check. S3 I/O runs outside Workflow execution."""

import asyncio
import os
from dataclasses import replace

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from temporalio.contrib.aws.s3driver import S3StorageDriver, S3StorageDriverClient
from temporalio.converter import DataConverter, ExternalStorage


class BotoS3Client(S3StorageDriverClient):
    """Use our existing boto3/CRT credential chain without blocking the event loop."""

    def __init__(self, client, bucket: str, owner: str):
        self.client, self.bucket, self.owner = client, bucket, owner

    def location(self, bucket, key):
        # Claims are input data. Never let one redirect this client to other data.
        if bucket != self.bucket or not key.startswith("v0/"):
            raise ValueError("Payload reference is outside Nicole's configured bucket")
        return {"Bucket": bucket, "Key": key, "ExpectedBucketOwner": self.owner}

    async def object_exists(self, *, bucket, key):
        try:
            await asyncio.to_thread(
                self.client.head_object, **self.location(bucket, key)
            )
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    async def put_object(self, *, bucket, key, data):
        await asyncio.to_thread(
            self.client.put_object,
            **self.location(bucket, key),
            Body=data,
            ServerSideEncryption="AES256",
        )

    async def get_object(self, *, bucket, key):
        location = self.location(bucket, key)

        def read():
            response = self.client.get_object(**location)
            try:
                return response["Body"].read()
            finally:
                response["Body"].close()

        return await asyncio.to_thread(read)


def data_converter():
    bucket = os.getenv("NICOLE_PAYLOAD_BUCKET")
    if not bucket:
        return DataConverter.default
    owner = os.environ["NICOLE_PAYLOAD_OWNER"]
    if len(owner) != 12 or not owner.isdigit():
        raise ValueError("NICOLE_PAYLOAD_OWNER must be an AWS account ID")
    threshold = int(os.getenv("NICOLE_PAYLOAD_THRESHOLD", str(256 * 1024)))
    if threshold < 0:
        raise ValueError("NICOLE_PAYLOAD_THRESHOLD must be nonnegative")
    client = boto3.client(
        "s3",
        region_name=os.environ["NICOLE_PAYLOAD_REGION"],
        config=Config(connect_timeout=3, read_timeout=15, retries={"max_attempts": 3}),
    )
    return replace(
        DataConverter.default,
        external_storage=ExternalStorage(
            drivers=[S3StorageDriver(BotoS3Client(client, bucket, owner), bucket)],
            payload_size_threshold=threshold,
        ),
    )
