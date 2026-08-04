from __future__ import annotations

import sys

from s3_storage_node.generation_guardian import namespace_cwd_command
from s3_storage_node.process_exec import parser


def test_namespace_cwd_wrapper_preserves_arguments() -> None:
    command = ["/usr/local/bin/weed", "filer", "-port=8888", "value with spaces"]

    wrapped = namespace_cwd_command(command, "/run/s3-storage-node/generated")

    assert wrapped == [
        sys.executable,
        "-m",
        "s3_storage_node.process_exec",
        "--cwd",
        "/run/s3-storage-node/generated",
        "--",
        *command,
    ]


def test_namespace_cwd_wrapper_is_noop_without_directory() -> None:
    command = ["/usr/local/bin/weed", "master"]

    assert namespace_cwd_command(command, None) == command
    assert namespace_cwd_command(command, "") == command


def test_process_exec_parser_keeps_command_arguments_verbatim() -> None:
    args = parser().parse_args(
        [
            "--cwd",
            "/run/s3-storage-node/generated",
            "--",
            "/usr/local/bin/weed",
            "filer",
            "-port=8888",
            "value with spaces",
        ]
    )

    assert args.cwd == "/run/s3-storage-node/generated"
    assert args.command == [
        "--",
        "/usr/local/bin/weed",
        "filer",
        "-port=8888",
        "value with spaces",
    ]
