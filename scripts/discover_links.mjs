/**
 * discover_links.mjs —— 用真实浏览器打开文章落地页，发现 PDF 与 Supporting Information 链接
 *
 * 解决两个老大难问题：
 *   1. SI 链接常是锚点/JS，不在 HTML 的 <a href> 里 —— 本脚本会从页面 JS 数据里正则挖文件名
 *      （如 advs6503-sup-0001-SuppMat.pdf、advs6503-sup-0002-VideoS1.mp4）
 *   2. 需要先过 Cloudflare 才能看到正文结构 —— 本脚本负责过验证并等待
 *
 * 用法:
 *   node discover_links.mjs --items items.json [--out links.json] [--headed] [--wait 90000]
 *
 * items.json:
 *   [{"name":"标签","doi":"10.1002/adma.202406848","landing":"https://.../doi/10.1002/..."}]
 *   landing 可省略，会按 DOI 前缀猜常见出版商路径。
 *
 * 输出的 links.json 可直接喂给 browser_fetch.mjs 当 targets.json 用。
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const argv = process.argv.slice(2);
const getArg = (k, d) => {
  const hit = argv.find(a => a.startsWith(`--${k}=`));
  return hit ? hit.slice(k.length + 3) : d;
};
const hasFlag = k => argv.includes(`--${k}`);

const ITEMS_FILE = getArg('items');
if (!ITEMS_FILE) { console.error('缺少 --items=items.json'); process.exit(2); }
const ITEMS = JSON.parse(fs.readFileSync(ITEMS_FILE, 'utf8'));
const OUT = getArg('out', 'links.json');
const HEADED = hasFlag('headed');
const WAIT_MS = Number(getArg('wait', '90000'));
const PORT = Number(getArg('port', '9415'));
const CHROME = getArg('chrome', findChrome());

function findChrome() {
  const cands = [
    process.env.CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    path.join(os.homedir(), 'AppData\\Local\\Google\\Chrome\\Application\\chrome.exe'),
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/google-chrome', '/usr/bin/chromium',
  ];
  for (const c of cands) if (c && fs.existsSync(c)) return c;
  throw new Error('未找到 Chrome/Edge，请用 --chrome=<路径>');
}

/** 按 DOI 猜落地页。
 *  注意：**默认走 https://doi.org/<DOI> 让出版商自己重定向**，不要直接拼裸域名——
 *  实测 onlinelibrary.wiley.com 裸域名会 ERR_CONNECTION_TIMED_OUT，
 *  而 doi.org 重定向后可正常进入文章页，且能拿到真实的期刊子域名作为后续相对路径的基准。 */
function guessLanding(doi) {
  const d = (doi || '').toLowerCase();
  // 已知稳定的直连形式（避免多一跳）
  if (d.startsWith('10.1038/')) return `https://www.nature.com/articles/${doi.split('/')[1]}`;
  if (d.startsWith('10.1103/')) {
    const j = /physrev([a-z]+)/i.exec(doi);
    const jrn = j ? `pr${j[1][0]}` : 'prb';
    return `https://journals.aps.org/${jrn}/abstract/${doi}`;
  }
  if (d.startsWith('10.1088/')) return `https://iopscience.iop.org/article/${doi}`;
  // 其余统一走 DOI 解析（Wiley / ACS / RSC / AIP 等）
  return `https://doi.org/${doi}`;
}

const profile = path.join(os.tmpdir(), 'litdl-disc');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const args = [
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check',
  '--disable-blink-features=AutomationControlled',
  '--disable-features=IsolateOrigins,site-per-process', '--window-size=1500,1000', 'about:blank',
];
if (!HEADED) args.unshift('--headless=new');
const chrome = spawn(CHROME, args, { stdio: 'ignore' });

async function wsUrl() {
  for (let i = 0; i < 60; i++) {
    try { const j = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json(); if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl; } catch { }
    await sleep(400);
  }
  throw new Error('Chrome 调试端口未就绪');
}
function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    let id = 0; const pending = new Map();
    ws.addEventListener('open', () => resolve({
      send(m, p = {}, s) { const mid = ++id; return new Promise((res, rej) => { pending.set(mid, { res, rej }); ws.send(JSON.stringify({ id: mid, method: m, params: p, sessionId: s })); }); },
    }));
    ws.addEventListener('error', () => reject(new Error('CDP WS 错误')));
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result); }
    });
  });
}
const browser = await connect(await wsUrl());
const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
const { sessionId: S } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
await browser.send('Page.enable', {}, S);
await browser.send('Runtime.enable', {}, S);
await browser.send('Page.addScriptToEvaluateOnNewDocument', {
  source: `Object.defineProperty(navigator,'webdriver',{get:()=>undefined}); if(!window.chrome) window.chrome={runtime:{}};`,
}, S).catch(() => { });

const ev = async (e, t = 200000) => {
  const r = await browser.send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true, timeout: t }, S);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'JS 异常');
  return r.result?.value;
};

// 页面内综合探测：可见链接 + JS 数据里的 SI 文件名
const PROBE = `JSON.stringify((()=>{
  const out = {title:document.title, url:location.href, links:[], siNames:[], supplUrls:[], text:''};
  out.text = (document.body?document.body.innerText:'').replace(/\\s+/g,' ').slice(0,300);
  // 可见链接
  out.links = [...document.querySelectorAll('a')]
    .map(a=>({href:a.href, text:(a.innerText||'').trim().slice(0,60)}))
    .filter(x=>/pdf|supplement|suppmat|supporting|download|epdf|/i.test(x.href+' '+x.text))
    .slice(0,40);
  const h = document.documentElement.innerHTML || '';
  // SI 文件名（关键：SI 视频等常只出现在 JS 数据里）
  out.siNames = [...new Set((h.match(/[a-z]{4}\\d{6,}-sup-\\d{4}-[A-Za-z0-9._-]+/gi)||[]))].slice(0,20);
  // downloadSupplement 形式的地址
  out.supplUrls = [...new Set((h.match(/[^"'\\s]*downloadSupplement[^"'\\s]*/gi)||[]))]
    .map(s=>s.replace(/&amp;/g,'&')).slice(0,20);
  return out;})())`;

const results = [];
for (const it of ITEMS) {
  const landing = it.landing || guessLanding(it.doi);
  console.log(`\n=== ${it.name || it.doi} ===`);
  console.log(`   落地页: ${landing}`);
  const rec = { name: it.name, doi: it.doi, landing, files: [] };
  try {
    await browser.send('Page.navigate', { url: landing }, S).catch(() => { });
    let waited = 0, st = {};
    while (waited < WAIT_MS) {
      await sleep(2500); waited += 2500;
      const v = await ev(`JSON.stringify({t:document.title,n:document.body?document.body.innerText.length:0})`).catch(() => '{}');
      st = JSON.parse(v || '{}');
      if (/just a moment|请稍候|安全验证|Checking|Are you a robot/i.test(st.t || '')) {
        if (waited % 15000 === 0) console.log(`   验证等待 ${waited / 1000}s${HEADED ? '（可手动点击）' : ''}`);
        continue;
      }
      if ((st.t || '').length > 15) break;
    }
    rec.pageTitle = st.t;
    console.log(`   标题: ${(st.t || '(空)').slice(0, 90)}`);
    if (!(st.t || '').length) console.log('   ⚠ 标题为空，可能未过验证');

    const probe = JSON.parse(await ev(PROBE) || '{}');
    rec.url = probe.url || landing;
    console.log(`   页面正文: ${(probe.text || '').slice(0, 110)}`);

    // Wiley 等站点的 SI 区域是延迟加载的：若首扫没挖到文件名，等几秒再扫两次
    if (!(probe.siNames || []).length && !(probe.supplUrls || []).length) {
      for (let i = 0; i < 2; i++) {
        await sleep(4000);
        // 轻微滚动可触发懒加载区块
        await ev(`window.scrollTo(0, document.body.scrollHeight*${0.5 + i * 0.3}); true`).catch(() => { });
        await sleep(1500);
        const p2 = JSON.parse(await ev(PROBE) || '{}');
        if ((p2.siNames || []).length || (p2.supplUrls || []).length) {
          probe.siNames = p2.siNames; probe.supplUrls = p2.supplUrls;
          probe.links = [...(probe.links || []), ...(p2.links || [])];
          console.log(`   （第 ${i + 2} 次扫描才发现 SI 线索）`);
          break;
        }
      }
    }

    const host = (() => { try { return new URL(rec.url).origin; } catch { return ''; } })();
    if (!/^https?:/.test(host)) console.log('   ⚠ 未取得有效页面来源，兜底链接可能拼错');
    const push = (url, role) => {
      if (!url || /^null|^undefined/.test(url) || rec.files.some(f => f.url === url)) return;
      rec.files.push({ url, role });
    };

    // 1) 页面里明确的 PDF 链接（只要 pdfdirect / pdf / article-pdf 形式）
    for (const l of (probe.links || [])) {
      if (/pdfdirect|\/doi\/pdf|article-pdf|\/pdf(\?|$)/i.test(l.href)) {
        push(l.href, /pdfdirect/.test(l.href) ? '正文(pdfdirect)' : '正文(PDF)');
      }
    }
    // 2) downloadSupplement 直链
    for (const u of (probe.supplUrls || [])) {
      push(u.startsWith('http') ? u : host + (u.startsWith('/') ? u : '/' + u), 'SI');
    }
    // 3) 由 SI 文件名线索构造下载地址（Wiley 形式）
    for (const n of (probe.siNames || [])) {
      const role = /video|\.mp4|\.mov/i.test(n) ? `SI视频(${n.split('-').pop()})` : 'SI';
      push(`${host}/action/downloadSupplement?doi=${encodeURIComponent(it.doi || '')}&file=${encodeURIComponent(n)}`, role);
    }
    // 4) 兜底：按 DOI 拼常见正文直链
    if (it.doi) {
      const d = it.doi.toLowerCase();
      if (d.startsWith('10.1002/')) push(`${host}/doi/pdfdirect/${it.doi}?download=true`, '正文(pdfdirect兜底)');
      if (d.startsWith('10.1021/')) push(`https://pubs.acs.org/doi/pdf/${it.doi}`, '正文兜底');
      if (d.startsWith('10.1103/')) {
        const j = /physrev([a-z]+)/i.exec(it.doi);
        const jrn = j ? `pr${j[1][0]}` : 'prb';
        push(`https://journals.aps.org/${jrn}/pdf/${it.doi}`, '正文兜底');
      }
      if (d.startsWith('10.1088/')) push(`https://iopscience.iop.org/article/${it.doi}/pdf`, '正文兜底');
    }

    console.log(`   SI 文件名线索: ${JSON.stringify((probe.siNames || []).slice(0, 6))}`);
    console.log(`   发现 ${rec.files.length} 个候选文件:`);
    rec.files.forEach(f => console.log(`     [${f.role}] ${f.url.slice(0, 115)}`));
    if (!rec.files.length) console.log('     （无）—— 可能是 Kasada 人机验证，或该文确实没有 SI');
  } catch (e) {
    rec.error = e.message;
    console.log(`   ✘ 异常: ${e.message}`);
  }
  results.push(rec);
}

// 输出可直接作为 browser_fetch.mjs 的 targets
const targets = results.map(r => ({ name: r.name, landing: r.landing, files: r.files }));
fs.writeFileSync(OUT, JSON.stringify({ results, targets }, null, 2), 'utf8');
console.log(`\n已写入 ${OUT}（含 results 与可直接用于 browser_fetch.mjs 的 targets）`);
try { await browser.send('Browser.close'); } catch { }
chrome.kill();
process.exit(0);
