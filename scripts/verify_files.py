#!/usr/bin/env python3
"""内容级校验下载文件（本 skill 的核心工具，必做步骤）。

用法:
    python verify_files.py <文件或目录> [更多...] [--registry 题录.json] [--json 输出.json]

判定逻辑（不要简化为只看魔数）:
    PDF   : %PDF- 魔数；或 base64 文本解码后为 %PDF-
    MP4   : 标准 box 头 00 00 00 18 + 'ftyp'，并校验 box 链
    ZIP   : PK\\x03\\x04（Office 文件）
    其他  : 报告真实头部字节，人工判断

若提供 --registry（元素含 doi/title 的 JSON 数组），会额外做内容比对：
    - 用 pypdf 提取首页文本
    - 标题实词命中率
    - 正文中是否出现该 DOI
这一步能识破「格式正确但内容错误」的下载（曾遇到拿到另一篇论文）。
"""
import argparse
import base64
import json
import os
import re
import struct
import sys


# ---------- 类型判定 ----------

def sniff(buf: bytes):
    """返回 (类型, 说明)"""
    if buf[:5] == b"%PDF-":
        return "pdf", "正常 PDF 魔数"
    if len(buf) > 8 and buf[4:8] == b"ftyp":
        size = struct.unpack(">I", buf[:4])[0]
        return ("mp4", f"标准 MP4 box 头 (size={size})") if size == 24 else \
               ("mp4", f"MP4 ftyp 但长度字段异常 (size={size})，可能需修复")
    if buf[4:8] == b"ftyp":
        return "mp4", "疑似 MP4 但缺 4 字节（前缀被替换），需 repair_files.py"
    if buf[:4] == b"PK\x03\x04":
        return "zip", "ZIP/Office"
    if buf[:2] == b"\xff\xd8":
        return "jpeg", ""
    if buf[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", ""
    if buf[:4] == b"%!PS":
        return "postscript", ""
    # base64 文本？
    head = re.sub(rb"\s+", b"", buf[:8192])
    if head and re.fullmatch(rb"[A-Za-z0-9+/=]+", head[:200] or b"x"):
        try:
            dec = base64.b64decode(head + b"=" * (-len(head) % 4))
            inner, _ = sniff(dec) if dec[:4] != b"PK\x03\x04" else ("zip", "")
            if inner not in ("unknown",):
                return "base64", f"base64 文本，解码后为 {inner}（需 repair_files.py）"
            if b"<!DOCTYPE html" in dec or b"<html" in dec[:200].lower():
                return "html-b64", "base64 编码的 HTML（很可能是 Cloudflare 挑战页）"
        except Exception:
            pass
    low = buf[:400].lower()
    if b"<!doctype html" in low or b"<html" in low:
        if b"just a moment" in buf[:8192].lower():
            return "html", "Cloudflare 挑战页"
        return "html", "HTML 页面（非文件）"
    return "unknown", f"头部字节 {buf[:12].hex(' ')}"


def mp4_boxes(buf: bytes, limit=10):
    off, out = 0, []
    while off + 8 <= len(buf) and len(out) < limit:
        size = struct.unpack(">I", buf[off:off + 4])[0]
        typ = buf[off + 4:off + 8].decode("latin1", "replace")
        out.append((typ, size))
        if size == 1 and off + 16 <= len(buf):          # 64 位扩展长度
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            out[-1] = (typ, size)
        if size < 8 or off + size > len(buf):
            out.append(("!!越界", off))
            break
        off += size
    return out


# ---------- 内容比对 ----------

def pdf_probe(path):
    """返回 (页数, 首页文本, DOI 列表)"""
    try:
        from pypdf import PdfReader
        import logging
        logging.getLogger("pypdf").setLevel(logging.ERROR)
        r = PdfReader(path)
        txt = ""
        for p in r.pages[:2]:
            try:
                txt += (p.extract_text() or "")
            except Exception:
                pass
            if len(txt) > 3000:
                break
        txt = re.sub(r"\s+", " ", txt)
        dois = [d.rstrip(".,;") for d in re.findall(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", txt)]
        return len(r.pages), txt, dois
    except ImportError:
        return None, "", []
    except Exception as e:
        return None, f"[pypdf 读取失败: {e}]", []


def match_registry(path, info, reg):
    """与题录比对：标题实词命中率 + DOI"""
    if not reg:
        return None
    pages, txt, dois = pdf_probe(path)
    best = None
    for item in reg:
        want = (item.get("doi") or "").lower().rstrip(".")
        words = [w for w in re.findall(r"[A-Za-z]{5,}", item.get("title") or "")][:5]
        hit = sum(1 for w in words if w.lower() in txt.lower())
        doi_hit = any(d.lower().rstrip(".") == want or d.lower().startswith(want[:16])
                      for d in dois) if want else False
        score = (doi_hit, hit)
        if best is None or score > best[0]:
            best = (score, item, hit, len(words), doi_hit)
    (_, item, hit, nwords, doi_hit) = best
    return {
        "matched_doi": item.get("doi"),
        "matched_title": (item.get("title") or "")[:80],
        "title_word_hits": f"{hit}/{nwords}",
        "doi_in_text": doi_hit,
        "verdict": "内容匹配" if (doi_hit or (nwords and hit >= max(2, nwords - 1))) else "内容可疑，需人工确认",
    }


# ---------- 主流程 ----------

def check_one(path, reg, min_pdf=8192):
    buf = open(path, "rb").read()
    kind, note = sniff(buf)
    rec = {
        "file": os.path.abspath(path),
        "name": os.path.basename(path),
        "bytes": len(buf),
        "kind": kind,
        "note": note,
        "ok": False,
    }
    if kind == "pdf":
        rec["ok"] = len(buf) >= min_pdf
        if not rec["ok"]:
            rec["warning"] = f"小于 {min_pdf} 字节，可能是残缺文件"
        pages, txt, dois = pdf_probe(path)
        rec["pages"] = pages
        rec["doi_in_text"] = dois[:2]
        rec["head"] = txt[:180]
    elif kind == "mp4":
        rec["boxes"] = mp4_boxes(buf)
        types = [b[0] for b in rec["boxes"]]
        rec["ok"] = "ftyp" in types and "moov" in types
    elif kind == "zip":
        rec["ok"] = len(buf) > 4096
    elif kind in ("jpeg", "png", "postscript"):
        rec["ok"] = len(buf) > 1024
    else:
        rec["ok"] = False
        rec["head_hex"] = buf[:32].hex(" ")
        rec["head_ascii"] = "".join(chr(c) if 32 <= c < 127 else "." for c in buf[:80])

    if reg and kind == "pdf" and rec["ok"]:
        rec["registry_match"] = match_registry(path, rec, reg)
        if rec["registry_match"] and rec["registry_match"]["verdict"] != "内容匹配":
            rec["ok"] = False
    if kind == "base64":
        rec["hint"] = "运行 repair_files.py 还原"
    return rec


def main():
    ap = argparse.ArgumentParser(description="下载文件内容级校验")
    ap.add_argument("paths", nargs="+", help="文件或目录")
    ap.add_argument("--registry", help="题录 JSON（数组，含 doi/title）")
    ap.add_argument("--json", dest="json_out", help="把结果写入 JSON")
    ap.add_argument("--min-pdf", type=int, default=8192, help="PDF 最小字节数阈值")
    a = ap.parse_args()

    reg = None
    if a.registry:
        reg = json.load(open(a.registry, encoding="utf-8"))
        if isinstance(reg, dict):
            reg = reg.get("items") or list(reg.values())

    targets = []
    for p in a.paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    # 跳过本 skill 自己产生的报告与临时文件，避免误报 FAIL
                    if f.startswith("_") or f.endswith(("_report.json", ".log")):
                        continue
                    if os.path.splitext(f)[1].lower() in (".json", ".txt", ".csv", ".md", ".mjs", ".py"):
                        continue
                    targets.append(os.path.join(root, f))
        else:
            targets.append(p)

    results = [check_one(t, reg, a.min_pdf) for t in targets if os.path.isfile(t)]

    ok = sum(1 for r in results if r["ok"])
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        extra = ""
        if r["kind"] == "pdf" and r.get("pages"):
            extra = f" {r['pages']:>3}页"
        elif r["kind"] == "mp4" and r.get("boxes"):
            extra = " " + "->".join(b[0] for b in r["boxes"][:4])
        else:
            extra = f" {r['note']}"
        print(f"[{mark}] {r['bytes']:>9,}B {r['kind']:<10}{extra}  {r['name']}")
        if not r["ok"]:
            for k in ("note", "head_hex", "head_ascii", "warning", "hint"):
                if r.get(k):
                    print(f"         {k}: {r[k]}")
            if r.get("registry_match"):
                print(f"         题录比对: {r['registry_match']['verdict']} "
                      f"(标题词 {r['registry_match']['title_word_hits']}, DOI {r['registry_match']['doi_in_text']})")
        elif r.get("registry_match"):
            m = r["registry_match"]
            print(f"         题录比对: {m['verdict']} (标题词 {m['title_word_hits']}, DOI {m['doi_in_text']})")

    print(f"\n合计 {len(results)} 个文件，通过 {ok}，失败 {len(results) - ok}")
    if a.json_out:
        json.dump(results, open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"结果已写入 {a.json_out}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
