// 把 QQ 私聊的历史对话导进 memory-core。
// 数据源：DSH 会话文件（多帧 zstd）里还原出的「他 / 我」真实对话。
// 两层写入：
//   1) 原始层：按时间间隔切成话题段，一段一条 episode（保留原始时间戳，带向量）
//   2) 概要层：按天（或按 3000 字分片）交给 /digest/extract，压成一句概要 + 实体关系
// 幂等：已导入的段记在 data/import/qq-import-state.json，重跑不会重复。
//
// 用法：
//   node tools/import_qq_chat.mjs            # 只打印计划，不写库
//   node tools/import_qq_chat.mjs --apply    # 真写
//   node tools/import_qq_chat.mjs --apply --no-digest   # 只写原始层
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import crypto from 'node:crypto';

const MAGIC = [0x28, 0xb5, 0x2f, 0xfd];
const WS_LIST = [
  '<LOCAL_PATH>',
  '<LOCAL_PATH>',
  '<LOCAL_PATH>',
];
const BASE = 'http://127.0.0.1:8780';
const TOKEN = process.env.MEMORY_TOKEN || 'local-token';
const STATE = '$MEMORY_ROOT/data/import/qq-import-state.json';
const APPLY = process.argv.includes('--apply');
const WITH_DIGEST = !process.argv.includes('--no-digest');

const GAP_SEC = 25 * 60;         // 超过 25 分钟没说话就断段（时间戳单位是秒）
const MAX_CHARS = 500;           // 单段上限，避免一条记忆装太多话题
const MIN_CHARS = 10;            // 太短的段（单句“嗯”）不单独成记忆
// 私聊里对方（管理员）的消息 sender 只会是这几种；群消息是群友昵称，一律跳过
const PRIVATE_SENDER = new Set(['私聊', 'i', '<OWNER_ID>']);

// ---------------------------------------------------------------- 读会话
function readSession(file) {
  const buf = fs.readFileSync(file);
  const idx = [];
  for (let i = 0; i + 4 <= buf.length; i++) {
    if (buf[i] === MAGIC[0] && buf[i + 1] === MAGIC[1] && buf[i + 2] === MAGIC[2] && buf[i + 3] === MAGIC[3]) idx.push(i);
  }
  const parts = [];
  for (let k = 0; k < idx.length; k++) {
    const end = k + 1 < idx.length ? idx[k + 1] : buf.length;
    try { parts.push(zlib.zstdDecompressSync(buf.subarray(idx[k], end)).toString('utf8')); } catch { /* 帧内误判 */ }
  }
  const evs = [];
  for (const line of parts.join('').split('\n')) {
    const s = line.trim();
    if (!s) continue;
    try { evs.push(JSON.parse(s)); } catch { /* 半行 */ }
  }
  return evs;
}

function snapshotOf(text) {
  const i = text.indexOf('【本次消息快照】');
  if (i < 0) return null;
  const seg = text.slice(i);
  const a = seg.indexOf('{');
  const b = seg.lastIndexOf('}');
  if (a < 0 || b <= a) return null;
  try { return JSON.parse(seg.slice(a, b + 1)); } catch { return null; }
}

const entries = new Map();
const recent = new Map();
// DSH 会话里的 time 是毫秒，memory-core 要秒，这里统一转成秒
const toSec = (t) => {
  const n = Number(t) || 0;
  return n > 1e11 ? Math.round(n / 1000) : Math.round(n);
};
function add(time, who, text) {
  const t = String(text || '').replace(/\u0000/g, '').trim();
  if (!t || t.length > 4000) return;
  const tm = toSec(time);
  const sig = who + '|' + t.replace(/\s+/g, '').slice(0, 200);
  const last = recent.get(sig);
  if (last != null && Math.abs(tm - last) < 300) return;  // 5 分钟内重试/重放只留一次
  recent.set(sig, tm);
  const key = who + '|' + tm + '|' + t.slice(0, 80);
  if (!entries.has(key)) entries.set(key, { time: tm, who, text: t });
}

function collect() {
  for (const ws of WS_LIST) {
    if (!fs.existsSync(ws)) continue;
    for (const d of fs.readdirSync(ws, { withFileTypes: true })) {
      if (!d.isDirectory()) continue;
      const f = path.join(ws, d.name, 'session.v4.jsonl.zstd');
      if (!fs.existsSync(f)) continue;
      const evs = readSession(f);
      if (!evs.length) continue;
      const usesSendTool = evs.some((e) => e.type === 'tool/call' && /qq_send_message|qq_reply/.test(String(e.data?.name || '')));
      const isQQ = evs.some((e) => {
        if (e.type !== 'user/message') return false;
        const t = (e.data?.content || []).filter((c) => c.type === 'text').map((c) => c.text).join('\n');
        return t.includes('【本次消息快照】') || t.includes('<user_message');
      });
      if (!isQQ) continue;
      for (const e of evs) {
        if (e.type === 'user/message') {
          const content = e.data?.content || e.data?.message?.content || [];
          const text = content.filter((c) => c.type === 'text').map((c) => c.text).join('\n');
          if (!text) continue;
          const snap = snapshotOf(text);
          if (snap) {
            for (const m of [...(snap.recent || []), ...(snap.messages || [])]) {
              if (!m.text) continue;
              const self = m.isSelf === true || m.sender === '我';
              // 只要私聊：群消息的 sender 是群友昵称（这里会出现 ^Two3_ 之类），丢掉
              if (!self && !PRIVATE_SENDER.has(String(m.sender || ''))) continue;
              add(m.time, self ? '我' : '他', m.text);
            }
          } else {
            for (const m of text.matchAll(/<user_message[^>]*>([\s\S]*?)<\/user_message>/g)) add(e.time, '他', m[1]);
          }
        } else if (e.type === 'tool/call') {
          if (!/qq_send_message|qq_reply/.test(String(e.data?.name || ''))) continue;
          let args = e.data?.arguments;
          if (typeof args === 'string') { try { args = JSON.parse(args); } catch { args = null; } }
          if (!args) continue;
          const msgs = Array.isArray(args.messages) ? args.messages
            : (typeof args.message === 'string' ? [args.message] : (typeof args.text === 'string' ? [args.text] : []));
          for (const m of msgs) {
            const t = typeof m === 'string' ? m : (m && (m.text || m.content));
            if (t) add(e.time, '我', t);
          }
        } else if (e.type === 'assistant/message' && !usesSendTool) {
          const content = e.data?.message?.content || e.data?.content || [];
          const text = content.filter((c) => c.type === 'text').map((c) => c.text).join('\n');
          if (text) add(e.time, '我', text);
        }
      }
    }
  }
  return [...entries.values()].sort((a, b) => a.time - b.time);
}

// ---------------------------------------------------------------- 切段
const day = (t) => new Date(t * 1000).toLocaleDateString('sv-SE');
const hm = (t) => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit' });
const fmt = (t) => `${day(t)} ${hm(t)}`;

function chunk(turns) {
  const out = [];
  let cur = null;
  for (const t of turns) {
    const prev = cur && cur.turns[cur.turns.length - 1];
    const brk = !cur
      || t.time - prev.time > GAP_SEC
      || cur.chars + t.text.length > MAX_CHARS
      || day(t.time) !== day(cur.turns[0].time) && t.time - prev.time > 10 * 60;
    if (brk) {
      if (cur) out.push(cur);
      cur = { turns: [t], chars: t.text.length };
    } else {
      cur.turns.push(t);
      cur.chars += t.text.length;
    }
  }
  if (cur) out.push(cur);
  return out
    .filter((c) => c.chars >= MIN_CHARS)
    .map((c, i) => {
      const from = c.turns[0].time;
      const to = c.turns[c.turns.length - 1].time;
      const body = c.turns.map((t) => `${t.who}：${t.text}`).join('\n');
      const text = `【QQ私聊 ${fmt(from)}】\n${body}`;
      return { i, from, to, n: c.turns.length, chars: c.chars, text, hash: crypto.createHash('sha1').update(text).digest('hex').slice(0, 16) };
    });
}

// 重要性：有「约定 / 偏好 / 身份 / 情绪」字样的段给高一点，寒暄低一点
const HOT = /记住|别忘|答应|约好|约定|以后|永远|第一次|喜欢|讨厌|不喜欢|名字|生日|我叫|你是|管理员|饲养员|token|米饭|沙弗|秘密|不许|说好|保证|想念|难过|生气|哭|梦/;
function importanceOf(c) {
  if (HOT.test(c.text)) return 0.55;
  if (c.n >= 8) return 0.42;
  return 0.35;
}

// ---------------------------------------------------------------- 写库
async function post(p, body) {
  const r = await fetch(`${BASE}${p}?token=${TOKEN}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({ status: 'error', message: 'bad json' }));
  if (j.status !== 'ok') throw new Error(`${p} -> ${JSON.stringify(j).slice(0, 200)}`);
  return j.data;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function postRetry(p, body, times = 3) {
  let err;
  for (let i = 0; i < times; i++) {
    try { return await post(p, body); } catch (e) { err = e; await sleep(800 * (i + 1)); }
  }
  throw err;
}

async function dropMemory(id) {
  try { await post('/forget', { id }); } catch { /* 删不掉就算了 */ }
}

function loadState() {  try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch { return { done: {}, digests: [] }; }
}
function saveState(s) {
  fs.mkdirSync(path.dirname(STATE), { recursive: true });
  fs.writeFileSync(STATE, JSON.stringify(s, null, 1), 'utf8');
}

// ---------------------------------------------------------------- main
const turns = collect();
const segs = chunk(turns);
const byDay = new Map();
for (const t of turns) {
  const d = day(t.time);
  if (!byDay.has(d)) byDay.set(d, []);
  byDay.get(d).push(t);
}

console.log(`对话 ${turns.length} 条 / ${turns.reduce((a, b) => a + b.text.length, 0)} 字`);
console.log(`时间 ${turns.length ? fmt(turns[0].time) : '-'} → ${turns.length ? fmt(turns.at(-1).time) : '-'}`);
console.log(`切段 ${segs.length} 段（平均 ${Math.round(turns.reduce((a, b) => a + b.text.length, 0) / segs.length)} 字）`);
for (const [d, list] of byDay) console.log(`  ${d}  ${String(list.length).padStart(4)} 条  ${list.reduce((a, b) => a + b.text.length, 0)} 字`);

if (!APPLY) {
  console.log('\n前 3 段预览：');
  for (const s of segs.slice(0, 3)) console.log('---', s.from, s.n, '条 ---\n' + s.text + '\n');
  console.log('（未写库。加 --apply 才真正写入）');
  process.exit(0);
}

const state = loadState();
let added = 0, skipped = 0, failed = 0;
for (const s of segs) {
  if (state.done[s.hash]) { skipped++; continue; }
  try {
    const d = await postRetry('/remember', {
      text: s.text,
      kind: 'episode',
      importance: importanceOf(s),
      valence: 0,
      source: 'qq-import',
      visibility: 'private',
      created_at: s.from,
      meta: { import: 'qq-chat', day: day(s.from), msgs: s.n, from: s.from, to: s.to },
    });
    state.done[s.hash] = d.id;
    added++;
    if (added % 10 === 0) { saveState(state); console.log(`  原始层已写 ${added}/${segs.length}`); }
  } catch (e) {
    failed++;
    console.log(`  失败 ${s.from}: ${String(e.message).slice(0, 120)}`);
  }
  await sleep(60);
}
saveState(state);
console.log(`原始层：新增 ${added}，已有 ${skipped}，失败 ${failed}`);

if (WITH_DIGEST) {
  const done = new Set(state.digests || []);
  for (const [d, list] of byDay) {
    // 一天切成不超过 3000 字的分片，避免提示词过长
    const parts = [];
    let buf = [], len = 0;
    for (const t of list) {
      if (len + t.text.length > 3000 && buf.length) { parts.push(buf); buf = []; len = 0; }
      buf.push(t); len += t.text.length;
    }
    if (buf.length) parts.push(buf);
    for (let p = 0; p < parts.length; p++) {
      const key = `${d}#${p}`;
      if (done.has(key)) continue;
      const text = parts[p].map((t) => `${t.who}：${t.text}`).join('\n');
      let ok = false;
      for (let attempt = 1; attempt <= 3 && !ok; attempt++) {
        try {
          const r = await postRetry('/digest/extract', {
            text: `【QQ私聊 ${d} 第${p + 1}部分】\n${text}`,
            visibility: 'private',
            source: 'qq-import-digest',
            created_at: parts[p][0].time,
          });
          const sum = String(r?.summary || '');
          // 模型没吐合法 JSON 时 digest 会走 fallback，把原文塞进 summary —— 这种算失败，删掉重试
          if (!sum || sum.startsWith('user：') || sum.includes('【QQ私聊') || sum.length < 6) {
            if (r?.id) await dropMemory(r.id);
            throw new Error('模型没吐有效 JSON');
          }
          console.log(`  概要 ${key} → #${r.id} ${sum.slice(0, 60)}`);
          state.digests = [...(state.digests || []), key];
          saveState(state);
          ok = true;
        } catch (e) {
          console.log(`  概要${attempt < 3 ? '重试' : '放弃'} ${key}: ${String(e.message).slice(0, 100)}`);
          await sleep(1200 * attempt);
        }
      }
      await sleep(300);
    }
  }
}

console.log('\n完成。');
