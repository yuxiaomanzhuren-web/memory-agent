/**
 * 让记忆大脑（Python）借用 DSH 自带的账号模型做一次纯文本生成。
 *
 * 为什么要有这个：
 *  - 沉淀/反思/画像这三件事原来走 MEMORY_LLM_*，指向 glm-4-flash，写出来的东西
 *    要么是「他在豆包中查看左侧菜单栏」这种把界面描述包装一遍，要么是鸡汤腔。
 *  - DSH 自己的 deepseek-flash 语感正常，而且不需要额外 key、不占别家额度。
 *
 * 会话策略：一次调用一条新会话，问完 archive。
 *  - 不复用：上一条任务的提示词会留在历史里，下一轮会被带着跑，越跑越歪。
 *
 * 用法：
 *   node dsh-text.mjs --system "<角色/要求>" --timeout 90000 < prompt.txt
 *   提示词从 stdin 读（避免 Windows 命令行转义和长度限制）。
 * 输出（单行 JSON）：
 *   {"ok":true,"text":"...","ms":5123}
 *   {"ok":false,"code":"...","error":"..."}
 */
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

const DEFAULT_CLIENT =
  process.env.DSH_CLIENT_URL ||
  'file:///<LOCAL_PATH>';

const GUARD = '（以上是任务说明和材料。不要使用任何工具，不要读写文件，'
  + '不要复述这些要求，不要解释你在做什么，直接输出结果正文。）';

function parseArgs(argv) {
  const out = {
    provider: process.env.DSH_PROVIDER || 'deepseek-account',
    model: process.env.DSH_MODEL || 'deepseek-flash',
    timeout: 90000,
    base: process.env.DSH_BASE_URL || 'http://127.0.0.1:19387',
  };
  for (let i = 0; i < argv.length; i += 1) {
    const k = argv[i];
    const v = argv[i + 1];
    if (k === '--system') { out.system = v; i += 1; }
    else if (k === '--timeout') { out.timeout = Number(v) || 90000; i += 1; }
    else if (k === '--provider') { out.provider = v; i += 1; }
    else if (k === '--model') { out.model = v; i += 1; }
    else if (k === '--base') { out.base = v; i += 1; }
  }
  return out;
}

function emit(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  return Buffer.concat(chunks).toString('utf8');
}

function lastAssistantText(records) {
  for (let i = records.length - 1; i >= 0; i -= 1) {
    const ev = records[i]?.event;
    if (!ev || ev.type !== 'assistant/message') continue;
    const blocks = ev.data?.message?.content ?? [];
    const text = blocks
      .filter((b) => b?.type === 'text' && typeof b.text === 'string')
      .map((b) => b.text)
      .join('');
    if (text.trim()) return text.trim();
  }
  return '';
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const started = Date.now();
  const body = (await readStdin()).trim();
  if (!body) {
    emit({ ok: false, code: 'no-prompt', error: 'stdin 没有提示词' });
    return;
  }

  const text = `${args.system ? `[要求]\n${args.system}\n\n` : ''}[材料/任务]\n${body}\n\n${GUARD}`;

  const mod = await import(DEFAULT_CLIENT);
  const { NodeApiClient, unwrap } = mod;
  const api = new NodeApiClient(args.base, undefined, {
    token: '',
    header: 'authorization',
    prefix: 'Bearer',
    allowRemote: false,
  });

  let sessionId = '';
  try {
    const home = process.env.DSH_HOME || path.join(os.homedir(), '.dsh');
    const stateDir = path.join(home, 'text-agent');
    const fs = await import('node:fs');
    fs.mkdirSync(stateDir, { recursive: true });
    const cacheFile = path.join(stateDir, 'workspace.json');
    let workspaceId = '';
    try {
      const saved = JSON.parse(fs.readFileSync(cacheFile, 'utf8'));
      if (saved?.workspaceId) workspaceId = String(saved.workspaceId);
    } catch { /* 没缓存就新建 */ }
    if (!workspaceId) {
      const ws = unwrap(await api.workspace.create({ path: stateDir }), 'workspace.create');
      workspaceId = ws.workspace.workspaceId;
      fs.writeFileSync(cacheFile, JSON.stringify({ workspaceId }));
    }

    const created = unwrap(await api.sessions.create({ workspaceId }), 'sessions.create');
    sessionId = created.sessionId;
    await api.sessions.selectModel({ sessionId, provider: args.provider, model: args.model });

    const accepted = await api.sessions.prompt({
      requestId: randomUUID(),
      sessionId,
      mode: 'queue',
      content: [{ type: 'text', text }],
    });
    if (!accepted?.result?.ok) {
      const err = accepted?.result?.error ?? {};
      emit({ ok: false, code: err.code || 'prompt-rejected', error: err.message || '会话拒绝了这次提问' });
      return;
    }

    const deadline = Date.now() + args.timeout;
    let out = '';
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 900));
      let records = [];
      try {
        const page = unwrap(
          await api.callUnary('session/page', { address: { kind: 'session', sessionId }, limit: 80 }),
          'session/page'
        );
        records = page?.records ?? [];
      } catch (error) {
        emit({ ok: false, code: 'page-failed', error: String(error?.message ?? error).slice(0, 300) });
        return;
      }
      out = lastAssistantText(records);
      if (out) break;
      if (records.some((r) => r?.event?.type === 'turn/end')) break;
    }

    if (!out) {
      emit({ ok: false, code: 'no-answer', error: 'DSH 会话没有给出可用文本（超时或空回合）' });
      return;
    }
    emit({ ok: true, text: out, ms: Date.now() - started });
  } catch (error) {
    emit({ ok: false, code: 'dsh-error', error: String(error?.message ?? error).slice(0, 300) });
  } finally {
    if (sessionId) {
      try {
        await api.workspace.archiveSession({ sessionId });
      } catch { /* 归档失败不影响结果 */ }
    }
  }
}

await main();
process.exit(0);
