/**
 * fetch_oa.mjs —— 开放获取兜底下载（订阅渠道被反爬拦截时用）
 *
 * 优先级（合规顺序，不要颠倒）：
 *   1. Unpaywall 定位的出版商官方 OA 副本
 *   2. 官方存档：OSTI / CHORUS / PMC
 *   3. 预印本：arXiv
 *
 * 用法:
 *   node fetch_oa.mjs --items items.json --out DIR [--mailto a@b.c]
 *
 * items.json:
 *   [{"name":"输出名","doi":"10.xxxx/yyyy","arxivId":"2304.09409","ostiId":"1994160",
 *     "pmcId":"PMC10724388","pdfUrl":"https://.../x.pdf"}]
 * 只需填已知的字段；脚本会自动用 Unpaywall 补全（需 --mailto）。
 */
import fs from 'node:fs';
import path from 'node:path';

const argv = process.argv.slice(2);
const getArg = (k, d) => {
  const hit = argv.find(a => a.startsWith(`--${k}=`));
  return hit ? hit.slice(k.length + 3) : d;
};
const ITEMS_FILE = getArg('items');
if (!ITEMS_FILE) { console.error('缺少 --items=items.json'); process.exit(2); }
const ITEMS = JSON.parse(fs.readFileSync(ITEMS_FILE, 'utf8'));
const OUTDIR = getArg('out', path.join(process.cwd(), 'downloads'));
const MAILTO = getArg('mailto', process.env.LIT_MAILTO || '');
const REPORT = getArg('report', path.join(OUTDIR, 'fetch_oa_report.json'));

fs.mkdirSync(OUTDIR, { recursive: true });

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const isPdf = b => b && b.length > 8000 && b.slice(0, 5).toString('latin1') === '%PDF-';

async function tryGet(url, label) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90000);
  try {
    const r = await fetch(url, {
      headers: { 'User-Agent': UA, Accept: 'application/pdf,*/*;q=0.8', 'Accept-Language': 'en-US,en;q=0.9' },
      redirect: 'follow', signal: ctrl.signal,
    });
    const ct = r.headers.get('content-type') || '';
    const buf = Buffer.from(await r.arrayBuffer());
    if (!r.ok) return { ok: false, note: `HTTP ${r.status}`, ct, len: buf.length };
    if (!isPdf(buf)) return { ok: false, note: `非 PDF (${ct}, ${buf.length}B)`, ct, len: buf.length };
    return { ok: true, via: label, buf, ct, finalUrl: r.url };
  } catch (e) {
    return { ok: false, note: e.name === 'AbortError' ? '超时' : e.message };
  } finally { clearTimeout(timer); }
}

async function unpaywall(doi) {
  if (!MAILTO) return { error: '需要真实邮箱（--mailto 或 LIT_MAILTO）；否则 Unpaywall 返回 422' };
  try {
    const r = await fetch(`https://api.unpaywall.org/v2/${encodeURIComponent(doi)}?email=${encodeURIComponent(MAILTO)}`,
      { headers: { 'User-Agent': UA } });
    if (!r.ok) return { error: `HTTP ${r.status}` };
    const j = await r.json();
    return {
      is_oa: j.is_oa, oa_status: j.oa_status,
      best_pdf: j.best_oa_location?.url_for_pdf || '',
      locations: (j.oa_locations || []).map(l => ({
        host: l.host_type, version: l.version, pdf: l.url_for_pdf, landing: l.url,
      })),
    };
  } catch (e) { return { error: e.message }; }
}

const report = [];
for (const it of ITEMS) {
  console.log(`\n=== ${it.name || it.doi || it.arxivId} ===`);
  const rec = { name: it.name, doi: it.doi, attempts: [] };
  const cands = [];

  if (it.pdfUrl) cands.push({ url: it.pdfUrl, label: '指定直链' });

  if (it.doi && MAILTO) {
    const u = await unpaywall(it.doi);
    rec.unpaywall = u;
    if (!u.error) {
      console.log(`   Unpaywall: is_oa=${u.is_oa} status=${u.oa_status}`);
      if (u.best_pdf) cands.push({ url: u.best_pdf, label: 'Unpaywall 最佳 OA' });
      for (const l of u.locations) {
        if (l.pdf && !cands.some(c => c.url === l.pdf)) {
          cands.push({ url: l.pdf, label: `Unpaywall/${l.host}/${l.version}` });
        }
      }
    } else {
      console.log(`   Unpaywall: ${u.error}`);
    }
  }
  if (it.arxivId) {
    cands.push({ url: `https://arxiv.org/pdf/${it.arxivId}`, label: 'arXiv 预印本' });
  }
  if (it.ostiId) {
    cands.push({ url: `https://www.osti.gov/servlets/purl/${it.ostiId}`, label: 'OSTI 官方存档' });
  }
  if (it.pmcId) {
    cands.push({ url: `https://pmc.ncbi.nlm.nih.gov/articles/${it.pmcId}/pdf/`, label: 'PMC 官方存档' });
  }

  if (!cands.length) {
    console.log('   无可用 OA 候选（可提供 arxivId / ostiId / pmcId，或提供 --mailto 让 Unpaywall 定位）');
    rec.error = 'no candidates';
    report.push(rec);
    continue;
  }

  rec.candidates = cands.map(c => c.label);
  for (const c of cands) {
    if (rec.ok) break;
    process.stdout.write(`   -> [${c.label}] ${c.url.slice(0, 100)} ... `);
    const r = await tryGet(c.url, c.label);
    rec.attempts.push({ url: c.url, label: c.label, ok: r.ok, note: r.note });
    if (r.ok) {
      const dest = path.join(OUTDIR, `${(it.name || 'oa').replace(/[\\/:*?"<>|\s]+/g, '_')}_${c.label.replace(/[\\/:*?"<>|\s]+/g, '')}.pdf`);
      fs.writeFileSync(dest, r.buf);
      Object.assign(rec, { ok: true, file: dest, bytes: r.buf.length, via: c.label });
      console.log(`✔ ${(r.buf.length / 1024).toFixed(0)} KB -> ${path.basename(dest)}`);
    } else {
      console.log(`✘ ${r.note}`);
    }
    await new Promise(s => setTimeout(s, 1200));
  }
  if (!rec.ok) { rec.error = '全部 OA 候选失败'; console.log('   ✘ 全部失败 —— 可能需要人工获取'); }
  report.push(rec);
  fs.writeFileSync(REPORT, JSON.stringify(report, null, 2), 'utf8');
}

console.log('\n===== OA 兜底汇总 =====');
let ok = 0, tot = report.length;
for (const r of report) {
  if (r.ok) { ok++; console.log(`  ✔ ${r.name}  ${(r.bytes / 1024).toFixed(0)} KB via ${r.via}`); }
  else console.log(`  ✘ ${r.name}  ${r.error || ''}`);
}
console.log(`\n成功 ${ok}/${tot}；报告: ${REPORT}`);
process.exit(ok === tot ? 0 : 1);
