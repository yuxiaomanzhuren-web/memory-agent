"""Conservative source-text gate; no model, DB, network, or provenance lookup.

Integration contract: after validating references, resolve each reference to its
COMPLETE user original and pass list[str], not quote snippets, model body/scene,
or raw-record dicts. This module cannot verify completeness or provenance.

Load once per extraction batch; reuse the immutable EntryPolicy for its entries.
The next load sees configuration changes. Evaluation never reloads configuration.
Retention here does not bypass the pipeline's other entry validation rules.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ['EntryPolicy', 'load_entry_policy', 'evaluate_entry']


@dataclass(frozen=True)
class EntryPolicy:
    """Validated snapshot with immutable, precompiled matching rules."""

    version: str
    lowinfo_fullmatch: tuple[re.Pattern, ...]
    protect_search: tuple[re.Pattern, ...]


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f'duplicate JSON key: {name}')
        result[name] = value
    return result


def _compile_patterns(data, field, location):
    patterns = data[field]
    if not isinstance(patterns, list):
        raise ValueError(f'invalid entry policy {location}: {field} must be a list')
    result = []
    for index, text in enumerate(patterns):
        label = f'{field}[{index}]'
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f'invalid entry policy {location}: {label} must be a nonempty regex string')
        try:
            pattern = re.compile(text)
        except re.error as exc:
            raise ValueError(f'invalid entry policy {location}: {label}: {exc}') from exc
        if pattern.search('') is not None:
            raise ValueError(f'invalid entry policy {location}: {label} must not match empty text')
        result.append(pattern)
    return tuple(result)


def load_entry_policy(path=None) -> EntryPolicy:
    """Load UTF-8 JSON: explicit path > MEMORY_ENTRY_POLICY_PATH > sibling JSON.

    Required keys: version (nonempty str), lowinfo_fullmatch (regex str list),
    protect_search (regex str list). Empty lists disable their respective rules.
    Missing/unreadable files, malformed schema/UTF-8/regex raise ValueError;
    an invalid override NEVER falls back to bundled deletion rules.
    """
    location = path
    if location is None:
        location = os.environ.get('MEMORY_ENTRY_POLICY_PATH')
        if location is None:
            location = Path(__file__).with_suffix('.json')
    try:
        if location == '':
            raise ValueError('empty policy path')
        data = json.loads(Path(location).read_text(encoding='utf-8-sig'), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError(f'invalid entry policy {location}: {exc}') from exc
    fields = {'version', 'lowinfo_fullmatch', 'protect_search'}
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError(f'invalid entry policy {location}: expected exactly {sorted(fields)}')
    version = data['version']
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f'invalid entry policy {location}: version must be a nonempty string')
    return EntryPolicy(version=version.strip(),
                       lowinfo_fullmatch=_compile_patterns(data, 'lowinfo_fullmatch', location),
                       protect_search=_compile_patterns(data, 'protect_search', location))


def evaluate_entry(sources: list[str], policy: EntryPolicy) -> dict:
    """Inspect ONLY complete verified originals. Any protected/unknown source wins.

    Only surrounding whitespace is trimmed; no content is truncated or removed.
    Empty lists/text retain. Wrong input types raise ValueError to expose caller
    mistakes, rather than silently classifying model fields or quoted fragments.
    Codes are stable across policy versions; a match is lexical, not semantic.
    """
    if not isinstance(policy, EntryPolicy):
        raise ValueError('policy must be returned by load_entry_policy')
    if not isinstance(sources, list) or any(not isinstance(source, str) for source in sources):
        raise ValueError('sources must be a list of complete verified original strings')

    def decision(keep, code, text):
        return dict(keep=keep, reason_code=code, reason_text=text, policy_version=policy.version)

    texts = [source.strip() for source in sources]
    if not texts:
        return decision(True, 'retain_no_sources', '没有可评估的完整原文，默认保留。')
    # Examine ALL sources for protection before returning even an unknown result.
    if any(pattern.search(text) is not None for text in texts for pattern in policy.protect_search):
        return decision(True, 'retain_protected', '至少一条完整原文命中配置中的保护模式，保留。')
    if all(text and any(pattern.fullmatch(text) is not None for pattern in policy.lowinfo_fullmatch)
           for text in texts):
        return decision(False, 'drop_low_information', '所有完整原文均完整匹配配置中的低信息模式，过滤。')
    return decision(True, 'retain_unknown', '至少一条完整原文未明确匹配低信息模式，默认保留。')
