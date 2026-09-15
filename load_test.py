"""Concurrent load test for the knowledge-answering pipeline.

This script calls ``main.answer_question`` directly. It exercises retrieval,
embedding and the LLM, but deliberately does not send any DingTalk messages.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# 压测直接调用业务函数：会走检索、Embedding 和大模型，但不会向钉钉群发送测试消息。
from main import NO_ANSWER, answer_question


# 未传入自定义问题时使用的默认题目，便于快速确认链路是否可用。
DEFAULT_QUESTIONS = [
    "财法通是什么？",
    "财法通适合哪些企业使用？",
    "遇到纠纷时才需要使用财法通吗？",
]


def parse_args() -> argparse.Namespace:
    # 定义命令行参数。requests 是总次数，concurrency 是同时并发多少个请求。
    parser = argparse.ArgumentParser(description="压测知识库问答链路，不发送钉钉消息。")
    parser.add_argument("--requests", type=int, default=1, help="总共执行多少次提问，默认 1")
    parser.add_argument("--concurrency", type=int, default=1, help="同时执行多少个提问，默认 1")
    parser.add_argument("--question", help="指定一个问题，所有请求都使用它")
    parser.add_argument("--questions-file", type=Path, help="问题文本文件：每行一个问题")
    return parser.parse_args()


def load_questions(args: argparse.Namespace) -> list[str]:
    # 问题来源有三个优先级：单个命令行问题、文本文件中的多条问题、内置默认问题。
    if args.question:
        return [args.question.strip()]
    if args.questions_file:
        if not args.questions_file.is_file():
            raise ValueError(f"找不到问题文件：{args.questions_file}")
        # 文本文件一行一个问题；空行和以 # 开头的说明行不会参与真实调用。
        questions = [line.strip() for line in args.questions_file.read_text(encoding="utf-8").splitlines()]
        questions = [question for question in questions if question and not question.startswith("#")]
        if not questions:
            raise ValueError("问题文件里没有可用问题。")
        return questions
    return DEFAULT_QUESTIONS


def answer_once(request_number: int, question: str) -> dict[str, object]:
    # 一个线程执行一次完整问答，并把结果归类为正常、无答案、繁忙或异常。
    started = time.perf_counter()
    # 即使某一次模型调用失败，也把错误记为一条测试结果，让其余并发请求继续完成。
    try:
        answer = answer_question(question)
        if answer == NO_ANSWER:
            status = "no_answer"
        elif "当前繁忙" in answer:
            status = "busy"
        else:
            status = "ok"
        return {
            "request": request_number,
            "question": question,
            "status": status,
            "duration_seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "request": request_number,
            "question": question,
            "status": "error",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }


def percentile(values: list[float], percentage: float) -> float:
    # 计算 P50、P95 等分位数。P95 表示“95% 的请求都不慢于这个时间”。
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentage)))
    return ordered[position]


def main() -> int:
    # 主流程：校验参数 → 并发提问 → 汇总速度和状态 → 写入 JSON 结果文件。
    args = parse_args()
    if args.requests < 1 or args.concurrency < 1:
        print("--requests 和 --concurrency 都必须大于等于 1。", file=sys.stderr)
        return 2

    # 先准备循环使用的问题列表，再提示使用者本次会真实消耗模型 API 配额。
    questions = load_questions(args)
    print(f"开始压测：总请求 {args.requests}，同时请求 {args.concurrency}。")
    print("注意：这会真实调用 Embedding 和大语言模型 API，但不会向钉钉发送消息。")

    wall_started = time.perf_counter()
    results: list[dict[str, object]] = []
    # 线程池控制同时执行的请求数；as_completed 会在每条请求结束时立即打印进度。
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(answer_once, number, questions[(number - 1) % len(questions)])
            for number in range(1, args.requests + 1)
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"[{result['request']}/{args.requests}] {result['status']} {result['duration_seconds']} 秒")

    # 全部请求结束后，按请求编号恢复顺序，并统计耗时、吞吐量和各种状态的数量。
    wall_seconds = time.perf_counter() - wall_started
    results.sort(key=lambda item: int(item["request"]))
    durations = [float(item["duration_seconds"]) for item in results]
    counts: dict[str, int] = {}
    for result in results:
        status = str(result["status"])
        counts[status] = counts.get(status, 0) + 1

    # 原始结果和汇总指标一起写进文件，之后可比较不同模型、并发数或优化前后的差异。
    summary = {
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "requests": args.requests,
        "concurrency": args.concurrency,
        "wall_seconds": round(wall_seconds, 3),
        "throughput_per_second": round(args.requests / wall_seconds, 3) if wall_seconds else 0,
        "status_counts": counts,
        "latency_seconds": {
            "average": round(statistics.mean(durations), 3),
            "p50": round(percentile(durations, 0.50), 3),
            "p95": round(percentile(durations, 0.95), 3),
            "slowest": round(max(durations), 3),
        },
        "results": results,
    }
    output_dir = Path("data/load-tests")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"load-test-{datetime.now():%Y%m%d-%H%M%S}.json"
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # 控制台只打印最重要的性能数字，完整每条结果仍保留在刚写入的 JSON 文件中。
    latency = summary["latency_seconds"]
    print("\n压测完成")
    print(f"成功：{counts.get('ok', 0)}；未找到答案：{counts.get('no_answer', 0)}；繁忙：{counts.get('busy', 0)}；错误：{counts.get('error', 0)}")
    print(f"总耗时：{summary['wall_seconds']} 秒；平均：{latency['average']} 秒；P95：{latency['p95']} 秒；最慢：{latency['slowest']} 秒")
    print(f"结果文件：{output_path.resolve()}")
    return 0


if __name__ == "__main__":
    # 将 main 的返回码交给 Windows，参数错误时命令行能正确显示失败状态。
    raise SystemExit(main())
