#!/usr/bin/env python3
"""Behringer WING 스냅샷 생성기 — 채널시트 스펙(JSON) → .snap
포맷: JSON `snapshot.9` (WING Edit 3.1 저장본·내장 기본 스냅샷 실측, 2026-11-17).
사용: python3 wing_gen.py spec.json 출력경로.snap [베이스.snap]

WING은 DM7과 구조가 다르다:
  · 스테레오는 '인접 채널 링크'가 아니라 **소스 모드 ST**(홀수+짝수 소스 한 쌍)를 채널 1개가 받는다
    → 시트의 스테레오 페어는 채널 1개로 합쳐지고 뒤 채널 번호가 당겨진다 (40채널 + AUX 8)
  · 이름·색은 소스(io.in.<그룹>.<n>)에 두고 채널이 따라간다(clink) — 소스와 채널 양쪽에 모두 기록
  · DCA·뮤트그룹 어사인은 채널 tags 문자열 ("#D1,#D3")
  · 버스·매트릭스는 원래 스테레오 — 모노 믹스는 busmono
"""
import json, os, re, sys, datetime

N_CH, N_AUX, N_BUS, N_MTX, N_DCA = 40, 8, 16, 8, 16
NAME_MAX = 16
SRC_GROUPS = {'LCL': 'Local', 'A': 'AES50 A', 'B': 'AES50 B', 'C': 'AES50 C',
              'CRD': 'Card (Dante 등)', 'MOD': 'Module', 'USB': 'USB'}
DCA_SLOTS = {'OnAir': 1, 'inst': 2, 'Sings': 3, 'Drums': 4, 'AMBI': 12}
# WING 색 번호(1~12): 1 회청 2 파랑 3 남색 4 청록 5 초록 6 올리브 7 노랑 8 주황 9 빨강 10 코랄 11 분홍 12 연보라
GROUP_COL = {'Drums': 9, 'Bass': 8, 'GTR': 7, 'AG': 6, 'Key': 5, 'Piano': 5,
             'Sings': 2, 'AMBI': 4, None: 1}
IEM_COL, MON_COL = 10, 6


def default_base():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'base', 'wing_default.snap')


def validate_base(path):
    """업로드된 WING 스냅샷이 생성기와 호환되는지 확인."""
    info = {}
    try:
        o = json.load(open(path, encoding='utf-8'))
    except Exception as e:
        return False, f'WING 스냅샷(.snap, JSON)이 아닙니다: {e}', info
    t = str(o.get('type', ''))
    info.update(type=t, model=o.get('creator_model', ''))
    ae = o.get('ae_data') or {}
    if not t.startswith('snapshot.') or not all(k in ae for k in ('io', 'ch', 'bus', 'dca')):
        return False, 'WING 스냅샷 구조(ae_data: io/ch/bus/dca)를 찾지 못했습니다', info
    if len(ae['ch']) < N_CH or 'in' not in ae['io']:
        return False, '채널 40개 구조가 아닙니다 — WING 스냅샷이 맞는지 확인해 주세요', info
    return True, 'OK', info


def _ascii_name(name, romanize=None):
    """WING 스크리블은 한글을 표시하지 못한다 — ASCII로 바꾸고 16자로 자른다."""
    s = str(name).strip()
    if any(ord(c) > 127 for c in s):
        s = romanize(s) if romanize else ''.join(c for c in s if ord(c) < 128)
    return re.sub(r'\s+', ' ', s).strip()[:NAME_MAX]


def _tags(old, dcas):
    keep = [t for t in str(old or '').split(',') if t and not re.fullmatch(r'#D\d+', t)]
    return ','.join(keep + [f'#D{n}' for n in sorted(set(dcas))])


def plan(spec, src_grp='LCL', src_size=None):
    """시트 → WING 스트립 배치 계획. (strips, buses, mtxs, warnings)"""
    warn = []
    chans = {c['ch']: c for c in spec['channels']}
    pair_first = {a: b for a, b in spec.get('pairs', [])}
    pair_second = {b for a, b in spec.get('pairs', [])}
    strips = []
    for ch in sorted(set(chans) | set(pair_first)):
        if ch in pair_second and (ch - 1) in pair_first and (ch - 1) % 2 == 1:
            continue                                   # 스테레오 소스의 R — 앞 채널에 합쳐짐
        c = chans.get(ch) or chans.get(pair_first.get(ch)) or {'name': ''}
        if ch in pair_first and ch % 2 == 1:
            strips.append({'src': ch, 'stereo': True, 'c': c, 'name': c['name']})
        elif ch in pair_first or ch in pair_second:
            # 짝수에서 시작하는 페어는 WING 스테레오 소스가 될 수 없다 → 모노 2채널을 좌우로
            first = ch in pair_first
            base = chans.get(ch if first else ch - 1, c)
            strips.append({'src': ch, 'stereo': False, 'c': base, 'name': base['name'],
                           'pan': -100 if first else 100, 'suffix': ' L' if first else ' R'})
            if first:
                warn.append(f'ch{ch}/{ch + 1} 페어는 짝수 소스에서 시작해 스테레오 소스로 묶을 수 없어 '
                            f'모노 2채널(L/R 패닝)로 배치했습니다')
        else:
            strips.append({'src': ch, 'stereo': False, 'c': c, 'name': c['name']})
    if src_size:
        over = [s['src'] for s in strips if s['src'] + (1 if s['stereo'] else 0) > src_size]
        if over:
            warn.append(f'소스 그룹 {src_grp}는 {src_size}개까지라 시트 {over[0]}번 이후 '
                        f'{len(over)}개 스트립은 소스 미연결(OFF)로 둡니다')
    if len(strips) > N_CH + N_AUX:
        dropped = strips[N_CH + N_AUX:]
        warn.append(f'WING은 채널 40 + AUX 8 = 48스트립까지입니다 — 시트 {dropped[0]["src"]}번부터 '
                    f'{len(dropped)}개 스트립은 넣지 못했습니다')
        strips = strips[:N_CH + N_AUX]
    elif len(strips) > N_CH:
        warn.append(f'채널 40개를 넘는 {len(strips) - N_CH}개 스트립은 AUX 1~{len(strips) - N_CH}에 배치했습니다')

    def bus_plan(items, pairs, limit, what):
        first = {a: b for a, b in pairs}
        second = {b for a, b in pairs}
        out = []
        for n in sorted(items):
            if n in second and (n - 1) in first:
                continue
            out.append({'n': n, 'stereo': n in first, 'm': items[n]})
        if len(out) > limit:
            warn.append(f'{what}는 {limit}개까지입니다 — {len(out) - limit}개는 넣지 못했습니다')
        return out[:limit]

    mixes = {int(k): v for k, v in spec.get('mixes', {}).items()}
    buses = bus_plan(mixes, [tuple(p) for p in spec.get('mix_pairs', [])], N_BUS, '버스')
    mtx = {int(k): v for k, v in spec.get('matrix', {}).items()}
    mpairs = [(n, n + 1) for n, v in mtx.items() if v.get('link') == 'L' and mtx.get(n + 1, {}).get('link') == 'R']
    mtxs = bus_plan(mtx, mpairs, N_MTX, '매트릭스')
    return strips, buses, mtxs, warn


def generate(spec_path, out_path, base_path=None, src_grp='LCL', romanize=None):
    spec = json.load(open(spec_path, encoding='utf-8'))
    base_path = base_path or default_base()
    snap = json.load(open(base_path, encoding='utf-8'))
    ae = snap['ae_data']
    if src_grp not in ae['io']['in']:
        raise RuntimeError(f'소스 그룹 {src_grp}가 이 스냅샷에 없습니다')
    srcs = ae['io']['in'][src_grp]
    strips, buses, mtxs, warn = plan(spec, src_grp, len(srcs))

    bus_of = {}                                        # 시트 믹스 번호 → WING 버스 번호
    for i, b in enumerate(buses, 1):
        bus_of[b['n']] = i
        if b['stereo']:
            bus_of[b['n'] + 1] = i
    iem_buses = sorted({bus_of[b['n']] for b in buses if b['m'].get('iem')})
    lvl = spec.get('iem_send_db')

    used_dca = set()
    for i, s in enumerate(strips, 1):
        c = s['c']
        name = _ascii_name(s['name'], romanize)
        if s.get('suffix'):
            name = name[:NAME_MAX - 2] + s['suffix']
        col = GROUP_COL.get(c.get('group'), 1)
        n = s['src']
        connected = n + (1 if s['stereo'] else 0) <= len(srcs)
        if connected:
            for k in ([n, n + 1] if s['stereo'] else [n]):
                src = srcs[str(k)]
                src['mode'] = 'ST' if s['stereo'] else 'M'
                src['name'], src['col'] = name, col
        strip = ae['ch'][str(i)] if i <= N_CH else ae['aux'][str(i - N_CH)]
        strip['in']['conn']['grp'] = src_grp if connected else 'OFF'
        strip['in']['conn']['in'] = n if connected else 1
        strip['name'], strip['col'] = name, col
        if 'pan' in s:
            strip['pan'] = s['pan']
        dcas = [DCA_SLOTS[d] for d in c.get('dca', []) if d in DCA_SLOTS]
        used_dca.update(dcas)
        strip['tags'] = _tags(strip.get('tags'), dcas)
        if lvl is not None:
            for b in iem_buses:
                snd = strip['send'][str(b)]
                snd['lvl'], snd['on'] = lvl, True

    names = {v: k for k, v in DCA_SLOTS.items()}
    names.update({int(k): v for k, v in spec.get('dca_names', {}).items()})
    for d in sorted(used_dca):
        if d <= N_DCA:
            ae['dca'][str(d)]['name'] = _ascii_name(names.get(d, f'DCA {d}'), romanize)

    for i, b in enumerate(buses, 1):
        bus = ae['bus'][str(i)]
        bus['name'] = _ascii_name(b['m']['name'], romanize)
        bus['busmono'] = not b['stereo']
        bus['col'] = IEM_COL if b['m'].get('iem') else MON_COL
    for i, m in enumerate(mtxs, 1):
        mx = ae['mtx'][str(i)]
        mx['name'] = _ascii_name(m['m']['name'], romanize)
        mx['busmono'] = not m['stereo']

    if 'created' in snap:
        snap['created'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=True, separators=(',', ':'))

    # 자체 검증: 다시 읽어 스트립 이름·소스가 계획과 같은지
    chk = json.load(open(out_path, encoding='utf-8'))['ae_data']
    for i, s in enumerate(strips, 1):
        strip = chk['ch'][str(i)] if i <= N_CH else chk['aux'][str(i - N_CH)]
        assert len(strip['name']) <= NAME_MAX
        if strip['in']['conn']['grp'] != 'OFF':
            assert strip['in']['conn']['in'] == s['src'], (i, strip['in']['conn'])
            assert chk['io']['in'][src_grp][str(s['src'])]['mode'] == ('ST' if s['stereo'] else 'M')
    n_st = sum(1 for s in strips if s['stereo'])
    print(f'OK: {os.path.basename(out_path)} — 스트립 {len(strips)}개(스테레오 {n_st}), '
          f'버스 {len(buses)}, 매트릭스 {len(mtxs)}, DCA {len(used_dca)}')
    for w in warn:
        print('  주의:', w)
    return {'strips': len(strips), 'stereo': n_st, 'buses': len(buses), 'matrix': len(mtxs), 'warnings': warn}


BUS_COL = {'IEM': 10, 'MON': 6, 'FX': 12, 'GRP': 4}


def generate_from_sheet(model, out_path, base_path=None, romanize=None, classify=None):
    """스트립 채널시트(stripsheet.parse 결과) → .snap. 시트의 번호 = WING 채널 번호(41~48은 AUX 1~8)."""
    snap = json.load(open(base_path or default_base(), encoding='utf-8'))
    ae = snap['ae_data']
    ins, outs = ae['io']['in'], ae['io']['out']
    warn = list(model.get('warnings', []))

    auto = {}
    if classify:                                     # 이름으로 악기 그룹·DCA 추정 (색·자동 DCA용)
        chans, _ = classify([(s['n'], s['name'], 'x') for s in model['strips']])
        auto = {c['ch']: c for c in chans}
    dca_no = {k.lower(): v for k, v in DCA_SLOTS.items()}
    dca_no.update({str(v).lower(): int(k) for k, v in model.get('dca_names', {}).items()})

    iem_buses = [b['n'] for b in model['buses'] if b['kind'] == 'IEM' and b['n'] <= N_BUS]
    lvl = model.get('iem_send_db')
    n_ok = n_st = 0
    for s in model['strips']:
        n = s['n']
        if n > N_CH + N_AUX:
            warn.append(f"{n}번 {s['name']}: WING은 48스트립(채널 40 + AUX 8)까지라 넣지 못했습니다")
            continue
        strip = ae['ch'][str(n)] if n <= N_CH else ae['aux'][str(n - N_CH)]
        name = _ascii_name(s['name'], romanize)
        a = auto.get(n, {})
        col = s.get('color') or GROUP_COL.get(a.get('group'), 1)
        dev, port, st = s['device'], s['port'], s['stereo']
        grp = ins.get(dev)
        if grp is None:
            warn.append(f"{n}번 {s['name']}: 입력 장비 '{dev}'는 WING에 없습니다 (LCL/AUX/A/B/C/CRD/USB 등) — 소스 미연결")
        elif port + (1 if st else 0) > len(grp):
            warn.append(f"{n}번 {s['name']}: {dev} {port}번은 범위를 넘습니다({len(grp)}개) — 소스 미연결")
            grp = None
        elif st and port % 2 == 0:
            warn.append(f"{n}번 {s['name']}: WING 스테레오는 홀수 포트에서 시작해야 합니다({dev} {port}) — 모노로 연결")
            st = False
        if grp is not None:
            for k in ([port, port + 1] if st else [port]):
                grp[str(k)]['mode'] = 'ST' if st else 'M'
                grp[str(k)]['name'], grp[str(k)]['col'] = name, col
            strip['in']['conn']['grp'], strip['in']['conn']['in'] = dev, port
        else:
            strip['in']['conn']['grp'], strip['in']['conn']['in'] = 'OFF', 1
        strip['name'], strip['col'] = name, col
        if s.get('dca') is None:
            dcas = [dca_no[d.lower()] for d in a.get('dca', []) if d.lower() in dca_no] if model.get('dca_auto', True) else []
        else:
            dcas = [d for d in s['dca'] if 1 <= d <= N_DCA]
        keep = [t for t in str(strip.get('tags') or '').split(',') if t and not re.fullmatch(r'#[DM]\d+', t)]
        strip['tags'] = ','.join(keep + [f'#D{d}' for d in sorted(set(dcas))]
                                 + [f'#M{m}' for m in sorted(set(s.get('mute') or [])) if 1 <= m <= 8])
        if lvl is not None:
            for b in iem_buses:
                strip['send'][str(b)]['lvl'], strip['send'][str(b)]['on'] = lvl, True
        n_ok += 1
        n_st += st

    def route(item, kind, what):
        o = item.get('out')
        if not o:
            return
        grp = outs.get(o['device'])
        if grp is None:
            warn.append(f"{what} {item['name']}: 출력 장비 '{o['device']}'는 WING에 없습니다")
            return
        base = 2 * (item['n'] - 1) + 1                 # 버스·메인·매트릭스의 L = 2n-1, R = 2n
        if item['stereo'] and len(o['ports']) == 1:
            warn.append(f"{what} {item['name']}: 스테레오인데 출력 포트가 1개라 L만 보냅니다")
        for i, p in enumerate(o['ports'][:2 if item['stereo'] else 1]):
            if str(p) not in grp:
                warn.append(f"{what} {item['name']}: {o['device']} {p}번 출력은 없습니다")
                continue
            grp[str(p)]['grp'], grp[str(p)]['in'] = kind, base + i

    for b in model['buses']:
        if b['n'] > N_BUS:
            warn.append(f"버스 {b['n']} {b['name']}: WING 버스는 16개까지입니다")
            continue
        bus = ae['bus'][str(b['n'])]
        bus['name'], bus['busmono'] = _ascii_name(b['name'], romanize), not b['stereo']
        bus['col'] = BUS_COL.get(b['kind'], MON_COL)
        route(b, 'BUS', '버스')
    for key, kind, limit, what in (('mains', 'MAIN', 4, '메인'), ('mtx', 'MTX', N_MTX, '매트릭스')):
        for m in model[key]:
            if m['n'] > limit:
                warn.append(f"{what} {m['n']}: WING은 {limit}개까지입니다")
                continue
            tgt = ae['main' if kind == 'MAIN' else 'mtx'][str(m['n'])]
            tgt['name'], tgt['busmono'] = _ascii_name(m['name'], romanize), not m['stereo']
            route(m, kind, what)
    for k, v in model.get('dca_names', {}).items():
        if 1 <= int(k) <= N_DCA:
            ae['dca'][str(int(k))]['name'] = _ascii_name(v, romanize)
    for k, v in model.get('mute_names', {}).items():
        if 1 <= int(k) <= 8:
            ae['mgrp'][str(int(k))]['name'] = _ascii_name(v, romanize)
    for d in model.get('direct', []):
        og, sg = outs.get(d['out_device']), ins.get(d['src_device'])
        if og is None or sg is None:
            warn.append(f"직결 라우팅 {d['out_device']} ← {d['src_device']}: WING에 없는 장비 이름입니다")
            continue
        for op, sp_ in zip(d['out_ports'], d['src_ports']):
            if str(op) in og and str(sp_) in sg:
                og[str(op)]['grp'], og[str(op)]['in'] = d['src_device'], sp_

    if 'created' in snap:
        snap['created'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=True, separators=(',', ':'))
    json.load(open(out_path, encoding='utf-8'))
    print(f"OK: {os.path.basename(out_path)} — 스트립 {n_ok}개(스테레오 {n_st}), 버스 {len(model['buses'])}, "
          f"메인 {len(model['mains'])}, 매트릭스 {len(model['mtx'])}, 직결 {len(model.get('direct', []))}건")
    for w in warn:
        print('  주의:', w)
    return {'strips': n_ok, 'stereo': n_st, 'warnings': warn}


if __name__ == '__main__':
    import sheet2spec
    if sys.argv[1].lower().endswith('.numbers'):          # 스트립 채널시트 작성본 → .snap
        import stripsheet
        m = stripsheet.parse(sys.argv[1])
        out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
            os.path.dirname(sys.argv[1]), (m['name'] or os.path.splitext(os.path.basename(sys.argv[1]))[0]) + '.snap')
        generate_from_sheet(m, out, sys.argv[3] if len(sys.argv) > 3 else None, sheet2spec.romanize, sheet2spec.classify)
        sys.exit(0)
    import sheet2spec
    generate(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None,
             sys.argv[4] if len(sys.argv) > 4 else 'LCL', sheet2spec.romanize)
