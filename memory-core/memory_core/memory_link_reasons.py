"""Stable public edge-rejection codes, with Chinese text and legacy adapters."""


class LinkValidationError(ValueError):
    def __init__(self, code, text, detail):
        super().__init__(detail)
        self.reason_code, self.reason_text = code, text


_KNOWN = {
    'topic link requires the same explicit topic': ('topic_mismatch', '两端话题不一致，不当作同一话题'),
    'linked memory outside this persona': ('scope_mismatch', '目标记忆不属于当前角色和会话，不建立连线'),
    'link requires evidence from both endpoints': ('endpoint_evidence_missing', '缺少两端原文证据，不建立连线'),
    'same book/topic alone does not establish the same event': ('event_key_mismatch', '两端没有相同且有效的事件键，不当作同一件事'),
    'same-event link requires an explicit continuation in the new original quote': ('event_continuation_missing', '新原话没有明确延续这件事，不建立同事件线'),
    'link was manually removed; automatic revival is forbidden': ('link_removed', '这对端点曾被手动拆线，不能自动恢复'),
    'link is unowned': ('link_unowned', '已有连线缺少当前范围的明确归属，不改动它'),
    'cannot link a memory to itself': ('self_link', '记忆不能与自己连线'),
    'invalid linked memory_id': ('target_invalid', '目标记忆编号无效，本条连线已跳过'),
    'unsupported experience relation': ('relation_invalid', '连线关系类型不受支持，本条连线已跳过'),
    'link proposal must be an object': ('proposal_invalid', '连线建议格式无效，本条连线已跳过'),
    'links must be a list of at most 4 proposals': ('proposal_list_invalid', '连线建议必须是至多四条的列表，本组建议已跳过'),
    'invalid linked memory metadata': ('target_metadata_invalid', '目标记忆元数据无效，本条连线已跳过'),
    'linked memory requires raw source references': ('target_source_invalid', '目标记忆缺少适用的原文引用，本条连线已跳过'),
    'stored event identity cannot be verified': ('event_identity_invalid', '已有事件键无法从来源核实，不建立同事件线'),
    'event anchors missing from citations': ('event_anchor_missing', '引用中缺少对象或动作证据，不建立同事件线'),
}


def reason_fields(error):
    if isinstance(error, LinkValidationError):
        return dict(reason_code=error.reason_code, reason_text=error.reason_text)
    message = str(error).removeprefix('ValueError: ')
    pair = _KNOWN.get(message)
    if pair is None and (message.startswith(('raw_', 'citation ', 'source_refs ', 'invalid_source_ref'))
                         or message.endswith('_mismatch')):
        pair = ('source_invalid', '连线引用失效或与原文不符，本条连线已跳过')
    code, text = pair or ('link_validation_failed', '连线校验未通过，本条连线已跳过')
    return dict(reason_code=code, reason_text=text)


def readable_skip(record):
    """Enrich old responses without mutating historical audit rows."""
    result = dict(record)
    for key, value in reason_fields(result.get('reason', '')).items():
        result.setdefault(key, value)
    return result
