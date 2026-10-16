"""Export one Beijing calendar day of Q&A events to an Excel workbook."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import xlsxwriter

BEIJING = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent
HEADERS = ["提问时间（北京时间）", "提问人员", "员工 ID", "问题", "回答", "处理状态",
           "对话模式", "参考资料", "模型线路", "耗时（秒）", "员工反馈", "发送状态", "错误", "问题编号"]


def question_rows(text: str, day: date) -> tuple[list[list[Any]], bool]:
    questions: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    feedback: dict[str, str] = {}
    partial = False
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # 正在追加的最后一行可以下次重导；已落盘的损坏记录不能悄悄漏掉。
            if number == len(lines) and not text.endswith("\n"):
                partial = True
                continue
            raise ValueError(f"日志第 {number} 行不是有效 JSON，已取消导出") from None
        if not isinstance(event, dict):
            raise ValueError(f"日志第 {number} 行不是事件对象")
        kind = event.get("event")
        if kind == "feedback":
            feedback[str(event.get("question_id"))] = {
                "useful": "有用", "not_useful": "没用",
            }.get(event.get("feedback"), str(event.get("feedback", "")))
            continue
        if kind not in {"question_received", "question_answered", "question_error", "reply_sent"}:
            continue
        ids = [str(value) for value in (event.get("question_id"), event.get("trace_id")) if value]
        if kind == "question_received" and event.get("id"):
            ids.append(str(event["id"]))
        key = next((aliases[value] for value in ids if value in aliases), None)
        if kind == "reply_sent":
            if key:
                questions[key]["delivery"] = "已发送" if event.get("success") else "发送失败"
            continue
        if key is None:
            key = ids[0] if ids else str(event.get("id") or f"line-{number}")
            try:
                created = datetime.fromisoformat(event["created_at"])
                if created.tzinfo is None:
                    raise ValueError("missing timezone")
            except (KeyError, TypeError, ValueError):
                raise ValueError(f"日志第 {number} 行缺少有效的带时区时间") from None
            questions[key] = {"time": created.astimezone(BEIJING), "status": "待回答或处理中"}
        for value in ids:
            aliases[value] = key
        question = questions[key]
        if kind == "question_received":
            question["question_id"] = event.get("id", key)
        question.update({name: value for name, value in event.items() if value is not None})
        if kind == "question_answered":
            question["status"] = "回答异常" if event.get("error") and not event.get("llm_success") else "已回答"
        elif kind == "question_error":
            question["status"] = "处理失败"

    rows = []
    for key, item in sorted(questions.items(), key=lambda pair: pair[1]["time"]):
        if item["time"].date() != day:
            continue
        question_id = str(item.get("question_id") or key)
        sources = "\n".join(str(source.get("title", "")) for source in item.get("sources", [])
                            if isinstance(source, dict))
        rows.append([
            item["time"].replace(tzinfo=None), item.get("sender_name") or item.get("sender_id", "未知"),
            str(item.get("sender_id", "")), item.get("question", ""), item.get("answer", ""),
            item["status"], "普通 AI 对话" if item.get("mode") == "chat" else "知识库问答",
            sources, item.get("llm_route", ""), item.get("elapsed_seconds"),
            feedback.get(question_id, ""), item.get("delivery", "未记录"), item.get("error", ""), question_id,
        ])
    return rows, partial


def export_workbook(text: str, day: date, output_dir: Path) -> tuple[Path, int]:
    rows, partial = question_rows(text, day)
    if len(rows) > 1_048_573:
        raise ValueError("记录数超过 Excel 单表上限，已取消导出")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"问答日志-{day.isoformat()}.xlsx"
    fd, name = tempfile.mkstemp(prefix=".qa-export-", suffix=".xlsx", dir=output_dir)
    os.close(fd)
    temporary = Path(name)
    try:
        with xlsxwriter.Workbook(str(temporary), {"strings_to_formulas": False, "strings_to_urls": False}) as book:
            sheet = book.add_worksheet("每日问答")
            header = book.add_format({"bold": True, "bg_color": "#17365D", "font_color": "white"})
            body = book.add_format({"text_wrap": True, "valign": "top"})
            time_format = book.add_format({"num_format": "yyyy-mm-dd hh:mm:ss", "valign": "top"})
            seconds = book.add_format({"num_format": "0.00", "valign": "top"})
            note = f"{day.isoformat()} 问答日志；导出时间（北京时间）：{datetime.now(BEIJING):%Y-%m-%d %H:%M:%S}；共 {len(rows)} 个问题"
            if partial:
                note += "；末行仍在写入，请稍后重新导出"
            sheet.merge_range("A1:N1", note, body)
            sheet.set_row(0, 32)
            sheet.write_row(1, 0, HEADERS, header)
            for row_number, values in enumerate(rows, 2):
                for column, value in enumerate(values):
                    if isinstance(value, str) and len(value) > 32767:
                        raise ValueError(f"第 {row_number + 1} 行第 {column + 1} 列超过 Excel 单元格长度上限，已取消导出")
                    style = time_format if column == 0 else seconds if column == 9 else body
                    sheet.write(row_number, column, value, style)
                sheet.set_row(row_number, 75)
            sheet.set_column("A:A", 23)
            sheet.set_column("B:C", 18)
            sheet.set_column("D:E", 65)
            sheet.set_column("F:M", 20)
            sheet.set_column("N:N", 35)
            sheet.freeze_panes(2, 0)
            sheet.autofilter(1, 0, len(rows) + 1, len(HEADERS) - 1)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target, len(rows)


def docker_log(project: str) -> str:
    result = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
         "--filter", "label=com.docker.compose.service=bot", "--format", "{{.ID}}"],
        capture_output=True, text=True, timeout=30,
    )
    containers = result.stdout.split()
    if result.returncode or len(containers) != 1:
        raise RuntimeError(f"无法找到 {project} 中唯一的运行中 bot 容器，请检查 Docker 和机器人状态")
    result = subprocess.run(
        ["docker", "exec", containers[0], "python", "-c",
         "import os,sys; from pathlib import Path; sys.stdout.buffer.write(Path(os.getenv('QA_LOG_PATH','data/qa_logs.jsonl')).read_bytes())"],
        capture_output=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError("读取机器人问答日志失败；确认容器存在且已生成 QA_LOG_PATH 文件")
    return result.stdout.decode("utf-8-sig")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=datetime.now(BEIJING).date(), help="YYYY-MM-DD，默认北京时间今天")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "qa_exports")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--log-path", type=Path, help="本地 JSONL 日志路径")
    source.add_argument("--docker-project", help="明确指定生产或测试 Compose project，读取其 bot 日志")
    args = parser.parse_args()
    try:
        text = docker_log(args.docker_project) if args.docker_project else args.log_path.read_text(encoding="utf-8-sig")
        path, count = export_workbook(text, args.date, args.output_dir)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, xlsxwriter.exceptions.XlsxWriterException) as exc:
        # 任务日志只保存状态，异常可能携带员工文本，不能打印异常内容。
        reason = str(exc) if isinstance(exc, (ValueError, RuntimeError)) and not isinstance(exc, UnicodeError) else type(exc).__name__
        print(f"导出失败（{reason}）：请检查日志源、Docker 状态、文件格式及目录权限，关闭已打开的同日期 Excel 后重试；已有文件保留。")
        return 1
    print(f"已导出 {count} 个问题：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
