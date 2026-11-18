"""Source labels: classify only at birth; transported missing labels never become claims."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import re
import sqlite3
from pathlib import Path

SOURCE_KINDS = frozenset(('direct', 'quote', 'forward', 'unknown'))
FIELDS=('send_role','stance','support_scope','stance_reason','classifier_version','support_target','ingest_source_kind')
DIRECT={'可作依据','可作主张依据'}
CLUE={'仅线索','仅可作线索'}
STANCES={'他说','他转述','转述','他引用','引用','他假设','假设','来源不明','助手自述'}
CLASSIFIER_VERSION='gate1-source-r9@51672881dfec0e2494c754c67184dbe0f2dd3f5ed6657dcf9a5fcc3524991a28'

_trace_path=None
_trace_enabled=ContextVar('memory_provenance_trace_enabled', default=True)


@contextmanager
def suppress_trace():
    token = _trace_enabled.set(False)
    try:
        yield
    finally:
        _trace_enabled.reset(token)


def configure_trace(path):
    global _trace_path
    _trace_path=Path(path)

def emit(event,raw_id,stage,**detail):
    if not _trace_enabled.get():
        return
    try:
        line='['+event+'] '+json.dumps(dict(raw_id=str(raw_id or 'unknown')[:256],stage=stage,**detail),ensure_ascii=False)
        print(line,flush=True)
        if _trace_path:
            with _trace_path.open('a',encoding='utf-8',newline='\n') as stream:stream.write(line+'\n')
    except (OSError,ValueError):
        pass

def normalize(value,*,raw_id=None,stage='transport'):
    value=value if isinstance(value,dict) else {}
    rid=raw_id or value.get('raw_id') or value.get('id')
    observed=value.get('role') or value.get('r')
    role=observed or value.get('send_role')
    stance,scope=value.get('stance'),value.get('support_scope')
    kind=value.get('ingest_source_kind')
    origin={} if 'ingest_source_kind' not in value else {
        'ingest_source_kind':kind if isinstance(kind,str) and kind in SOURCE_KINDS else 'unknown'}
    missing=[key for key,v in [('stance',stance),('support_scope',scope)] if not v]
    valid=stance in STANCES and scope in DIRECT|CLUE|{'不可用'}
    if observed and value.get('send_role') and observed!=value['send_role']:valid=False
    if stance=='助手自述' and role!='assistant' or stance=='他说' and role!='user':valid=False
    if origin and origin['ingest_source_kind']!='direct' and scope in DIRECT:valid=False
    if missing or not valid:
        emit('stance_missing',rid,stage,missing=missing or ['invalid_marker'])
        return dict(send_role=role,stance='来源不明',support_scope='不可用' if scope=='不可用' else '仅可作线索',
                    stance_reason='来源标记缺失或无效，不重新推断主张归属',classifier_version=value.get('classifier_version','missing'),**origin)
    out={k:value[k] for k in FIELDS if k in value}
    out['send_role']=role
    out.update(origin)
    if stance not in ('他说','助手自述') and scope in DIRECT:
        emit('stance_inconsistent',rid,stage)
        out.update(stance='来源不明',support_scope='仅可作线索',stance_reason='来源与依据档位矛盾，需人工核对')
    return out

def rank(value):
    scope=value.get('support_scope')
    return 2 if scope in DIRECT else 1 if scope in CLUE else 0

def constrain(ref, checkpoint, *, stage):
    own=normalize(ref,stage=stage)
    other=normalize(checkpoint,raw_id=ref.get('raw_id'),stage=stage)
    return {**ref,**(other if rank(other)<rank(own) else own)}

def readable(value):
    return {'他转述':'转述','他引用':'引用','他假设':'假设'}.get(value.get('stance'),value.get('stance','来源不明'))

def aggregate(refs,*,stage='aggregate'):
    tags=[normalize(r,stage=stage) for r in refs]
    if not tags:return normalize({},stage=stage)
    level=min(rank(t) for t in tags)
    same=all(t.get('stance')==tags[0].get('stance') and t.get('send_role')==tags[0].get('send_role') for t in tags)
    same_role=all(t.get('send_role')==tags[0].get('send_role') for t in tags)
    return dict(send_role=tags[0].get('send_role') if same_role else 'mixed',stance=tags[0]['stance'] if same else '来源不明',
                support_scope=tags[0]['support_scope'] if level==2 and same else '不可用' if level==0 else '仅可作线索',
                stance_reason='各原话来源一致' if same else '多源主张归属不同，逐条见source_refs')

def classify_source(record):
    if record['r']=='assistant':
        # Existing citation speaker binding: evidence of the assistant's own utterance only.
        return dict(send_role='assistant',stance='助手自述',support_scope='可作依据',
                    support_target='assistant_utterance',stance_reason='已确认的助手发言；不能证明用户主张',
                    classifier_version=CLASSIFIER_VERSION+';speaker-binding-r14')
    return dict(_classify_user(record),classifier_version=CLASSIFIER_VERSION)

def _classify_user(record):
    """Classify before excerpting: a quote cannot lose its enclosing context."""
    role, text = record['r'], record['t'].strip()
    stance, reason, support = '来源不明', '无法确认这段材料是谁的主张', '仅可作线索'
    if role != 'user':
        reason, support = '发送者不是用户，不能证明用户的主张', '不可用'
    elif re.search(r'^(?:转发|转贴|转述|代转)[：:\s]|^.+[.:：]\s*\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}', text, re.M):
        stance, reason = '他转述', '原消息带转发、转述或聊天记录的来源标记'
    elif re.search(r'^(?:引用|原文|报告原文)[：:]|^#{1,6}[^\n]*[（(](?:子代理|助手|模型)[^\n]*报告', text, re.M):
        stance, reason = '他引用', '原消息标明引用或报告作者是其他主体'
    elif re.search(r'^(?:假如|假设|举例|例如|测试用例|测试样本)|\bfixture\b', text, re.I):
        stance, reason = '他假设', '原消息明确标为假设、举例或测试材料'
    elif re.search(r'^(?:他说|她说|你说|老师说|助手说|模型说|报告说|报告写|文件说)', text):
        stance, reason = '他转述', '原消息明确将话语归给其他主体'
    elif re.fullmatch(r'[“「『"].+[”」』"]', text, re.S):
        stance, reason = '他引用', '整段消息明确包在引号内'
    elif re.search(r'(?m)^\s*(?:#{1,6}\s|>\s?|[-*]\s|```)|\*\*|`|[“”「」『』"]', text):
        reason = '文档、代码或引用样式的消息缺少明确主张归属，无法确认是本人陈述还是搬来的材料'
    elif len(text) > 240 or len(text.splitlines()) > 2:
        reason = '长段或多段材料缺少明确主张归属，不能仅凭 user 身份认定为本人陈述'
    elif re.fullmatch(r'[\u3400-\u9fff]{1,4}', text) and not re.search(r'[我你他她它了没不是有要在]', text):
        reason = '只有简短主题词，缺少可独立核验的陈述；不能据此补出个人经历'
    else:
        # ponytail: unmarked chat statements are attributed as presented; identical
        # unmarked pastes need transport provenance or human review to distinguish.
        stance, reason, support = '他说', '普通聊天陈述，未发现引用、转述、假设或文档来源歧义标记', '可作主张依据'
    return dict(send_role=role, stance=stance, stance_reason=reason, support_scope=support)

def decode_tags(value):
    if isinstance(value,str):
        try:return json.loads(value)
        except ValueError:return {}
    return value if isinstance(value,dict) else {}

def indexed_tags(row,record,*,stage='raw_index'):
    # The index is a transport checkpoint: an empty label must not be repaired by reclassification.
    return normalize(decode_tags(dict(row).get('source_tags')),raw_id=record.get('id'),stage=stage)

def registry_tags(conn,record):
    try:
        row=conn.execute('SELECT * FROM memory_pipeline_raw WHERE raw_id=?',(record.get('id'),)).fetchone()
    except sqlite3.OperationalError:
        row=None
    if row is None:return normalize(record,stage='raw_read')
    row=dict(row)
    digest=record.get('_raw_sha256') or record.get('sha256')
    if digest and row['sha256']!=digest:
        emit('stance_missing',record.get('id'),'raw_read',missing=['raw_hash_mismatch'])
        return normalize({},raw_id=record.get('id'),stage='raw_read')
    return indexed_tags(row,record,stage='raw_read')
