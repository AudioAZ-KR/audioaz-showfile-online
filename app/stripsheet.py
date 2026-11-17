#!/usr/bin/env python3
"""스트립 채널시트(공용 양식) — WING·DiGiCo·DM7 공용. 양식 작성(write_template)과 읽기(parse).

양식의 생각: 한 줄 = 콘솔의 채널 스트립 1개. 스테레오는 'ST' 한 칸(스트립 1개가 포트 2개를 씀).
이름만 적으면 입력 장비·포트·DCA·색은 자동으로 채워진다. 달라지는 곳만 적는다.
  python3 stripsheet.py 양식.numbers [--example]     양식 파일 만들기
  python3 stripsheet.py --read 작성본.numbers          읽은 결과(JSON) 확인
"""
import json, re, sys, unicodedata

FORMAT = '스트립 채널시트 v2'
# v2: A4 세로 한 장에 맞춘 배치 — 입력 64채널을 좌우 두 블록(1~32 | 33~64), 장비+포트를 한 칸('A 1')으로
N_IN, N_BLOCK, N_BUS, N_MAIN, N_MTX, N_DCA, N_MUTE, N_DIRECT = 64, 32, 24, 4, 8, 16, 8, 8
T_SET, T_IN, T_BUS, T_MM, T_GRP, T_DIR, T_HELP = '설정', '입력 채널', '버스', '메인·매트릭스', 'DCA·뮤트그룹', '직결 라우팅', '작성법'
SET_COLS = ['행사명', '콘솔', '기본 입력 장비', 'IEM 센드', 'DCA 자동']
SET_DEFAULT = ['', 'WING', 'CRD', '-20', '예']
IN_COLS = ['번호', '이름', 'ST', '입력', 'DCA', '뮤트', '마이크·메모']
IN_W = [20, 78, 20, 40, 28, 22, 54]                      # 262pt × 2블록 + 간격 = A4 세로 폭
BUS_COLS = ['번호', '이름', '종류', 'ST', '출력', '메모']
BUS_W = [22, 92, 36, 24, 52, 74]
MM_COLS = ['구분', '이름', 'ST', '출력']
MM_W = [46, 88, 22, 68]
GRP_COLS = ['구분', '이름']
GRP_W = [60, 164]
DIR_COLS = ['출력', '소스', '메모']
DIR_W = [72, 72, 156]
HELP = [
    ('기본', '한 줄 = 콘솔 채널 스트립 1개. 이름을 적은 줄만 사용하고 빈 줄은 건너뜁니다(번호는 그대로). 노란 칸만 채우면 됩니다.'),
    ('이름', '영문·숫자 권장(WING 16자). 한글은 로마자로 바뀝니다.'),
    ('ST', '스테레오 채널이면 ST. 스트립은 1개, 포트는 2개(L·R 연속)를 씁니다. 모노는 비움.'),
    ('입력', '장비와 포트를 한 칸에: A 1 · CRD 13 · LCL 3. 비우면 윗줄 다음 포트로 자동(ST는 2칸). 숫자만 적으면 같은 장비의 그 포트. '
            '맨 위가 비어 있으면 설정의 기본 입력 장비 1번부터. WING의 ST는 홀수 포트에서 시작.'),
    ('DCA', '번호를 쉼표로: 1,3 · 비우면 이름 보고 자동(설정의 DCA 자동) · 없음은 -'),
    ('뮤트', '뮤트그룹 번호: 1 또는 1,2 · 비우면 없음'),
    ('마이크·메모', '마이크·스탠드·비고. 쇼파일에는 들어가지 않습니다.'),
    ('설정', '행사명 = 파일 이름(예: 261120_G2A) · 콘솔 = WING / DiGiCo / DM7 · 기본 입력 장비 = 입력 칸을 비운 줄에 쓰는 장비 · '
            'IEM 센드 = 종류가 IEM인 버스로 가는 채널 센드(dB, 비우면 안 건드림) · DCA 자동 = 예 / 아니오'),
    ('버스 종류', 'IEM(인이어) / MON(웨지) / FX / GRP(서브그룹). IEM·FX는 스테레오, MON·GRP는 모노가 기본 — 다르면 ST 칸에 ST 또는 M.'),
    ('출력', '장비와 포트: A 1-2 · LCL 7 · CRD 3-4. 메인·매트릭스는 ST 칸에 M을 적으면 모노.'),
    ('직결 라우팅', '믹스를 거치지 않고 입력을 출력으로 그대로 넘길 때. 예: 출력 C 1-48 ← 소스 CRD 1-48 (단테 → AES50 C → DP48).'),
    ('장비 이름', 'WING: LCL(로컬) AUX A B C(AES50) CRD(카드·단테) USB · DiGiCo: 세션의 랙 이름 그대로(예: SD-Rack 1) · DM7: 비워도 됩니다.'),
    ('주의', '표 이름과 머리글(파란 줄)은 바꾸지 마세요. 행이 모자라면 표 아래에 행을 추가하고 번호를 이어 적습니다.'),
]
EXAMPLE_IN = [  # (이름, ST, 입력, DCA, 뮤트, 메모)
    ('Kick', '', '', '', '', 'Beta91'), ('Snare', '', '', '', '', 'SM57'), ('HH', '', '', '', '', ''),
    ('Tom1', '', '', '', '', ''), ('Tom2', '', '', '', '', ''), ('OH', 'ST', '7', '', '', 'C414 x2'),
    ('Bass', '', '', '', '', 'DI'), ('EG', 'ST', '11', '', '', ''), ('Key 1', 'ST', '', '', '', ''),
    ('Piano', 'ST', '', '', '', ''), ('AG', '', '', '', '', ''), ('MTR', 'ST', '', '', '', ''),
    ('Click', '', '', '-', '', ''), ('Vox 1', '', 'A 1', '', '1', 'Beta87'), ('Vox 2', '', '', '', '1', ''),
    ('Vox 3', '', '', '', '1', ''), ('Leader', '', '', '1,3', '1', ''), ('Pastor', '', '', '', '2', ''),
    ('TB', '', 'LCL 1', '-', '', '토크백'),
]
EXAMPLE_BUS = [('IEM Leader', 'IEM', '', 'A 1-2', ''), ('IEM Vox', 'IEM', '', 'A 3-4', ''), ('IEM Band', 'IEM', '', 'A 5-6', ''),
               ('MON Drum', 'MON', '', 'A 7', ''), ('MON Front', 'MON', '', 'A 8', ''), ('Reverb', 'FX', '', '', '')]
EXAMPLE_MM = {'MAIN 1': ('Main LR', '', 'LCL 7-8'), 'MTX 1': ('Stream', '', 'CRD 1-2'), 'MTX 2': ('Lobby', 'M', 'LCL 1')}
EXAMPLE_DIR = [('C 1-48', 'CRD 1-48', '단테 → DP48 (AES50 C)')]
DEFAULT_DCA = {1: 'OnAir', 2: 'inst', 3: 'Sings', 4: 'Drums'}
COLORS = {'회색': 1, '파랑': 2, '남색': 3, '청록': 4, '초록': 5, '올리브': 6, '노랑': 7, '주황': 8,
          '빨강': 9, '코랄': 10, '분홍': 11, '보라': 12}
ROW_H, HEAD_H, GAP = 20, 20, 44        # 표 제목 줄 포함 간격


# ───────────────────────────── 양식 만들기

def write_template(path, example=False):
    from numbers_parser import Document, RGB, Alignment, HorizontalJustification as HJ, VerticalJustification as VJ
    doc = Document(sheet_name='입력', table_name=T_SET, num_rows=2, num_cols=len(SET_COLS),
                   num_header_rows=1, num_header_cols=0)
    mid, ctr = Alignment(HJ.LEFT, VJ.MIDDLE), Alignment(HJ.CENTER, VJ.MIDDLE)
    navy, ink = RGB(12, 34, 68), RGB(12, 34, 68)

    def style(name, **kw):
        kw.setdefault('text_wrap', False)
        kw.setdefault('text_inset', 2.0)
        return doc.add_style(name=name, **kw)

    S = {
        'head': style('AZ Head', bold=True, font_size=8.5, font_color=RGB(255, 255, 255), bg_color=navy, alignment=ctr),
        'key': style('AZ Key', bold=True, font_size=9.0, font_color=ink, bg_color=RGB(225, 234, 247), alignment=ctr),
        'fill': style('AZ Fill', font_size=10.0, font_color=ink, bg_color=RGB(255, 251, 230), alignment=mid),
        'fillc': style('AZ FillC', font_size=10.0, font_color=ink, bg_color=RGB(255, 251, 230), alignment=ctr),
        'opt': style('AZ Opt', font_size=9.0, font_color=RGB(60, 78, 105), alignment=ctr),
        'note': style('AZ Note', font_size=8.0, font_color=RGB(96, 110, 132), alignment=mid),
        'help': style('AZ Help', font_size=9.0, font_color=RGB(60, 78, 105), alignment=mid, text_wrap=True, text_inset=4.0),
    }

    def table(sheet, name, cols, nrows, widths, x=0, y=0, first=None, row_h=ROW_H, header_cols=1):
        t = first or sheet.add_table(name, x=x, y=y, num_rows=nrows + 1, num_cols=len(cols),
                                     num_header_rows=1, num_header_cols=header_cols)
        for c, (h, w) in enumerate(zip(cols, widths)):
            t.write(0, c, h, style=S['head'])
            t.col_width(c, w)
        t.row_height(0, HEAD_H)
        for r in range(1, nrows + 1):
            t.row_height(r, row_h)
        return t

    def put(t, r, c, v, st):
        t.write(r, c, v, style=S[st])

    # 시트 1 (A4 세로): 설정 한 줄 + 입력 1~32 | 33~64
    sh = doc.sheets[0]
    t = table(sh, T_SET, SET_COLS, 1, [150, 84, 110, 90, 100], first=sh.tables[0], header_cols=0)
    for c, v in enumerate(SET_DEFAULT):
        put(t, 1, c, '261120_Example' if example and c == 0 else v, 'fillc')
    y = HEAD_H + ROW_H + GAP
    bw = sum(IN_W)
    for b in range(N_IN // N_BLOCK):
        lo = b * N_BLOCK + 1
        t = table(sh, f'{T_IN} {lo}-{lo + N_BLOCK - 1}', IN_COLS, N_BLOCK, IN_W, x=b * (bw + 10), y=y)
        for r in range(1, N_BLOCK + 1):
            n = lo + r - 1
            row = EXAMPLE_IN[n - 1] if example and n <= len(EXAMPLE_IN) else ('',) * 6
            put(t, r, 0, str(n), 'key'); put(t, r, 1, row[0], 'fill'); put(t, r, 2, row[1], 'fillc')
            for c in (3, 4, 5):
                put(t, r, c, row[c - 1], 'opt')
            put(t, r, 6, row[5], 'note')

    # 시트 2 (A4 세로): 왼쪽 버스 + 직결 라우팅 / 오른쪽 메인·매트릭스 + DCA·뮤트그룹
    doc.add_sheet('출력', T_BUS, num_rows=N_BUS + 1, num_cols=len(BUS_COLS))
    sh = doc.sheets[1]
    R2 = 18                                             # 출력 시트는 줄 높이를 줄여 A4 높이에 맞춤
    t = table(sh, T_BUS, BUS_COLS, N_BUS, BUS_W, first=sh.tables[0], row_h=R2)
    for r in range(1, N_BUS + 1):
        row = EXAMPLE_BUS[r - 1] if example and r <= len(EXAMPLE_BUS) else ('',) * 5
        put(t, r, 0, str(r), 'key'); put(t, r, 1, row[0], 'fill'); put(t, r, 2, row[1], 'fillc')
        put(t, r, 3, row[2], 'opt'); put(t, r, 4, row[3], 'opt'); put(t, r, 5, row[4], 'note')
    t = table(sh, T_DIR, DIR_COLS, N_DIRECT, DIR_W, x=0, y=HEAD_H + N_BUS * R2 + GAP, header_cols=0, row_h=R2)
    for r in range(1, N_DIRECT + 1):
        row = EXAMPLE_DIR[r - 1] if example and r <= len(EXAMPLE_DIR) else ('',) * 3
        put(t, r, 0, row[0], 'fillc'); put(t, r, 1, row[1], 'fillc'); put(t, r, 2, row[2], 'note')
    x2 = sum(BUS_W) + 10
    keys = [f'MAIN {i}' for i in range(1, N_MAIN + 1)] + [f'MTX {i}' for i in range(1, N_MTX + 1)]
    t = table(sh, T_MM, MM_COLS, len(keys), MM_W, x=x2, y=0, row_h=R2)
    for r, k in enumerate(keys, 1):
        row = EXAMPLE_MM.get(k, ('',) * 3) if example else ('',) * 3
        put(t, r, 0, k, 'key'); put(t, r, 1, row[0], 'fill'); put(t, r, 2, row[1], 'opt'); put(t, r, 3, row[2], 'opt')
    gkeys = [f'DCA {i}' for i in range(1, N_DCA + 1)] + [f'MUTE {i}' for i in range(1, N_MUTE + 1)]
    t = table(sh, T_GRP, GRP_COLS, len(gkeys), GRP_W, x=x2, y=HEAD_H + len(keys) * R2 + GAP, row_h=R2)
    for r, k in enumerate(gkeys, 1):
        n = int(k.split()[1])
        v = DEFAULT_DCA.get(n, '') if k.startswith('DCA') else ''
        if example and k in ('MUTE 1', 'MUTE 2'):
            v = 'Vocals' if n == 1 else 'Speech'
        put(t, r, 0, k, 'key'); put(t, r, 1, v, 'fill')

    # 시트 3 (A4 세로): 작성법
    doc.add_sheet('작성법', T_HELP, num_rows=len(HELP) + 1, num_cols=2)
    sh = doc.sheets[2]
    t = table(sh, T_HELP, ['칸', '쓰는 법'], len(HELP), [84, 450], first=sh.tables[0], row_h=44)
    for r, (k, d) in enumerate(HELP, 1):
        put(t, r, 0, k, 'key'); put(t, r, 1, d, 'help')
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


def _rows(tables, name, cols=None):
    """표를 {열이름: 값} 목록으로. 열 순서가 바뀌어도 머리글로 찾는다.
    이름이 같은 말로 시작하는 표(입력 채널 1-32, 입력 채널 33-64)는 이어 붙인다."""
    out = []
    for tname in sorted(tables, key=lambda k: (len(k), k)):
        if tname != name and not tname.startswith(name + ' '):
            continue
        rows = tables[tname]
        if not rows:
            continue
        head = [h.strip() for h in rows[0]]
        out += [{h: (r[i] if i < len(r) else '') for i, h in enumerate(head) if h} for r in rows[1:]]
    return out


def _devport(cell, upper=True):
    """'A 1' 'CRD 1-48' 'SD-Rack 1:13' '13' → (장비 또는 '', [포트…])"""
    s = _s(cell)
    if not s:
        return '', []
    m = re.fullmatch(r'(.*?)[\s:]*(\d+(?:\s*[-~]\s*\d+)?)', s)
    if not m:
        return (s.upper() if upper and len(s) <= 4 else s), []
    dev = m.group(1).strip(' :')
    return (dev.upper() if upper and len(dev) <= 4 else dev), _nums(m.group(2))


def is_strip_sheet(path):
    try:
        t = _tables(path)
    except Exception:
        return False
    return T_SET in t and any(k == T_IN or k.startswith(T_IN + ' ') for k in t)


def parse(path):
    """작성본 → 콘솔 중립 모델. warnings에 자동 처리·무시한 내용을 남긴다."""
    tb = _tables(path)
    if not any(k == T_IN or k.startswith(T_IN + ' ') for k in tb):
        raise RuntimeError(f"'{T_IN}' 표를 찾지 못했습니다 — 스트립 채널시트 양식이 맞는지 확인해 주세요")
    warn = []
    srows = _rows(tb, T_SET)
    if srows and '항목' in srows[0]:                       # v1: 세로형(항목 | 값)
        setting = {r['항목']: r.get('값', '') for r in srows if r.get('항목')}
    else:                                                  # v2: 가로형(머리글 = 항목, 다음 줄 = 값)
        setting = dict(srows[0]) if srows else {}
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
    for r in sorted(_rows(tb, T_IN), key=lambda x: int(x['번호']) if x.get('번호', '').isdigit() else 0):
        if not r.get('이름') or not r.get('번호', '').isdigit():
            continue
        st = r.get('ST', '').upper() in ('ST', 'S', 'STEREO', '스테레오', 'O', 'Y')
        if '입력' in r:                                     # v2: 한 칸 'A 1'
            d, ports = _devport(r['입력'])
        else:                                              # v1: 입력 장비 | 포트
            d, ports = _devport(r.get('입력 장비', ''))[0] or _s(r.get('입력 장비')).upper(), _nums(r.get('포트'))
        if d:
            dev = d
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
        if '출력' in r:
            d, ports = _devport(r['출력'])
        else:
            d, ports = _s(r.get('출력 장비')).upper(), _nums(r.get('포트'))
        return {'device': d, 'ports': ports} if d else None

    for r in _rows(tb, T_BUS):
        if not r.get('이름') or not r.get('번호', '').isdigit():
            continue
        kind = (r.get('종류') or 'MON').upper()
        st = r.get('ST', '').upper()
        stereo = st.startswith('S') if st else kind in ('IEM', 'FX')
        model['buses'].append({'n': int(r['번호']), 'name': r['이름'], 'kind': kind, 'stereo': stereo, 'out': outs(r)})
    for r in _rows(tb, T_MM):
        m = re.fullmatch(r'(MAIN|MTX)\s*(\d+)', r.get('구분', '').upper())
        if not m or not r.get('이름'):
            continue
        st = r.get('ST', '').upper()
        item = {'n': int(m.group(2)), 'name': r['이름'], 'stereo': not st.startswith('M'), 'out': outs(r)}
        model['mains' if m.group(1) == 'MAIN' else 'mtx'].append(item)
    for r in _rows(tb, T_GRP):
        m = re.fullmatch(r'(DCA|MUTE)\s*(\d+)', r.get('구분', '').upper())
        if m and r.get('이름'):
            model['dca_names' if m.group(1) == 'DCA' else 'mute_names'][int(m.group(2))] = r['이름']
    for r in _rows(tb, T_DIR):
        if '출력' in r:
            (od, o), (sd, s_) = _devport(r['출력']), _devport(r.get('소스', ''))
        else:
            od, o, sd, s_ = _s(r.get('출력 장비')).upper(), _nums(r.get('출력 포트')), _s(r.get('소스 장비')).upper(), _nums(r.get('소스 포트'))
        if od and sd and o and s_:
            if len(o) != len(s_):
                warn.append(f"직결 라우팅 {od} ← {sd}: 포트 개수가 달라 짧은 쪽에 맞춥니다")
            model['direct'].append({'out_device': od, 'out_ports': o, 'src_device': sd, 'src_ports': s_})
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
