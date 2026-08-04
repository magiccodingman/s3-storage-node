from __future__ import annotations

from unittest.mock import Mock

import s3_storage_node.s3check as s3check


def test_canary_connects_to_worker_host(monkeypatch) -> None:
    connection = Mock()
    response = Mock()
    response.status = 200
    response.read.return_value = b""
    connection.getresponse.return_value = response
    constructor = Mock(return_value=connection)
    monkeypatch.setattr(s3check.http.client, "HTTPConnection", constructor)

    s3check._request("169.254.254.2", 18333, "GET", "/", b"", None, None, "example")

    constructor.assert_called_once_with("169.254.254.2", 18333, timeout=10)
