"""Gate-one retrieval units only. Never grants found or expands scope."""
import re
from functools import lru_cache

# Gate one only. Exact-mode dictionary boundaries; no cross-word character grams.
# Keep single-character content words (吃 / 盘 / 糖); drop grammatical words by POS.
_FUNCTION_POS = {'r', 'c', 'p', 'e', 'y', 'w', 'uj', 'ul', 'ud', 'uz', 'ug', 'uv'}
_SHELL_WORDS = {'是', '的', '了', '过', '要', '那个', '这个', '什么', '哪个', '哪些',
                '怎么', '如何', '是不是', '有没有', '后来', '之前', '现在', '到底',
                '还', '能', '有', '让', '们'}
_DAY_PARTS = {'早上', '上午', '中午', '下午', '傍晚', '晚上', '夜里', '晚间'}
_GENERIC = {'处理', '事情', '东西', '问题', '名字', '时候', '以前', '时间', '开始',
            '看看', '需要', '不用', '没有', '不', '没', '未', '别', '想', '到', '做', '天'}
_ATOMIC = re.compile(r'(?<![A-Za-z0-9_])[_A-Za-z][_A-Za-z0-9]*(?:\(\))?|\d+(?:\.\d+)?')
_PROHIBITIONS = {'别','不要','不许','不准','禁止','勿'}
_NEGATORS = _PROHIBITIONS | {'不','没','没有','未'}
_LEXICAL_NEGATION = {'别人','别的','别处','别样','别墅','不要紧','没问题','不一定','不仅','不但','不然','不错','不得不',
                     '不用','不能','不得','不可以'}
_SLEEP_FORMS = {'睡':'睡','睡觉':'睡','睡眠':'睡'}
_PEOPLE = {'我':'user','你':'assistant','您':'assistant','他':'third','她':'third','它':'third'}


@lru_cache(maxsize=1)
def _segmenter():
    try:
        import jieba
        from jieba.posseg import POSTokenizer
    except ImportError as exc:
        raise RuntimeError('gate1_tokenizer_missing: install jieba==0.42.1 in the service Python') from exc
    tokenizer = jieba.Tokenizer()
    return POSTokenizer(tokenizer)


@lru_cache(maxsize=2048)
def negations(text):
    """Small lexical polarity hints, not general Chinese entailment or fact permission."""
    # Question shells and fixed positive replies are not asserted negation.
    text=re.sub(r'有没有|是不是|能不能|会不会|可不可以|没问题|不要紧', ' ', text)
    parts=[(pair.word,pair.flag) for pair in _segmenter().cut(text,HMM=False)]
    found=[];i=0
    while i<len(parts):
        word,flag=parts[i];operator=word if word in _NEGATORS else None;tail=''
        if not operator and word not in _LEXICAL_NEGATION and flag not in {'r','n','nr','ns','nt','nz'}:
            match=re.match(r'(不许|不要|不准|禁止|没有|别|勿|没|未|不)(.+)',word)
            if match:operator,tail=match.groups()
        if not operator:i+=1;continue
        j=i+1;surface=word
        target=_PEOPLE.get(parts[i-1][0]) if i else None
        uncertain=bool(i and parts[i-1][0] in {'不是','并非','不能','不得','不可以'})
        while not tail and j<len(parts) and (not parts[j][0].strip() or parts[j][0] in {'再','先'}):
            surface+=parts[j][0];j+=1
        if not tail and j<len(parts) and parts[j][0] in _PEOPLE:
            target=_PEOPLE[parts[j][0]];surface+=parts[j][0];j+=1
        while not tail and j<len(parts) and parts[j][0] in _NEGATORS:
            uncertain=True;surface+=parts[j][0];j+=1
        if not tail:
            if j>=len(parts) or not any(ch.isalnum()for ch in parts[j][0]):i=j;continue
            tail=parts[j][0];surface+=tail;j+=1
        if tail in _SLEEP_FORMS and j<len(parts):
            following=parts[j][0]
            # 别睡过头/不要睡太久 restrict a longer action, not sleep itself.
            if following.strip() and any(ch.isalnum()for ch in following) and following not in {'了','吧','啊','呀','呢','哦'}:
                uncertain=True
        kind='unknown' if uncertain else 'prohibition' if operator in _PROHIBITIONS else 'nonoccurrence' if operator in {'没','没有','未'} else 'negative'
        found.append(dict(operator=operator,predicate=_SLEEP_FORMS.get(tail,tail),kind=kind,
                          surface=surface,target=target))
        i=j
    return tuple(found)


def negative_relation(plan,quote):
    hints=plan['negations']
    if not hints or plan['quoted_literals']:return None
    words,_,_=_lexical(quote)
    predicates={hint['predicate']for hint in hints}
    objects={word for word in plan['object_terms'] if _SLEEP_FORMS.get(word,word)not in predicates}
    if objects and not objects & words:return None
    source=negations(quote)
    related=any(hint['predicate'] in {_SLEEP_FORMS.get(word,word)for word in words} for hint in hints)
    pairs=[(a,b)for a in hints for b in source if a['predicate']==b['predicate']
           and not(a['target'] and b['target'] and a['target']!=b['target'])]
    if all(any(a==hint and a['kind']==b['kind'] and a['kind']!='unknown'for a,b in pairs)for hint in hints):
        return 'same_direction'
    return 'unconfirmed' if related else None


def polarity_fragment(unit,plan):
    # Remove only an actual query negation fragment, never an object such as 别墅.
    return any(unit==hint['operator'] or unit==hint['surface'] or
               unit in hint['surface'][:len(hint['operator'])+1] for hint in plan['negations'])


@lru_cache(maxsize=2048)
def _lexical(text):
    words, phrases, objects, chunk = [], [], [], []
    def flush():
        if len(chunk) > 1:
            phrases.append(''.join(chunk))
            phrases.extend(a + b for a, b in zip(chunk, chunk[1:]))
        chunk.clear()
    # Protect complete code identifiers, not fragments such as validate out of _validate().
    cursor = 0
    for atom in list(_ATOMIC.finditer(text)) + [None]:
        end = atom.start() if atom else len(text)
        for pair in _segmenter().cut(text[cursor:end], HMM=False):
            word, flag = pair.word, pair.flag
            if (not any(ch.isalnum() for ch in word) or (flag in _FUNCTION_POS and word not in _NEGATORS)
                    or word in _SHELL_WORDS):
                # An attributive action is useful as a phrase as well as the verb 吃.
                if word == '的' and chunk:
                    phrases.append(''.join(chunk) + word)
                    phrases.append(chunk[-1] + word)
                flush()
                continue
            words.append(word.casefold())
            if flag.startswith('n') and word not in _GENERIC:
                objects.append(word.casefold())
            chunk.append(word.casefold())
        flush()
        if atom:
            words.append(atom.group().casefold())
            if not atom.group()[0].isdigit():
                objects.append(atom.group().casefold())
            cursor = atom.end()
    return frozenset(words), frozenset(phrases), frozenset(objects)


def lexical_overlap(plan, quote):
    words, _, _ = _lexical(quote)
    return set(plan['topics']) & words


def lexical_match(plan, quote):
    overlap = lexical_overlap(plan, quote)
    if plan['explanation_query'] and len(overlap - _GENERIC) < 2:
        return False  # One repeated action does not explain why it happened.
    if negative_relation(plan,quote):
        return True  # New polarity/predicate matches remain clue-only in the gate.
    # If a concrete object is named, a generic action cannot substitute for it.
    # 北海道 + 去 must not match an unrelated 去; 清理哪个盘 can match 清理这个吧.
    anchors = set(plan['object_terms'])
    if not anchors:
        specific = set(plan['topics']) - _GENERIC
        anchors = {word for word in specific if len(word) >= 2} or specific
    return bool(overlap & anchors)

VERBS=('调整到','调整为','设置成','设置为','调到','调成','改成','换成','设成','定为','调为','改为','换为','设为')
VERB=re.compile('|'.join(VERBS))
NUMBER=r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万半]+)'
UNIT=r'(?:毫秒|秒钟|分钟|小时|秒|时|天|次|[kKmMgGtT][bB]|[mM][sS]|[sS])'
QUANTITY=re.compile(NUMBER+r'\s*'+UNIT)
QUOTES=re.compile(r'「([^」]+)」|『([^』]+)』|“([^”]+)”|"([^"\n]+)"')
SLOT=re.compile(r'(?:多少|几|多久|多长)(?:个)?(?:毫秒|秒钟|分钟|小时|秒|时|天|次)?|什么|哪个|哪种|怎么')
NOISE={'多少','多久','还是','到底','应该','现在','之前','后来','事情','东西','问题','知道'}


def analyze(text,literal,tokenize):
    quotes=[next(v for v in m.groups() if v is not None) for m in QUOTES.finditer(text)]
    verbs=[m.group() for m in VERB.finditer(literal)]
    values=[re.sub(r'\s+','',m.group()) for m in QUANTITY.finditer(literal)]
    clean=SLOT.sub('',QUANTITY.sub(' ',VERB.sub(' ',literal)))
    # 调静默到多少秒: retain the separable setting verb and its literal object.
    separated=re.fullmatch(r'\s*(?:把)?([调改换设定])(.+?)([到成为])\s*',clean)
    if separated:
        verbs.append(separated[1]+separated[3]);clean=separated[2]
    words, phrases, objects = _lexical(literal)
    clean_words, clean_phrases, _ = _lexical(clean)
    topics=sorted(clean_words - NOISE - _DAY_PARTS)
    quoted=list(dict.fromkeys(quotes))
    hints=negations(literal)
    units=quoted if quoted else sorted(set(words)|set(clean_words)|set(phrases)|set(clean_phrases)|set(verbs)|set(values)|
        {hint['surface']for hint in hints}|
        {re.sub(r'\s+','',m.group()) for m in re.finditer('(?:'+VERB.pattern+r')\s*'+NUMBER+r'\s*'+UNIT,literal)})
    return dict(quoted_literals=quoted,verbs=list(dict.fromkeys(verbs)),quantities=list(dict.fromkeys(values)),
                topics=topics,units=units,object_terms=sorted(objects - _DAY_PARTS),negations=hints,
                explanation_query=bool(re.match(r'^(?:为什么|为何|怎么会)', literal)),
                setting=bool(verbs and (SLOT.search(literal) or values)))


def matches(plan,quote):
    if plan['quoted_literals']:
        return all(part in quote for part in plan['quoted_literals'])
    if not plan['setting'] or not plan['topics']:
        return False
    compact=re.sub(r'\s+','',quote)
    if not all(topic.casefold() in compact.casefold() for topic in plan['topics']):
        return False
    if plan['quantities'] and not all(v in compact for v in plan['quantities']):
        return False
    # A matched object plus an explicit setting action is a candidate, never a fact verdict.
    return bool(VERB.search(compact) or re.search(r'[调改换设定]'+NUMBER+UNIT,compact))


def annotate(result,plan):
    searched=result.get('searched')
    if isinstance(searched,dict):
        searched['evidence_tokens']=plan['units']  # Strict coverage grams stay internal, never injected as query units.
        searched['query_units']=plan['units']
        searched['query_tokens']=plan['units']
        searched['protected_verbs']=plan['verbs'];searched['quantity_units']=plan['quantities']
        if plan['quoted_literals']:searched['quoted_literals']=plan['quoted_literals']
    evidence=result.get('evidence')
    if evidence:
        result['recall_miss_reason']=None
        result['recall_diagnostics']=dict(stage='hit',route=evidence.get('match_method') or 'lexical_units',
                                        query_units=plan['units'])
    elif result['state']=='absent':
        result['recall_miss_reason']='source_not_qualified' if result.get('source_clues') else 'no_eligible_raw_hit'
        result['recall_diagnostics']=dict(stage='source_qualification' if result.get('source_clues') else 'raw_match',
                                        query_units=plan['units'],scope_applied=True)
    elif result.get('recall_miss_reason'):
        result.setdefault('recall_diagnostics',dict(stage='query_analysis',query_units=plan['units']))
    else:
        result['recall_miss_reason']=None  # A deliberately untriggered statement is not a failed search.
    return result
