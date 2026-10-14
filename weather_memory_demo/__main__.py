from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _bootstrap_local_sdk() -> None:
    project_root = Path(__file__).resolve().parents[1]
    sdk_source = project_root / "sdk" / "python" / "src"
    if sdk_source.is_dir() and str(sdk_source) not in sys.path:
        sys.path.insert(0, str(sdk_source))


_bootstrap_local_sdk()

from .core import (  # noqa: E402
    DEFAULT_MEMORY_FILE,
    DEFAULT_SOCKET,
    PromptError,
    WeatherMemoryApplication,
    WeatherServiceError,
)
from .web import serve  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m weather_memory_demo",
        description="查询天气，同时展示 bianbu-agent-memoryd 召回的相关记忆和文件路径。",
    )
    parser.add_argument(
        "--socket",
        default=os.environ.get("BIANBU_AGENT_MEMORY_SOCKET", DEFAULT_SOCKET),
        help="bianbu-agent-memoryd UDS 路径",
    )
    parser.add_argument(
        "--memory-file",
        default=str(DEFAULT_MEMORY_FILE),
        help="用于演示文件来源证据的 JSON 记忆文件",
    )
    parser.add_argument("--memory-timeout", type=float, default=5.0)
    parser.add_argument("--weather-timeout", type=float, default=12.0)
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="启动时不写入随项目提供的两条示例天气偏好",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="执行一次 CLI 查询")
    ask.add_argument("prompt", nargs="?", help="例如：帮我查询珠海明天的天气？")
    ask.add_argument("--json", action="store_true", help="输出完整 JSON")
    ask.add_argument("--session-id", default=None)

    web = commands.add_parser("web", help="启动网页查询界面")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8080)

    seed = commands.add_parser("seed", help="只写入示例天气记忆")
    seed.add_argument("--json", action="store_true", help="输出完整 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        application = WeatherMemoryApplication.connect(
            args.socket,
            timeout=args.memory_timeout,
            weather_timeout=args.weather_timeout,
            memory_file=args.memory_file,
        )
    except Exception as error:
        print(
            f"无法连接记忆服务 {args.socket}：{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2

    try:
        seeded: list[dict[str, Any]] = []
        if not args.no_seed or args.command == "seed":
            seeded = application.seed_demo_memories()
        if args.command == "seed":
            if args.json:
                print(json.dumps({"seeded": seeded}, ensure_ascii=False, indent=2))
            else:
                print(f"已准备 {len(seeded)} 条示例天气记忆：")
                for item in seeded:
                    print(f"- {item['memory_id']}  {item['file_path']}")
            return 0
        if args.command == "ask":
            prompt = args.prompt or input("请输入天气查询提示词：").strip()
            result = application.ask(prompt, session_id=args.session_id)
            if args.json:
                print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            else:
                _print_human_result(result)
            return 0
        if args.command == "web":
            serve(application, host=args.host, port=args.port)
            return 0
        raise AssertionError(f"unknown command: {args.command}")
    except (PromptError, WeatherServiceError, ValueError) as error:
        print(f"执行失败：{error}", file=sys.stderr)
        return 1
    finally:
        application.close()


def _print_human_result(result: Any) -> None:
    print("\n天气回复")
    print(result.answer)
    print(f"\n相关记忆（{len(result.memories)} 条）")
    if not result.memories:
        print("- 未召回到相关长期记忆")
    for index, memory in enumerate(result.memories, 1):
        print(f"\n[{index}] {memory.summary or memory.kind}")
        print(f"    内容: {memory.content}")
        print(f"    memory_id: {memory.memory_id}")
        print(f"    类型/版本: {memory.kind} / revision {memory.revision}")
        print(f"    来源: {memory.source}")
        print(f"    标签: {', '.join(memory.tags)}")
        if memory.file_paths:
            print("    文件路径:")
            for path in memory.file_paths:
                print(f"      - {path}")
        else:
            print("    文件路径: 无文件路径证据")
        if memory.evidence:
            print("    证据引用:")
            for evidence in memory.evidence:
                suffix = (
                    f" sha256={evidence['evidence_hash']}"
                    if evidence.get("evidence_hash")
                    else ""
                )
                print(
                    f"      - {evidence['evidence_type']}:{evidence['evidence_id']}{suffix}"
                )
    print(f"\nSession: {result.session_id}")
    if result.memory_applied:
        print("已应用记忆: " + "；".join(result.memory_applied))
    for warning in result.warnings:
        print(f"警告: {warning}")


if __name__ == "__main__":
    raise SystemExit(main())
