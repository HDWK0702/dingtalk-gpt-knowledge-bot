"""Build or refresh the local vector index from approved Obsidian Markdown notes."""

from knowledge import load_chunks
from rag import build_index, index_path


def main() -> None:
    chunks = load_chunks()
    count = build_index(chunks)
    print(f"向量索引建立成功：{count} 个文本段落")
    print(f"索引文件：{index_path().resolve()}")


if __name__ == "__main__":
    main()
