#!/usr/bin/env python3
"""쇼파일 생성기 로컬 앱 서버 v2 — http://127.0.0.1:8787
데스크탑 '쇼파일 생성기.app'이 이 서버를 띄우고 브라우저를 연다.

v2: 채널시트 → DM7 + KLANG + SuperRack Performer 3종 통합.
    생성 전 '검토' 단계에서 채널 이름을 사용자 네이밍 사전(naming_vocab.json,
    실제 채널시트 129장에서 학습)과 대조 — 불일치 이름은 확인/수정 후에만 생성.
"""
import copy
import datetime
import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sheet2spec, dm7_gen, klang_gen, sprk_gen
from run_pipeline import next_version

FROZEN = getattr(sys, 'frozen', False)
RES = getattr(sys, '_MEIPASS', HERE)          # 번들 리소스 (동결 시)


def _app_version():
    """Return the release version shared by the UI and downloads."""
    try:
        with open(os.path.join(RES, 'VERSION'), encoding='utf-8') as f:
            return f.read().strip() or '0.1.0'
    except OSError:
        return '0.1.0'


APP_VERSION = _app_version()
RELEASE_CHANNEL = 'BETA'
if FROZEN:
    _cfg_dir = os.path.expanduser('~/Library/Application Support/쇼파일 생성기')
    os.makedirs(_cfg_dir, exist_ok=True)
    CFG = os.path.join(_cfg_dir, 'config.json')
else:
    CFG = os.path.join(HERE, 'config.json')
VOCAB_PATH = os.path.join(RES, 'naming_vocab.json')
SPRK_BASE = os.path.join(RES, 'base', 'sprk_base.sprk')
TEMPLATE_PATH = os.path.join(RES, 'base', '00_채널시트 템플릿.numbers')
ONLINE_TEMPLATE_PATH = os.path.join(RES, 'base', '00_채널시트_템플릿.xlsx')
OFFLINE_APP_PATH = os.path.join(RES, 'base', '쇼파일 생성기_오프라인_AppleSilicon.zip')
EXAMPLE_SHEET_PATH = os.path.join(RES, 'base', '250927_오펄스_작성예제.xlsx')
PRESETS = os.path.expanduser('~/Library/Containers/com.klang.klangapp2/Data/Library/KLANGtechnologies/Presets')
PORT = int(os.environ.get('PORT', '8787'))
STAGE = '/tmp/showfile_out'   # 로컬 스테이징 — iCloud가 느려도 생성은 즉시 완료
UPLOADS = '/tmp/showfile_uploads'


def _purge_uploads(max_age=3600):
    """약관 고지대로 업로드 임시파일을 주기 삭제 (1시간 경과분)"""
    import time as _t
    try:
        now = _t.time()
        for d in os.listdir(UPLOADS):
            path = os.path.join(UPLOADS, d)
            try:
                if now - os.path.getmtime(path) > max_age:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass
CUSTOM_DM7_BASE = os.path.join(_cfg_dir if FROZEN else HERE, 'custom_dm7_base.dm7f')


def dm7_base_path():
    return CUSTOM_DM7_BASE if os.path.isfile(CUSTOM_DM7_BASE) else None
ONLINE = os.environ.get('SHOWFILE_ONLINE', '').lower() in ('1', 'true', 'yes')
_SRV = {}                     # 실행 중 서버 (LAN 모드 전환 시 재바인드용)


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _timed(fn, timeout):
    """fn을 데몬 스레드에서 실행, timeout 내 완료 못 하면 None (iCloud 블록 가드)"""
    box = {}
    th = threading.Thread(target=lambda: box.__setitem__('v', fn()), daemon=True)
    th.start()
    th.join(timeout)
    return box.get('v')


def pick_version(base, dirs, exts, timeout=6):
    """기존 next_version과 동일하되 디렉토리 조회가 iCloud에서 블록되면 시간 접미사로 폴백"""
    def read():
        s = set()
        for d in dirs:
            try:
                s |= set(os.listdir(d))
            except OSError:
                pass
        return s
    names = _timed(read, timeout)
    if names is None:
        return base + datetime.datetime.now().strftime('_%H%M')
    def ex(n):
        return any(n + e in names for e in exts)
    if not ex(base):
        return base
    v = 2
    while ex(f'{base}_V{v}'):
        v += 1
    return f'{base}_V{v}'

# 타 업계 관용 표기 → 사용자 네이밍 family (제안용)
ALIAS = {
    'bd': 'kick', 'kik': 'kick', 'kick drum': 'kick', 'sd': 'snare top', 'sn': 'snare',
    'snr': 'snare', 'ht': 'tom', 'ft': 'tom', 'floor tom': 'tom', 'rack tom': 'tom',
    'hat': 'hh', 'hats': 'hh', 'hihat': 'hh', 'hi hat': 'hh', 'hi-hat': 'hh',
    'oh l': 'oh', 'oh r': 'oh', 'ohl': 'oh', 'ohr': 'oh', 'overhead': 'oh', 'over head': 'oh',
    'egt': 'eg', 'e.gt': 'eg', 'e gt': 'eg', 'elec gtr': 'eg', 'electric': 'eg', '일렉': 'eg',
    'agt': 'ag', 'a.gt': 'ag', 'a gt': 'ag', 'ac gtr': 'ag', 'acoustic': 'ag', '통기타': 'ag', '어쿠스틱': 'ag',
    'keys': 'key', 'keyboard': 'key', '건반': 'key', 'syn': 'synth', '신디': 'synth',
    'pno': 'piano', 'grand': 'piano', '피아노': 'piano', 'ba': 'bass', '베이스': 'bass',
    'vo': 'vox', 'voc': 'vox', 'vocal': 'vox', 'ld': 'leader', 'lead vox': 'leader',
    'mr': 'mtr', 'music': 'mtr', '반주': 'mtr', 'track': 'mtr',
    'amb': 'ambi', 'ambient': 'ambi', 'room': 'ambi',
    'talkback': 'tb', 'talk back': 'tb', '토크백': 'tb',
    '목사': 'pastor', '목사님': 'pastor', '설교': 'pastor', '설교자': 'pastor',
    '사회': 'mc', '사회자': 'mc', 'per': '타악', 'perc': '타악', '퍼커션': '타악',
    'cho': 'chorus', '코러스': 'chorus', '합창단': '합창', '드럼': 'drum',
    'gt': 'gtr', 'guitar': 'gtr', 'spk': 'speech', '멘트': 'speech',
}

# 학습 사전에 없을 때 제안하는 업계 표준 약어 (짧게, 온라인 통용 표기)
STD_ABBR = {
    # 현악
    'violin': 'Vln', '바이올린': 'Vln', 'viola': 'Vla', '비올라': 'Vla',
    'cello': 'Vc', '첼로': 'Vc', 'violoncello': 'Vc',
    'contrabass': 'Cb', 'double bass': 'Cb', '콘트라베이스': 'Cb', '더블베이스': 'Cb',
    'harp': 'Hp', '하프': 'Hp', 'strings': 'Str', '스트링': 'Str',
    # 목관
    'flute': 'Fl', '플룻': 'Fl', '플루트': 'Fl', 'piccolo': 'Picc', '피콜로': 'Picc',
    'oboe': 'Ob', '오보에': 'Ob', 'clarinet': 'Cl', '클라리넷': 'Cl',
    'bassoon': 'Bsn', '바순': 'Bsn', 'recorder': 'Rec', '리코더': 'Rec',
    # 금관
    'horn': 'Hn', '호른': 'Hn', 'french horn': 'Hn', 'trumpet': 'Tp', '트럼펫': 'Tp',
    'trombone': 'Tbn', '트롬본': 'Tbn', 'tuba': 'Tba', '튜바': 'Tba',
    # 색소폰
    'sax': 'Sax', 'saxophone': 'Sax', '색소폰': 'Sax',
    'alto sax': 'A.Sax', '알토색소폰': 'A.Sax', 'tenor sax': 'T.Sax', '테너색소폰': 'T.Sax',
    'soprano sax': 'S.Sax', 'bari sax': 'B.Sax', 'baritone sax': 'B.Sax',
    # 클래식 타악
    'timpani': 'Timp', '팀파니': 'Timp', 'vibraphone': 'Vib', '비브라폰': 'Vib',
    'marimba': 'Mar', '마림바': 'Mar', 'xylophone': 'Xyl', '실로폰': 'Xyl',
    'glockenspiel': 'Glock', 'cymbal': 'Cym', '심벌': 'Cym',
    'tambourine': 'Tamb', '탬버린': 'Tamb', 'triangle': 'Tri', '트라이앵글': 'Tri',
    # 퍼커션
    'djembe': 'Djem', '젬베': 'Djem', 'bongo': 'Bongo', '봉고': 'Bongo',
    'timbales': 'Timb', '팀발레스': 'Timb', 'shaker': 'Shkr', '쉐이커': 'Shkr',
    'cowbell': 'Cow', 'windchime': 'Chime', '윈드차임': 'Chime',
    # 건반
    'organ': 'Org', '오르간': 'Org', 'accordion': 'Acc', '아코디언': 'Acc',
    'electric piano': 'EP', '일렉피아노': 'EP', 'rhodes': 'Rhodes',
    # 기타류
    'ukulele': 'Uke', '우쿨렐레': 'Uke', 'mandolin': 'Mand', '만돌린': 'Mand',
    'banjo': 'Banjo', '밴조': 'Banjo', 'classical guitar': 'C.Gt', '클래식기타': 'C.Gt',
    # 보컬 파트
    'soprano': 'Sop', '소프라노': 'Sop', 'alto': 'Alto', '알토': 'Alto',
    'tenor': 'Ten', '테너': 'Ten', 'baritone': 'Bar', '바리톤': 'Bar',
    'choir': 'Choir', '성가대': 'Choir', '중창': 'Ens',
    # 방송·기타
    'narration': 'Nar', '나레이션': 'Nar', '내레이션': 'Nar', 'announcer': 'Ann', '아나운서': 'Ann',
    'video': 'VTR', '영상': 'VTR', 'vtr': 'VTR',
    'laptop': 'PC', '노트북': 'PC', '컴퓨터': 'PC', 'ipad': 'iPad', '아이패드': 'iPad',
    '휴대폰': 'Phone', '핸드폰': 'Phone', 'headset': 'HS', '헤드셋': 'HS',
    'lavalier': 'Lav', '라발리에': 'Lav', '핀마이크': 'Pin', 'pin mic': 'Pin',
    '무선': 'WL', 'audience': 'Aud', '관중': 'Aud', '객석': 'Aud',
}


def std_abbr_suggest(sheet_name):
    """표준 약어 제안. 앞 서수는 파트 표기로 유지, 뒤 숫자는 주자 번호(붙여쓰기).
    '1st 바이올린' → '1st Vln' / '1st 바이올린 2' → '1st Vln2' / '바이올린 3' → 'Vln3'"""
    import unicodedata
    s = unicodedata.normalize('NFC', str(sheet_name)).strip()
    s = re.sub(r'\(.*?\)', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    ORD = {1: '1st', 2: '2nd', 3: '3rd'}
    sec = ''
    m = re.match(r'^(\d+)(st|nd|rd|th)?[\s._-]+(.+)$', s, re.I)
    if m:
        n = int(m.group(1))
        sec = ORD.get(n, f'{n}th')
        s = m.group(3)
    num = ''
    m2 = re.search(r'[\s._-]*(\d+)$', s)
    if m2:
        num = m2.group(1)
        s = s[:m2.start()]
    lr = ''
    m3 = re.match(r'^(.*?)[\s._-]+([LRlr])$', s.strip())
    if m3:
        s, lr = m3.group(1), m3.group(2).upper()
    key = s.strip(' -_.').lower()
    abbr = STD_ABBR.get(key)
    if not abbr and ' ' in key:                     # "1st violin solo" 류 — 마지막 단어 시도
        abbr = STD_ABBR.get(key.split()[-1])
    if not abbr:
        return None
    out = (sec + ' ' if sec else '') + abbr + num + ((' ' + lr) if lr else '')
    return out[:12]


CHAINS = {'vocal': ['Clear Voice Live', 'CrvEqtrL', 'Pro-Q 4', 'F6-RTA'],
          'inst': ['Pro-Q 4', 'F6-RTA'],
          'none': []}
CHAIN_LABEL = {'vocal': '보컬 (CVL→Crv→Q4→F6)', 'inst': '악기 (Q4→F6)', 'none': '빈 랙'}
STEREO_UNSUPPORTED = {'Clear Voice Live'}   # 템플릿에 모노 변형만 있는 플러그인


DEFAULT_CFG = {
    'sheets_dir': os.path.expanduser('~/Desktop'),
    'recursive': False,
    'dm7_out_dir': os.path.expanduser('~/Desktop/쇼파일/DM7'),
    'klang_out_dir': os.path.expanduser('~/Desktop/쇼파일/KLANG'),
    'sprk_out_dir': os.path.expanduser('~/Desktop/쇼파일/SuperRack'),
    'klang_presets_copy': True,
    'lan_mode': False,
}


def config():
    try:
        c = json.load(open(CFG))
    except (OSError, ValueError):
        c = dict(DEFAULT_CFG)
        save_config(c)
    for k, v in DEFAULT_CFG.items():
        c.setdefault(k, v)
    return c


def save_config(c):
    json.dump(c, open(CFG, 'w'), ensure_ascii=False, indent=1)


def vocab():
    try:
        return json.load(open(VOCAB_PATH))['families']
    except Exception:
        return {}


def normalize(name):
    import unicodedata
    s = unicodedata.normalize('NFC', str(name))
    s = re.sub(r'\(.*?\)', '', s.strip().replace('\n', ' '))
    s = re.sub(r'\s+', ' ', s).strip()
    if not s:
        return ''
    low = s.lower()
    low = re.sub(r'(?<=[\s\-_])(?:[lr]|left|right)$', '', low)
    low = re.sub(r'[\s\-_]*\d+$', '', low)
    low = re.sub(r'^\d+(?!st|nd|rd|th)[\s\-_.]*', '', low)
    return low.strip(' -_.')


def name_suffix(name):
    m = re.search(r'([\s\-_]+(?:\d+|[LRlr])|\d+)$', str(name).strip())
    return (' ' + m.group(1).strip(' -_')) if m else ''


def review_name(sheet_name, vb):
    """(status, suggestion, kind) — known | suggest(vocab/abbr) | unknown"""
    fam = normalize(sheet_name)
    if not fam:
        return 'known', None, None
    if fam in vb:
        return 'known', None, None
    tgt = None
    if fam in ALIAS and ALIAS[fam] in vb:
        tgt = ALIAS[fam]
    else:
        near = difflib.get_close_matches(fam, list(vb), n=3, cutoff=0.78)
        if near:
            tgt = max(near, key=lambda k: vb[k]['count'])
    if tgt:
        rep = vb[tgt]['rep']
        suf = name_suffix(sheet_name)
        if re.match(r'^\d+(st|nd|rd|th)\b', rep, re.I) and suf.strip().isdigit():
            return 'suggest', (rep + suf.strip())[:12], 'vocab'   # 파트 표기: 1st Vln2
        return 'suggest', (rep + suf).strip()[:12], 'vocab'

    abbr = std_abbr_suggest(sheet_name)
    if abbr:
        return 'suggest', abbr, 'abbr'
    return 'unknown', None, None


def sprk_chain_auto(c):
    low = c['name'].lower()
    if 'click' in low or '클릭' in c['name']:
        return 'none'
    if re.search(r'\btb\b|^tb|tb$', low):
        return 'none'
    if c.get('group') == 'Sings':
        return 'vocal'
    if c.get('group') in ('Drums', 'Key', 'GTR', 'AG', 'Piano', 'Bass'):
        return 'inst'
    return 'none'


def apply_name_edits(chans, edits):
    """chans: [(ch,name,io)] / edits: {"ch": "new name"}"""
    out = []
    for ch, nm, io in chans:
        nm2 = edits.get(str(ch))
        out.append((ch, nm2 if nm2 is not None else nm, io))
    return out


def build_review(sheet_path, edits=None):
    chans, outputs = sheet2spec.parse(sheet_path)
    if edits:
        chans = apply_name_edits(chans, edits)
    channels, pairs = sheet2spec.classify(chans)
    mixes, mix_pairs, matrix = sheet2spec.parse_outputs(outputs)
    vb = vocab()
    sheet_names = {ch: nm for ch, nm, io in chans}
    pairmap = {}
    for a, b in pairs:
        pairmap[a] = b
        pairmap[b] = a
    second = {b for a, b in pairs}
    rows = []
    for c in channels:
        ch = c['ch']
        status, sug, sug_kind = review_name(sheet_names.get(ch, c['name']), vb)
        rows.append({
            'ch': ch, 'sheet_name': sheet_names.get(ch, ''), 'name': c['name'],
            'klang_name': c.get('klang_name', ''), 'group': c.get('group'),
            'dca': c.get('dca', []), 'status': status, 'suggestion': sug,
            'sug_kind': sug_kind,
            'pair': pairmap.get(ch), 'is_second': ch in second,
            'chain': sprk_chain_auto(c),
        })
    return {'channels': rows, 'pairs': [list(p) for p in pairs],
            'mixes': mixes, 'matrix': matrix,
            'n_unknown': sum(1 for r in rows if r['status'] == 'unknown'),
            'n_suggest': sum(1 for r in rows if r['status'] == 'suggest')}


def build_sprk_spec(spec, chain_overrides):
    pairmap = dict(tuple(p) for p in spec.get('pairs', []))
    second = {b for a, b in spec.get('pairs', [])}
    racks = []
    rack = 1
    for c in sorted(spec['channels'], key=lambda x: x['ch']):
        ch = c['ch']
        if ch in second:
            continue
        chs = [ch] + ([pairmap[ch]] if ch in pairmap else [])
        name = c['name']
        if ch in pairmap and c.get('klang_name') == 'OH':
            name = 'OH'
        elif ch in pairmap:
            m = re.match(r'^(.*?)[\s\-_]+[Ll](?:eft)?$', name)
            if m and m.group(1):
                name = m.group(1)
        kind = chain_overrides.get(str(ch), 'auto')
        if kind == 'auto':
            kind = sprk_chain_auto(c)
        chain = [pl for pl in CHAINS[kind]
                 if not (len(chs) == 2 and pl in STEREO_UNSUPPORTED)]
        racks.append({'rack': rack, 'name': name, 'ch': chs, 'chain': chain,
                      '_group': c.get('group'), '_click': ('click' in c['name'].lower()
                                                           or '클릭' in c['name'])})
        rack += 1
    assert rack - 1 <= 64, f'랙 {rack-1}개 — SuperRack 한도(64) 초과'

    # 커스텀 레이어 자동 배치 (실제 쇼파일 학습 컨벤션: Sings→Inst→Drums→Etc, 16스트립/페이지)
    PAGE_OF = {'Sings': 'Sings', 'Piano': 'Inst', 'Key': 'Inst', 'GTR': 'Inst',
               'AG': 'Inst', 'Bass': 'Inst', 'Drums': 'Drums', 'AMBI': 'Etc', None: 'Etc'}
    buckets = {'Sings': [], 'Inst': [], 'Drums': [], 'Etc': []}
    for r in racks:
        if r.pop('_click'):
            r.pop('_group', None)
            continue                      # 클릭은 레이어 제외 (라이브 중 만질 일 없음)
        buckets[PAGE_OF.get(r.pop('_group'), 'Etc')].append(r['rack'])
    layers = []
    for label in ('Sings', 'Inst', 'Drums', 'Etc'):
        items = buckets[label]
        for pi in range(0, len(items), 16):
            page = items[pi:pi + 16]
            nm = label if pi == 0 else f'{label} {pi // 16 + 1}'
            layers.append({'name': nm,
                           'tracks': [{'rack': rk, 'strip': i} for i, rk in enumerate(page)]})
    layers = layers[:4]                   # OVV1 페이지 4개 한도
    return {'racks': racks, 'layers': layers}


def generate(req):
    sheet = req['sheet_path']
    edits = req.get('edits') or {}
    chans, outputs = sheet2spec.parse(sheet)
    chans = apply_name_edits(chans, edits)
    channels, pairs = sheet2spec.classify(chans)
    mixes, mix_pairs, matrix = sheet2spec.parse_outputs(outputs)

    base_spec = sheet2spec.build_spec(sheet)      # 이름·그룹 목록 등 뼈대
    spec = dict(base_spec)
    spec.update(channels=channels, pairs=[list(p) for p in pairs],
                mixes=mixes, mix_pairs=mix_pairs, matrix=matrix)
    groups_used = [g for g in ['Drums', 'Key', 'GTR', 'AG', 'Sings', 'Piano', 'Bass', 'AMBI']
                   if any(c.get('group') == g for c in channels)]
    spec['groups'] = groups_used
    if any('AMBI' in c.get('dca', []) for c in channels):
        spec['dca_names'] = {'12': 'AMBI'}
    else:
        spec.pop('dca_names', None)

    c = config()
    dm7_dir, klang_dir, sprk_dir = c['dm7_out_dir'], c['klang_out_dir'], c['sprk_out_dir']
    os.makedirs(STAGE, exist_ok=True)
    spec['name'] = pick_version(spec['name'], [dm7_dir, klang_dir, sprk_dir, STAGE],
                                ['.dm7f', '.KLANGshow', '.sprk'])
    spec['snapshot'] = (spec.get('ascii_name') or spec['name']).replace('_', '')[:10]
    copies = []   # (staged_path, dst_dir)

    d = req.get('dm7') or {}
    if d.get('enabled'):
        s = copy.deepcopy(spec)
        if not d.get('links', True):
            s['pairs'] = []
        if not d.get('dca', True):
            for ch in s['channels']:
                ch['dca'] = []
            s.pop('dca_names', None)
        if not d.get('mix', True):
            s['mixes'], s['mix_pairs'] = {}, []
        if not d.get('matrix', True):
            s['matrix'] = {}
        sp = '/tmp/showfile_spec_dm7.json'
        json.dump(s, open(sp, 'w'), ensure_ascii=False)
        out = os.path.join(STAGE, spec['name'] + '.dm7f')
        dm7_gen.generate(sp, out, dm7_base_path())
        copies.append((out, dm7_dir))

    k = req.get('klang') or {}
    if k.get('enabled'):
        s = copy.deepcopy(spec)
        if not k.get('links', True):
            s['pairs'] = []
        s['options'] = {
            'auto_group': k.get('auto_group', True),
            'color_match': k.get('color_match', True),
            'panning': k.get('panning', True),
            'i3d': k.get('i3d', True),
            'gain_minus15': k.get('gain_minus15', True),
            'hide_unused': k.get('hide_unused', True),
        }
        sp = '/tmp/showfile_spec_klang.json'
        json.dump(s, open(sp, 'w'), ensure_ascii=False)
        out = os.path.join(STAGE, spec['name'] + '.KLANGshow')
        klang_gen.generate(sp, out)
        copies.append((out, klang_dir))
        if k.get('presets_copy', True):
            copies.append((out, PRESETS))

    p = req.get('sprk') or {}
    if p.get('enabled'):
        overrides = p.get('chains') or {}
        if not p.get('auto_chain', True):
            overrides = {str(ch['ch']): 'none' for ch in spec['channels']}
        ss = build_sprk_spec(spec, overrides)
        sp = '/tmp/showfile_spec_sprk.json'
        json.dump(ss, open(sp, 'w'), ensure_ascii=False)
        out = os.path.join(STAGE, spec['name'] + '.sprk')
        try:
            sprk_gen.generate(sp, out, SPRK_BASE)
        except (RuntimeError, AssertionError) as e:
            raise RuntimeError(f'SuperRack 생성 실패: {e}')
        copies.append((out, sprk_dir))

    # 백그라운드 iCloud 배포 — 데몬이 멈춰 있어도 앱은 즉시 응답
    done = set()
    def finisher():
        for src, dstdir in copies:
            try:
                os.makedirs(dstdir, exist_ok=True)
                shutil.copy(src, os.path.join(dstdir, os.path.basename(src)))
                done.add((src, dstdir))
            except Exception:
                pass
    if not ONLINE:
        th = threading.Thread(target=finisher, daemon=True)
        th.start()
        th.join(4)

    files = []
    for src, dstdir in copies:
        if dstdir == PRESETS:
            continue
        if ONLINE:
            files.append({'path': src, 'staged': src, 'synced': True,
                          'download': '/download/output/' + quote(os.path.basename(src))})
        else:
            files.append({'path': os.path.join(dstdir, os.path.basename(src)),
                          'staged': src, 'synced': (src, dstdir) in done})
    return {'name': spec['name'], 'files': files,
            'channels': len(spec['channels']), 'pairs': len(spec['pairs']),
            'pending': 0 if ONLINE else len(copies) - len(done)}


def choose_folder(prompt):
    try:
        r = subprocess.run(['osascript', '-e',
                            f'POSIX path of (choose folder with prompt "{prompt}")'],
                           capture_output=True, text=True, timeout=300)
        p = r.stdout.strip().rstrip('/')
        return p if r.returncode == 0 and p else None
    except Exception:
        return None


def choose_file():
    try:
        r = subprocess.run(['osascript', '-e',
                            'POSIX path of (choose file with prompt "채널시트 파일을 선택하세요")'],
                           capture_output=True, text=True, timeout=300)
        p = r.stdout.strip()
        return p if r.returncode == 0 and p else None
    except Exception:
        return None


def scan_sheets():
    c = config()
    d = c['sheets_dir']
    out = []
    try:
        for f in os.listdir(d):
            if f.lower().endswith(('.numbers', '.xlsx')) and not f.startswith('.'):
                p = os.path.join(d, f)
                out.append({'name': f, 'path': p, 'mtime': os.path.getmtime(p)})
    except OSError:
        pass
    out.sort(key=lambda x: -x['mtime'])
    return out[:40]


def save_template():
    """브라우저 격리 속성 없이 템플릿을 Downloads에 저장한다."""
    if not os.path.isfile(TEMPLATE_PATH):
        raise FileNotFoundError('채널시트 템플릿 파일을 찾을 수 없습니다.')
    downloads = os.path.expanduser('~/Downloads')
    os.makedirs(downloads, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(TEMPLATE_PATH))
    dst = os.path.join(downloads, stem + ext)
    version = 2
    while os.path.exists(dst):
        dst = os.path.join(downloads, f'{stem} {version}{ext}')
        version += 1
    shutil.copyfile(TEMPLATE_PATH, dst)
    subprocess.run(['xattr', '-c', dst], capture_output=True)
    subprocess.run(['open', '-R', dst], capture_output=True)
    return dst


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == '/':
            b = (HTML.replace('{{APP_VERSION}}', APP_VERSION)
                     .replace('{{RELEASE_CHANNEL}}', RELEASE_CHANNEL)
                     .encode())
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == '/download/template':
            template_path = ONLINE_TEMPLATE_PATH if ONLINE else TEMPLATE_PATH
            if not os.path.isfile(template_path):
                self._json({'error': '채널시트 템플릿 파일을 찾을 수 없습니다.'}, 404)
                return
            filename = os.path.basename(template_path)
            size = os.path.getsize(template_path)
            self.send_response(200)
            content_type = ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
                            if filename.lower().endswith('.xlsx')
                            else 'application/vnd.apple.numbers')
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(size))
            self.send_header(
                'Content-Disposition',
                f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with open(template_path, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        elif self.path == '/download/offline':
            if not os.path.isfile(OFFLINE_APP_PATH):
                self._json({'error': '오프라인 앱 파일을 찾을 수 없습니다.'}, 404)
                return
            filename = f'쇼파일_생성기_오프라인_v{APP_VERSION}_AppleSilicon.zip'
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(os.path.getsize(OFFLINE_APP_PATH)))
            self.send_header('Content-Disposition',
                             f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with open(OFFLINE_APP_PATH, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        elif self.path == '/download/example':
            if not os.path.isfile(EXAMPLE_SHEET_PATH):
                self._json({'error': '예제 채널시트 파일을 찾을 수 없습니다.'}, 404)
                return
            filename = os.path.basename(EXAMPLE_SHEET_PATH)
            self.send_response(200)
            self.send_header(
                'Content-Type',
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            self.send_header('Content-Length', str(os.path.getsize(EXAMPLE_SHEET_PATH)))
            self.send_header('Content-Disposition',
                             f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with open(EXAMPLE_SHEET_PATH, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        elif self.path == '/api/version':
            self._json({'version': APP_VERSION,
                        'channel': RELEASE_CHANNEL.lower()})
        elif self.path == '/api/state':
            c = config()
            ip = lan_ip() if c.get('lan_mode') else None
            self._json({'sheets': scan_sheets(), 'config': c,
                        'online': ONLINE,
                        'lan': {'on': bool(c.get('lan_mode')),
                                'url': f'http://{ip}:{PORT}' if ip else None},
                        'dm7_base': {'custom': dm7_base_path() is not None,
                                     'name': c.get('dm7_base_name', '')}})
        elif self.path == '/terms':
            try:
                md = open(os.path.join(RES, 'base', 'TERMS.md'), encoding='utf-8').read()
            except OSError:
                md = '약관 문서를 찾을 수 없습니다.'
            import html as _h
            body = _h.escape(md)
            page = ('<meta charset="utf-8"><title>이용약관 · 쇼파일 생성기</title>'
                    '<body style="max-width:760px;margin:40px auto;padding:0 20px;'
                    'font:15px/1.75 -apple-system,\'Apple SD Gothic Neo\',sans-serif;color:#0C2244">'
                    '<pre style="white-space:pre-wrap;font:inherit">' + body + '</pre></body>')
            b = page.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path.startswith('/download/output/'):
            filename = os.path.basename(unquote(self.path.split('/download/output/', 1)[1]))
            path = os.path.join(STAGE, filename)
            if not filename or not os.path.isfile(path):
                self._json({'error': '생성 파일을 찾을 수 없습니다.'}, 404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(os.path.getsize(path)))
            self.send_header('Content-Disposition',
                             f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with open(path, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        if self.path == '/api/upload':
            try:
                n = int(self.headers.get('Content-Length', 0))
                if n <= 0 or n > 30 * 1024 * 1024:
                    self._json({'error': '파일은 30MB 이하만 업로드할 수 있습니다.'}, 413)
                    return
                import cgi
                form = cgi.FieldStorage(
                    fp=self.rfile, headers=self.headers,
                    environ={'REQUEST_METHOD': 'POST',
                             'CONTENT_TYPE': self.headers.get('Content-Type', '')})
                item = form['sheet']
                filename = os.path.basename(item.filename or '')
                ext = os.path.splitext(filename)[1].lower()
                if ext not in ('.numbers', '.xlsx'):
                    self._json({'error': '.numbers 또는 .xlsx 파일만 가능합니다.'}, 400)
                    return
                os.makedirs(UPLOADS, exist_ok=True)
                _purge_uploads()
                upload_dir = tempfile.mkdtemp(prefix='upload_', dir=UPLOADS)
                path = os.path.join(upload_dir, filename)
                with open(path, 'wb') as out:
                    shutil.copyfileobj(item.file, out)
                self._json({'name': filename, 'path': path,
                            'mtime': os.path.getmtime(path)})
            except Exception as e:
                self._json({'error': f'업로드 실패: {e}'}, 500)
            return
        if self.path == '/api/upload_base':
            try:
                n = int(self.headers.get('Content-Length', 0))
                if n <= 0 or n > 10 * 1024 * 1024:
                    self._json({'error': '리셋 쇼파일은 10MB 이하만 가능합니다.'}, 413)
                    return
                import cgi
                form = cgi.FieldStorage(
                    fp=self.rfile, headers=self.headers,
                    environ={'REQUEST_METHOD': 'POST',
                             'CONTENT_TYPE': self.headers.get('Content-Type', '')})
                item = form['base']
                filename = os.path.basename(item.filename or '')
                if not filename.lower().endswith('.dm7f'):
                    self._json({'error': '.dm7f 파일만 업로드할 수 있습니다.'}, 400)
                    return
                tmp = CUSTOM_DM7_BASE + '.tmp'
                os.makedirs(os.path.dirname(tmp), exist_ok=True)
                with open(tmp, 'wb') as out:
                    shutil.copyfileobj(item.file, out)
                ok, msg, info = dm7_gen.validate_base(tmp)
                if not ok:
                    os.remove(tmp)
                    self._json({'error': msg, 'info': info}, 400)
                    return
                os.replace(tmp, CUSTOM_DM7_BASE)
                c = config()
                c['dm7_base_name'] = filename
                save_config(c)
                self._json({'ok': True, 'name': filename, 'info': info})
            except Exception as e:
                self._json({'error': f'업로드 실패: {e}'}, 500)
            return
        n = int(self.headers.get('Content-Length', 0))
        req = json.loads(self.rfile.read(n) or b'{}')
        try:
            if self.path == '/api/reset_base':
                try:
                    os.remove(CUSTOM_DM7_BASE)
                except OSError:
                    pass
                c = config()
                c.pop('dm7_base_name', None)
                save_config(c)
                self._json({'ok': True})
            elif self.path == '/api/choose':
                key = req['key']
                prompts = {'sheets_dir': '채널시트 폴더를 선택하세요',
                           'dm7_out_dir': 'DM7 쇼파일 저장 폴더를 선택하세요',
                           'klang_out_dir': '클랑 쇼파일 저장 폴더를 선택하세요',
                           'sprk_out_dir': 'SuperRack 쇼파일 저장 폴더를 선택하세요'}
                p = choose_folder(prompts[key])
                if p:
                    c = config()
                    c[key] = p
                    save_config(c)
                self._json({'path': p, 'config': config()})
            elif self.path == '/api/choose_file':
                self._json({'path': choose_file()})
            elif self.path == '/api/parse':
                self._json(build_review(req['sheet_path'], req.get('edits')))
            elif self.path == '/api/generate':
                if ONLINE:
                    self._json({
                        'error': '온라인 버전은 분석 체험만 제공합니다. '
                                 '실제 쇼파일은 오프라인 버전에서 생성해 주세요.'
                    }, 403)
                else:
                    self._json(generate(req))
            elif self.path == '/api/set_lan':
                c = config()
                c['lan_mode'] = bool(req.get('on'))
                save_config(c)
                if _SRV.get('srv'):
                    threading.Timer(0.3, _SRV['srv'].shutdown).start()
                ip = lan_ip() if c['lan_mode'] else None
                self._json({'on': c['lan_mode'],
                            'url': f'http://{ip}:{PORT}' if ip else None})
            elif self.path == '/api/open':
                subprocess.run(['open', '-R', req['path']] if os.path.isfile(req['path'])
                               else ['open', req['path']])
                self._json({'ok': True})
            elif self.path == '/api/download_template':
                self._json({'ok': True, 'path': save_template()})
            else:
                self._json({'error': 'not found'}, 404)
        except Exception as e:
            self._json({'error': str(e)}, 500)


HTML = r'''<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>쇼파일 생성기 ({{RELEASE_CHANNEL}} v{{APP_VERSION}})</title>
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyBpZD0i66Gc6rOgIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0ODgiIGhlaWdodD0iNTU1IiB2aWV3Qm94PSIwIDAgNDg4IDU1NSI+IDxwYXRoIGlkPSLrqqjslpFfMSIgZGF0YS1uYW1lPSLrqqjslpEgMSIgZmlsbD0iIzE4NzdmMiIgZmlsbC1ydWxlPSJldmVub2RkIiBkPSJNMjUzLjc3NSwzLjdjMzUuNCwyMC40NTQsMTg5LjczNiwxMDkuODMyLDIyNC4wNTksMTI5LjY2Nyw0LjUxMywzLjEyLDguMSw1LjIzMiw4Ljk0MywxNC42N1Y0MDMuOWMtMC4xMjQsOS44MjgtMi44NTcsMTUuNzE0LTExLjE3NywyMS4xTDI1Ny4yNzksNTUxLjE1N2MtOS45Myw1LjI1My0xNy44MzUsMy44Mi0yNi4yMTMtLjE3M0wxMC41NjksNDIzLjgyNkM0LjgsNDIwLjU4MywxLjIzMiw0MTYuMiwxLjQsNDA1LjkzVjE0NS40MjRhMTAuMzYyLDEwLjM2MiwwLDAsMSw1LjQ2NS05LjU2OUMzNS44MjcsMTE5LjA5MiwxOTkuNjMxLDIzLjg4NCwyMzUuODksMy4yMDcsMjQwLjc3NCwwLjgzNywyNDYuMjM1LjcsMjUzLjc3NSwzLjdaIi8+IDxwYXRoIGlkPSJBX+uzteyCrCIgZGF0YS1uYW1lPSJBIOuzteyCrCIgZmlsbD0iI2ZmZiIgZmlsbC1ydWxlPSJldmVub2RkIiBkPSJNNTQuMDU5LDE0Mi44MWMyNC40NS0xNC40NTIsMTQxLjczNS04My41NjEsMTc3LjgxNC0xMDQuODg3LDUuMTIzLTMuNzc0LDE0LjA4LTMuNjk0LDIwLjA0OS0uMDdMNDM1LjEsMTQ0LjEwOGM2LjU0LDMuNjg0LDIuODU2LDUuODc5LTEuMjYxLDguMDE3bC0zNi44MjUsMjEuMzMxYy0yLjgzNywxLjczNC01LjgyLDMuMTQ5LTExLjU4My4xNTYtMTUuNjk0LTkuMjMzLTY3Ljk5LTM5Ljk0OC02Ny45OS0zOS45NDhsLTk4Ljc1Miw1OC4zNzEsNjYuNzg0LDM5LjI4OWM0LjA2NywyLjQsMy43ODIsNi42LTIuMDMzLDkuMTkzbC0zMS43LDE4LjczOGMtNy43NjQsNS4yNDktMTMuNiwyLjU4NS0xNy43NzQuMDQyLTM0LjA2OC0xOS44ODQtMTU1LjQxNy05MC43MTMtMTc5LjktMTA1LjA2MUM0OS41MDcsMTUxLjgzNSw1MCwxNDUuMTgsNTQuMDU5LDE0Mi44MVptOTAuMDYsNS45NSw5Ny43NDQtNTguMzcxLDM1LjI2OCwyMi4xNDEtOTcuNzQ0LDU3LjM2NFoiLz4gPHBhdGggaWQ9IkFf67O17IKsXzIiIGRhdGEtbmFtZT0iQSDrs7XsgqwgMiIgZmlsbD0iI2ZmZiIgZmlsbC1ydWxlPSJldmVub2RkIiBkPSJNNDMuMDQsMTgxLjk1MmMzNy45NzksMjEuNjU1LDE1MS44Miw4Ni43LDE3OS40MzYsMTAyLjQ0OGExNS40MTUsMTUuNDE1LDAsMCwxLDcuNiwxMy40MzljLTAuMjkyLDM3LjExNS0xLjM3OSwxNzUuNi0xLjY2NywyMTIuMjQ1LTAuMDk0LDcuMzY3LTUuMjM2LDUuODc1LTguMTcxLDQuMjI3bC0zNi4wNzUtMjAuOTkzYy0zLjQzMS0yLjA2Ny01Ljc0LTMuOTgyLTYuMDYxLTkuMTU4LDAuMjY1LTE4Ljk1NSwxLjExNC03OS42NTQsMS4xMTQtNzkuNjU0TDc5LjU4NCwzNDcuN1M3OC43MSw0MDAsNzguNCw0MjIuMDE1YzAuMTYsNS4yNy0uMjM5LDEwLjIwOC01LjUzNiw3Ljc3TDM0LjYxNSw0MDcuOTc0Yy0zLTIuMDMyLTYuMDY0LTQuOTE4LTUuOTY1LTEwLjgzNiwwLjMyOS0zMi42NzksMS42NTQtMTY0LjQ3OSwyLjA5Mi0yMDguMDU1QzMwLjQzNCwxODMuNiwzNC41MzEsMTc2LjI3LDQzLjA0LDE4MS45NTJabTM3LjIzOCw3OS42MzEsOTkuMTMzLDU1LjkzNi0xLjc4Miw0MS41NTdMNzkuMzcsMzAyLjY0MloiLz4gPHBhdGggaWQ9Ilpf67O17IKsIiBkYXRhLW5hbWU9Ilog67O17IKsIiBmaWxsPSIjZmZmIiBmaWxsLXJ1bGU9ImV2ZW5vZGQiIGQ9Ik0yNjMuNjYsMjg0LjA1N0w0NTAuMDgsMTcyLjcyYzIuMTc5LTEuMTM1LDYuNzIzLS40MTQsNi4zOTIsNS4zNjMsMCwxNi4wNjcuMDI0LDQ5LjE3NiwwLjAyNCw0OS4xNzZsLTEyMC45MiwxODguMkw0NDguOTMsMzQ3LjkwOWMyLjEtMS4zLDYuMzEtMi43NjcsNi41NTksMy4xdjQwLjUyMmMwLjEsNS41MDctLjUyOCw5LjUxMy04LjAwNywxNS42QzQxNS4yLDQyNi4zNiwyOTQuNzkzLDQ5OC4wNDUsMjYzLjIxNCw1MTYuOTA1Yy03LjY2NSwzLjUtOC42MTQtLjk5NC04LjQ0Ni0zLjk3NSwwLTE1LjkzMy4xOTQtNDkuMTY3LDAuMTk0LTQ5LjE2N0wzNzYuODksMjc0LjU2LDI2Mi41NzgsMzQyLjEwOWMtNS43MDYsMi4yODgtNi42MDgtMi02LjYwOC00LjI2MVYyOTguMjM3QzI1NS45NywyOTIuNDE4LDI1NS4yNzcsMjg4LjUzMywyNjMuNjYsMjg0LjA1N1oiLz4gPC9zdmc+IA==">
<style>
/* ── AudioAZ 장비톤 (equipment tone) : 무채색 패널 · 헤어라인 · 최소 라운드 · 단일 강조색 ── */
:root{--bg:#EDF2FA;--panel:#FFFFFF;--panel2:#F1F6FD;--line:#DCE6F5;--line2:#C3D2E8;--tx:#0C2244;--tx2:#5B6C86;--mute:#8FA0BB;--acc:#1877F2;--acc-tx:#FFFFFF;--ok:#2E8B47;--warn:#B07400;--bad:#C4384A;--oktx:#22703A;--badtx:#A32A3B;--dm7:#0E5FCC;--klang:#3E9A57;--sprk:#2F93B8;--field:#FFFFFF;--tdline:#E4EBF6;--step:#5B6C86;--swoff:#D9E4F4;--chkbd:#9FB3D1}
:root[data-theme=dark]{--bg:#081729;--panel:#0E1E33;--panel2:#132A45;--line:#1F3A5C;--line2:#2B4A72;--tx:#E8F1FE;--tx2:#8FA5C4;--mute:#5E7699;--acc:#4D96F5;--acc-tx:#06132A;--ok:#52B368;--warn:#F5B93C;--bad:#F0788A;--oktx:#6FD388;--badtx:#F48E9A;--dm7:#5B8DEF;--klang:#58B368;--sprk:#45A9CF;--field:#0B1930;--tdline:#1A3253;--step:#8FA5C4;--swoff:#1F3A5C;--chkbd:#3E5A82}
html{color-scheme:light dark}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.55 -apple-system,"Apple SD Gothic Neo","Pretendard",system-ui,sans-serif;padding:0 20px 96px;-webkit-font-smoothing:antialiased}
.wrap{max-width:980px;margin:0 auto}
.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace}
/* header : 장비 앞판 */
header{display:flex;align-items:center;gap:14px;height:58px;margin:0 -20px 22px;padding:0 20px;border-bottom:1px solid var(--line);background:var(--panel)}
.logo{width:26px;height:26px;display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1px solid var(--line2);border-radius:3px;background:var(--panel2)}
.logo svg{width:16px;height:auto;display:block}
h1{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;font-size:15px;font-weight:700;color:var(--tx);letter-spacing:-.01em}
.beta-badge,.version-badge{font:600 11px/1 ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.06em;color:var(--tx2)}
.beta-badge{text-transform:uppercase}
.version-badge.kr{border-left:1px solid var(--line2);padding-left:10px;letter-spacing:.14em;color:var(--mute)}
.beta-badge::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--acc);margin-right:6px;vertical-align:1px}
.sub{font:500 11px/1 ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--mute);margin-top:4px}
.headcopy{flex:1;min-width:0}
.headtools{display:flex;align-items:center;gap:8px;margin-left:auto}
.templatebtn{display:inline-flex;align-items:center;gap:8px;height:32px;padding:0 12px;border-radius:3px;background:transparent;border:1px solid var(--line2);color:var(--tx);font:600 12px/1 inherit;cursor:pointer;white-space:nowrap;transition:.12s}
.templatebtn:hover{border-color:var(--tx2);background:var(--panel2)}
.templatebtn .filetype{font:600 10px/1 ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.06em;color:var(--tx2);border-left:1px solid var(--line2);padding-left:8px}
.themebtn{width:32px;height:32px;border-radius:3px;background:transparent;border:1px solid var(--line2);color:var(--tx2);cursor:pointer;flex-shrink:0;display:flex;align-items:center;justify-content:center}
.themebtn:hover{border-color:var(--tx2);color:var(--tx)}
/* online notice : 얇은 상태 스트립 */
.trialbanner{display:none;align-items:center;gap:14px;margin:-6px 0 20px;padding:10px 14px;border:1px solid var(--line);border-left:3px solid var(--acc);border-radius:3px;background:var(--panel)}
.trialbanner .trialicon{display:none}
.trialbanner .trialcopy{flex:1;min-width:0}.trialbanner .trialtitle{font-size:13px;font-weight:700}.trialbanner .trialdesc{margin-top:2px;color:var(--tx2);font-size:12px}
.trialactions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:6px}.exampledownload{color:var(--tx);font-size:12px;font-weight:600;text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--line2)}.exampledownload:hover{text-decoration-color:var(--tx)}
.trialdownload{display:inline-flex;align-items:center;justify-content:center;height:32px;padding:0 14px;border-radius:3px;background:var(--tx);color:var(--bg);font-size:12px;font-weight:700;text-decoration:none;white-space:nowrap}
.trialdownload:hover{opacity:.85}
.toast{position:fixed;top:14px;left:50%;z-index:20;transform:translate(-50%,-10px);padding:9px 14px;border-radius:3px;background:var(--tx);color:var(--bg);font-size:12.5px;font-weight:600;opacity:0;pointer-events:none;transition:.15s}
.toast.show{opacity:1;transform:translate(-50%,0)}
/* section label */
.step{display:flex;align-items:center;gap:10px;font:600 11px/1 ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.12em;text-transform:uppercase;color:var(--step);margin:24px 0 8px}
.step::after{content:"";flex:1;height:1px;background:var(--line)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:14px}
.searchrow{display:flex;gap:8px;margin-bottom:10px}
input[type=text]{flex:1;height:32px;background:var(--field);border:1px solid var(--line2);border-radius:3px;padding:0 10px;color:var(--tx);font-size:13px;outline:none}
input[type=text]:focus{border-color:var(--tx2)}
.btn{height:32px;background:transparent;border:1px solid var(--line2);border-radius:3px;padding:0 12px;color:var(--tx);font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap;transition:.12s}
.btn:hover{background:var(--panel2);border-color:var(--tx2)}
.list{max-height:250px;overflow-y:auto;display:flex;flex-direction:column;border:1px solid var(--line);border-radius:3px;background:var(--field)}
.sheet{padding:8px 12px;cursor:pointer;display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid var(--tdline);font-size:13px}
.sheet:last-child{border-bottom:0}
.sheet:hover{background:var(--panel2)}
.sheet.sel{background:var(--panel2);box-shadow:inset 3px 0 0 var(--acc)}
.sheet .d{color:var(--tx2);font-size:11.5px;flex-shrink:0;font-family:ui-monospace,"SF Mono",Menlo,monospace}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.out{border-radius:4px;border:1px solid var(--line);background:var(--panel);overflow:hidden;transition:.12s;border-left-width:3px}
.out.dm7{border-left-color:var(--line2)}.out.klang{border-left-color:var(--line2)}.out.sprk{border-left-color:var(--line2)}
.out.on.dm7{border-left-color:var(--dm7)}
.out.on.klang{border-left-color:var(--klang)}
.out.on.sprk{border-left-color:var(--sprk)}
.ohead{display:flex;align-items:center;gap:12px;padding:12px 14px;cursor:pointer;user-select:none}
.oicon{width:28px;height:28px;border-radius:3px;display:flex;align-items:center;justify-content:center;border:1px solid var(--line2);color:var(--tx2);background:var(--panel2)}
.on .oicon{color:var(--tx)}
.oname{font-weight:700;font-size:14px}
.odesc{font:500 11px/1.4 ui-monospace,"SF Mono",Menlo,monospace;color:var(--tx2);letter-spacing:.02em}
/* 하드웨어 슬라이드 스위치 */
.sw{margin-left:auto;width:38px;height:20px;border-radius:3px;background:var(--swoff);border:1px solid var(--line2);position:relative;transition:.12s;flex-shrink:0}
.sw::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:14px;border-radius:2px;background:var(--tx2);transition:.12s}
.on .sw{background:var(--panel2);border-color:var(--acc)}
.on .sw::after{left:18px;background:var(--acc)}
.obody{padding:0 14px 14px;display:none;border-top:1px solid var(--line)}
.on .obody{display:block}
.saverow{display:flex;align-items:center;gap:8px;background:var(--field);border:1px solid var(--line);border-radius:3px;padding:6px 10px;margin-bottom:10px}
.saverow .p{flex:1;font:500 11.5px/1.4 ui-monospace,"SF Mono",Menlo,monospace;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.saverow .btn{height:26px;padding:0 9px;font-size:11.5px}
.optt{font:600 10.5px/1 ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.12em;text-transform:uppercase;color:var(--step);margin:12px 0 6px}
.opt{display:flex;align-items:center;gap:10px;padding:6px 4px;cursor:pointer;border-radius:3px;font-size:13.5px}
.opt:hover{background:var(--panel2)}
.chk{width:16px;height:16px;border-radius:2px;border:1px solid var(--chkbd);background:var(--field);display:flex;align-items:center;justify-content:center;font-size:11px;color:transparent;flex-shrink:0;transition:.1s}
.opt.on .chk{color:var(--acc-tx);border-color:var(--acc);background:var(--acc)}
.opt .h{margin-left:auto;font:500 11px/1 ui-monospace,"SF Mono",Menlo,monospace;color:var(--tx2)}
/* bottom action bar */
.gen{position:fixed;left:0;right:0;bottom:0;padding:12px 20px calc(12px + env(safe-area-inset-bottom));background:var(--panel);border-top:1px solid var(--line)}
.genbtn{max-width:980px;margin:0 auto;display:block;width:100%;height:44px;border:1px solid var(--acc);border-radius:3px;background:var(--acc);color:var(--acc-tx);font-size:14px;font-weight:700;cursor:pointer;font-family:inherit;letter-spacing:.01em}
.genbtn:hover{filter:brightness(1.05)}
.genbtn:disabled{background:transparent;border-color:var(--line2);color:var(--mute);cursor:default;filter:none}
.result{margin-top:14px;display:none}
.rfile{display:flex;align-items:center;gap:10px;background:var(--field);border:1px solid var(--line);border-radius:3px;padding:8px 12px;margin-top:6px;font-size:13px}
.rfile .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12px}
.ok{color:var(--ok)} .err{color:var(--bad);font-size:13px;margin-top:10px;display:none}
.spin{display:inline-block;width:14px;height:14px;border:2px solid rgba(0,0,0,.2);border-top-color:currentColor;border-radius:50%;animation:r .7s linear infinite;vertical-align:-2px;margin-right:8px}
@keyframes r{to{transform:rotate(360deg)}}
.folderline{display:flex;align-items:center;gap:8px;margin-top:10px;font:500 11.5px/1.4 ui-monospace,"SF Mono",Menlo,monospace;color:var(--tx2)}
.folderline label{font-family:-apple-system,"Apple SD Gothic Neo",system-ui,sans-serif;font-size:12.5px;color:var(--tx)}
@media(max-width:680px){
  body{padding:0 12px 96px}
  header{height:auto;padding:12px;margin:0 -12px 18px;flex-wrap:wrap}
  .headtools{width:100%;margin-left:0}
  .sub{display:none}
  .templatebtn{flex:1;justify-content:center}
  .searchrow{flex-wrap:wrap}
  .searchrow input{flex-basis:100%}
  .trialbanner{align-items:flex-start;flex-wrap:wrap}.trialdownload{width:100%}
}
/* 검토 테이블 */
#review{display:none}
.sumbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;font-size:12.5px}
.pill{padding:4px 10px;border-radius:3px;background:var(--field);border:1px solid var(--line);font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:11.5px}
.pill.k{color:var(--oktx);border-color:var(--ok)} .pill.s{color:var(--warn);border-color:var(--warn)} .pill.u{color:var(--badtx);border-color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font:600 10.5px/1 ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--step);text-align:left;padding:8px;border-bottom:1px solid var(--line2);position:sticky;top:0;background:var(--panel)}
td{padding:6px 8px;border-bottom:1px solid var(--tdline);vertical-align:middle}
tr.u td{background:color-mix(in srgb,var(--bad) 7%,transparent)} tr.u td:first-child{box-shadow:inset 3px 0 0 var(--bad)}
tr.s td{background:color-mix(in srgb,var(--warn) 8%,transparent)} tr.s td:first-child{box-shadow:inset 3px 0 0 var(--warn)}
.tbl{max-height:430px;overflow-y:auto;border:1px solid var(--line);border-radius:3px;background:var(--field)}
.nmin{width:100%;min-width:110px;height:28px;background:var(--field);border:1px solid var(--line2);border-radius:3px;padding:0 8px;color:var(--tx);font-size:13px;outline:none}
.nmin:focus{border-color:var(--tx2)}
tr.u .nmin{border-color:var(--bad)}
tr.edited .nmin,tr.confirmed .nmin{border-color:var(--ok)}
.badge{display:inline-block;font:600 10.5px/1 ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:.04em;padding:4px 7px;border-radius:2px;white-space:nowrap;border:1px solid transparent}
.b-k{color:var(--oktx);border-color:var(--ok)}
.b-s{color:var(--warn);border-color:var(--warn);cursor:pointer}
.b-u{color:var(--badtx);border-color:var(--bad)}
.b-ok{color:var(--oktx);border-color:var(--ok)}
.stb{display:inline-block;font:600 10px/1 ui-monospace,"SF Mono",Menlo,monospace;padding:3px 5px;border-radius:2px;border:1px solid var(--line2);color:var(--tx2)}
.useb{font-size:11px;height:24px;padding:0 8px;border-radius:2px;background:transparent;border:1px solid var(--bad);color:var(--badtx);cursor:pointer;white-space:nowrap}
.useb:hover{background:var(--panel2)}
.chsel{height:28px;background:var(--field);border:1px solid var(--line2);border-radius:3px;color:var(--tx);font-size:12px;padding:0 6px;outline:none}
.subname{font-size:11px;color:var(--tx2)}
.parsing{padding:30px;text-align:center;color:var(--tx2);font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:12px}
</style></head><body><div class="wrap">
<header>
  <div class="logo"><svg id="로고" xmlns="http://www.w3.org/2000/svg" width="488" height="555" viewBox="0 0 488 555"> <path id="모양_1" data-name="모양 1" fill="#1877f2" fill-rule="evenodd" d="M253.775,3.7c35.4,20.454,189.736,109.832,224.059,129.667,4.513,3.12,8.1,5.232,8.943,14.67V403.9c-0.124,9.828-2.857,15.714-11.177,21.1L257.279,551.157c-9.93,5.253-17.835,3.82-26.213-.173L10.569,423.826C4.8,420.583,1.232,416.2,1.4,405.93V145.424a10.362,10.362,0,0,1,5.465-9.569C35.827,119.092,199.631,23.884,235.89,3.207,240.774,0.837,246.235.7,253.775,3.7Z"/> <path id="A_복사" data-name="A 복사" fill="#fff" fill-rule="evenodd" d="M54.059,142.81c24.45-14.452,141.735-83.561,177.814-104.887,5.123-3.774,14.08-3.694,20.049-.07L435.1,144.108c6.54,3.684,2.856,5.879-1.261,8.017l-36.825,21.331c-2.837,1.734-5.82,3.149-11.583.156-15.694-9.233-67.99-39.948-67.99-39.948l-98.752,58.371,66.784,39.289c4.067,2.4,3.782,6.6-2.033,9.193l-31.7,18.738c-7.764,5.249-13.6,2.585-17.774.042-34.068-19.884-155.417-90.713-179.9-105.061C49.507,151.835,50,145.18,54.059,142.81Zm90.06,5.95,97.744-58.371,35.268,22.141-97.744,57.364Z"/> <path id="A_복사_2" data-name="A 복사 2" fill="#fff" fill-rule="evenodd" d="M43.04,181.952c37.979,21.655,151.82,86.7,179.436,102.448a15.415,15.415,0,0,1,7.6,13.439c-0.292,37.115-1.379,175.6-1.667,212.245-0.094,7.367-5.236,5.875-8.171,4.227l-36.075-20.993c-3.431-2.067-5.74-3.982-6.061-9.158,0.265-18.955,1.114-79.654,1.114-79.654L79.584,347.7S78.71,400,78.4,422.015c0.16,5.27-.239,10.208-5.536,7.77L34.615,407.974c-3-2.032-6.064-4.918-5.965-10.836,0.329-32.679,1.654-164.479,2.092-208.055C30.434,183.6,34.531,176.27,43.04,181.952Zm37.238,79.631,99.133,55.936-1.782,41.557L79.37,302.642Z"/> <path id="Z_복사" data-name="Z 복사" fill="#fff" fill-rule="evenodd" d="M263.66,284.057L450.08,172.72c2.179-1.135,6.723-.414,6.392,5.363,0,16.067.024,49.176,0.024,49.176l-120.92,188.2L448.93,347.909c2.1-1.3,6.31-2.767,6.559,3.1v40.522c0.1,5.507-.528,9.513-8.007,15.6C415.2,426.36,294.793,498.045,263.214,516.905c-7.665,3.5-8.614-.994-8.446-3.975,0-15.933.194-49.167,0.194-49.167L376.89,274.56,262.578,342.109c-5.706,2.288-6.608-2-6.608-4.261V298.237C255.97,292.418,255.277,288.533,263.66,284.057Z"/> </svg></div>
  <div class="headcopy"><h1>쇼파일 생성기 <span class="version-badge">v{{APP_VERSION}}</span><span class="beta-badge">{{RELEASE_CHANNEL}}</span><span class="version-badge kr">MADE IN KOREA</span></h1><div class="sub">AudioAZ · Channel sheet → DM7 · KLANG · SuperRack</div></div>
  <div class="headtools">
    <button class="templatebtn" onclick="saveTemplate()"><span>채널시트 템플릿</span><span class="filetype">XLSX</span></button>
    <button class="themebtn" id="themebtn" onclick="toggleTheme()" title="라이트/다크 전환"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z"/></svg></button>
  </div>
</header>
<div class="trialbanner" id="trialbanner">
  <div class="trialicon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg></div>
  <div class="trialcopy"><div class="trialtitle">온라인 미리보기</div><div class="trialdesc">채널시트를 업로드하면 네이밍·스테레오 페어·플러그인 체인을 검토합니다. 쇼파일 생성·저장은 Mac 앱에서 진행합니다.</div><div class="trialactions"><a class="exampledownload" href="/download/example">작성 예제 XLSX (Numbers에서 열기 가능)</a></div></div>
  <a class="trialdownload" href="/download/offline">Mac 앱 다운로드</a>
</div>
<div class="toast" id="toast"></div>

<div class="step">01 &nbsp;채널시트</div>
<div class="card">
  <div class="searchrow">
    <input type="text" id="q" placeholder="시트 검색...">
    <button class="btn" onclick="chooseFile()">다른 파일...</button>
    <button class="btn" onclick="chooseDir('sheets_dir')">폴더 변경</button>
  </div>
  <input type="file" id="uploadInput" accept=".numbers,.xlsx" style="display:none" onchange="uploadSheet(this.files[0])">
  <div class="list" id="list"></div>
  <div class="folderline"><span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></span><span id="sheetsdir"></span></div>
  <div class="folderline"><span><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="2"/><path d="M7.8 8.4a6 6 0 0 0 0 7.2M16.2 8.4a6 6 0 0 1 0 7.2M4.9 5.6a10 10 0 0 0 0 12.8M19.1 5.6a10 10 0 0 1 0 12.8"/></svg></span>
    <label style="cursor:pointer;display:flex;align-items:center;gap:6px">
      <input type="checkbox" id="lanchk" onchange="setLan(this.checked)">
      같은 네트워크에서 접속 허용 (아이패드·다른 맥)</label>
    <span id="lanurl" style="font-weight:700;color:var(--tx)"></span></div>
</div>

<div id="review">
<div class="step">02 &nbsp;채널 검토 — 네이밍 확인 후 적용</div>
<div class="card">
  <div class="sumbar" id="sumbar"></div>
  <div class="tbl"><table>
    <thead><tr><th style="width:36px">CH</th><th>시트 이름</th><th style="width:24%">적용 이름</th><th>상태</th><th style="width:150px">SuperRack 체인</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table></div>
</div>
</div>

<div id="outs" style="display:none">
<div class="step" id="outputsStep">03 &nbsp;출력 · 저장 위치 · 옵션</div>
<div class="grid">
  <div class="out dm7" id="card_dm7">
    <div class="ohead" onclick="toggleOut('dm7')">
      <div class="oicon"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/></svg></div>
      <div><div class="oname">DM7 쇼파일</div><div class="odesc">.dm7f &middot; Reset 베이스</div></div>
      <div class="sw"></div>
    </div>
    <div class="obody dm7o">
      <div class="optt">저장 위치</div>
      <div class="saverow"><span class="p" id="dm7dir"></span><button class="btn" onclick="chooseDir('dm7_out_dir')">변경</button></div>
      <div class="optt">리셋 쇼파일 (베이스)</div>
      <div class="saverow"><span class="p" id="dm7base">AudioAZ 기본 쇼파일</span>
        <button class="btn" onclick="document.getElementById('basefile').click()">내 리셋 쇼파일 업로드</button>
        <button class="btn" id="baseresetbtn" style="display:none" onclick="resetBase()">기본으로</button>
        <input type="file" id="basefile" accept=".dm7f" style="display:none" onchange="uploadBase(this)"></div>
      <div class="optt">세부 옵션</div>
      <div id="dm7opts"></div>
    </div>
  </div>
  <div class="out klang" id="card_klang">
    <div class="ohead" onclick="toggleOut('klang')">
      <div class="oicon"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/></svg></div>
      <div><div class="oname">클랑 쇼파일</div><div class="odesc">.KLANGshow &middot; KOS 6</div></div>
      <div class="sw"></div>
    </div>
    <div class="obody klango">
      <div class="optt">저장 위치</div>
      <div class="saverow"><span class="p" id="klangdir"></span><button class="btn" onclick="chooseDir('klang_out_dir')">변경</button></div>
      <div class="optt">세부 옵션</div>
      <div id="klangopts"></div>
    </div>
  </div>
  <div class="out sprk" id="card_sprk">
    <div class="ohead" onclick="toggleOut('sprk')">
      <div class="oicon"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/></svg></div>
      <div><div class="oname">SuperRack 쇼파일</div><div class="odesc">.sprk &middot; Performer &middot; 디폴트 셋 베이스</div></div>
      <div class="sw"></div>
    </div>
    <div class="obody sprko">
      <div class="optt">저장 위치</div>
      <div class="saverow"><span class="p" id="sprkdir"></span><button class="btn" onclick="chooseDir('sprk_out_dir')">변경</button></div>
      <div class="optt">세부 옵션</div>
      <div id="sprkopts"></div>
    </div>
  </div>
</div>
</div>

<div class="result card" id="result"></div>
<div class="err" id="err"></div>

<footer style="margin-top:30px;padding:14px 0 6px;border-top:1px solid var(--line);font-size:11px;line-height:1.7;color:var(--tx2)">
  © 2026 AudioAZ · 무료 배포판 · <a href="/terms" target="_blank" style="color:var(--tx);font-weight:600;text-decoration:underline;text-underline-offset:3px;text-decoration-color:var(--line2)">이용약관·면책 고지</a><br>
  Yamaha·DM7, KLANG, Waves·SuperRack 등은 각 소유자의 상표이며, 본 소프트웨어는 해당 제조사와 제휴·승인 관계가 없는 독립 소프트웨어입니다.
  생성 파일은 <b>공연 전 반드시 장비에서 사전 점검</b> 후 사용하세요. 업로드 파일은 분석 후 자동 삭제됩니다.
</footer>
<div class="gen"><button class="genbtn" id="go" onclick="gen()" disabled>시트를 선택하세요</button></div>
</div>
<script>
let curTheme=localStorage.getItem('theme')||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
function applyTheme(){document.documentElement.dataset.theme=curTheme;const b=document.getElementById('themebtn');if(b)b.innerHTML=curTheme==='dark'?'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4"/></svg>':'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z"/></svg>';}
function toggleTheme(){curTheme=curTheme==='dark'?'light':'dark';localStorage.setItem('theme',curTheme);applyTheme();}
document.documentElement.dataset.theme=curTheme;
document.addEventListener('DOMContentLoaded',applyTheme);
const DM7OPTS=[["links","스테레오 링크","시트 페어 + OH 관례"],["dca","DCA 어사인","OnAir/inst/Sings/Drums/AMBI"],["mix","믹스 버스 네이밍·링크","IEM 페어 + Pan Link"],["matrix","매트릭스 네이밍","TOP/SUB/Main"]];
const KLANGOPTS=[["links","스테레오 링크",""],["auto_group","자동 그룹","최대 8개 + 정렬"],["color_match","채널 색상 = 그룹 색",""],["panning","패닝 템플릿","드럼 이미지 + 스테레오 폭"],["i3d","i3D 모드","전 믹스"],["gain_minus15","인풋 페이더 -15dB",""],["hide_unused","미사용 채널 숨김",""],["presets_copy","KLANG 앱에 자동 등록","프리셋 폴더 복사"]];
const SPRKOPTS=[["auto_chain","플러그인 체인 자동 배치","보컬/악기별 · 표에서 개별 수정"]];
const CHAIN_LABELS={auto:"자동",vocal:"보컬 체인",inst:"악기 체인",none:"빈 랙"};
let sheets=[],sel=null,busy=false,review=null,edits={},chains={},confirmed={},isOnline=false;
const st={dm7:{enabled:false},klang:{enabled:false},sprk:{enabled:false}};
DM7OPTS.forEach(o=>st.dm7[o[0]]=true);KLANGOPTS.forEach(o=>st.klang[o[0]]=true);SPRKOPTS.forEach(o=>st.sprk[o[0]]=true);

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}
function renderOpts(){
  for(const [id,opts,side] of [["dm7opts",DM7OPTS,"dm7"],["klangopts",KLANGOPTS,"klang"],["sprkopts",SPRKOPTS,"sprk"]]){
    document.getElementById(id).innerHTML=opts.map(o=>
      `<div class="opt ${st[side][o[0]]?'on':''}" data-key="${o[0]}" onclick="tOpt('${side}','${o[0]}',this)"><div class="chk">&#10003;</div>${o[1]}${o[2]?`<span class="h">${o[2]}</span>`:''}</div>`).join('');
  }
}
function tOpt(side,k,el){st[side][k]=!st[side][k];el.classList.toggle('on');}
function toggleOut(side){st[side].enabled=!st[side].enabled;document.getElementById('card_'+side).classList.toggle('on');if(side==='sprk'&&review)renderReview();updateGo();}
function unresolved(){
  if(!review)return 0;
  return review.channels.filter(r=>r.status==='unknown'&&!confirmed[r.ch]&&!(r.ch in edits)).length;
}
function updateGo(){
  const g=document.getElementById('go');
  if(isOnline){
    g.disabled=false;
    g.onclick=()=>{window.location.href='/download/offline';};
    g.innerHTML='실제 쇼파일 생성하기 &mdash; 오프라인 생성기 다운로드 (Mac용)';
    return;
  }
  const any=st.dm7.enabled||st.klang.enabled||st.sprk.enabled;
  const u=unresolved();
  g.disabled=!sel||!any||busy||!review||u>0;
  g.innerHTML=busy?'<span class="spin"></span>생성 중...':
    !sel?'시트를 선택하세요':
    !review?'시트 분석 중...':
    u>0?`확인 필요한 이름 ${u}건 &mdash; 검토 후 진행`:
    !any?'출력물을 선택하세요':
    `생성하기 &mdash; ${sel.name.replace(/\.(numbers|xlsx)$/,'')}`;
}
function renderList(){
  const q=(document.getElementById('q')?.value||'').toLowerCase();
  document.getElementById('list').innerHTML=sheets.filter(s=>s.name.toLowerCase().includes(q)).map(s=>
    `<div class="sheet ${sel&&sel.path===s.path?'sel':''}" onclick='pick(${JSON.stringify(s).replace(/'/g,"&#39;")})'>
      <span>${esc(s.name.replace(/\.(numbers|xlsx)$/,''))}</span><span class="d">${new Date(s.mtime*1000).toLocaleDateString('ko-KR')}</span></div>`).join('');
}
async function pick(s){
  sel=s;review=null;edits={};chains={};confirmed={};renderList();updateGo();
  document.getElementById('review').style.display='block';
  document.getElementById('outs').style.display='none';
  document.getElementById('tbody').innerHTML='';
  document.getElementById('sumbar').innerHTML='<div class="parsing"><span class="spin" style="border-top-color:var(--tx2)"></span> 시트 분석 중...</div>';
  try{
    const r=await (await fetch('/api/parse',{method:'POST',body:JSON.stringify({sheet_path:s.path})})).json();
    if(r.error)throw new Error(r.error);
    review=r;renderReview();
    document.getElementById('outs').style.display='block';
  }catch(e){
    document.getElementById('sumbar').innerHTML=`<span style="color:var(--bad)">분석 실패: ${esc(e.message)}</span>`;
  }
  updateGo();
}
function statusCell(r){
  const done=confirmed[r.ch]||(r.ch in edits);
  if(r.status==='known')return '<span class="badge b-k">&#10003; 일치</span>';
  if(done)return '<span class="badge b-ok">&#10003; 확인됨</span>';
  if(r.status==='suggest')return `<span class="badge b-s" title="클릭하면 적용" onclick="applySug(${r.ch})">${r.sug_kind==='abbr'?'약어 제안':'제안'}: ${esc(r.suggestion)}</span>`;
  return `<button class="useb" onclick="keepName(${r.ch})">그대로 사용</button> <span class="badge b-u">사전에 없음</span>`;
}
function rowCls(r){
  const done=confirmed[r.ch]||(r.ch in edits);
  return (r.status==='unknown'&&!done?'u ':r.status==='suggest'&&!done?'s ':'')+(r.ch in edits?'edited ':'')+(confirmed[r.ch]?'confirmed':'');
}
function renderReview(){
  const k=review.channels.filter(r=>r.status==='known').length;
  const s=review.channels.filter(r=>r.status==='suggest').length;
  const u=review.channels.filter(r=>r.status==='unknown').length;
  document.getElementById('sumbar').innerHTML=
    `<span class="pill">채널 ${review.channels.length}개 &middot; 페어 ${review.pairs.length}</span>
     <span class="pill k">&#10003; 사전 일치 ${k}</span>
     ${s?`<span class="pill s">제안 ${s}</span>`:''}
     ${u?`<span class="pill u">확인 필요 ${u}</span>`:''}
     <span class="pill" style="margin-left:auto;cursor:pointer" onclick="pick(sel)">&#8635; 다시 분석</span>`;
  document.getElementById('tbody').innerHTML=review.channels.map(r=>{
    const nm=(r.ch in edits)?edits[r.ch]:r.name;
    const chain=chains[r.ch]||'auto';
    const auto=CHAIN_LABELS[r.chain]||r.chain;
    return `<tr class="${rowCls(r)}" id="row${r.ch}">
      <td>${r.ch}${r.pair&&!r.is_second?`&ndash;${r.pair}`:''}</td>
      <td>${esc(r.sheet_name)}${r.pair&&!r.is_second?' <span class="stb">ST</span>':''}</td>
      <td><input class="nmin" maxlength="12" value="${esc(nm)}" onchange="editName(${r.ch},this.value)">
        ${r.klang_name&&r.klang_name!==r.name?`<div class="subname">KLANG: ${esc(r.klang_name)}</div>`:''}</td>
      <td>${statusCell(r)}</td>
      <td><select class="chsel" onchange="chains[${r.ch}]=this.value" ${st.sprk.enabled?'':'disabled'}>
        ${['auto','vocal','inst','none'].map(c=>`<option value="${c}" ${c===chain?'selected':''}>${c==='auto'?'자동 · '+auto:CHAIN_LABELS[c]}</option>`).join('')}
      </select></td></tr>`;
  }).join('');
}
function editName(ch,v){
  v=v.trim();
  const r=review.channels.find(x=>x.ch===ch);
  if(!v||v===r.name){delete edits[ch];}else{edits[ch]=v;}
  renderReview();updateGo();
}
function applySug(ch){
  const r=review.channels.find(x=>x.ch===ch);
  edits[ch]=r.suggestion.slice(0,12);
  renderReview();updateGo();
}
function keepName(ch){confirmed[ch]=true;renderReview();updateGo();}
async function load(){
  const r=await (await fetch('/api/state')).json();
  isOnline=!!r.online;
  sheets=r.sheets;setDirs(r.config);renderList();renderOpts();updateGo();
  if(r.online){
    document.querySelector('.searchrow').innerHTML=`<button class="btn" style="width:100%;padding:14px;font-weight:700" onclick="document.getElementById('uploadInput').click()">채널시트 업로드 (.numbers / .xlsx)</button>`;
    document.getElementById('trialbanner').style.display='flex';
    document.querySelectorAll('.folderline').forEach(el=>el.style.display='none');
    document.querySelectorAll('.saverow').forEach(el=>{
      el.style.display='none';
      if(el.previousElementSibling?.classList.contains('optt'))el.previousElementSibling.style.display='none';
    });
    document.getElementById('outputsStep').innerHTML='3 &middot; 생성 옵션 미리보기 &mdash; 실제 생성은 오프라인 버전에서';
    st.klang.presets_copy=false;
    document.querySelector('#klangopts [data-key="presets_copy"]')?.remove();
  }
  if(r.lan){document.getElementById('lanchk').checked=r.lan.on;
    document.getElementById('lanurl').textContent=r.lan.url||'';}
  if(r.dm7_base)renderBase(r.dm7_base.custom, r.dm7_base.name);
}
async function uploadSheet(file){
  if(!file)return;
  busy=true;updateGo();showToast('채널시트 업로드 중...');
  try{
    const fd=new FormData();fd.append('sheet',file,file.name);
    const r=await (await fetch('/api/upload',{method:'POST',body:fd})).json();
    if(r.error)throw new Error(r.error);
    sheets=[r];renderList();pick(r);showToast('업로드 완료');
  }catch(e){showToast(e.message);}
  busy=false;updateGo();
}
function renderBase(custom, name){
  document.getElementById('dm7base').textContent=custom?('사용자 기본 쇼파일: '+name):'AudioAZ 기본 쇼파일';
  document.getElementById('baseresetbtn').style.display=custom?'':'none';
}
async function uploadBase(inp){
  const f=inp.files[0]; if(!f)return; inp.value='';
  const fd=new FormData(); fd.append('base', f);
  const r=await (await fetch('/api/upload_base',{method:'POST',body:fd})).json();
  if(r.error){alert('리셋 쇼파일 업로드 실패: '+r.error);return;}
  renderBase(true, r.name);
}
async function resetBase(){
  await fetch('/api/reset_base',{method:'POST',body:'{}'});
  renderBase(false,'');
}
async function setLan(on){
  const r=await (await fetch('/api/set_lan',{method:'POST',body:JSON.stringify({on})})).json();
  document.getElementById('lanurl').textContent=r.url?r.url+' ← 다른 기기에서 이 주소로 접속':'';
}
function shortPath(p){p=p.replace(/^\/Users\/[^/]+/,'~').replace('/Library/Mobile Documents/com~apple~CloudDocs','/iCloud').replace('/Library/Mobile Documents/com~apple~Numbers/Documents','/iCloud Numbers');return p;}
function setDirs(c){
  document.getElementById('sheetsdir').textContent=shortPath(c.sheets_dir);
  document.getElementById('dm7dir').textContent=shortPath(c.dm7_out_dir);
  document.getElementById('klangdir').textContent=shortPath(c.klang_out_dir);
  document.getElementById('sprkdir').textContent=shortPath(c.sprk_out_dir);
}
async function chooseDir(key){
  const r=await (await fetch('/api/choose',{method:'POST',body:JSON.stringify({key})})).json();
  if(r.config){setDirs(r.config);if(key==='sheets_dir')load();}
}
async function chooseFile(){
  const r=await (await fetch('/api/choose_file',{method:'POST',body:'{}'})).json();
  if(r.path){const s={name:r.path.split('/').pop(),path:r.path,mtime:Date.now()/1000};
    document.getElementById('list').insertAdjacentHTML('afterbegin',`<div class="sheet sel"><span>${esc(s.name)}</span><span class="d">직접 선택</span></div>`);
    pick(s);}
}
function showToast(msg){
  const el=document.getElementById('toast');el.textContent=msg;el.classList.add('show');
  clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>el.classList.remove('show'),2600);
}
async function saveTemplate(){
  if(document.querySelector('.searchrow button')?.textContent.includes('채널시트 업로드')){
    window.location.href='/download/template';
    showToast('채널시트 템플릿 다운로드를 시작합니다.');
    return;
  }
  try{
    const r=await (await fetch('/api/download_template',{method:'POST',body:'{}'})).json();
    if(r.error)throw new Error(r.error);
    showToast('다운로드 완료 · '+r.path.split('/').pop());
  }catch(e){showToast('다운로드 실패 · '+e.message);}
}
async function gen(){
  busy=true;updateGo();
  document.getElementById('err').style.display='none';
  const editsStr={};for(const k in edits)editsStr[String(k)]=edits[k];
  const chainsStr={};for(const k in chains)if(chains[k]!=='auto')chainsStr[String(k)]=chains[k];
  const body={sheet_path:sel.path,edits:editsStr,dm7:st.dm7,klang:st.klang,
    sprk:{...st.sprk,chains:chainsStr}};
  try{
    const r=await (await fetch('/api/generate',{method:'POST',body:JSON.stringify(body)})).json();
    if(r.error)throw new Error(r.error);
    const res=document.getElementById('result');
    res.style.display='block';
    res.innerHTML=`<div style="font-weight:700"><span class="ok">&#10003;</span> ${esc(r.name)} &mdash; 채널 ${r.channels}개, 페어 ${r.pairs}개</div>`+
      (r.pending?`<div style="font-size:12px;color:var(--warn);margin-top:6px">iCloud 동기화 지연 &mdash; 로컬 사본은 완료, 백그라운드에서 계속 저장돼요</div>`:'')+
      r.files.map(f=>{const nm=f.path.split('/').pop();const openP=f.synced?f.path:f.staged;
      return `<div class="rfile"><span class="n">${esc(nm)}${f.synced?'':' <span style="color:var(--warn)">(로컬 사본)</span>'}</span>
      ${f.download?`<a class="btn" href="${f.download}" download>다운로드</a>`:`<button class="btn" onclick='fetch("/api/open",{method:"POST",body:JSON.stringify({path:${JSON.stringify(openP)}})})'>폴더에서 보기</button>`}</div>`}).join('');
    res.scrollIntoView({behavior:'smooth'});
  }catch(e){
    const el=document.getElementById('err');el.style.display='block';el.textContent='생성 실패: '+e.message;
  }
  busy=false;updateGo();
}
document.getElementById('q').addEventListener('input',renderList);
load();
</script></body></html>'''


def _open_browser():
    url = f'http://127.0.0.1:{PORT}'
    r = subprocess.run(['open', url], capture_output=True)
    if r.returncode != 0:                      # LaunchServices 오류 폴백
        subprocess.run(['open', '-a', 'Safari', url], capture_output=True)


def main():
    try:
        s = socket.socket()
        s.settimeout(0.3)
        if s.connect_ex(('127.0.0.1', PORT)) == 0:
            s.close()
            if FROZEN:
                _open_browser()
            return  # 이미 실행 중
        s.close()
    except Exception:
        pass
    if FROZEN:
        threading.Timer(0.7, _open_browser).start()
    while True:
        env_host = os.environ.get('SHOWFILE_HOST')
        host = env_host or ('0.0.0.0' if config().get('lan_mode') else '127.0.0.1')
        srv = ThreadingHTTPServer((host, PORT), H)
        _SRV['srv'] = srv
        srv.serve_forever()   # set_lan이 shutdown()하면 새 호스트로 재바인드
        srv.server_close()


if __name__ == '__main__':
    main()
