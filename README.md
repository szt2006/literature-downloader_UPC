# literature-downloader

一个给 AI 编码助手（DeepSeek Harness / Claude Code 等支持 skill 的工具）用的**文献检索与全文下载**技能包。

在校园网 / 图书馆订阅环境下，帮你把论文正文和 **Supporting Information** 抓下来、校验完整性、整理成题录表和归档目录。

---

## 它解决什么问题

手动下一篇论文很麻烦，批量下十篇更麻烦。这个技能包针对的是实际下载时反复遇到的三类坑：

### 坑一：出版商反爬，脚本直连必被拒

Wiley、ACS、AIP、RSC、APS 都有 Cloudflare 保护，`curl` / `requests` 一律 403。
本包用**真实 Chrome 会话**先过验证，再借助已建立的会话取文件。

### 坑二：看起来成功了，其实拿到的不是论文

这是最危险的失败——**HTTP 200 + 合法 PDF，但内容不对**。真实遇到过的形态：

| 你以为是 | 实际拿到 | 文件头特征 |
|---|---|---|
| 论文 PDF | 另一篇论文 | `%PDF-`（格式完全合法） |
| 论文 PDF | Cloudflare 挑战页 | base64 文本 `PCFET0NUWVBFIGh0bWw+` |
| 论文 PDF | HTML 存根 | `<!DOCTYPE html>` |
| 论文 PDF | base64 文本 | `JVBERi0xLjcK` |
| SI 视频 | 带 4 字节垃圾的 MP4 | `ftyp` 偏移错位，播放器打不开 |

本包提供 `verify_files.py` 做**内容级校验**（按真实字节判类型 + 用 pypdf 比对标题词与 DOI），
以及 `repair_files.py` 还原编码/头部问题。

### 坑三：SI 藏在页面 JS 数据里，链接里根本看不到

Wiley 的 Supporting Information 常以锚点呈现。但页面 HTML 的 JS 数据里能正则挖出真实文件名：

```js
// 挖出：advs6503-sup-0001-SuppMat.pdf、advs6503-sup-0002-VideoS1.mp4 ……
const names = [...new Set(html.match(/[a-z]{4}\d{6,}-sup-\d{4}-[A-Za-z0-9._-]+/gi) || [])];
```

`discover_links.mjs` 做了这件事，**连 SI 视频这种隐藏文件也能找出来**。

---

## 渠道支持情况

| 渠道 | 情况 | 取全文方式 |
|---|---|---|
| **APS**（Phys. Rev.） | ✅ 全自动 | DOI **大小写敏感**，需用 CrossRef 规范形式 |
| **Springer Nature** | ✅ 全自动 | 直链 |
| **IOP** | ✅ 全自动 | 直链 |
| **arXiv / OSTI / PMC / NSF-PAR** | ✅ 全自动 | 合法 OA 兜底 |
| **Wiley** | ⚠️ 需浏览器会话 | 正文须 `pdfdirect` + `download=true`；SI 走 `downloadSupplement` |
| **ACS** | ⚠️ 需浏览器会话 | 页面内 fetch 可取 |
| **AIP** | ⚠️ 需浏览器 + OA 兜底 | 官网受 Turnstile 保护，优先用 OSTI/CHORUS 版本 |
| **Elsevier / ScienceDirect** | ❌ 需人工 | Kasada 人机验证，**不绕过** |
| **CNKI（知网）** | ❌ 需人工 | 滑块验证码，**不绕过** |
| **Web of Science** | ⚠️ 仅索引 | 不提供全文，需跳转出版商 |

> 注意：站点**根路径 403 ≠ 全文不可下载**。实测 APS 根路径返回 Cloudflare 403，
> 但 `journals.aps.org/<jrn>/pdf/<DOI>` 可直接下载全文。全文可用性必须用具体 DOI 实测。

---

## 安装

把本目录放到你的助手 skill 目录下：

```bash
# DeepSeek Harness / 兼容 ~/.dsh/skills 的工具
git clone https://github.com/<you>/literature-downloader.git \
  ~/.dsh/skills/literature-downloader

# 如果你在 Windows PowerShell 下
git clone https://github.com/<you>/literature-downloader.git "$env:USERPROFILE\.dsh\skills\literature-downloader"
```

**依赖**：

```bash
pip install pypdf          # 内容级校验需要
# Python 3.8+；Chrome 或 Edge 已安装（脚本会自动探测路径）
```

---

## 快速开始

```bash
# 先设一个真实邮箱（CrossRef / Unpaywall 要求；用 test@example.com 会被拒）
export LIT_MAILTO=you@example.edu          # PowerShell: $env:LIT_MAILTO="you@example.edu"

# 0. 环境勘察 —— 务必先确认校园网/VPN 已连！
python scripts/probe_access.py

# 1. 题录解析：拿规范 DOI、核对引用是否有误、查 OA 副本
python scripts/resolve_refs.py "10.1002/advs.202304179" --unpaywall --out refs.json

# 2. 发现正文与 SI 链接（会打开浏览器过 Cloudflare）
#    先写 items.json：[{"name":"标签","doi":"10.1002/advs.202304179"}]
node scripts/discover_links.mjs --headed --items items.json --out links.json

# 3. 下载（可直接吃上一步的 links.json）
node scripts/browser_fetch.mjs --headed --out downloads --targets links.json

# 3b. 订阅渠道被拦时走 OA 兜底
node scripts/fetch_oa.mjs --items oa_items.json --out downloads

# 4. 校验（必做，不要跳）
python scripts/verify_files.py downloads --registry registry.json

# 4b. 有编码/头部问题时还原
python scripts/repair_files.py downloads

# 5. 按主题归档
python scripts/archive_topic.py --scan downloads --topic "主题名" --root D:\refs
```

`--headed` 会在需要人工点验证码时把浏览器窗口显示出来。遇到 Cloudflare 挑战且 headless 过不去时用它。

---

## 脚本清单

| 脚本 | 用途 |
|---|---|
| `probe_access.py` | 出口 IP、TLS 证书链、各出版商可达性与反爬特征 |
| `resolve_refs.py` | 引用/DOI → 规范 DOI + CrossRef 权威元数据（含 PII、全文链接） |
| `discover_links.mjs` | 打开落地页提取 PDF/SI 链接，从 JS 数据挖 SI 文件名 |
| `browser_fetch.mjs` | 三通道依次尝试取文件（页面 fetch / CDP 拦截 / 下载事件） |
| `fetch_oa.mjs` | OA 兜底：Unpaywall + arXiv/OSTI/PMC |
| `verify_files.py` | **内容级校验**：真实类型 + 标题/DOI 比对 |
| `repair_files.py` | 还原 base64 文本 / 重建 MP4 box 头 |
| `archive_topic.py` | 按主题归档到标准目录结构 |

所有脚本路径都通过命令行参数传入，不硬编码；输出 JSON 便于串接。

---

## 排障顺序（按此顺序查，不要跳步）

1. **校园网 / VPN 是否真的连着** ← 最高频原因，先查这个
2. 该站点是否需要浏览器会话（看 `probe_access.py` 的反爬列）
3. URL 形式是否正确（Wiley 必须 `pdfdirect` + `download=true`；APS 大小写敏感）
4. 是否有 OA 兜底（`fetch_oa.mjs`，优先官方存档）
5. 是否需人工介入（Kasada / 滑块）→ 给用户手动步骤
6. **最后**才怀疑脚本本身

> 血泪教训：曾出现"同一脚本、同一 DOI，VPN 断开时 403、连上后立刻成功"的情况。
> 当时的错误反应是去改脚本、怀疑 Cloudflare、换 URL 形式——**全都不是原因**。
> 下载失败率异常高时，先查出口 IP。

---

## 合规

- **不绕过**要求人工完成的人机验证（ScienceDirect Kasada、CNKI 滑块）。遇到这类一律报告"需人工介入"。
- 只使用**图书馆订阅**与**开放获取**渠道。OA 兜底优先选出版商官方存档（OSTI / CHORUS / PMC），其次预印本。
- 下载的文献版权归原作者与出版商所有，仅供个人学习科研使用，**请勿再分发**。

---

## 许可

MIT（见 [LICENSE](LICENSE)）。

欢迎 PR 补充新的出版商渠道、修 bug、或把探测结果更新到 `probe_access.py`。
