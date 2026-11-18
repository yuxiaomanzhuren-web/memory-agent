"""Explicit provenance checks; neither a truth oracle nor passive surfacing."""
from .memory_evidence import scope_from
from .turn_recall import TurnRecall

TRIGGERS = {'manual', 'user_recall', 'user_correction', 'self_correction', 'state_mismatch'}


def verify_memory(memory, data):
    scope = scope_from(data)
    trigger = data.get('trigger', 'manual')
    if not isinstance(trigger, str) or trigger not in TRIGGERS:
        raise ValueError('invalid verification trigger')
    text = data.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        raise ValueError('verification needs 1..1000 characters of query or original text')
    result = TurnRecall(memory).recall({**scope, 'text': text.strip(),
        'diagnostics': data.get('diagnostics') is True}, explicit_lookup=True)
    return {**result, 'scope': scope, 'trigger': trigger,
            'verdict': 'unavailable' if result.get('error') else 'evidence_found' if result['hits'] else 'no_evidence',
            'state_refresh_required': trigger == 'state_mismatch',
            'notice': '命中只表示找到了可回查来源，不自动证明概括或当前状态为真；无证据时不要猜。'}