"""Compatibility ingress for the existing QQ bridge and legacy HTTP clients."""
from ...rawlog import timestamp_seconds


def append_raw(raw, data):
    ts = timestamp_seconds(data.get("ts"))
    session = str(data.get("session") or "private")
    if data.get("message_id") is not None:
        role = str(data.get("role") or "user").lower()
        if role not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        extra = {"message_id": str(data["message_id"])}
        extra.update({key: data[key] for key in (
            "send_role", "stance", "support_scope", "stance_reason", "classifier_version", "support_target"
        ) if key in data})
        one = raw.append(session, role, str(data.get("text") or ""), ts=ts, extra=extra)
        ids = [one] if one else []
    elif data.get("user") is not None or data.get("reply") is not None:
        ids = raw.append_turn(session, str(data.get("user") or ""), str(data.get("reply") or ""), ts=ts)
    else:
        one = raw.append(session, str(data.get("role") or "user"), str(data.get("text") or ""), ts=ts)
        ids = [one] if one else []
    return {"ids": ids, "source_tags": raw.source_tags_for(ids, session)}
