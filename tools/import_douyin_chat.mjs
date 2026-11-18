// 把抖音侧（douyin_say / douyin_recent）能找回的对话补进 memory-core。
// 量不大：她的发言 17 条 + 群里快照几段。大部分抖音互动是管理员在 QQ 里转述的，
// 那部分已随 QQ 私聊导入，这里只补抖音侧原声。
// 用法：node tools/import_douyin_chat.mjs [--apply]
import fs from 'node:fs';
import path from 'node:path';
import { readSessionEvents } from './zstd_frames.mjs';

const WS_LIST = [
  '<LOCAL_PATH>',
  '<LOCAL_PATH>',
];
const BASE = 'http://127.0.0.1:8780';
const TOKEN = 'local-token';
const APPLY = process.argv.includes('--apply');
const GAP_SEC = 45 * 60;
const MAX_CHARS = 500;

const toSec = (t) => { const n = Number(t) || 0; return n > 1e11 ? Math.round(n / 1000) : Math.round(n); };
const day = (t) => new Date(t * 1000).toLocaleDateString('sv-SE');
const hm = (t) => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });

const turns = [];
const seen = new Set();
const push = (time, who, text) => {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  if (!t || t === '[表情]' || t.length > 600) return;
  const key = who + '|' + t.slice(0, 120);
  if (seen.has(key)) return;
  seen.add(key);
  turns.push({ time: toSec(time), who, text: t });
};

for (const ws of WS_LIST) {
  if (!fs.existsSync(ws)) continue;
  for (const n of fs.readdirSync(ws)) {
    const f = path.join(ws, n, 'session.v4.jsonl.zstd');
    if (!fs.existsSync(f)) continue;
    const evs = readSessionEvents(f);
    const recentIds = new Set(evs.filter((e) => e.type === 'tool/call' && /douyin_recent/.test(String(e.data?.name || ''))).map((e) => e.data?.callId));
    for (const e of evs) {
      if (e.type === 'tool/call' && /douyin_say/.test(String(e.data?.name || ''))) {
        let a = e.data?.arguments;
        if (typeof a === 'string') { try { a = JSON.parse(a); } catch { a = {}; } }
        const t = a?.message || a?.text || a?.content;
        if (t) push(e.time, '我', t);
      } else if (e.type === 'tool/result') {
        const cid = e.data?.message?.source?.callId || e.data?.message?.toolCallId;
        if (!cid || !recentIds.has(cid)) continue;
        const txt = (e.data?.message?.content || []).filter((c) => c.type === 'text').map((c) => c.text).join('');
        // 返回是纯文本快照，一行一条：`uid（标签）：内容`
        for (const line of txt.split('\n')) {
          const m = line.match(/^(\d+)(?:（([^）]*)）)?：(.+)$/);
          if (!m) continue;
          push(e.time, '他', m[3]);
        }
      }
    }
  }
}
turns.sort((a, b) => a.time - b.time);
console.log(`抖音侧找回 ${turns.length} 条`);

// 切段
const segs = [];
let cur = null;
for (const t of turns) {
  const prev = cur && cur.turns[cur.turns.length - 1];
  if (!cur || t.time - prev.time > GAP_SEC || cur.chars + t.text.length > MAX_CHARS) {
    if (cur) segs.push(cur);
    cur = { turns: [t], chars: t.text.length };
  } else { cur.turns.push(t); cur.chars += t.text.length; }
}
if (cur) segs.push(cur);
console.log(`切 ${segs.length} 段：`);
for (const s of segs) console.log('  -', day(s.turns[0].time), hm(s.turns[0].time), s.turns.length, '条');

if (!APPLY) { console.log('（未写库，加 --apply）'); process.exit(0); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
for (const s of segs) {
  const from = s.turns[0].time;
  const text = `【抖音 ${day(from)} ${hm(from)}】\n` + s.turns.map((t) => `${t.who}：${t.text}`).join('\n');
  try {
    const r = await fetch(`${BASE}/remember?token=${TOKEN}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text, kind: 'episode', importance: 0.4, source: 'dy-import',
        visibility: 'private', created_at: from,
        meta: { import: 'douyin-chat', day: day(from), msgs: s.turns.length },
      }),
    });
    const j = await r.json();
    console.log(j.status === 'ok' ? `  写入 #${j.data.id} ${day(from)} ${hm(from)}` : `  失败 ${JSON.stringify(j).slice(0, 120)}`);
  } catch (e) { console.log('  失败', String(e.message).slice(0, 100)); }
  await sleep(80);
}
console.log('完成');
