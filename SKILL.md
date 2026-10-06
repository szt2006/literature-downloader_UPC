---
name: literature-downloader
description: >-
  文献检索与全文下载（含 Supporting Information）。当用户要求"下载论文/文献""找某篇文献的SI/补充材料"、
  给出 DOI / 标题 / 期刊卷期页、要求从 Web of Science / Wiley / ACS / Elsevier / Springer / APS / AIP / RSC /
  CNKI / 知网 / arXiv 等获取全文，或要在校园网+图书馆订阅环境下批量抓取 PDF 时使用。
  覆盖：题录解析与 DOI 规范化、开放获取链路、出版商反爬（Cloudflare / Kasada）应对、
  Supporting Information 定位与下载、下载文件的内容级校验（防止拿到错误论文或 HTML 页面）、
  题录表与综述文档生成、以及按主题独立归档。
  Use for retrieving papers and their supporting information: resolving DOIs and citations,
  downloading publisher PDFs and SI under institutional access, bypassing bot protection with a
  real browser session, verifying downloaded files by content rather than HTTP status, and
  producing bibliography tables and per-topic archives.
---

# 文献检索与下载（skill）

## 0. 两条铁律（其余都可灵活处理）

### 铁律一：不臆断任务之间的关系

**每次文献任务相互独立，可能相关也可能毫不相干。**

- 只按用户当前给出的主题/DOI/标题检索。**不要**从上一轮任务、会话技能库、工作目录残留文件推断用户的"研究方向"。
- **不要**自选试跑主题。需要示例时**先问用户**。
- 归档时**按主题独立命名**，不要把多个主题塞进一个"项目"目录，也不要在文档里编造"A 任务/B 任务"这类关联叙事。
- 不要把上一轮的关键词带入下一轮检索。

### 铁律二：HTTP 200 ≠ 文件有效

本 skill 存在的核心理由：下载链路会以多种方式**静默失败**，仅看状态码必然出错。真实踩过的坑：

| 失败形态 | 表现 | 识破方法 |
|---|---|---|
| 拿到**另一篇论文** | HTTP 200 + 合法 PDF | 提取正文比对标题/DOI |
| 拿到 **Cloudflare 挑战页** | HTTP 200 + text/html 但**文件头是 base64 的 `PCFET0NUWVBFIGh0bWw+`** | 解 base64 看是否 `<!DOCTYPE html>` |
| 拿到 **HTML 存根** | 保存成 `.pdf` 但内容是网页 | 校验 `%PDF-` 魔数 |
| PDF 是 **base64 文本** | 文件头 `JVBERi0xLjcK` | 解码后再校验 |
| MP4 带 **4 字节垃圾前缀** | 播放器无法识别 | 校验 `ftyp`/`moov`/`mdat` box 链 |
| 文件过小 | 几 KB 的"PDF" | 尺寸阈值（PDF ≥ 8 KB） |

**因此：每个下载文件都必须过 `scripts/verify_files.py`，按真实字节判定类型，并与题录比对。**

---

## 1. 环境勘察（先做，30 秒）

不同机器的访问链路完全不同，**先探测再下载**：

```bash
python scripts/probe_access.py            # 出口 IP、证书、各出版商可达性
```

### 1.0 ⚠ 第一件事：确认图书馆资源/VPN 是否真的连着

**这是最容易被忽略、也最容易误判的一条。** 实测教训：

> 同一套脚本、同一个 DOI，在 VPN 未连接时 Wiley 返回 **HTTP 403 + 580 KB HTML**；
> 连上 VPN 后同一脚本立刻拿到 4987 KB 的合法 PDF。
> 当时的错误反应是去怀疑脚本、怀疑 Cloudflare、怀疑 URL 形式——**全都不是原因**。

因此：
- 大批量下载**失败率异常高**时，先查出口 IP（`probe_access.py`），**不要先改脚本**。
- 商用 VPN 客户端（天融信 NGVONE / 深信服 EasyConnect）断开后，`SV-Connection` 适配器可能仍显示 Up。
  要对比 `Get-NetIPAddress` 和实际出口 IP 才能确认。
- 用户说"已连接图书馆资源"时，**先验证再开工**；中途掉线会很常见。

必须确认：

1. **出口 IP 是否在机构网段**（CERNET/教育网、校园网）。图书馆订阅按机构 IP 生效。
   - 若用户提到"已连接图书馆资源"，通常是 SSL VPN（天融信 NGVONE / 深信服 EasyConnect 等）。
   - **确认 VPN 真的在承载流量**：对比 `api.ipify.org` 与出版商页面回显的 `clientIP`。曾遇到 VPN 显示已连接但 WoS 流量走的是境外出口（AWS）的情况。
2. **证书是否正常**：`python scripts/probe_access.py --cert <host>` 读 TLS 证书链。
   - 曾遇 `onlinelibrary.wiley.com` 裸域名 `ERR_TLS_CERT_ALTNAME_INVALID`，但期刊子域名正常。
   - **该现象可能是间歇性的**：同一台机器后一次探测两个域名证书都正常。因此**不要把它当成稳定结论写死**，
     遇到时优先试子域名，或稍后重试。
   - 若发现中间人重签 CA（如 `Topsec_api_ca`），说明 VPN 在做 SSL 拦截，需换链路。
3. **邮箱**：CrossRef / Unpaywall 要求**真实邮箱**。Unpaywall 用 `test@example.com` 这类地址会返回 HTTP 422
   `Please use your own email address in API calls`。**首次使用前问用户要一个邮箱**，或使用环境变量 `LIT_MAILTO`。

---

## 2. 渠道可用性矩阵（实测结论，按优先级）

| 渠道 | 可用性 | 取全文的正确方式 |
|---|---|---|
| **APS**（Phys. Rev.） | ✅ 全自动 | `https://journals.aps.org/<jrn>/pdf/10.1103/<DOI后缀>`。**DOI 大小写敏感**，必须用 CrossRef 返回的规范形式（`PhysRevB.99.014104` 而非 `physrevb.99.014104`） |
| **Springer Nature** | ✅ 全自动 | `https://link.springer.com/content/pdf/<DOI>.pdf` 或 `https://www.nature.com/articles/<后缀>.pdf` |
| **IOP** | ✅ 全自动 | `https://iopscience.iop.org/article/<DOI>/pdf` |
| **arXiv / OSTI / PMC / NSF-PAR** | ✅ 全自动 | 预印本与官方存档版，**订阅渠道被拦时的合法兜底** |
| **Wiley** | ⚠️ 需浏览器会话 | 正文：`https://<期刊子域>.onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true`（**必须 `pdfdirect` + `download=true`**；`/doi/pdf/` 与 `/doi/epdf/` 返回 HTML）。SI：`/action/downloadSupplement?doi=<DOI>&file=<文件名>` |
| **ACS** | ⚠️ 需浏览器会话 | `https://pubs.acs.org/doi/pdf/<DOI>`（页面内 fetch 可取）。SI 在 `article-supplement` 路径下 |
| **AIP** | ⚠️ 需浏览器 + 兜底 | 官网受 Cloudflare Turnstile 保护，页面内 fetch 会被 **CORS/CSP 拦截**（`TypeError: Failed to fetch`）。优先用 OSTI/CHORUS 版本 |
| **Elsevier / ScienceDirect** | ❌ 自动化不可用 | Kasada 人机验证（"Are you a robot?"）。**不绕过**，请用户在浏览器手动下载 |
| **CNKI（知网）** | ❌ 自动化不可用 | 滑块验证码。**不绕过**，请用户手动检索下载 |
| **Web of Science** | ⚠️ 仅索引 | 不提供全文，只给题录与引文，需跳转出版商 |
| **ACS / Wiley 反爬** | — | 均为 Cloudflare。脚本直连必被 403 |

**判断依据**：403 + `text/html` + 6 KB + 内容含 `Just a moment...` = Cloudflare；
403 + 800 KB 级 HTML = Kasada。**不要浪费轮次硬试，直接切浏览器会话或换 OA 兜底。**

> 注意区分：**根路径 403 ≠ 全文不可下载**。实测 APS 站点根路径返回 Cloudflare 403，
> 但 `journals.aps.org/<jrn>/pdf/<DOI>` 可直接下载全文。
> 因此 `probe_access.py` 的输出只用来判断"需不需要浏览器"，**全文可用性必须用具体 DOI 实测**。

### 落地页怎么选：优先 `https://doi.org/<DOI>`

**不要直接拼出版商裸域名。** 实测 `onlinelibrary.wiley.com` 会 `ERR_CONNECTION_TIMED_OUT`，
而走 `https://doi.org/<DOI>` 重定向后立刻正常进入文章页，并且能拿到**真实的期刊子域名**
（如 `advanced.onlinelibrary.wiley.com`）——这一点很关键，因为后续的相对路径和同源 `fetch` 都要靠它。

`discover_links.mjs` 已内置该策略（Nature / APS / IOP 走直连，其余走 doi.org）。

---

## 3. 核心技法：真实浏览器会话取全文

这是本 skill 最有价值的部分。**纯 curl / fetch 无法通过 Cloudflare**，必须用真实 Chrome。

### 3.1 方案对比（按可靠性排序）

| 方案 | 可靠性 | 适用 |
|---|---|---|
| **A. 页面内 `fetch()` 取响应体** | 高 | 同源文件（ACS、Wiley 同域）。天然携带已验证的会话 Cookie |
| **B. `Fetch.enable` 拦截响应体** | 中 | 下载型响应（Wiley SI）。可能因 PDF 阅读器吞掉响应而失败 |
| **C. `Browser.downloadWillBegin` 下载事件** | 中 | 真实下载流。**易被 `Page.navigate` 打断**，且需 `Browser.setDownloadBehavior` |
| **D. 导出 Cookie 给 curl** | 低 | **不可靠**：Cloudflare 的 Cookie 常为 HttpOnly，CDP 取不到（实测 `cookie 0 个`） |
| **E. 直接 curl** | 无 | 必被 Cloudflare 403 |

**首选 A**：先 `Page.navigate` 到文章落地页过验证，再在页面上下文里 `fetch(文件URL)` 并把结果转 base64 回传。

### 3.2 必需的 Chrome 启动参数

```
--headless=new                          # 调试期建议加 --headed，便于人工点验证
--disable-blink-features=AutomationControlled
--user-data-dir=<独立profile>            # 保持会话，避免每次重新过验证
--no-first-run --no-default-browser-check
--window-size=1500,1000
--disable-features=IsolateOrigins,site-per-process
```

并注入反检测脚本（`Page.addScriptToEvaluateOnNewDocument`）：

```js
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
if(!window.chrome) window.chrome={runtime:{}};
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
```

### 3.3 关键技巧：从页面 JS 数据里挖 SI 文件名

Wiley 的 SI 链接常是锚点而非直接文件链接。**在页面 HTML 里正则搜文件名**：

```js
const h = document.documentElement.innerHTML;
const names = [...new Set(h.match(/[a-z]{4}\d{6,}-sup-\d{4}-[A-Za-z0-9._-]+/gi) || [])];
// 例：advs6503-sup-0001-SuppMat.pdf / advs6503-sup-0002-VideoS1.mp4
// 再由 <落地页URL>/action/downloadSupplement?doi=<DOI>&file=<文件名> 构造下载地址
```

**这一步是发现 SI 视频等隐藏文件的关键**，从页面可见链接里是找不到的。

### 3.4 遇到人机验证

- 在 `--headed` 模式运行，**提示用户在窗口里手动点击**（本 skill 会自动等待并轮询页面标题变化）。
- **等待判据不能用"页面有文字"**——Cloudflare 挑战页本身就有文字。必须用**标题匹配**（如标题含文章名）或正文字符数阈值。
  曾因判据过松，把挑战页当成正常页，得出"该文无 SI"的错误结论。

现成实现：`scripts/browser_fetch.mjs`。

---

## 4. 标准工作流

```
0. 环境勘察         probe_access.py            # 出口IP / 证书 / 渠道可达性
1. 题录解析         resolve_refs.py            # 引用 -> 规范 DOI + 权威元数据（CrossRef）
2. 定位全文与SI     discover_links.mjs         # 页面内挖 PDF/SI 直链（含从JS数据挖文件名）
3. 下载             browser_fetch.mjs          # 方案A/B/C 依次尝试
                    fetch_oa.mjs               # OA 兜底（arXiv/OSTI/PMC/Unpaywall）
4. 内容校验         verify_files.py            # 【必做】按字节判定类型 + 与题录比对
5. 归档             archive_topic.py           # 按主题独立归档
6. 交付             题录表 + 说明文档           # 见 §6
```

**每一步都要落盘中间结果**（JSON），便于断点续跑与事后复核。

---

## 5. 内容级校验：怎么做才对

`scripts/verify_files.py` 的判定逻辑（**不要简化为只看魔数**）：

**PDF**
```python
head = open(p,'rb').read(200000)
if head[:5] == b'%PDF-':                       # 正常
    ...
elif re.fullmatch(rb'[A-Za-z0-9+/=\s]+', head[:4000]):
    decoded = base64.b64decode(...)            # ACS 部分 SI 是 base64 文本
    assert decoded[:5] == b'%PDF-'
else:
    # 可能是 HTML（Cloudflare/错误页）或 base64 的 HTML
```
**真正可靠的验证是内容比对**，而非魔数：用 `pypdf` 提取首页文本，检查
① 标题实词命中率（≥3/5 视为匹配）② 正文中是否出现该 DOI。

**MP4**
```python
struct.unpack('>I', b[0:4])[0] == 24 and b[4:8] == b'ftyp'   # 标准 box 头
# 校验 box 链：ftyp -> moov -> free -> mdat，且 mdat 可用 64 位扩展长度（size==1）
```
Wiley 的视频 SI 若以 `.bin` 落地或头部多 4 字节垃圾，用
`scripts/repair_files.py` 还原（base64 解码 / 重建 box 头 / 剥离前缀）。

**别忘了一个 PDF 可能是完全正确的格式但内容错误**——这类失败只有内容比对能发现。

---

## 6. 归档与交付规范

### 6.1 目录结构（按主题独立）

归档根目录用在 `--root` 指定（默认：当前目录下的 `papers/`）。给出一个建议布局：

```
<root>/<主题>_<YYYYMMDD>/
├── 00_<主题>说明.md               # 渠道结论、关键原文摘录、待办
├── 01_交付成果/                   # 题录表 xlsx、说明 md/docx
├── 02_来源文献/                   # 正文 PDF，按 <年份>_<期刊简称>_<主题>/ 或 <年份>/<期刊>/
├── 03_补充材料/                   # SI（PDF/视频/表格），文件名需自解释
└── 04_工具链/                     # 本次使用的脚本副本 + 过程数据（JSON）
```

**主题之间不合并。** 同一主题多次任务可续用同一目录，但**不要跨主题建"项目"。**

### 6.2 文件命名

- 正文：`正文_<简短标题>.pdf`；SI：`SI-1_<用途>.pdf`、`SI视频S1_<内容>（39秒）.mp4`
- **不要**把哈希、URL 片段、`.bin.bin` 这类东西留在最终文件名里。
- SI 与正文要能一眼对应。

### 6.3 交付文档

- **题录表**：`序号 / 标题 / 作者 / 期刊 / 年 / 卷 / 期 / 页 / 出版商 / DOI / OA状态 / SI情况 / 本地文件`。
  权威元数据一律取自 CrossRef，**不要用 OpenAlex 的期刊名**（会出现 `Physical review. B./Physical review. B` 这类脏数据）。
- **说明文档**：写清"用户需求 → 实际取得什么 → 未取得的原因与手动获取路径"，并摘录与需求直接相关的原文段落。
- 生成 Office 文件时加载对应 skill（`office-xlsx` / `office-docx`），并用其 checker 校验。

---

## 7. 边界与合规

- **不绕过**要求人工完成的人机验证（ScienceDirect Kasada、CNKI 滑块）。这类一律报告"需人工介入"并给出手动步骤。
- 全程只用**图书馆订阅**与**开放获取**渠道。OA 兜底优先选出版商官方存档（OSTI/CHORUS/PMC），其次预印本。
- 交付物中注明：仅供个人学习科研使用，版权归原作者与出版商。
- **更正用户引用中的错误**：曾遇用户给的卷期页不存在（`J. Chem. Educ. 2015, 92(10), 1714–1720` 实际是 2026, 103(1), 175–184）。
  用 CrossRef 按期刊卷期交叉核查，明确指出并给出真实信息，不要默默按错误引用去找。

---

## 8. scripts 一览（均经实测验证）

| 脚本 | 用途 | 验证情况 |
|---|---|---|
| `probe_access.py` | 出口 IP、TLS 证书链、各出版商可达性 | 实测通过（正确识别 Cloudflare 与证书状态） |
| `resolve_refs.py` | 引用/DOI → 规范 DOI + CrossRef 权威元数据（含 DOI 大小写、PII、全文链接） | 实测通过（`physrevb` → `PhysRevB` 规范化；ISSN 限定检索定位到正确文献） |
| `discover_links.mjs` | 浏览器打开落地页，提取 PDF/SI 链接，并从页面 JS 数据挖 SI 文件名 | 实测通过（挖出 3 个 SI 含 2 个视频） |
| `browser_fetch.mjs` | 方案 A/B/C 依次尝试取文件，支持人工过人机验证 | 实测通过（Wiley 正文 4987 KB；SI 与归档版字节完全一致） |
| `fetch_oa.mjs` | OA 兜底：Unpaywall 定位副本 + arXiv/OSTI/PMC 直链 | 逻辑验证通过 |
| `verify_files.py` | 【必做】内容级校验：真实类型 + 标题/DOI 比对 | 实测通过（11 个真实文件全过；正确识别 Cloudflare base64 页面） |
| `repair_files.py` | 还原 base64 文本 / 重建 MP4 box 头 / 剥离前缀 | 实测通过（5 个合成用例：base64±前缀、MP4 垃圾头、正常文件、CF 页面） |
| `archive_topic.py` | 按主题归档到标准目录结构 | 实测通过 |

所有脚本的公共约定：
- 路径由命令行参数或环境变量传入，**不硬编码**；
- 邮箱读 `LIT_MAILTO`（或 `--mailto`），**首次使用先问用户**；
- Chrome 路径自动探测（`CHROME_PATH` 或 `--chrome=`），未找到会明确报错；
- 输出机器可读 JSON（`--json` / `--out`）便于串接。

### 完整流水线示例

```bash
export LIT_MAILTO=you@example.edu

# 0. 环境勘察（务必先确认 VPN 已连）
python scripts/probe_access.py

# 1. 题录解析（拿到规范 DOI 与 OA 位置；核对用户引用是否有误）
python scripts/resolve_refs.py "10.1002/advs.202304179" --unpaywall --out refs.json

# 2. 发现正文与 SI 链接（会过 Cloudflare；SI 文件名从页面 JS 数据挖）
node scripts/discover_links.mjs --headed --items items.json --out links.json

# 3. 下载（可直接吃 links.json）
node scripts/browser_fetch.mjs --headed --out downloads --targets links.json

# 3b. 订阅渠道被拦时走 OA 兜底
node scripts/fetch_oa.mjs --items oa_items.json --out downloads

# 4. 校验（必做）
python scripts/verify_files.py downloads --registry registry.json

# 4b. 有编码/头部问题时还原
python scripts/repair_files.py downloads

# 5. 归档
python scripts/archive_topic.py --scan downloads --topic "<主题>" --root D:\refs
```

### 排障顺序（按此顺序查，不要跳步）

1. **VPN / 出口 IP 是否正常** ← 最高频原因，先查这个
2. 该站点是否需要浏览器会话（`probe_access.py` 反爬列）
3. URL 形式是否正确（Wiley 必须 `pdfdirect` + `download=true`；APS 大小写敏感）
4. 是否有 OA 兜底（`fetch_oa.mjs`，优先官方存档）
5. 是否需人工介入（Kasada / CNKI 滑块）→ 报告并给手动步骤
6. 最后才怀疑脚本本身
