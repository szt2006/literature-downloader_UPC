#!/usr/bin/env python3
"""还原下载文件的编码/头部问题（出版商 CDN 的常见坑）。

用法:
    python repair_files.py <文件或目录> [--dry-run] [--json 报告.json]

处理三类问题：
  1. base64 文本       —— ACS 部分 SI 返回的是 base64 文本，而非二进制
  2. 4 字节垃圾前缀     —— Wiley 视频 SI 落盘时头部多 4 字节，需重建 MP4 box 头
  3. 64 位 box 长度缺头 —— 前缀被替换成 0x18 单字节，需补回 00 00 00

判定一律基于真实字节，不做扩展名推断。
"""
import argparse
import base64
import glob
import json
import os
import re
import struct
import sys


def is_pdf(b):
    return b[:5] == b"%PDF-"


def is_mp4(b):
    return len(b) > 8 and b[4:8] == b"ftyp"


def boxes(b, limit=10):
    off, out = 0, []
    while off + 8 <= len(b) and len(out) < limit:
        size = struct.unpack(">I", b[off:off + 4])[0]
        typ = b[off + 4:off + 8].decode("latin1", "replace")
        if size == 1 and off + 16 <= len(b):          # 64 位扩展长度
            size = struct.unpack(">Q", b[off + 8:off + 16])[0]
        out.append((typ, size))
        if size < 8 or off + size > len(b):
            out.append(("!!越界", off))
            break
        off += size
    return out


def try_b64_decode(b):
    """第 5 字节起（或整体）为 base64 文本时解码"""
    for start in (4, 0):
        payload = b[start:]
        txt = re.sub(rb"\s+", b"", payload)
        if not txt or not re.fullmatch(rb"[A-Za-z0-9+/=]+", txt[:4000] or b"x"):
            continue
        try:
            dec = base64.b64decode(txt + b"=" * (-len(txt) % 4))
        except Exception:
            continue
        if is_pdf(dec) or is_mp4(dec) or dec[:4] == b"PK\x03\x04":
            if is_mp4(dec) and dec[:4] != b"\x00\x00\x00\x18":
                dec = b"\x00\x00\x00" + dec            # 补回 box 长度字段
            return dec, f"base64 解码（偏移 {start}）"
        if b"<!DOCTYPE html" in dec[:400] or b"Just a moment" in dec[:4000]:
            return None, "内容是 Cloudflare/HTML 页面（非文件，需重新获取）"
    return None, None


def try_fix_mp4_head(b):
    """[数据] + 0x18 + 'ftyp...' -> 00 00 00 18 + 'ftyp...'"""
    idx = b.find(b"ftyp")
    if idx < 1:
        return None, None
    fixed = b"\x00\x00\x00" + b[idx - 1:]
    if is_mp4(fixed) and struct.unpack(">I", fixed[:4])[0] == 24:
        return fixed, "重建 MP4 box 头（补回 3 字节长度高位）"
    return None, None


def handle(path, dry_run=False):
    b = open(path, "rb").read()
    name = os.path.basename(path)

    if is_pdf(b):
        return {"file": name, "action": "无需处理", "kind": "pdf", "bytes": len(b)}
    if is_mp4(b) and struct.unpack(">I", b[:4])[0] == 24:
        return {"file": name, "action": "无需处理", "kind": "mp4", "bytes": len(b), "boxes": boxes(b)}
    if b[:4] == b"PK\x03\x04":
        return {"file": name, "action": "无需处理", "kind": "zip", "bytes": len(b)}

    dec, why = try_b64_decode(b)
    if dec is not None:
        if not dry_run:
            open(path, "wb").write(dec)
        kind = "pdf" if is_pdf(dec) else "mp4" if is_mp4(dec) else "zip"
        rec = {"file": name, "action": why, "kind": kind, "bytes_before": len(b), "bytes_after": len(dec)}
        if kind == "mp4":
            rec["boxes"] = boxes(dec)
        if not dry_run:
            rec["written"] = True
        return rec
    if why:
        return {"file": name, "action": "无法还原", "reason": why, "bytes": len(b)}

    fixed, why2 = try_fix_mp4_head(b)
    if fixed is not None:
        if not dry_run:
            open(path, "wb").write(fixed)
        return {"file": name, "action": why2, "kind": "mp4",
                "bytes_before": len(b), "bytes_after": len(fixed), "boxes": boxes(fixed),
                "written": not dry_run}

    head = b[:16].hex(" ")
    return {"file": name, "action": "无法识别", "bytes": len(b), "head_hex": head}


def main():
    ap = argparse.ArgumentParser(description="还原下载文件的编码/头部问题")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--dry-run", action="store_true", help="只诊断不写回")
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args()

    targets = []
    for p in a.paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                targets += [os.path.join(root, f) for f in sorted(files)]
        else:
            targets += glob.glob(p) or [p]

    results = [handle(t, a.dry_run) for t in targets if os.path.isfile(t)]

    fixed = 0
    for r in results:
        act = r["action"]
        if act == "无需处理":
            print(f"  [跳过] {r['kind']:<4} {r['bytes']:>10,}B  {r['file']}")
        elif act == "无法识别":
            print(f"  [??]   {r['bytes']:>10,}B head={r.get('head_hex')}  {r['file']}")
        elif act == "无法还原":
            print(f"  [--]   {r['reason']}  {r['file']}")
        else:
            fixed += 1
            extra = f" boxes={'->'.join(b[0] for b in r.get('boxes', [])[:4])}" if r.get("boxes") else ""
            mark = "(dry-run)" if a.dry_run else "已写回"
            print(f"  [修]   {r['bytes_before']:>10,}B -> {r['bytes_after']:>10,}B  {r['file']}")
            print(f"         {act} {mark}{extra}")

    print(f"\n合计 {len(results)} 个文件，需修复 {fixed} 个" + ("（dry-run，未写回）" if a.dry_run else ""))
    if a.json_out:
        json.dump(results, open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"报告: {a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
