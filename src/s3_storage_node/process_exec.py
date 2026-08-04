from __future__ import annotations

import argparse
import os


class ProcessExecError(RuntimeError):
    pass


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="s3-storage-node-process-exec")
    command.add_argument("--cwd", required=True)
    command.add_argument("command", nargs=argparse.REMAINDER)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        raise ProcessExecError("process command is required")
    os.chdir(args.cwd)
    os.execvpe(command[0], command, os.environ.copy())
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
