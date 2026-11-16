from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from robot_corpus.control_plane import Application, run, verify


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='robot_corpus', description='工业机器人具身数据采集编排中心')
    commands = root.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--fixture", required=True)
    execute.add_argument("--database", required=True)
    execute.add_argument("--output", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--database", required=True)
    check.add_argument("--output", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--database", required=True)
    migrate = commands.add_parser(
        "migrate-coverage",
        help="按 v2 覆盖口径复核已封存数据集，保留历史数字并输出来源会话与质量判断的差异")
    migrate.add_argument("--database", required=True)
    migrate.add_argument("--dataset", required=True)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "run":
        result = run(args.fixture, args.database, args.output)
    elif args.command == "verify":
        result = verify(args.database, args.output)
    elif args.command == "migrate-coverage":
        result = Application(args.database).migrate_dataset_coverage(args.dataset)
    else:
        result = Application(args.database).inspect()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
