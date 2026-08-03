from __future__ import annotations

import hashlib
import hmac
import http.client
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit


class S3CheckError(RuntimeError):
    pass


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _authorization(method: str, path: str, host: str, payload: bytes, access_key: str, secret_key: str) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join([method, quote(path, safe="/"), "", canonical_headers, signed_headers, payload_hash])
    scope = f"{date_stamp}/us-east-1/s3/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    date_key = _sign(("AWS4" + secret_key).encode(), date_stamp)
    region_key = hmac.new(date_key, b"us-east-1", hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    signing_key = hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    return {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": authorization,
        "Content-Length": str(len(payload)),
    }


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    body: bytes,
    access_key: str | None,
    secret_key: str | None,
    signature_host: str,
) -> tuple[int, bytes]:
    headers = {"Host": signature_host, "Content-Length": str(len(body))}
    if access_key and secret_key:
        headers.update(_authorization(method, path, signature_host, body, access_key, secret_key))
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _run_canary_once(
    host: str,
    port: int,
    access_key: str | None,
    secret_key: str | None,
    signature_host: str,
) -> None:
    bucket = "s3-storage-node-health"
    key = "canary"
    data = b"s3-storage-node-canary-v1"

    status, body = _request(host, port, "PUT", f"/{bucket}", b"", access_key, secret_key, signature_host)
    if status not in {200, 204, 409}:
        raise S3CheckError(f"bucket canary failed with HTTP {status}: {body[:200]!r}")
    status, body = _request(host, port, "PUT", f"/{bucket}/{key}", data, access_key, secret_key, signature_host)
    if status not in {200, 201, 204}:
        raise S3CheckError(f"PUT canary failed with HTTP {status}: {body[:200]!r}")
    status, body = _request(host, port, "GET", f"/{bucket}/{key}", b"", access_key, secret_key, signature_host)
    if status != 200 or body != data:
        raise S3CheckError(f"GET canary failed with HTTP {status} or content mismatch")
    status, body = _request(host, port, "DELETE", f"/{bucket}/{key}", b"", access_key, secret_key, signature_host)
    if status not in {200, 204}:
        raise S3CheckError(f"DELETE canary failed with HTTP {status}: {body[:200]!r}")


def run_canary(
    port: int,
    access_key: str | None,
    secret_key: str | None,
    *,
    host: str = "127.0.0.1",
    external_url: str = "",
    retry_seconds: float = 15.0,
    retry_interval_seconds: float = 1.0,
) -> None:
    signature_host = f"{host}:{port}"
    if external_url:
        parsed = urlsplit(external_url)
        if not parsed.netloc:
            raise S3CheckError(f"invalid external_url for S3 canary: {external_url}")
        signature_host = parsed.netloc

    deadline = time.monotonic() + max(0.0, retry_seconds)
    last_error: S3CheckError | None = None
    while True:
        try:
            _run_canary_once(host, port, access_key, secret_key, signature_host)
            return
        except S3CheckError as exc:
            last_error = exc
            if time.monotonic() >= deadline:
                raise last_error
            time.sleep(min(retry_interval_seconds, max(0.0, deadline - time.monotonic())))
