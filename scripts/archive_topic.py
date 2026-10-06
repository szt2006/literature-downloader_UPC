#!/usr/bin/env python3
"""按主题归档下载成果到标准目录结构。

**铁律：每次文献任务相互独立。** 不要跨主题合并到同一个「项目」目录，
也不要在说明文档里编造任务之间的关联叙事。

用法:
    # 从 plan.json 归档（推荐，可复核）
    python archive_topic.py --plan plan.json --root D:\\refs

    # 交互式快速归档：扫描一个下载目录，按类型分组
    python archive_topic.py --scan <下载目录> --topic "钙钛矿太阳能电池" --root D:\\refs

    # 不指定 --root 时归档到当前目录下的 papers/

plan.json 格式:
{
  "topic": "钙钛矿太阳能电池",
  "date": "20260101",
  "files": [
    {"src": "D:\\\\dl\\\\a.pdf", "dst": "02_来源文献/2019_Nature/正文_xxx.pdf", "role": "正文"},
    {"src": "D:\\\\dl\\\\si1.pdf", "dst": "02_来源文献/2019_Nature/SI-1_xxx.pdf", "role": "SI"}
  ],
  "notes": "可选：写入 00_说明.md 的正文"
}

产出结构:
    <root>/<topic>_<date>/
      ├── 00_<topic>说明.md
      ├── 01_交付成果/
      ├── 02_来源文献/
      ├── 03_补充材料/
      └── 04_工具链/
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import date


def sniff(path):
    with open(path, "rb") as f:
        b = f.read(16)
    if b[:5] == b"%PDF-":
        return "PDF"
    if len(b) > 8 and b[4:8] == b"ftyp":
        return "MP4"
    if b[:4] == b"PK\x03\x04":
        return "ZIP/Office"
    return "未知"


def validate(path, min_pdf=8192):
    """归档前必须校验，不要归档 HTML 存根/Cloudflare 页面"""
    kind = sniff(path)
    size = os.path.getsize(path)
    if kind == "PDF":
        return (size >= min_pdf), f"PDF {size:,}B" + ("" if size >= min_pdf else "（过小，疑似残缺）")
    if kind == "MP4":
        return size > 10000, f"MP4 {size:,}B"
    if kind == "ZIP/Office":
        return size > 4096, f"Office {size:,}B"
    head = open(path, "rb").read(200)
    if b"<!DOCTYPE html" in head or b"<html" in head.lower():
        return False, "HTML 页面（非文件，很可能是反爬页）"
    return False, f"未知类型 head={head[:12].hex(' ')}"


SUBDIR_OF = {
    "正文": "02_来源文献",
    "SI": "03_补充材料",
    "补充材料": "03_补充材料",
    "交付": "01_交付成果",
    "工具": "04_工具链",
}


def do_plan(plan, root, dry_run=False):
    topic = plan["topic"]
    d = plan.get("date") or date.today().strftime("%Y%m%d")
    base = os.path.join(root, f"{topic}_{d}")
    print(f"归档根: {base}")
    if not dry_run:
        os.makedirs(base, exist_ok=True)

    ok = skip = 0
    for f in plan["files"]:
        src, dst = f["src"], f["dst"]
        if not os.path.isfile(src):
            print(f"  [缺失] {src}")
            skip += 1
            continue
        good, why = validate(src)
        if not good:
            print(f"  [拒绝] {why}  {os.path.basename(src)}")
            skip += 1
            continue
        target = os.path.join(base, dst)
        if not dry_run:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
        print(f"  [归档] {why:<22} -> {dst}")
        ok += 1

    for sub in ("01_交付成果", "02_来源文献", "03_补充材料", "04_工具链"):
        if not dry_run:
            os.makedirs(os.path.join(base, sub), exist_ok=True)

    note = os.path.join(base, f"00_{topic}说明.md")
    if not dry_run and not os.path.exists(note):
        with open(note, "w", encoding="utf-8") as fp:
            fp.write(f"# {topic} —— 文献获取说明\n\n")
            fp.write(f"- 归档日期：{d}\n")
            fp.write(f"- 来源：校园网图书馆订阅 / 开放获取\n\n")
            if plan.get("notes"):
                fp.write(plan["notes"].rstrip() + "\n")
            else:
                fp.write("<!-- 待填：渠道结论、关键原文摘录、未取得项及手动获取路径 -->\n")
        print(f"  已创建说明文件: 00_{topic}说明.md")

    print(f"\n归档 {ok} 个文件，跳过 {skip} 个" + ("（dry-run）" if dry_run else ""))
    return 0


def do_scan(scan_dir, topic, root, dry_run=False):
    """扫描目录，按序数自动命名并分组"""
    files = []
    for r, _, fs in os.walk(scan_dir):
        for f in sorted(fs):
            if f.startswith("_") or f.endswith((".json", ".log", ".md", ".mjs", ".py")):
                continue
            files.append(os.path.join(r, f))
    if not files:
        print("未发现可归档文件")
        return 1
    print(f"发现 {len(files)} 个文件：")
    plan_files = []
    for i, p in enumerate(files, 1):
        good, why = validate(p)
        kind = "正文" if (i == 1 or kind_is_main(p)) else "SI"
        name = os.path.basename(p)
        dst = f"{SUBDIR_OF.get(kind,'03_补充材料')}/{name}"
        print(f"  {'[ok]' if good else '[!!]'} {why:<22} -> {dst}")
        if good:
            plan_files.append({"src": p, "dst": dst, "role": kind})
    return do_plan({"topic": topic, "files": plan_files}, root, dry_run)


def kind_is_main(p):
    n = os.path.basename(p).lower()
    return bool(re.search(r"正文|main|article|paper", n))


def main():
    ap = argparse.ArgumentParser(description="按主题归档（主题之间相互独立）")
    ap.add_argument("--plan", help="plan.json 路径")
    ap.add_argument("--scan", help="扫描该目录并自动分组")
    ap.add_argument("--topic", help="主题名（用于目录命名）")
    ap.add_argument("--root", default=os.path.join(os.getcwd(), "papers"),
                    help="归档根目录（默认：当前目录下的 papers/）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.plan:
        plan = json.load(open(a.plan, encoding="utf-8"))
        if not plan.get("topic"):
            ap.error("plan.json 缺少 topic")
        return do_plan(plan, a.root, a.dry_run)
    if a.scan:
        if not a.topic:
            ap.error("--scan 需要同时给出 --topic")
        return do_scan(a.scan, a.topic, a.root, a.dry_run)
    ap.error("请给出 --plan 或 --scan")


if __name__ == "__main__":
    sys.exit(main())
