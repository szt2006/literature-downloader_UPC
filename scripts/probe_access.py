#!/usr/bin/env python3
"""环境勘察：出口 IP、TLS 证书链、各出版商可达性。

用法:
    python probe_access.py                 # 全部检查
    python probe_access.py --cert wiley    # 只看证书
    python probe_access.py --json out.json
"""
import argparse
import json
import socket
import ssl
import sys
import urllib.request
import urllib.error

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 出口 IP 探测（多源，取第一个成功的；某些站点会限流或重置连接）
IP_ENDPOINTS = [
    "https://ipinfo.io/json",
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/all.json",
    "https://ipapi.co/json/",
]

# 出版商站点：用于判断可达性 + 反爬特征
PUBLISHERS = {
    "APS":          "https://journals.aps.org/",
    "Springer":     "https://link.springer.com/",
    "Nature":       "https://www.nature.com/",
    "IOP":          "https://iopscience.iop.org/",
    "Wiley(裸域)":   "https://onlinelibrary.wiley.com/",
    "Wiley(子域)":   "https://advanced.onlinelibrary.wiley.com/",
    "ACS":          "https://pubs.acs.org/",
    "AIP":          "https://pubs.aip.org/",
    "Elsevier":     "https://www.sciencedirect.com/",
    "RSC":          "https://pubs.rsc.org/",
    "arXiv":        "https://arxiv.org/",
    "OSTI":         "https://www.osti.gov/",
    "PubMedCentral":"https://pmc.ncbi.nlm.nih.gov/",
    "WebOfScience": "https://www.webofscience.com/",
    "CNKI":         "https://kns.cnki.net/",
    "CrossRef":     "https://api.crossref.org/works/10.1038/s41586-021-03819-2",
    "OpenAlex":     "https://api.openalex.org/works?per-page=1",
    "Unpaywall":    "https://api.unpaywall.org/v2/10.1038/s41586-021-03819-2?email=__MAILTO__",
}

CERT_HOSTS = {
    "wiley":            "onlinelibrary.wiley.com",
    "wiley-sub":        "advanced.onlinelibrary.wiley.com",
    "acs":              "pubs.acs.org",
    "aip":              "pubs.aip.org",
    "sciencedirect":    "www.sciencedirect.com",
    "aps":              "journals.aps.org",
    "cnki":             "kns.cnki.net",
    "wos":              "www.webofscience.com",
}


def http_probe(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            try:
                body = r.read(4096)
            except Exception:
                body = b""
            return {"status": r.status, "ct": r.headers.get("content-type", ""),
                    "bytes_head": len(body), "final": r.geturl(),
                    "sniff": sniff_antibot(body)}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(4096)
        except Exception:                      # 某些站点错误响应体读不到（连接被重置/超时）
            body = b""
        return {"status": e.code, "ct": e.headers.get("content-type", "") if e.headers else "",
                "bytes_head": len(body), "final": getattr(e, "url", url),
                "sniff": sniff_antibot(body)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def sniff_antibot(body: bytes):
    """识别反爬特征，帮助判断该走哪条链路"""
    low = body.lower()
    if b"just a moment" in low or b"cf-challenge" in low or b"challenges.cloudflare.com" in low:
        return "Cloudflare 挑战"
    if b"are you a robot" in low or b"kasada" in low or b"captcha" in low:
        return "Kasada/验证码"
    if b"<html" in low[:200]:
        return "HTML"
    return "非 HTML"


def cert_probe(host, timeout=15, port=443):
    out = {"host": host}
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                c = ss.getpeercert()
                out["trusted"] = True
                out["subject"] = dict(x[0] for x in c.get("subject", []))
                out["issuer"] = dict(x[0] for x in c.get("issuer", []))
                out["notAfter"] = c.get("notAfter")
                out["SAN"] = c.get("subjectAltName", [])[:6]
    except ssl.SSLCertVerificationError as e:
        out["trusted"] = False
        out["error"] = f"证书校验失败: {e.verify_message}"
        out["hint"] = "可能是 VPN 中间人重签，或站点边缘节点配置问题（可试该站的期刊子域名）"
    except Exception as e:
        out["trusted"] = False
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main():
    ap = argparse.ArgumentParser(description="文献下载环境勘察")
    ap.add_argument("--mailto", help="真实邮箱（用于 CrossRef/Unpaywall），也可用环境变量 LIT_MAILTO")
    ap.add_argument("--cert", help="只探测该主机的证书（键名见 CERT_HOSTS）")
    ap.add_argument("--json", dest="json_out")
    a = ap.parse_args()

    import os
    mailto = a.mailto or os.environ.get("LIT_MAILTO")
    result = {}

    if a.cert:
        hosts = [CERT_HOSTS.get(a.cert, a.cert)]
        for h in hosts:
            print(f"--- 证书: {h}")
            print(json.dumps(cert_probe(h), ensure_ascii=False, indent=1))
        return 0

    print("=" * 66)
    print("1) 出口 IP（判断是否在机构网段）")
    print("=" * 66)
    egress = []
    for u in IP_ENDPOINTS:
        r = http_probe(u)
        if "error" not in r:
            print(f"   {u} -> {r['status']}")
            egress.append(r)
        else:
            print(f"   {u} -> {r['error']}")
    result["egress"] = egress
    print("   判读要点：")
    print("     - IP 属于 CERNET / 教育网 / 校园网段 => 图书馆订阅大概率按机构 IP 生效")
    print("     - 务必与出版商页面回显的 clientIP 交叉核对，确认 VPN 真的承载了流量")
    print("       （曾遇 VPN 显示已连接，但 WoS 页面回显的 clientIP 是境外 AWS 出口）")
    print("     - 注意：本脚本用 urllib 探测，部分站点（如 ipinfo 无 Accept 头会 406）需带浏览器头")

    print()
    print("=" * 66)
    print("2) 出版商 / API 可达性与反爬特征")
    print("=" * 66)
    pubs = {}
    for name, url in PUBLISHERS.items():
        if "__MAILTO__" in url:
            if not mailto:
                print(f"   {name:<14} 跳过（未提供 --mailto / LIT_MAILTO）")
                pubs[name] = {"skipped": "需要真实邮箱"}
                continue
            url = url.replace("__MAILTO__", mailto)
        r = http_probe(url)
        pubs[name] = r
        if "error" in r:
            print(f"   {name:<14} ERR  {r['error'][:60]}")
        else:
            tag = {"全自动可用": "", "Cloudflare 挑战": "← 需浏览器会话",
                   "Kasada/验证码": "← 需人工介入", "HTML": "← 需浏览器会话"}
            note = tag.get(r["sniff"], "")
            print(f"   {name:<14} {r['status']:<4} {r['ct'][:28]:<30} {r['sniff']:<16}{note}")
    result["publishers"] = pubs

    print()
    print("=" * 66)
    print("3) TLS 证书链（查 VPN 是否中间人重签）")
    print("=" * 66)
    certs = {}
    for key, host in CERT_HOSTS.items():
        c = cert_probe(host)
        certs[key] = c
        if c.get("trusted"):
            print(f"   {key:<14} OK   issuer={c['issuer'].get('commonName') or c['issuer'].get('organizationName')}")
        else:
            print(f"   {key:<14} FAIL {c.get('error', '')[:64]}")
    result["certs"] = certs

    print()
    print("=" * 66)
    print("结论提示")
    print("=" * 66)
    print("   ⚠ 重要：本表只反映「根路径 + urllib」的可达性，**不等于全文 PDF 不可下载**。")
    print("     实测反例：APS 站点根路径返回 Cloudflare 403，但 /<jrn>/pdf/<DOI> 可直接下载全文。")
    print("     => 该表用于判断「需不需要浏览器会话」，全文可用性必须用具体 DOI 实测。")
    print()
    cf = [k for k, v in pubs.items() if v.get("sniff") == "Cloudflare 挑战"]
    ks = [k for k, v in pubs.items() if v.get("sniff") == "Kasada/验证码"]
    bad_cert = [k for k, v in certs.items() if not v.get("trusted")]
    if cf:
        print(f"   Cloudflare 挑战（脚本直连会被 403，需浏览器会话）: {', '.join(cf)}")
    if ks:
        print(f"   人机验证（需人工介入，**不要绕过**）: {', '.join(ks)}")
    if bad_cert:
        print(f"   证书异常: {', '.join(bad_cert)}")
        print("     先排查是否有 VPN 中间人 CA（如 Topsec_api_ca）；若无，多半是站点边缘节点问题，")
        print("     可试该站的期刊子域名（曾遇裸域名证书异常、子域名正常），也可能是间歇性，稍后重试。")
    if not mailto:
        print("   尚未提供邮箱：CrossRef/Unpaywall 需要真实邮箱，否则 Unpaywall 返回 422。**请先向用户索取。**")

    if a.json_out:
        json.dump(result, open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n结果已写入 {a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
