/**
 * browser_fetch.mjs —— 用真实 Chrome 会话取出版商文件（PDF / SI / 任意附件）
 *
 * 为什么需要它：Wiley / ACS / AIP / RSC / APS 等站点有 Cloudflare 保护，
 * 纯 curl / fetch 必被 403。必须用真实浏览器先通过验证，再借助已建立的会话取文件。
 *
 * 三种通道（按可靠性依次尝试，可用 --channels 指定）：
 *   A. page-fetch   ：在页面上下文里 fetch() 取响应体 → base64 回传。同源最可靠（首选）
 *   B. fetch-dom    ：CDP Fetch 域拦截响应体。下载型响应可用，但可能被 PDF 阅读器吞掉
 *   C. download     ：Browser.downloadWillBegin 下载事件。需 setDownloadBehavior，易被导航打断
 *
 * 用法：
 *   node browser_fetch.mjs --targets targets.json [--out DIR] [--headed] [--wait 90000]
 *
 * targets.json 格式：
 *   [{
 *     "name": "输出文件名（不含扩展名）",
 *     "landing": "https://.../doi/10.xxxx/yyyy",          // 先访问此页过 Cloudflare
 *     "files": [                                           // 要取的文件
 *       {"url": "https://.../doi/pdfdirect/10.xxxx/yyyy?download=true", "role": "正文"},
 *       {"url": "https://.../action/downloadSupplement?...", "role": "SI"}
 *     ]
 *   }]
 *
 * 退出码：0 全部成功；1 有失败（详情见 --report 指定的 JSON）
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

// ---------- 参数 ----------
const argv = process.argv.slice(2);
const getArg = (k, d) => {
  const hit = argv.find(a => a.startsWith(`--${k}=`));
  return hit ? hit.slice(k.length + 3) : d;
};
const hasFlag = k => argv.includes(`--${k}`);

const TARGETS_FILE = getArg('targets');
if (!TARGETS_FILE) {
  console.error('缺少 --targets=targets.json');
  process.exit(2);
}
// 兼容两种输入：纯数组，或 discover_links.mjs 输出的 {results, targets}
const _raw = JSON.parse(fs.readFileSync(TARGETS_FILE, 'utf8'));
const TARGETS = Array.isArray(_raw) ? _raw : (_raw.targets || _raw.results || []);
if (!TARGETS.length) { console.error('targets 为空'); process.exit(2); }
const OUTDIR = getArg('out', path.join(process.cwd(), 'downloads'));
const REPORT = getArg('report', path.join(OUTDIR, 'browser_fetch_report.json'));
const HEADED = hasFlag('headed');
const WAIT_MS = Number(getArg('wait', '90000'));
const CHANNELS = getArg('channels', 'page-fetch,fetch-dom,download').split(',').map(s => s.trim());
const PORT = Number(getArg('port', '9411'));
const CHROME = getArg('chrome', findChrome());

function findChrome() {
  const cands = [
    process.env.CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    path.join(os.homedir(), 'AppData\\Local\\Google\\Chrome\\Application\\chrome.exe'),
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/google-chrome', '/usr/bin/chromium', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ];
  for (const c of cands) if (c && fs.existsSync(c)) return c;
  throw new Error('未找到 Chrome/Edge，请用 --chrome=<路径> 或设置 CHROME_PATH');
}

fs.mkdirSync(OUTDIR, { recursive: true });
const DL_DIR = path.join(OUTDIR, '_dl_tmp');
fs.mkdirSync(DL_DIR, { recursive: true });
const profile = path.join(os.tmpdir(), 'litdl-chrome-profile');
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ---------- 启动 Chrome ----------
const args = [
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  '--no-first-run', '--no-default-browser-check',
  '--disable-blink-features=AutomationControlled',
  '--disable-features=IsolateOrigins,site-per-process,Translate',
  '--window-size=1500,1000', '--disable-popup-blocking',
  '--always-open-pdf-externally',
  'about:blank',
];
if (!HEADED) args.unshift('--headless=new');
const chrome = spawn(CHROME, args, { stdio: 'ignore' });

// ---------- 最小 CDP 客户端 ----------
async function wsUrl() {
  for (let i = 0; i < 60; i++) {
    try {
      const j = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
    } catch { }
    await sleep(400);
  }
  throw new Error('Chrome 调试端口未就绪');
}

function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    let id = 0;
    const pending = new Map();
    const handlers = new Map();
    ws.addEventListener('open', () => resolve({
      send(method, params = {}, sessionId) {
        const mid = ++id;
        return new Promise((res, rej) => {
          pending.set(mid, { res, rej });
          ws.send(JSON.stringify({ id: mid, method, params, sessionId }));
        });
      },
      on(m, fn) { handlers.set(m, fn); },
    }));
    ws.addEventListener('error', () => reject(new Error('CDP WebSocket 错误')));
    ws.addEventListener('message', ev => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const p = pending.get(m.id);
        pending.delete(m.id);
        m.error ? p.rej(new Error(JSON.stringify(m.error))) : p.res(m.result);
      } else if (m.method && handlers.has(m.method)) {
        handlers.get(m.method)(m.params, m.sessionId);
      }
    });
  });
}

const browser = await connect(await wsUrl());
await browser.send('Browser.setDownloadBehavior',
  { behavior: 'allow', downloadPath: DL_DIR, eventsEnabled: true }).catch(() => { });
const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
const { sessionId: S } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
await browser.send('Page.enable', {}, S);
await browser.send('Runtime.enable', {}, S);
await browser.send('Network.enable', {}, S);
await browser.send('Page.addScriptToEvaluateOnNewDocument', {
  source: `Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
           if(!window.chrome) window.chrome={runtime:{}};
           Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
           Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});`,
}, S).catch(() => { });

const evaluate = async (expr, timeout = 240000) => {
  const r = await browser.send('Runtime.evaluate',
    { expression: expr, returnByValue: true, awaitPromise: true, timeout }, S);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || 'JS 异常');
  return r.result?.value;
};

// ---------- 人机验证等待 ----------
// 关键：判据必须用「标题匹配」或正文字符数，不能用「页面有文字」——
// Cloudflare 挑战页本身就有文字，判据过松会把挑战页当成正常页。
async function waitChallenge(landingTitleHint, maxMs) {
  let waited = 0, last = {};
  while (waited < maxMs) {
    await sleep(2500); waited += 2500;
    const v = await evaluate(
      `JSON.stringify({t:document.title,n:document.body?document.body.innerText.length:0,
                       txt:(document.body?document.body.innerText:'').slice(0,150)})`
    ).catch(() => '{}');
    last = JSON.parse(v || '{}');
    const s = `${last.t || ''} ${last.txt || ''}`;
    if (/just a moment|请稍候|安全验证|Checking your browser|正在进行|Attention Required|Are you a robot/i.test(s)) {
      if (waited % 15000 === 0) {
        console.log(`      人机验证等待 ${waited / 1000}s${HEADED ? '（可在窗口中手动点击）' : '（headless）'}`);
      }
      continue;
    }
    if (landingTitleHint && (last.t || '').length > 15) return last;
    if (!landingTitleHint && (last.n || 0) > 1500) return last;
  }
  return last;
}

// ---------- 通道 A：页面内 fetch ----------
async function pageFetch(url) {
  const expr = `(async()=>{try{
    const r=await fetch(${JSON.stringify(url)},{credentials:'include',redirect:'follow'});
    const ct=r.headers.get('content-type')||''; const cd=r.headers.get('content-disposition')||'';
    const ab=await r.arrayBuffer(); const u8=new Uint8Array(ab);
    const magic=String.fromCharCode(...u8.slice(0,4));
    let b64='';
    if(u8.length>1500){let s='';const CH=0x8000;for(let i=0;i<u8.length;i+=CH)s+=String.fromCharCode.apply(null,u8.subarray(i,i+CH));b64=btoa(s);}
    return JSON.stringify({status:r.status,ct,cd,len:u8.length,magic,finalUrl:r.url,b64});}
    catch(e){return JSON.stringify({error:String(e)})}})()`;
  try { return JSON.parse(await evaluate(expr) || '{}'); }
  catch (e) { return { error: e.message }; }
}

// ---------- 通道 B：CDP Fetch 拦截 ----------
async function fetchDom(url) {
  let cap = null;
  await browser.send('Fetch.enable',
    { patterns: [{ urlPattern: '*', requestStage: 'Response' }] }, S).catch(() => { });
  browser.on('Fetch.requestPaused', async (p, sid) => {
    try {
      const hdrs = Object.entries(p.responseHeaders || {});
      const ct = hdrs.find(([k]) => k.toLowerCase() === 'content-type')?.[1] || '';
      const cd = hdrs.find(([k]) => k.toLowerCase() === 'content-disposition')?.[1] || '';
      if (!cap) cap = { url: p.request.url, ct, cd, ...(await browser.send('Fetch.getResponseBody', { requestId: p.requestId }, sid)) };
    } catch (e) { if (!cap) cap = { url: p.request.url, error: e.message }; }
    try { await browser.send('Fetch.continueResponse', { requestId: p.requestId }, sid); }
    catch { try { await browser.send('Fetch.continueRequest', { requestId: p.requestId }, sid); } catch { } }
  });
  await browser.send('Page.navigate', { url }, S).catch(() => { });
  for (let i = 0; i < 40; i++) { await sleep(1500); if (cap && (cap.base64 || cap.body)) break; }
  await browser.send('Fetch.disable', {}, S).catch(() => { });
  return cap || {};
}

// ---------- 通道 C：下载事件 ----------
let downloads = [];
browser.on('Browser.downloadWillBegin', p => {
  downloads.push({ guid: p.guid, url: p.url, filename: p.suggestedFilename, state: 'begin' });
});
browser.on('Browser.downloadProgress', p => {
  const d = downloads.find(x => x.guid === p.guid);
  if (d) d.state = p.state;
});
async function downloadEvent(url) {
  downloads = [];
  for (const f of fs.readdirSync(DL_DIR)) { try { fs.unlinkSync(path.join(DL_DIR, f)); } catch { } }
  await browser.send('Page.navigate', { url }, S).catch(() => { });
  for (let i = 0; i < 40; i++) {
    await sleep(1500);
    if (downloads.some(d => d.state === 'completed' || d.state === 'canceled')) break;
  }
  const done = downloads.find(d => d.state === 'completed');
  if (!done) return {};
  const fp = path.join(DL_DIR, done.filename);
  if (!fs.existsSync(fp)) return {};
  const buf = fs.readFileSync(fp);
  fs.unlinkSync(fp);
  return { from: 'download', filename: done.filename, len: buf.length, buf };
}

// ---------- 主流程 ----------
const isPdf = b => b && b.length > 8000 && b.slice(0, 5).toString('latin1') === '%PDF-';
const looksLikeFile = b => b && b.length > 1500 &&
  (isPdf(b) || b.slice(4, 8).toString('latin1') === 'ftyp' || b.slice(0, 2).toString('latin1') === 'PK');

const report = [];
for (const t of TARGETS) {
  console.log(`\n=== ${t.name} ===`);
  const rec = { name: t.name, landing: t.landing, files: [] };

  await browser.send('Page.navigate', { url: t.landing }, S).catch(() => { });
  const st = await waitChallenge(true, WAIT_MS);
  console.log(`   落地: ${(st.t || '(空)').slice(0, 90)}`);
  if (!(st.t || '').length) console.log('   ⚠ 落地页标题为空，可能未过验证；后续可能失败');

  for (const f of (t.files || [])) {
    const out = { url: f.url, role: f.role || '', ok: false, tried: [] };
    const base = `${t.name}${f.role ? '_' + f.role : ''}`.replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 140);

    for (const ch of CHANNELS) {
      if (out.ok) break;
      out.tried.push(ch);
      if (ch === 'page-fetch') {
        const g = await pageFetch(f.url);
        if (looksLikeFile(g.b64 ? Buffer.from(g.b64, 'base64') : null)) {
          const buf = Buffer.from(g.b64, 'base64');
          const ext = isPdf(buf) ? '.pdf' : buf.slice(4, 8).toString('latin1') === 'ftyp' ? '.mp4'
            : buf.slice(0, 2).toString('latin1') === 'PK' ? (g.cd && /\.docx/i.test(g.cd) ? '.docx' : '.zip') : '.bin';
          const dest = path.join(OUTDIR, base + ext);
          fs.writeFileSync(dest, buf);
          Object.assign(out, { ok: true, via: 'page-fetch', file: dest, bytes: buf.length, ct: g.ct });
          console.log(`   ✔ [page-fetch] ${(buf.length / 1024).toFixed(0)} KB  ${g.ct}  -> ${path.basename(dest)}`);
        } else {
          console.log(`   ✘ [page-fetch] HTTP ${g.status ?? '-'} ct=${g.ct || '-'} len=${g.len || 0} ${g.error || ''}`);
        }
      } else if (ch === 'fetch-dom') {
        const c = await fetchDom(f.url);
        const buf = c.base64 ? Buffer.from(c.body, 'base64') : c.body ? Buffer.from(c.body, 'binary') : null;
        if (looksLikeFile(buf)) {
          const ext = isPdf(buf) ? '.pdf' : buf.slice(4, 8).toString('latin1') === 'ftyp' ? '.mp4' : '.bin';
          const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(c.cd || '');
          const dest = path.join(OUTDIR, base + ext);
          fs.writeFileSync(dest, buf);
          Object.assign(out, { ok: true, via: 'fetch-dom', file: dest, bytes: buf.length, cd: m ? m[1] : '' });
          console.log(`   ✔ [fetch-dom] ${(buf.length / 1024).toFixed(0)} KB  -> ${path.basename(dest)}`);
        } else {
          console.log(`   ✘ [fetch-dom] 未捕获有效内容${c.error ? ' (' + c.error + ')' : ''}`);
        }
      } else if (ch === 'download') {
        const d = await downloadEvent(f.url);
        if (looksLikeFile(d.buf)) {
          const ext = path.extname(d.filename || '') ||
            (isPdf(d.buf) ? '.pdf' : d.buf.slice(4, 8).toString('latin1') === 'ftyp' ? '.mp4' : '.bin');
          const dest = path.join(OUTDIR, base + ext);
          fs.writeFileSync(dest, d.buf);
          Object.assign(out, { ok: true, via: 'download', file: dest, bytes: d.buf.length, suggested: d.filename });
          console.log(`   ✔ [download] ${(d.buf.length / 1024).toFixed(0)} KB (${d.filename}) -> ${path.basename(dest)}`);
        } else {
          console.log('   ✘ [download] 未产生完成状态的下载');
        }
      }
    }
    if (!out.ok) console.log(`   ✘ ${f.role || f.url} 全部通道失败；建议：换 OA 兜底或请用户手动下载`);
    rec.files.push(out);
  }
  report.push(rec);
  fs.writeFileSync(REPORT, JSON.stringify(report, null, 2), 'utf8');
}

console.log('\n===== 汇总 =====');
let okN = 0, totN = 0;
for (const r of report) {
  for (const f of r.files) {
    totN++;
    if (f.ok) { okN++; console.log(`  ✔ ${r.name} / ${f.role || '-'}  ${(f.bytes / 1024).toFixed(0)} KB via ${f.via}`); }
    else console.log(`  ✘ ${r.name} / ${f.role || '-'}  失败`);
  }
}
console.log(`\n成功 ${okN}/${totN}；报告: ${REPORT}`);
try { await browser.send('Browser.close'); } catch { }
chrome.kill();
process.exit(okN === totN ? 0 : 1);
