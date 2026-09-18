from pathlib import Path
from markitdown import MarkItDown

SRC = Path(r"E:\E:\投喂资料\掘金AI培训投喂资料\财税工商产品")   # ← 改成 Word 所在目录
DST = Path(r"E:\knowledge-group\Knowledge-group")                           # 输出到原地；想分开就改成 Path(r"E:\输出目录")

md = MarkItDown()                   # 只创建一次，复用
ok, fail = 0, []

for f in SRC.rglob("*.doc"):       # 递归找所有 .docx
    out = (DST / f.relative_to(SRC)).with_suffix(".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(md.convert(str(f)).text_content, encoding="utf-8")
        ok += 1
        print(f"✓ {f.name}")
    except Exception as e:
        fail.append((f.name, str(e)))
        print(f"✗ {f.name}: {e}")

print(f"\n完成：成功 {ok}，失败 {len(fail)}")
for name, err in fail:
    print(f"  - {name}: {err}")
