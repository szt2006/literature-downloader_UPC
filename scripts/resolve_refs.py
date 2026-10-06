#!/usr/bin/env python3
"""题录解析：把用户给的引用/DOI/标题解析为规范 DOI + 权威元数据。

用法:
    python resolve_refs.py "10.1002/adma.201902765"
    python resolve_refs.py "A general-purpose neural network potential" --journal 0002-7863
    python resolve_refs.py --file refs.txt --out refs.json
    python resolve_refs.py "10.1016/j.cpc.2021.108171" --unpaywall

要点：
  * CrossRef 是权威来源。**规范 DOI 大小写很重要**——APS 的 PDF 路径大小写敏感
    （`10.1103/PhysRevB.99.014104` 有效，`physrevb` 全小写会 404）。
  * Unpaywall 需要**真实邮箱**，否则返回 422。用 --mailto 或环境变量 LIT_MAILTO。
  * 用户给的引用**可能是错的**。用 --journal <ISSN> 按卷期页交叉核查，并明确指出差异。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "literature-downloader/1.0 (research use)"
CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org"


def get_json(url, tries=3, timeout=40):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            body = ""
            try:
                body = e.read(300).decode("utf-8", "replace")
            except Exception:
                pass
            return {"_error": f"HTTP {e.code}", "_body": body}
        except Exception as e:
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            return {"_error": f"{type(e).__name__}: {e}"}
    return {"_error": "重试耗尽"}


def norm_doi(s):
    s = (s or "").strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    return s.strip()


def from_crossref(doi):
    j = get_json(f"{CROSSREF}/works/{urllib.parse.quote(doi)}")
    if "_error" in j:
        return {"_error": j["_error"], "_body": j.get("_body", "")}
    m = j["message"]
    links = [{"ct": l.get("content-type"), "url": l.get("URL")} for l in (m.get("link") or [])]
    alt = m.get("alternative-id") or []
    return {
        "doi": m.get("DOI"),                       # 规范大小写
        "title": (m.get("title") or [""])[0],
        "journal": (m.get("container-title") or [""])[0],
        "short_journal": (m.get("short-container-title") or [""])[0] if m.get("short-container-title") else "",
        "year": (m.get("issued", {}).get("date-parts", [[None]])[0][0]),
        "volume": m.get("volume", ""), "issue": m.get("issue", ""),
        "page": m.get("page", ""), "article_number": m.get("article-number", ""),
        "publisher": m.get("publisher", ""),
        "type": m.get("type", ""),
        "authors": [f"{a.get('given','')} {a.get('family','')}".strip() for a in (m.get("author") or [])],
        "cited_by": m.get("is-referenced-by-count", 0),
        "issn": m.get("ISSN") or [],
        "pii": next((a.get("id") for a in alt if a.get("type") == "pii"), ""),
        "links": links,
        "url": m.get("URL", ""),
        "license": [l.get("URL") for l in (m.get("license") or [])],
    }


def search_crossref(query, journal_issn=None, rows=5):
    p = {"query.bibliographic": query, "rows": str(rows),
         "select": "DOI,title,container-title,issued,volume,issue,page,author,publisher,type"}
    if journal_issn:
        url = f"{CROSSREF}/journals/{journal_issn}/works?{urllib.parse.urlencode(p)}"
    else:
        url = f"{CROSSREF}/works?{urllib.parse.urlencode(p)}"
    j = get_json(url)
    if "_error" in j:
        return {"_error": j["_error"]}
    out = []
    for m in j["message"]["items"]:
        out.append({
            "doi": m.get("DOI"),
            "title": (m.get("title") or [""])[0],
            "journal": (m.get("container-title") or [""])[0],
            "year": m.get("issued", {}).get("date-parts", [[None]])[0][0],
            "volume": m.get("volume", ""), "issue": m.get("issue", ""),
            "page": m.get("page", ""),
            "publisher": m.get("publisher", ""),
        })
    return {"candidates": out, "total": j["message"].get("total-results")}


def unpaywall(doi, mailto):
    if not mailto:
        return {"_error": "需要真实邮箱：Unpaywall 对 test@example.com 之类返回 422"}
    j = get_json(f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={urllib.parse.quote(mailto)}")
    if "_error" in j:
        return j
    locs = [{
        "host": l.get("host_type"), "version": l.get("version"),
        "pdf": l.get("url_for_pdf"), "landing": l.get("url"),
    } for l in (j.get("oa_locations") or [])]
    return {
        "is_oa": j.get("is_oa"), "oa_status": j.get("oa_status"),
        "journal": j.get("journal_name"), "publisher": j.get("publisher"),
        "best_pdf": (j.get("best_oa_location") or {}).get("url_for_pdf"),
        "locations": locs,
    }


def main():
    ap = argparse.ArgumentParser(description="引用/DOI -> 规范 DOI + 权威元数据")
    ap.add_argument("refs", nargs="*", help="DOI 或标题")
    ap.add_argument("--file", help="每行一个引用/DOI 的文件")
    ap.add_argument("--journal", help="按该 ISSN 限定检索（用于卷期核查）")
    ap.add_argument("--mailto", default=os.environ.get("LIT_MAILTO"),
                    help="真实邮箱（Unpaywall/CrossRef 礼貌池）；或用 LIT_MAILTO")
    ap.add_argument("--unpaywall", action="store_true", help="额外查 OA 副本位置")
    ap.add_argument("--out", help="结果 JSON 路径")
    a = ap.parse_args()

    refs = list(a.refs)
    if a.file:
        refs += [l.strip() for l in open(a.file, encoding="utf-8") if l.strip()]
    if not refs:
        ap.error("请给出引用/DOI，或用 --file")

    results = []
    for ref in refs:
        print("=" * 70)
        print(f"输入: {ref}")
        doi = norm_doi(ref)
        rec = {"input": ref}
        if re.match(r"^10\.\d{4,9}/", doi):
            meta = from_crossref(doi)
            if "_error" in meta:
                print(f"  CrossRef 查询失败: {meta['_error']} {meta.get('_body','')[:120]}")
                rec["error"] = meta["_error"]
            else:
                rec.update(meta)
                if meta["doi"] != doi:
                    print(f"  ⚠ DOI 规范化为: {meta['doi']}（请用此大小写，APS 等站点大小写敏感）")
                print(f"  标题  : {meta['title']}")
                print(f"  来源  : {meta['journal']} {meta['year']}; {meta['volume']}"
                      f"({meta['issue']}): {meta['page'] or meta['article_number']}")
                print(f"  出版商: {meta['publisher']}  被引: {meta['cited_by']}")
                if meta["pii"]:
                    print(f"  PII   : {meta['pii']}（Elsevier 直链用）")
                for l in meta["links"][:4]:
                    print(f"  链接  : [{l['ct']}] {l['url']}")
        else:
            print("  非 DOI，按标题/书目检索 CrossRef")
            s = search_crossref(ref, a.journal)
            if "_error" in s:
                print(f"  检索失败: {s['_error']}")
                rec["error"] = s["_error"]
            else:
                print(f"  命中 {s.get('total')} 条，前 {len(s['candidates'])} 条：")
                for c in s["candidates"]:
                    print(f"    - {c['doi']}  {c['year']}  {c['journal']}  {c['volume']}({c['issue']}):{c['page']}")
                    print(f"      {c['title'][:100]}")
                rec["candidates"] = s["candidates"]
                if a.journal and s["candidates"]:
                    print("  ⚠ 已按 ISSN 限定；请核对卷期页是否与用户给的引用一致"
                          "（用户引用可能有误，需明确指出）")

        if a.unpaywall and rec.get("doi"):
            u = unpaywall(rec["doi"], a.mailto)
            rec["unpaywall"] = u
            if "_error" not in u:
                print(f"  OA    : {u['is_oa']} ({u['oa_status']})  最佳 PDF: {u.get('best_pdf')}")
                for l in u["locations"][:5]:
                    print(f"    [{l['host']}/{l['version']}] {l['pdf'] or l['landing']}")
            else:
                print(f"  OA 查询: {u['_error']}")
        results.append(rec)
        time.sleep(0.8)

    if a.out:
        json.dump(results, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n已写入 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
