import fs from 'fs';
import zlib from 'zlib';

/** 读取 DSH 追加写入的多帧 zstd 文件，返回拼接后的 utf8 文本 */
export function readSessionText(file) {
  const b = fs.readFileSync(file);
  const pos = [];
  for (let i = 0; i < b.length - 3; i++) {
    if (b[i] === 0x28 && b[i + 1] === 0xB5 && b[i + 2] === 0x2F && b[i + 3] === 0xFD) pos.push(i);
  }
  if (!pos.length) return '';
  const parts = [];
  for (const o of pos) {
    try { parts.push(zlib.zstdDecompressSync(b.subarray(o)).toString('utf8')); } catch { /* skip bad frame */ }
  }
  return parts.join('');
}

export function readSessionEvents(file) {
  const text = readSessionText(file);
  const out = [];
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t || t[0] !== '{') continue;
    try { out.push(JSON.parse(t)); } catch { /* ignore */ }
  }
  return out;
}
