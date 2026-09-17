"""Build or refresh the local vector index from approved Obsidian Markdown notes."""

from knowledge import load_chunks
from rag import build_index, index_path
from postgres_store import is_enabled


def main() -> None:
    # 先读取通过状态、部门等筛选的知识 Chunk，再为每段生成向量并写入本地 rag_index.json。
    # 修改知识正文、Chunk 规则或 Embedding 模型后，需要重新运行这个脚本。
    chunks = load_chunks()
    count = build_index(chunks)
    print(f"向量索引建立成功：{count} 个文本段落")
    if is_enabled():
        print("索引位置：PostgreSQL + pgvector（Docker 卷 postgres_data）")
    else:
        print(f"索引文件：{index_path().resolve()}")


if __name__ == "__main__":
    # 只在命令行直接运行时建索引，import 本文件不会意外调用 Embedding API。
    main()
