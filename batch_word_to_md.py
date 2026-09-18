"""Batch-convert Word files by reusing the project's MarkItDown command."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


WORD_SUFFIXES = {".doc", ".docx"}


def find_converter(explicit: str | None) -> str:
    """Find MarkItDown in the project virtual environment or PATH."""
    candidates = [
        Path(explicit) if explicit else None,
        Path(__file__).parent / ".venv" / "Scripts" / "markitdown.exe",
        Path(__file__).parent / ".venv" / "bin" / "markitdown",
    ]
    for candidate in candidates:
        if candidate and candidate.exists():
            return str(candidate)
    from_path = shutil.which("markitdown")
    if from_path:
        return from_path
    raise FileNotFoundError(
        "找不到 MarkItDown。请先在项目虚拟环境安装依赖，或使用 --converter 指定可执行文件。"
    )


def convert_folder(
    input_dir: Path,
    output_dir: Path,
    converter: str,
    *,
    recursive: bool = True,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Convert Word files and return (converted, skipped, failed)."""
    pattern = "**/*" if recursive else "*"
    failed_dir = output_dir / "转换失败"
    files = sorted(
        path
        for path in input_dir.glob(pattern)
        if path.is_file()
        and not path.is_relative_to(failed_dir)
        and path.suffix.lower() in WORD_SUFFIXES
    )
    converted = skipped = failed = 0

    for source in files:
        relative = source.relative_to(input_dir).with_suffix(".md")
        target = output_dir / relative
        if not force and target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
            print(f"跳过（未变化）：{source}")
            skipped += 1
            continue
        print(f"转换：{source} -> {target}")
        if dry_run:
            converted += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [converter, str(source), "-o", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            converted += 1
            continue
        failed += 1
        failed_target = failed_dir / source.relative_to(input_dir)
        try:
            failed_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, failed_target)
            print(f"已复制失败文件：{failed_target}")
        except OSError as copy_error:
            print(f"复制失败文件时出错：{copy_error}", file=sys.stderr)
        print(f"失败：{source}\n{result.stderr.strip() or result.stdout.strip()}", file=sys.stderr)

    return converted, skipped, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="批量把文件夹中的 Word 文件转换成 Markdown")
    parser.add_argument("input_dir", type=Path, help="包含 .doc/.docx 文件的文件夹")
    parser.add_argument("-o", "--output-dir", type=Path, help="输出 Markdown 文件夹，默认是输入文件夹下的 markdown")
    parser.add_argument("--converter", help="指定 MarkItDown 可执行文件路径")
    parser.add_argument("--no-recursive", action="store_true", help="只处理当前文件夹，不处理子文件夹")
    parser.add_argument("--force", action="store_true", help="强制重新转换已有 Markdown")
    parser.add_argument("--dry-run", action="store_true", help="只显示将转换的文件，不真正转换")
    args = parser.parse_args()

    input_dir = args.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        parser.error(f"输入文件夹不存在：{input_dir}")
    output_dir = (args.output_dir or input_dir / "markdown").expanduser().resolve()
    try:
        converter = find_converter(args.converter)
        converted, skipped, failed = convert_folder(
            input_dir,
            output_dir,
            converter,
            recursive=not args.no_recursive,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(f"完成：转换 {converted} 个，跳过 {skipped} 个，失败 {failed} 个。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
