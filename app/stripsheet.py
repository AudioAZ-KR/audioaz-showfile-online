#!/usr/bin/env python3
"""스트립 채널시트(공용 양식) — WING·DiGiCo·DM7 공용. 양식 작성(write_template)과 읽기(parse).

양식의 생각: 한 줄 = 콘솔의 채널 스트립 1개. 스테레오는 'ST' 한 칸(스트립 1개가 포트 2개를 씀).
이름만 적으면 입력 장비·포트·DCA·색은 자동으로 채워진다. 달라지는 곳만 적는다.
  python3 stripsheet.py 양식.numbers [--example]     양식 파일 만들기
  python3 stripsheet.py --read 작성본.numbers          읽은 결과(JSON) 확인
"""
import json, re, sys, unicodedata

FORMAT = '스트립 채널시트 v1'
N_IN, N_BUS, N_MAIN, N_MTX, N_DCA, N_MUTE, N_DIRECT = 64, 24, 4, 8, 16, 8, 8
T_SET, T_IN, T_BUS, T_MM, T_GRP, T_DIR, T_HELP = '설정', '입력 채널', '버스', '메인·매트릭스', 'DCA·뮤트그룹', '직결 라우팅', '작성법'
IN_COLS = ['번호', '이름', 'ST', '입력 장비', '포트', 'DCA', '뮤트', '색', '마이크·메모']
BUS_COLS = ['번호', '이름', '종류', 'ST', '출력 장비', '포트', '메모']
MM_COLS = ['구분', '이름', 'ST', '출력 장비', '포트', '메모']
GRP_COLS = ['구분', '이름']
DIR_COLS = ['출력 장비', '출력 포트', '소스 장비', '소스 포트', '메모']
SETTINGS = [
    ('행사명', '', '파일 이름이 됩니다 · 예: 261120_G2A'),
    ('콘솔', 'WING', 'WING / DiGiCo / DM7 — 만들 쇼파일'),
    ('기본 입력 장비', 'CRD', "입력 채널 표의 '입력 장비'를 비운 줄에 적용 · WING: LCL / A / B / C / CRD(단테 카드) · DiGiCo: 랙 이름"),
    ('IEM 센드', '-20', '종류가 IEM인 버스로 가는 채널 센드 기본값(dB) · 비우면 건드리지 않음'),
    ('DCA 자동', '예', '예 = DCA 칸이 빈 채널을 이름 보고 자동 배정 · 아니오 = 적은 것만'),
]
HELP = [
    ('기본', '한 줄 = 콘솔 채널 스트립 1개. 이름을 적은 줄만 사용하고, 빈 줄은 건너뜁니다(번호는 그대로 유지).'),
    ('이름', '영문·숫자 권장(WING 16자). 한글은 로마자로 바뀝니다.'),
    ('ST', '스테레오 채널이면 ST. 스트립은 1개, 포트는 2개(L·R 연속)를 씁니다. 모노는 비움.'),
    ('입력 장비', '비우면 윗줄과 같은 장비(맨 위는 설정의 기본 입력 장비). 바뀌는 줄에만 적습니다.'),
    ('포트', '비우면 윗줄 다음 번호가 자동으로 들어갑니다(ST는 2칸). 건너뛸 때만 숫자를 적습니다. WING의 ST는 홀수 포트에서 시작.'),
    ('DCA', '번호를 쉼표로: 1,3 · 비우면 자동(설정) · 없음은 -'),
    ('뮤트', '뮤트그룹 번호를 쉼표로: 1,2 · 비우면 없음'),
    ('색', '비우면 악기 종류로 자동. 지정: 빨강 주황 노랑 올리브 초록 청록 파랑 남색 코랄 분홍 보라 회색 (또는 1~12)'),
    ('버스 종류', 'IEM(인이어) / MON(웨지) / FX / GRP(서브그룹). IEM은 스테레오, MON은 모노가 기본 — 다르면 ST 칸에 ST 또는 M.'),
    ('출력 포트', '장비와 포트: 1 또는 1-2. 여러 곳이면 버스 줄을 복제하지 말고 직결 라우팅 표를 쓰세요.'),
    ('직결 라우팅', '믹스를 거치지 않고 입력을 출력으로 그대로 넘길 때. 예: 출력 C 1-48 ← 소스 CRD 1-48 (단테 → AES50 → DP48)'),
    ('콘솔별 장비 이름', 'WING: LCL(로컬) AUX A B C(AES50) CRD(카드·단테) USB · DiGiCo: 세션의 랙 이름 그대로(예: SD-Rack 1, Local, MADI 1) · DM7: 채널 번호 그대로라 비워도 됩니다.'),
    ('행이 모자라면', '표 아래에 행을 추가하고 번호를 이어 적으면 됩니다. 표·열 이름은 바꾸지 마세요.'),
]
EXAMPLE_IN = [  # (이름, ST, 장비, 포트, DCA, 뮤트, 색, 메모)
    ('Kick', '', '', '', '', '', '', 'Beta91'), ('Snare', '', '', '', '', '', '', 'SM57'), ('HH', '', '', '', '', '', '', ''),
    ('Tom1', '', '', '', '', '', '', ''), ('Tom2', '', '', '', '', '', '', ''), ('OH', 'ST', '', '7', '', '', '', 'C414 x2 — 6번 포트 건너뜀'),
    ('Bass', '', '', '', '', '', '', 'DI'), ('EG', 'ST', '', '11', '', '', '', ''), ('Key 1', 'ST', '', '', '', '', '', ''),
    ('Piano', 'ST', '', '', '', '', '', ''), ('AG', '', '', '', '', '', '', ''), ('MTR', 'ST', '', '', '', '', '', ''),
    ('Click', '', '', '', '-', '', '', ''), ('Vox 1', '', 'A', '1', '', '1', '', 'AES50 A 스테이지박스'), ('Vox 2', '', '', '', '', '1', '', ''),
    ('Vox 3', '', '', '', '', '1', '', ''), ('Leader', '', '', '', '1,3', '1', '빨강', ''), ('Pastor', '', '', '', '', '2', '', ''),
    ('TB', '', 'LCL', '1', '-', '', '', '토크백'),
]
EXAMPLE_BUS = [('IEM Leader', 'IEM', '', 'A', '1-2', ''), ('IEM Vox', 'IEM', '', 'A', '3-4', ''), ('IEM Band', 'IEM', '', 'A', '5-6', ''),
               ('MON Drum', 'MON', '', 'A', '7', ''), ('MON Front', 'MON', '', 'A', '8', ''), ('Reverb', 'FX', 'ST', '', '', '')]
EXAMPLE_MM = {'MAIN 1': ('Main LR', '', 'LCL', '7-8', ''), 'MTX 1': ('Stream', '', 'CRD', '1-2', ''), 'MTX 2': ('Lobby', 'M', 'LCL', '1', '')}
EXAMPLE_DIR = [('C', '1-48', 'CRD', '1-48', '단테 → DP48 (AES50 C)')]
DEFAULT_DCA = {1: 'OnAir', 2: 'inst', 3: 'Sings', 4: 'Drums'}
COLORS = {'회색': 1, '파랑': 2, '남색': 3, '청록': 4, '초록': 5, '올리브': 6, '노랑': 7, '주황': 8,
          '빨강': 9, '코랄': 10, '분홍': 11, '보라': 12}


# ───────────────────────────── 양식 만들기

def write_template(path, example=False):
    from numbers_parser import Document, Style, RGB, Alignment, HorizontalJustification as HJ, VerticalJustification as VJ
    doc = Document(sheet_name='입력', table_name=T_SET, num_rows=len(SETTINGS) + 1, num_cols=3,
                   num_header_rows=1, num_header_cols=1)
    mid = Alignment(HJ.LEFT, VJ.MIDDLE)
    ctr = Alignment(HJ.CENTER, VJ.MIDDLE)
    S = {
        'head': doc.add_style(name='AZ Head', bold=True, font_size=11.0, font_color=RGB(255, 255, 255), bg_color=RGB(12, 34, 68), alignment=ctr),
        'key': doc.add_style(name='AZ Key', bold=True, font_size=11.0, font_color=RGB(12, 34, 68), bg_color=RGB(225, 234, 247), alignment=ctr),
        'fill': doc.add_style(name='AZ Fill', font_size=12.0, font_color=RGB(12, 34, 68), bg_color=RGB(255, 251, 230), alignment=mid),
        'fillc': doc.add_style(name='AZ FillC', font_size=12.0, font_color=RGB(12, 34, 68), bg_color=RGB(255, 251, 230), alignment=ctr),
        'opt': doc.add_style(name='AZ Opt', font_size=11.0, font_color=RGB(60, 78, 105), alignment=ctr),
        'note': doc.add_style(name='AZ Note', font_size=10.0, font_color=RGB(110, 124, 146), alignment=mid),
    }

    def table(sheet, name, cols, nrows, widths, y, first=None):
        t = first or sheet.add_table(name, x=0, y=y, num_rows=nrows + 1, num_cols=len(cols),
                                     num_header_rows=1, num_header_cols=1)
        for c, (h, w) in enumerate(zip(cols, widths)):
            t.write(0, c, h, style=S['head'])
            t.col_width(c, w)
        t.row_height(0, 26)
        for r in range(1, nrows + 1):
            t.row_height(r, 24)
        return t

    def put(t, r, c, v, style):
        t.write(r, c, v if v != '' else '', style=S[style])

    # 시트 1: 설정 + 입력 채널
    sh = doc.sheets[0]
    t = table(sh, T_SET, ['항목', '값', '설명'], len(SETTINGS), [130, 170, 620], 0, first=sh.tables[0])
    for r, (k, v, d) in enumerate(SETTINGS, 1):
        if example and k == '행사명':
            v = '261120_Example'
        put(t, r, 0, k, 'key'); put(t, r, 1, v, 'fillc'); put(t, r, 2, d, 'note')
    y = (len(SETTINGS) + 1) * 26 + 70
    t = table(sh, T_IN, IN_COLS, N_IN, [56, 190, 50, 110, 60, 80, 60, 70, 244], y)
    for r in range(1, N_IN + 1):
        row = EXAMPLE_IN[r - 1] if example and r <= len(EXAMPLE_IN) else ('',) * 8
        put(t, r, 0, str(r), 'key')
        put(t, r, 1, row[0], 'fill')
        for c in range(2, 8):
            put(t, r, c, row[c - 1], 'fillc' if c == 2 else 'opt')
        put(t, r, 8, row[7], 'note')

    # 시트 2: 출력
    doc.add_sheet('출력', T_BUS, num_rows=N_BUS + 1, num_cols=len(BUS_COLS))
    sh = doc.sheets[1]
    t = table(sh, T_BUS, BUS_COLS, N_BUS, [56, 190, 70, 50, 110, 70, 300], 0, first=sh.tables[0])
    for r in range(1, N_BUS + 1):
        row = EXAMPLE_BUS[r - 1] if example and r <= len(EXAMPLE_BUS) else ('',) * 6
        put(t, r, 0, str(r), 'key'); put(t, r, 1, row[0], 'fill'); put(t, r, 2, row[1], 'fillc')
        for c in (3, 4, 5):
            put(t, r, c, row[c - 1], 'opt')
        put(t, r, 6, row[5], 'note')
    y = (N_BUS + 1) * 26 + 70
    keys = [f'MAIN {i}' for i in range(1, N_MAIN + 1)] + [f'MTX {i}' for i in range(1, N_MTX + 1)]
    t = table(sh, T_MM, MM_COLS, len(keys), [76, 190, 50, 110, 70, 350], y)
    for r, k in enumerate(keys, 1):
        row = EXAMPLE_MM.get(k, ('',) * 5) if example else ('',) * 5
        put(t, r, 0, k, 'key'); put(t, r, 1, row[0], 'fill')
        for c in (2, 3, 4):
            put(t, r, c, row[c - 1], 'opt')
        put(t, r, 5, row[4], 'note')
    y += (len(keys) + 1) * 26 + 70
    keys = [f'DCA {i}' for i in range(1, N_DCA + 1)] + [f'MUTE {i}' for i in range(1, N_MUTE + 1)]
    t = table(sh, T_GRP, GRP_COLS, len(keys), [76, 190], y)
    for r, k in enumerate(keys, 1):
        put(t, r, 0, k, 'key')
        n = int(k.split()[1])
        v = DEFAULT_DCA.get(n, '') if k.startswith('DCA') else ''
        if example and k == 'MUTE 1':
            v = 'Vocals'
        if example and k == 'MUTE 2':
            v = 'Speech'
        put(t, r, 1, v, 'fill')
    y += (len(keys) + 1) * 26 + 70
    t = table(sh, T_DIR, DIR_COLS, N_DIRECT, [110, 90, 110, 90, 446], y)
    for r in range(1, N_DIRECT + 1):
        row = EXAMPLE_DIR[r - 1] if example and r <= len(EXAMPLE_DIR) else ('',) * 5
        for c in range(4):
            put(t, r, c, row[c], 'opt' if c else 'key')
        put(t, r, 4, row[4], 'note')

    # 시트 3: 작성법
    doc.add_sheet('작성법', T_HELP, num_rows=len(HELP) + 1, num_cols=2)
    sh = doc.sheets[2]
    t = table(sh, T_HELP, ['칸', '쓰는 법'], len(HELP), [150, 770], 0, first=sh.tables[0])
    for r, (k, d) in enumerate(HELP, 1):
        put(t, r, 0, k, 'key'); put(t, r, 1, d, 'note')
        t.row_height(r, 34)
    doc.save(path)


# ───────────────────────────── 읽기

def _s(v):
    if v is None:
        return ''
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return unicodedata.normalize('NFC', str(v)).replace('\n', ' ').strip()


def _nums(s):
    """'1,3' '1 3' '1-3' → [1,3] / [1,2,3]"""
    out = []
    for part in re.split(r'[,\s/]+', _s(s)):
        m = re.fullmatch(r'(\d+)\s*[-~]\s*(\d+)', part)
        if m:
            out += list(range(int(m.group(1)), int(m.group(2)) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def _tables(path):
    from numbers_parser import Document
    doc = Document(path)
    out = {}
    for sh in doc.sheets:
        for t in sh.tables:
            rows = [[_s(c) for c in r] for r in t.rows(values_only=True)]
            out[unicodedata.normalize('NFC', t.name).strip()] = rows
    return out


def _rows(tables, name, cols):
    """표를 {열이름: 값} 목록으로. 열 순서가 바뀌어도 머리글로 찾는다."""
    rows = tables.get(name)
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]
    idx = {c: head.index(c) for c in cols if c in head}
    return [{c: (r[i] if i < len(r) else '') for c, i in idx.items()} for r in rows[1:]]


def is_strip_sheet(path):
    try:
        t = _tables(path)
    except Exception:
        return False
    return T_IN in t and T_SET in t


def parse(path):
    """작성본 → 콘솔 중립 모델. warnings에 자동 처리·무시한 내용을 남긴다."""
    tb = _tables(path)
    if T_IN not in tb:
        raise RuntimeError(f"'{T_IN}' 표를 찾지 못했습니다 — 스트립 채널시트 양식이 맞는지 확인해 주세요")
    warn = []
    setting = {r['항목']: r['값'] for r in _rows(tb, T_SET, ['항목', '값']) if r.get('항목')}
    default_dev = (setting.get('기본 입력 장비') or 'LCL').upper()
    iem = setting.get('IEM 센드', '')
    model = {
        'format': FORMAT,
        'name': setting.get('행사명', ''),
        'console': (setting.get('콘솔') or 'WING').upper(),
        'default_device': default_dev,
        'iem_send_db': float(iem) if re.fullmatch(r'-?\d+(\.\d+)?', iem) else None,
        'dca_auto': not setting.get('DCA 자동', '예').startswith('아니'),
        'strips': [], 'buses': [], 'mains': [], 'mtx': [], 'dca_names': {}, 'mute_names': {}, 'direct': [],
    }

    dev, nxt = default_dev, {}
    for r in _rows(tb, T_IN, IN_COLS):
        if not r.get('이름') or not r.get('번호', '').isdigit():
            continue
        st = r.get('ST', '').upper() in ('ST', 'S', 'STEREO', '스테레오', 'O', 'Y')
        if r.get('입력 장비'):
            dev = r['입력 장비'].upper() if len(r['입력 장비']) <= 4 else r['입력 장비']
        ports = _nums(r.get('포트'))
        port = ports[0] if ports else nxt.get(dev, 1)
        if st and not ports and port % 2 == 0 and model['console'] == 'WING':
            port += 1                                   # WING 스테레오 소스는 홀수 포트에서 시작
            warn.append(f"{r['번호']}번 {r['이름']}(ST): WING 스테레오는 홀수 포트에서 시작해 {dev} {port}번으로 배정했습니다")
        nxt[dev] = port + (2 if st else 1)
        dca = r.get('DCA', '')
        col = r.get('색', '')
        model['strips'].append({
            'n': int(r['번호']), 'name': r['이름'], 'stereo': st, 'device': dev, 'port': port,
            'dca': None if dca == '' else _nums(dca),          # None = 자동, [] = 없음
            'mute': _nums(r.get('뮤트')),
            'color': COLORS.get(col, int(col) if col.isdigit() else None),
            'memo': r.get('마이크·메모', ''),
        })

    def outs(r):
        d = r.get('출력 장비', '')
        d = d.upper() if 0 < len(d) <= 4 else d
        return {'device': d, 'ports': _nums(r.get('포트'))} if d else None

    for r in _rows(tb, T_BUS, BUS_COLS):
        if not r.get('이름') or not r.get('번호', '').isdigit():
            continue
        kind = (r.get('종류') or 'MON').upper()
        st = r.get('ST', '').upper()
        stereo = st.startswith('S') if st else kind in ('IEM', 'FX')
        model['buses'].append({'n': int(r['번호']), 'name': r['이름'], 'kind': kind, 'stereo': stereo, 'out': outs(r)})
    for r in _rows(tb, T_MM, MM_COLS):
        m = re.fullmatch(r'(MAIN|MTX)\s*(\d+)', r.get('구분', '').upper())
        if not m or not r.get('이름'):
            continue
        st = r.get('ST', '').upper()
        item = {'n': int(m.group(2)), 'name': r['이름'], 'stereo': not st.startswith('M'), 'out': outs(r)}
        model['mains' if m.group(1) == 'MAIN' else 'mtx'].append(item)
    for r in _rows(tb, T_GRP, GRP_COLS):
        m = re.fullmatch(r'(DCA|MUTE)\s*(\d+)', r.get('구분', '').upper())
        if m and r.get('이름'):
            model['dca_names' if m.group(1) == 'DCA' else 'mute_names'][int(m.group(2))] = r['이름']
    for r in _rows(tb, T_DIR, DIR_COLS):
        o, s = _nums(r.get('출력 포트')), _nums(r.get('소스 포트'))
        if r.get('출력 장비') and r.get('소스 장비') and o and s:
            if len(o) != len(s):
                warn.append(f"직결 라우팅 {r['출력 장비']} {r['출력 포트']} ← {r['소스 장비']} {r['소스 포트']}: 개수가 달라 짧은 쪽에 맞춥니다")
            model['direct'].append({'out_device': r['출력 장비'].upper(), 'out_ports': o,
                                    'src_device': r['소스 장비'].upper(), 'src_ports': s})
    if not model['strips']:
        warn.append('입력 채널 표에 이름이 적힌 줄이 없습니다')
    model['warnings'] = warn
    return model


if __name__ == '__main__':
    a = sys.argv[1:]
    if a and a[0] == '--read':
        print(json.dumps(parse(a[1]), ensure_ascii=False, indent=1))
    else:
        write_template(a[0], example='--example' in a)
        print('OK:', a[0])
