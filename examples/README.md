# 示例

## items.json —— 给 `discover_links.mjs` 用

```bash
node scripts/discover_links.mjs --headed --items examples/items.json --out links.json
```

字段说明：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 标签，用于输出文件名与日志 |
| `doi` | 是 | 用于从页面 JS 数据构造 SI 下载地址 |
| `landing` | 否 | 落地页。**省略时脚本会按 DOI 自动猜**（Nature/APS/IOP 直连，其余走 `https://doi.org/<DOI>`） |

> 建议**不要**手写出版商裸域名。实测 `onlinelibrary.wiley.com` 会连接超时，
> 而走 `https://doi.org/<DOI>` 重定向后正常，并能拿到真实的期刊子域名。

输出 `links.json` 可直接喂给 `browser_fetch.mjs`：

```bash
node scripts/browser_fetch.mjs --headed --out downloads --targets links.json
```

---

## oa_items.json —— 给 `fetch_oa.mjs` 用

```bash
node scripts/fetch_oa.mjs --items examples/oa_items.json --out downloads
```

字段说明（除 `name` 外全部可选，填得越多候选越多）：

| 字段 | 说明 |
|---|---|
| `name` | 标签 |
| `doi` | 有它 + `LIT_MAILTO` 时，会自动用 Unpaywall 定位 OA 副本 |
| `arxivId` | arXiv 预印本 ID，如 `2304.09409` |
| `ostiId` | OSTI 存档 ID，如 `1994160` |
| `pmcId` | PMC ID，如 `PMC10724388` |
| `pdfUrl` | 已知的 PDF 直链，会最先尝试 |

**优先顺序**：指定直链 → Unpaywall 定位的出版商官方 OA → OSTI/PMC 官方存档 → arXiv 预印本。

---

## 完整流程示例

```bash
export LIT_MAILTO=you@example.edu

python scripts/probe_access.py                                    # 0. 先确认 VPN
python scripts/resolve_refs.py "10.1002/advs.202304179" --unpaywall --out refs.json
node scripts/discover_links.mjs --headed --items examples/items.json --out links.json
node scripts/browser_fetch.mjs --headed --out downloads --targets links.json
python scripts/verify_files.py downloads                          # 必做
python scripts/archive_topic.py --scan downloads --topic "示例主题"
```
