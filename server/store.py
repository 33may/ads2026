"""File store access: texts live as objects in one MinIO bucket, key = reference."""
import os

import boto3
from botocore.exceptions import ClientError


class Store:
    def __init__(self):
        self._s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["MINIO_ENDPOINT"],
            aws_access_key_id=os.environ["MINIO_ROOT_USER"],
            aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
        )
        self._bucket = os.environ["MINIO_BUCKET"]
        self._ensure_bucket()

    def _ensure_bucket(self):
        try:
            self._s3.create_bucket(Bucket=self._bucket)
        except ClientError as e:
            if e.response["Error"]["Code"] not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise

    def put(self, reference, text):
        self._s3.put_object(Bucket=self._bucket, Key=reference, Body=text.encode("utf-8"))

    def get(self, reference):
        try:
            obj = self._s3.get_object(Bucket=self._bucket, Key=reference)
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                raise KeyError(reference) from None
            raise
        return obj["Body"].read().decode("utf-8")

    def delete(self, reference):
        self._s3.delete_object(Bucket=self._bucket, Key=reference)

    def list(self, prefix=""):
        resp = self._s3.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
        return [o["Key"] for o in resp.get("Contents", [])]
