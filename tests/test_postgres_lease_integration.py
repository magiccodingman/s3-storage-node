from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_LEASE_INTEGRATION") != "1",
    reason="set RUN_POSTGRES_LEASE_INTEGRATION=1 to run PostgreSQL lease tests",
)


def test_postgres_writer_lease_serializes_owners_and_tokens(tmp_path: Path) -> None:
    import psycopg
    from psycopg import sql

    from s3_storage_node.writer_lease import PostgresLeaseBackend, WriterLeaseConfig

    dsn = os.environ["WRITER_LEASE_TEST_DSN"]
    dsn_file = tmp_path / "postgres-dsn"
    dsn_file.write_text(dsn + "\n", encoding="utf-8")
    table = f"s3sn_lease_{secrets.token_hex(5)}"
    config = WriterLeaseConfig(
        backend="postgres",
        lease_name="integration-dataset",
        node_id="integration-node",
        ttl_seconds=8,
        renew_interval_seconds=2,
        retry_interval_seconds=1,
        fence_margin_seconds=2,
        takeover_delay_seconds=0,
        connect_timeout_seconds=3,
        postgres_dsn_file=str(dsn_file),
        postgres_schema="public",
        postgres_table=table,
        auto_create=True,
    )
    first = PostgresLeaseBackend(config)
    second = PostgresLeaseBackend(config)
    first.initialize()
    try:
        epoch_one = first.acquire("node-a:session-a")
        assert epoch_one is not None
        assert second.acquire("node-b:session-b") is None
        renewed = first.renew("node-a:session-a", epoch_one.fencing_token)
        assert renewed is not None
        assert first.release("node-a:session-a", epoch_one.fencing_token)

        epoch_two = second.acquire("node-b:session-b")
        assert epoch_two is not None
        assert epoch_two.fencing_token > epoch_one.fencing_token
        assert second.block_takeover("node-b:session-b", epoch_two.fencing_token, "fence failed")

        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("UPDATE {} SET lease_until = clock_timestamp() - interval '1 second'").format(
                        sql.Identifier("public", table)
                    )
                )
        assert first.acquire("node-a:session-c") is None
        assert second.unblock(epoch_two.fencing_token)
        epoch_three = first.acquire("node-a:session-c")
        assert epoch_three is not None
        assert epoch_three.fencing_token > epoch_two.fencing_token
    finally:
        with psycopg.connect(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier("public", table)))
                cursor.execute(
                    sql.SQL("DROP TABLE IF EXISTS {}").format(
                        sql.Identifier("public", f"{table}_epochs")
                    )
                )
