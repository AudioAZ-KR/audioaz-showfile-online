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
PRESETS = os.path.expanduser('~/Library/Containers/com.klang.klangapp2/Data/Library/KLANGtechnologies/Presets')
PORT = int(os.environ.get('PORT', '8787'))
STAGE = '/tmp/showfile_out'   # 로컬 스테이징 — iCloud가 느려도 생성은 즉시 완료
UPLOADS = '/tmp/showfile_uploads'
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


CHAINS = {'vocal': ['CrvEqtrL', 'Pro-Q 4', 'F6-RTA'],
          'inst': ['Pro-Q 4', 'F6-RTA'],
          'none': []}
CHAIN_LABEL = {'vocal': '보컬 (CrvEqtrL→Q4→F6)', 'inst': '악기 (Q4→F6)', 'none': '빈 랙'}


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
        racks.append({'rack': rack, 'name': name, 'ch': chs, 'chain': CHAINS[kind]})
        rack += 1
    assert rack - 1 <= 64, f'랙 {rack-1}개 — SuperRack 한도(64) 초과'
    return {'racks': racks}


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
        dm7_gen.generate(sp, out)
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
            b = HTML.encode()
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
            filename = os.path.basename(OFFLINE_APP_PATH)
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Length', str(os.path.getsize(OFFLINE_APP_PATH)))
            self.send_header('Content-Disposition',
                             f"attachment; filename*=UTF-8''{quote(filename)}")
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with open(OFFLINE_APP_PATH, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)
        elif self.path == '/api/state':
            c = config()
            ip = lan_ip() if c.get('lan_mode') else None
            self._json({'sheets': scan_sheets(), 'config': c,
                        'online': ONLINE,
                        'lan': {'on': bool(c.get('lan_mode')),
                                'url': f'http://{ip}:{PORT}' if ip else None}})
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
                upload_dir = tempfile.mkdtemp(prefix='upload_', dir=UPLOADS)
                path = os.path.join(upload_dir, filename)
                with open(path, 'wb') as out:
                    shutil.copyfileobj(item.file, out)
                self._json({'name': filename, 'path': path,
                            'mtime': os.path.getmtime(path)})
            except Exception as e:
                self._json({'error': f'업로드 실패: {e}'}, 500)
            return
        n = int(self.headers.get('Content-Length', 0))
        req = json.loads(self.rfile.read(n) or b'{}')
        try:
            if self.path == '/api/choose':
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
<title>쇼파일 생성기</title>
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyBpZD0i66Gc6rOgIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI0ODgiIGhlaWdodD0iNTU1IiB2aWV3Qm94PSIwIDAgNDg4IDU1NSI+IDxwYXRoIGlkPSLrqqjslpFfMSIgZGF0YS1uYW1lPSLrqqjslpEgMSIgZmlsbD0iIzE4NzdmMiIgZmlsbC1ydWxlPSJldmVub2RkIiBkPSJNMjUzLjc3NSwzLjdjMzUuNCwyMC40NTQsMTg5LjczNiwxMDkuODMyLDIyNC4wNTksMTI5LjY2Nyw0LjUxMywzLjEyLDguMSw1LjIzMiw4Ljk0MywxNC42N1Y0MDMuOWMtMC4xMjQsOS44MjgtMi44NTcsMTUuNzE0LTExLjE3NywyMS4xTDI1Ny4yNzksNTUxLjE1N2MtOS45Myw1LjI1My0xNy44MzUsMy44Mi0yNi4yMTMtLjE3M0wxMC41NjksNDIzLjgyNkM0LjgsNDIwLjU4MywxLjIzMiw0MTYuMiwxLjQsNDA1LjkzVjE0NS40MjRhMTAuMzYyLDEwLjM2MiwwLDAsMSw1LjQ2NS05LjU2OUMzNS44MjcsMTE5LjA5MiwxOTkuNjMxLDIzLjg4NCwyMzUuODksMy4yMDcsMjQwLjc3NCwwLjgzNywyNDYuMjM1LjcsMjUzLjc3NSwzLjdaIi8+IDxwYXRoIGlkPSJBX+uzteyCrCIgZGF0YS1uYW1lPSJBIOuzteyCrCIgZmlsbD0iI2ZmZiIgZmlsbC1ydWxlPSJldmVub2RkIiBkPSJNNTQuMDU5LDE0Mi44MWMyNC40NS0xNC40NTIsMTQxLjczNS04My41NjEsMTc3LjgxNC0xMDQuODg3LDUuMTIzLTMuNzc0LDE0LjA4LTMuNjk0LDIwLjA0OS0uMDdMNDM1LjEsMTQ0LjEwOGM2LjU0LDMuNjg0LDIuODU2LDUuODc5LTEuMjYxLDguMDE3bC0zNi44MjUsMjEuMzMxYy0yLjgzNywxLjczNC01LjgyLDMuMTQ5LTExLjU4My4xNTYtMTUuNjk0LTkuMjMzLTY3Ljk5LTM5Ljk0OC02Ny45OS0zOS45NDhsLTk4Ljc1Miw1OC4zNzEsNjYuNzg0LDM5LjI4OWM0LjA2NywyLjQsMy43ODIsNi42LTIuMDMzLDkuMTkzbC0zMS43LDE4LjczOGMtNy43NjQsNS4yNDktMTMuNiwyLjU4NS0xNy43NzQuMDQyLTM0LjA2OC0xOS44ODQtMTU1LjQxNy05MC43MTMtMTc5LjktMTA1LjA2MUM0OS41MDcsMTUxLjgzNSw1MCwxNDUuMTgsNTQuMDU5LDE0Mi44MVptOTAuMDYsNS45NSw5Ny43NDQtNTguMzcxLDM1LjI2OCwyMi4xNDEtOTcuNzQ0LDU3LjM2NFoiLz4gPHBhdGggaWQ9IkFf67O17IKsXzIiIGRhdGEtbmFtZT0iQSDrs7XsgqwgMiIgZmlsbD0iI2ZmZiIgZmlsbC1ydWxlPSJldmVub2RkIiBkPSJNNDMuMDQsMTgxLjk1MmMzNy45NzksMjEuNjU1LDE1MS44Miw4Ni43LDE3OS40MzYsMTAyLjQ0OGExNS40MTUsMTUuNDE1LDAsMCwxLDcuNiwxMy40MzljLTAuMjkyLDM3LjExNS0xLjM3OSwxNzUuNi0xLjY2NywyMTIuMjQ1LTAuMDk0LDcuMzY3LTUuMjM2LDUuODc1LTguMTcxLDQuMjI3bC0zNi4wNzUtMjAuOTkzYy0zLjQzMS0yLjA2Ny01Ljc0LTMuOTgyLTYuMDYxLTkuMTU4LDAuMjY1LTE4Ljk1NSwxLjExNC03OS42NTQsMS4xMTQtNzkuNjU0TDc5LjU4NCwzNDcuN1M3OC43MSw0MDAsNzguNCw0MjIuMDE1YzAuMTYsNS4yNy0uMjM5LDEwLjIwOC01LjUzNiw3Ljc3TDM0LjYxNSw0MDcuOTc0Yy0zLTIuMDMyLTYuMDY0LTQuOTE4LTUuOTY1LTEwLjgzNiwwLjMyOS0zMi42NzksMS42NTQtMTY0LjQ3OSwyLjA5Mi0yMDguMDU1QzMwLjQzNCwxODMuNiwzNC41MzEsMTc2LjI3LDQzLjA0LDE4MS45NTJabTM3LjIzOCw3OS42MzEsOTkuMTMzLDU1LjkzNi0xLjc4Miw0MS41NTdMNzkuMzcsMzAyLjY0MloiLz4gPHBhdGggaWQ9Ilpf67O17IKsIiBkYXRhLW5hbWU9Ilog67O17IKsIiBmaWxsPSIjZmZmIiBmaWxsLXJ1bGU9ImV2ZW5vZGQiIGQ9Ik0yNjMuNjYsMjg0LjA1N0w0NTAuMDgsMTcyLjcyYzIuMTc5LTEuMTM1LDYuNzIzLS40MTQsNi4zOTIsNS4zNjMsMCwxNi4wNjcuMDI0LDQ5LjE3NiwwLjAyNCw0OS4xNzZsLTEyMC45MiwxODguMkw0NDguOTMsMzQ3LjkwOWMyLjEtMS4zLDYuMzEtMi43NjcsNi41NTksMy4xdjQwLjUyMmMwLjEsNS41MDctLjUyOCw5LjUxMy04LjAwNywxNS42QzQxNS4yLDQyNi4zNiwyOTQuNzkzLDQ5OC4wNDUsMjYzLjIxNCw1MTYuOTA1Yy03LjY2NSwzLjUtOC42MTQtLjk5NC04LjQ0Ni0zLjk3NSwwLTE1LjkzMy4xOTQtNDkuMTY3LDAuMTk0LTQ5LjE2N0wzNzYuODksMjc0LjU2LDI2Mi41NzgsMzQyLjEwOWMtNS43MDYsMi4yODgtNi42MDgtMi02LjYwOC00LjI2MVYyOTguMjM3QzI1NS45NywyOTIuNDE4LDI1NS4yNzcsMjg4LjUzMywyNjMuNjYsMjg0LjA1N1oiLz4gPC9zdmc+IA==">
<style>
:root{--bg:#EDF2FA;--card:#ffffff;--card2:#F1F6FD;--line:#DCE6F5;--tx:#0C2244;--tx2:#5B6C86;
--dm7:#0E5FCC;--klang:#3FA24A;--sprk:#2D9BC7;--accent:#1877F2;--warn:#B07E14;--bad:#E15B68;--ok:#3FA24A;
--field:#ffffff;--swoff:#D9E4F4;--chkbd:#9FB3D1;--oktx:#2E7D38;--badtx:#C74553;--step:#46618C;--tdline:rgba(220,230,245,.7)}
:root[data-theme=dark]{--bg:#081729;--card:#0E1E33;--card2:#132A45;--line:#1F3A5C;--tx:#E8F1FE;--tx2:#8FA5C4;
--dm7:#4D96F5;--klang:#52B368;--sprk:#3FB2DE;--accent:#4D96F5;--warn:#F5B93C;--bad:#F0788A;--ok:#52B368;
--field:#0B1930;--swoff:#1F3A5C;--chkbd:#3E5A82;--oktx:#6FD388;--badtx:#F0919E;--step:#7E97BF;--tdline:rgba(31,58,92,.6)}
html{color-scheme:light dark}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:15px/1.6 -apple-system,"Apple SD Gothic Neo",sans-serif;padding:32px 20px 120px}
.wrap{max-width:980px;margin:0 auto}
header{display:flex;align-items:center;gap:16px;margin-bottom:28px;padding:22px 24px;border-radius:20px;background:radial-gradient(circle at 82% 0,rgba(77,150,245,.42),transparent 32%),linear-gradient(135deg,#07172B,#0C2852 52%,#0D58AE);box-shadow:0 14px 36px rgba(12,48,100,.28)}
.logo{width:52px;height:52px;border-radius:14px;background:#fff;display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 0 18px rgba(24,119,242,.5)}
.logo svg{width:32px;height:auto;display:block}
h1{font-size:21px;font-weight:800;color:#fff}
.sub{font-size:13px;color:#B9CDEB}
.headcopy{flex:1;min-width:0}
.headtools{display:flex;align-items:center;gap:9px;margin-left:auto}
.templatebtn{display:flex;align-items:center;gap:9px;padding:12px 17px;border-radius:12px;background:#fff;border:2px solid #fff;color:#0E5FCC;font:800 13px/1 -apple-system,"Apple SD Gothic Neo",sans-serif;text-decoration:none;white-space:nowrap;transition:.15s;cursor:pointer;box-shadow:0 5px 16px rgba(0,0,0,.22)}
.templatebtn:hover{background:#EAF3FF;border-color:#EAF3FF;transform:translateY(-2px);box-shadow:0 8px 20px rgba(0,0,0,.28)}
.templatebtn .ico{font-size:18px}
.templatebtn .filetype{font:800 9px/1 ui-monospace,SFMono-Regular,monospace;color:#fff;background:#1877F2;border-radius:5px;padding:4px 5px;letter-spacing:.04em}
.offlinebtn{display:none;align-items:center;gap:8px;padding:11px 15px;border-radius:12px;background:rgba(5,20,42,.52);border:1px solid rgba(255,255,255,.42);color:#fff;font:750 12px/1 -apple-system,"Apple SD Gothic Neo",sans-serif;text-decoration:none;white-space:nowrap;transition:.15s}
.offlinebtn:hover{background:rgba(5,20,42,.75);transform:translateY(-1px)}
.offlinebtn .os{font:800 9px/1 ui-monospace,SFMono-Regular,monospace;color:#0E5FCC;background:#fff;border-radius:5px;padding:4px 5px}
.toast{position:fixed;top:20px;left:50%;z-index:20;transform:translate(-50%,-20px);padding:11px 16px;border-radius:12px;background:#0C2244;color:#fff;font-size:13px;font-weight:700;box-shadow:0 10px 30px rgba(0,0,0,.25);opacity:0;pointer-events:none;transition:.2s}
.toast.show{opacity:1;transform:translate(-50%,0)}
.step{font-size:12px;font-weight:800;letter-spacing:.12em;color:var(--step);margin:26px 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;box-shadow:0 4px 16px rgba(12,34,68,.06)}
.searchrow{display:flex;gap:10px;margin-bottom:12px}
input[type=text]{flex:1;background:var(--field);border:1px solid var(--line);border-radius:10px;padding:9px 14px;color:var(--tx);font-size:14px;outline:none}
input[type=text]:focus{border-color:var(--accent)}
.btn{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:9px 14px;color:var(--tx);font-size:13px;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.list{max-height:250px;overflow-y:auto;display:flex;flex-direction:column;gap:4px}
.sheet{padding:9px 14px;border-radius:10px;cursor:pointer;display:flex;justify-content:space-between;gap:10px;border:1px solid transparent}
.sheet:hover{background:var(--card2)}
.sheet.sel{background:rgba(24,119,242,.08);border-color:var(--accent)}
.sheet .d{color:var(--tx2);font-size:12px;flex-shrink:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.out{border-radius:16px;border:1px solid var(--line);background:var(--card);overflow:hidden;transition:.15s;box-shadow:0 4px 16px rgba(12,34,68,.06)}
.out.on.dm7{border-color:var(--dm7)}
.out.on.klang{border-color:var(--klang)}
.out.on.sprk{border-color:var(--sprk)}
.ohead{display:flex;align-items:center;gap:12px;padding:16px 18px;cursor:pointer}
.oicon{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:19px}
.dm7 .oicon{background:rgba(14,95,204,.10);color:var(--dm7)}
.klang .oicon{background:rgba(63,162,74,.12);color:var(--klang)}
.sprk .oicon{background:rgba(45,155,199,.12);color:var(--sprk)}
.oname{font-weight:700;font-size:15px}
.odesc{font-size:12px;color:var(--tx2)}
.sw{margin-left:auto;width:44px;height:26px;border-radius:13px;background:var(--swoff);border:1px solid var(--line);position:relative;transition:.15s;flex-shrink:0}
.sw::after{content:"";position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(12,34,68,.25);transition:.15s}
.on.dm7 .sw{background:var(--dm7);border-color:var(--dm7)}
.on.klang .sw{background:var(--klang);border-color:var(--klang)}
.on.sprk .sw{background:var(--sprk);border-color:var(--sprk)}
.on .sw::after{left:20px}
.obody{padding:0 18px 16px;display:none}
.on .obody{display:block}
.saverow{display:flex;align-items:center;gap:8px;background:var(--card2);border-radius:10px;padding:8px 12px;margin-bottom:12px}
.saverow .p{flex:1;font-size:12px;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.saverow .btn{padding:5px 10px;font-size:12px}
.optt{font-size:11px;font-weight:800;letter-spacing:.1em;color:var(--step);margin:10px 0 6px}
.opt{display:flex;align-items:center;gap:10px;padding:7px 4px;cursor:pointer;border-radius:8px;font-size:14px}
.opt:hover{background:var(--card2)}
.chk{width:19px;height:19px;border-radius:6px;border:1.5px solid var(--chkbd);display:flex;align-items:center;justify-content:center;font-size:12px;color:transparent;flex-shrink:0;transition:.1s}
.opt.on .chk{color:#fff;border-color:transparent}
.dm7o .opt.on .chk{background:var(--dm7)}
.klango .opt.on .chk{background:var(--klang)}
.sprko .opt.on .chk{background:var(--sprk)}
.opt .h{margin-left:auto;font-size:11px;color:var(--tx2)}
.gen{position:fixed;left:0;right:0;bottom:0;padding:16px 20px calc(16px + env(safe-area-inset-bottom));background:linear-gradient(transparent,var(--bg) 30%)}
.genbtn{max-width:980px;margin:0 auto;display:block;width:100%;padding:16px;border:none;border-radius:14px;background:linear-gradient(135deg,#1877F2,#0E5FCC);color:#fff;font-size:16px;font-weight:800;cursor:pointer;font-family:inherit;box-shadow:0 8px 22px rgba(24,119,242,.35)}
.genbtn:disabled{opacity:.4;cursor:default;box-shadow:none}
.result{margin-top:16px;display:none}
.rfile{display:flex;align-items:center;gap:10px;background:var(--card2);border-radius:10px;padding:10px 14px;margin-top:8px;font-size:13px}
.rfile .n{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ok{color:var(--ok)} .err{color:var(--bad);font-size:13px;margin-top:10px;display:none}
.spin{display:inline-block;width:16px;height:16px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:r .7s linear infinite;vertical-align:-3px;margin-right:8px}
@keyframes r{to{transform:rotate(360deg)}}
.folderline{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:12px;color:var(--tx2)}
.themebtn{width:40px;height:40px;border-radius:11px;background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.28);font-size:17px;cursor:pointer;flex-shrink:0}
.themebtn:hover{background:rgba(255,255,255,.22)}
@media(max-width:680px){
  body{padding:18px 12px 110px}
  header{align-items:flex-start;flex-wrap:wrap}
  .headcopy{flex:1}
  .headtools{width:100%;margin-left:68px}
  .templatebtn{flex:1;justify-content:center}
  .searchrow{flex-wrap:wrap}
  .searchrow input{flex-basis:100%}
}
/* 검토 테이블 */
#review{display:none}
.sumbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px;font-size:13px}
.pill{padding:4px 12px;border-radius:20px;background:var(--card2);border:1px solid var(--line)}
.pill.k{color:var(--oktx)} .pill.s{color:var(--warn)} .pill.u{color:var(--badtx)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{font-size:11px;font-weight:800;letter-spacing:.08em;color:var(--step);text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--card)}
td{padding:6px 8px;border-bottom:1px solid var(--tdline);vertical-align:middle}
tr.u td{background:rgba(225,91,104,.05)}
tr.s td{background:rgba(245,185,60,.08)}
.tbl{max-height:430px;overflow-y:auto;border:1px solid var(--line);border-radius:12px}
.nmin{width:100%;min-width:110px;background:var(--field);border:1px solid var(--line);border-radius:8px;padding:5px 9px;color:var(--tx);font-size:13px;outline:none}
.nmin:focus{border-color:var(--accent)}
tr.u .nmin{border-color:rgba(225,91,104,.6)}
tr.edited .nmin,tr.confirmed .nmin{border-color:var(--ok)}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;white-space:nowrap}
.b-k{background:rgba(63,162,74,.14);color:var(--oktx)}
.b-s{background:rgba(245,185,60,.16);color:var(--warn);cursor:pointer}
.b-u{background:rgba(225,91,104,.14);color:var(--badtx)}
.b-ok{background:rgba(63,162,74,.14);color:var(--oktx)}
.stb{display:inline-block;font-size:10px;font-weight:800;padding:1px 6px;border-radius:6px;background:rgba(24,119,242,.12);color:var(--dm7)}
.useb{font-size:11px;padding:3px 9px;border-radius:8px;background:var(--field);border:1px solid var(--bad);color:var(--badtx);cursor:pointer;white-space:nowrap}
.useb:hover{background:rgba(225,91,104,.08)}
.chsel{background:var(--field);border:1px solid var(--line);border-radius:8px;color:var(--tx);font-size:12px;padding:4px 6px;outline:none}
.subname{font-size:11px;color:var(--tx2)}
.parsing{padding:30px;text-align:center;color:var(--tx2)}
</style></head><body><div class="wrap">
<header>
  <div class="logo"><svg id="로고" xmlns="http://www.w3.org/2000/svg" width="488" height="555" viewBox="0 0 488 555"> <path id="모양_1" data-name="모양 1" fill="#1877f2" fill-rule="evenodd" d="M253.775,3.7c35.4,20.454,189.736,109.832,224.059,129.667,4.513,3.12,8.1,5.232,8.943,14.67V403.9c-0.124,9.828-2.857,15.714-11.177,21.1L257.279,551.157c-9.93,5.253-17.835,3.82-26.213-.173L10.569,423.826C4.8,420.583,1.232,416.2,1.4,405.93V145.424a10.362,10.362,0,0,1,5.465-9.569C35.827,119.092,199.631,23.884,235.89,3.207,240.774,0.837,246.235.7,253.775,3.7Z"/> <path id="A_복사" data-name="A 복사" fill="#fff" fill-rule="evenodd" d="M54.059,142.81c24.45-14.452,141.735-83.561,177.814-104.887,5.123-3.774,14.08-3.694,20.049-.07L435.1,144.108c6.54,3.684,2.856,5.879-1.261,8.017l-36.825,21.331c-2.837,1.734-5.82,3.149-11.583.156-15.694-9.233-67.99-39.948-67.99-39.948l-98.752,58.371,66.784,39.289c4.067,2.4,3.782,6.6-2.033,9.193l-31.7,18.738c-7.764,5.249-13.6,2.585-17.774.042-34.068-19.884-155.417-90.713-179.9-105.061C49.507,151.835,50,145.18,54.059,142.81Zm90.06,5.95,97.744-58.371,35.268,22.141-97.744,57.364Z"/> <path id="A_복사_2" data-name="A 복사 2" fill="#fff" fill-rule="evenodd" d="M43.04,181.952c37.979,21.655,151.82,86.7,179.436,102.448a15.415,15.415,0,0,1,7.6,13.439c-0.292,37.115-1.379,175.6-1.667,212.245-0.094,7.367-5.236,5.875-8.171,4.227l-36.075-20.993c-3.431-2.067-5.74-3.982-6.061-9.158,0.265-18.955,1.114-79.654,1.114-79.654L79.584,347.7S78.71,400,78.4,422.015c0.16,5.27-.239,10.208-5.536,7.77L34.615,407.974c-3-2.032-6.064-4.918-5.965-10.836,0.329-32.679,1.654-164.479,2.092-208.055C30.434,183.6,34.531,176.27,43.04,181.952Zm37.238,79.631,99.133,55.936-1.782,41.557L79.37,302.642Z"/> <path id="Z_복사" data-name="Z 복사" fill="#fff" fill-rule="evenodd" d="M263.66,284.057L450.08,172.72c2.179-1.135,6.723-.414,6.392,5.363,0,16.067.024,49.176,0.024,49.176l-120.92,188.2L448.93,347.909c2.1-1.3,6.31-2.767,6.559,3.1v40.522c0.1,5.507-.528,9.513-8.007,15.6C415.2,426.36,294.793,498.045,263.214,516.905c-7.665,3.5-8.614-.994-8.446-3.975,0-15.933.194-49.167,0.194-49.167L376.89,274.56,262.578,342.109c-5.706,2.288-6.608-2-6.608-4.261V298.237C255.97,292.418,255.277,288.533,263.66,284.057Z"/> </svg> </div>
  <div class="headcopy"><h1>쇼파일 생성기</h1><div class="sub">AudioAZ &middot; 채널시트 &rarr; DM7 &middot; KLANG &middot; SuperRack</div></div>
  <div class="headtools">
    <button class="templatebtn" onclick="saveTemplate()"><span class="ico">&#11015;</span><span>채널시트 템플릿 다운받기</span><span class="filetype">XLSX</span></button>
    <a class="offlinebtn" href="/download/offline"><span>&#128187;</span><span>오프라인 버전 다운로드</span><span class="os">MAC</span></a>
    <button class="themebtn" id="themebtn" onclick="toggleTheme()" title="라이트/다크 전환">&#127769;</button>
  </div>
</header>
<div class="toast" id="toast"></div>

<div class="step">1 &middot; 채널시트 선택</div>
<div class="card">
  <div class="searchrow">
    <input type="text" id="q" placeholder="시트 검색...">
    <button class="btn" onclick="chooseFile()">다른 파일...</button>
    <button class="btn" onclick="chooseDir('sheets_dir')">폴더 변경</button>
  </div>
  <input type="file" id="uploadInput" accept=".numbers,.xlsx" style="display:none" onchange="uploadSheet(this.files[0])">
  <div class="list" id="list"></div>
  <div class="folderline"><span>&#128193;</span><span id="sheetsdir"></span></div>
  <div class="folderline"><span>&#128225;</span>
    <label style="cursor:pointer;display:flex;align-items:center;gap:6px">
      <input type="checkbox" id="lanchk" onchange="setLan(this.checked)">
      같은 네트워크에서 접속 허용 (아이패드·다른 맥)</label>
    <span id="lanurl" style="font-weight:700;color:var(--accent)"></span></div>
</div>

<div id="review">
<div class="step">2 &middot; 채널 검토 &mdash; 내 네이밍과 다른 이름은 확인 후 적용</div>
<div class="card">
  <div class="sumbar" id="sumbar"></div>
  <div class="tbl"><table>
    <thead><tr><th style="width:36px">CH</th><th>시트 이름</th><th style="width:24%">적용 이름</th><th>상태</th><th style="width:150px">SuperRack 체인</th></tr></thead>
    <tbody id="tbody"></tbody>
  </table></div>
</div>
</div>

<div id="outs" style="display:none">
<div class="step" id="outputsStep">3 &middot; 출력 &middot; 저장 위치 &middot; 세부 옵션</div>
<div class="grid">
  <div class="out dm7" id="card_dm7">
    <div class="ohead" onclick="toggleOut('dm7')">
      <div class="oicon">&#127899;</div>
      <div><div class="oname">DM7 쇼파일</div><div class="odesc">.dm7f &middot; Reset 베이스</div></div>
      <div class="sw"></div>
    </div>
    <div class="obody dm7o">
      <div class="optt">저장 위치</div>
      <div class="saverow"><span class="p" id="dm7dir"></span><button class="btn" onclick="chooseDir('dm7_out_dir')">변경</button></div>
      <div class="optt">세부 옵션</div>
      <div id="dm7opts"></div>
    </div>
  </div>
  <div class="out klang" id="card_klang">
    <div class="ohead" onclick="toggleOut('klang')">
      <div class="oicon">&#127927;</div>
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
      <div class="oicon">&#127898;</div>
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

<div class="gen"><button class="genbtn" id="go" onclick="gen()" disabled>시트를 선택하세요</button></div>
</div>
<script>
let curTheme=localStorage.getItem('theme')||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
function applyTheme(){document.documentElement.dataset.theme=curTheme;const b=document.getElementById('themebtn');if(b)b.innerHTML=curTheme==='dark'?'&#9728;&#65039;':'&#127769;';}
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
    document.querySelector('.offlinebtn').style.display='flex';
    document.querySelectorAll('.folderline').forEach(el=>el.style.display='none');
    document.querySelectorAll('.saverow').forEach(el=>{
      el.style.display='none';
      if(el.previousElementSibling?.classList.contains('optt'))el.previousElementSibling.style.display='none';
    });
    document.getElementById('outputsStep').innerHTML='3 &middot; 출력 &middot; 세부 옵션';
    st.klang.presets_copy=false;
    document.querySelector('#klangopts [data-key="presets_copy"]')?.remove();
  }
  if(r.lan){document.getElementById('lanchk').checked=r.lan.on;
    document.getElementById('lanurl').textContent=r.lan.url||'';}
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
      (r.pending?`<div style="font-size:12px;color:var(--warn);margin-top:6px">&#9203; iCloud 동기화 지연 &mdash; 로컬 사본은 완료, 백그라운드에서 계속 저장돼요</div>`:'')+
      r.files.map(f=>{const nm=f.path.split('/').pop();const openP=f.synced?f.path:f.staged;
      return `<div class="rfile"><span>${nm.endsWith('.dm7f')?'&#127899;':nm.endsWith('.sprk')?'&#127898;':'&#127927;'}</span><span class="n">${esc(nm)}${f.synced?'':' <span style="color:var(--warn)">(로컬 사본)</span>'}</span>
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
