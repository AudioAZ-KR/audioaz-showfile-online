#!/usr/bin/env python3
"""KLANG 쇼파일 생성기 — 채널시트 스펙(JSON) → .KLANGshow
2026-07-21 확정 규격: 64ch/12믹스, i3D, -15dB, 패닝 템플릿, 그룹 색, 스냅샷 동기화.
사용: python3 klang_gen.py spec.json 출력경로.KLANGshow [베이스.KLANGshow]
"""
import zlib, json, sys, os, math, datetime
import xml.dom.minidom as minidom


def generate(spec_path, out_path, base_path=None):
    spec = json.load(open(spec_path))
    if base_path is None:
        base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'base', 'AudioAZTest.KLANGshow')
    x = zlib.decompress(open(base_path, 'rb').read()).decode('utf-8')
    d = minidom.parseString(x)
    root = d.documentElement
    name = spec.get('ascii_name') or spec['name']
    root.setAttribute('Name', name)
    root.setAttribute('LastChanged', datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'))

    opts = spec.get('options', {})
    def opt(k): return opts.get(k, True)
    groups = spec['groups'] if opt('auto_group') else []
    assert len(groups) <= 8, '그룹은 8개까지'
    chans = {c['ch']: c for c in spec['channels']}
    pairs = [tuple(p) for p in spec.get('pairs', [])]
    linkmap = {}
    for a, b in pairs:
        linkmap[a] = b
        linkmap[b] = a

    # 표시 순서: 그룹 순 → 그룹 없는 보이는 채널 → 숨김
    order = []
    for gi in range(len(groups)):
        order += [c['ch'] for c in spec['channels'] if c.get('group') == groups[gi]]
    order += [c['ch'] for c in spec['channels'] if c.get('group') is None]
    order += [ch for ch in range(1, 65) if ch not in chans]
    assert len(order) == 64, f'표시 순서 {len(order)}개 != 64'
    index = {ch: i for i, ch in enumerate(order)}

    ul = [n for n in root.childNodes if n.nodeType == 1 and n.tagName == 'UserList'][0]
    users = [n for n in ul.childNodes if n.nodeType == 1 and n.tagName == 'User']
    u1 = users[0]
    gcol = {}
    for g in u1.getElementsByTagName('Group'):
        gid = int(g.getAttribute('ID'))
        if gid < len(groups):
            g.setAttribute('Name', groups[gid])
        gcol[gid] = g.getAttribute('Colour')

    for cs in u1.getElementsByTagName('ChannelSettings'):
        ic = int(cs.getAttribute('InputChannel'))
        cs.setAttribute('Index', str(index[ic]))
        if opt('gain_minus15'):
            cs.setAttribute('ChannelGaindB', '-15.00')
        if opt('panning'):
            cs.setAttribute('R', '0.38000')
            cs.setAttribute('Theta', '1.57080')
        if ic in chans:
            c = chans[ic]
            nm = c.get('klang_name', c['name'])
            assert all(ord(ch_) < 128 for ch_ in nm), f'KLANG 이름 한글 불가: {nm} (klang_name으로 로마자 지정)'
            cs.setAttribute('Name', nm)
            cs.setAttribute('Visible', '1')
            gid = groups.index(c['group']) if c.get('group') in groups else 8
            cs.setAttribute('GroupID', str(gid))     # 0-based, 8 = 그룹 없음
            cs.setAttribute('LinkedStereoChannel', str(linkmap.get(ic, -1)))
            if gid < 8 and opt('color_match'):
                cs.setAttribute('Color', gcol[gid])  # 채널 색 = 그룹 색
            pan = c.get('pan', 'mono') if opt('panning') else None
            if pan is None:
                pass
            elif pan == 'oh':                          # OH 페어: mode 4, Ride 좌/HH 우
                first = linkmap.get(ic, 0) > ic
                sgn = -1 if first else 1
                cs.setAttribute('ChannelMode', '4')
                cs.setAttribute('WidthOffset', f'{sgn*61:.2f}')
                cs.setAttribute('Phi', f'{sgn*1.06465:.5f}')
            elif pan == 'mono':                      # 모노 채널: 모노 모드 + 센터
                cs.setAttribute('ChannelMode', '1')
                cs.setAttribute('WidthOffset', '0.00')
                cs.setAttribute('Phi', '0.00000')
            elif isinstance(pan, dict) and 'pos_deg' in pan:   # 드럼 개별 위치 (HH/탐)
                cs.setAttribute('ChannelMode', '3')
                cs.setAttribute('WidthOffset', '0.00')
                cs.setAttribute('Phi', f'{math.radians(pan["pos_deg"]):.5f}')
            elif isinstance(pan, dict) and 'width_deg' in pan: # 스테레오 악기: 첫 채널 +
                first = linkmap.get(ic, 0) > ic
                sgn = 1 if first else -1
                cs.setAttribute('ChannelMode', '3')
                cs.setAttribute('WidthOffset', f'{sgn*pan["width_deg"]:.2f}')
                cs.setAttribute('Phi', f'{sgn*math.radians(pan["width_deg"]):.5f}')
        else:
            cs.setAttribute('Name', f'CH {ic}')
            cs.setAttribute('Visible', '0' if opt('hide_unused') else '1')
            cs.setAttribute('GroupID', '8')
            cs.setAttribute('LinkedStereoChannel', '-1')
            cs.setAttribute('ChannelMode', '3')
            cs.setAttribute('WidthOffset', '0.00')

    # Mix1 → 12개 믹스 복제, 전 믹스 i3D
    def gc(u, t):
        return [n for n in u.childNodes if n.nodeType == 1 and n.tagName == t][0]
    g1 = gc(u1, 'Groups')
    cl1 = gc(u1, 'ChannelSettingsList')
    for u in users[1:]:
        u.replaceChild(g1.cloneNode(True), gc(u, 'Groups'))
        u.replaceChild(cl1.cloneNode(True), gc(u, 'ChannelSettingsList'))
    if opt('i3d'):
        for u in users:
            u.setAttribute('Mode', '4')
            u.setAttribute('UseTracking', '1')

    # 스냅샷 동기화 (리콜해도 유지) + 액티브 스냅샷 이름 (10자 제한)
    tpl = {int(c.getAttribute('InputChannel')): {k: c.getAttribute(k) for k in
           ('R', 'Theta', 'Phi', 'WidthOffset', 'ChannelMode', 'ChannelGaindB')}
           for c in u1.getElementsByTagName('ChannelSettings')}
    sn = root.getElementsByTagName('Snapshots')[0]
    for s in sn.getElementsByTagName('Snapshot'):
        for su in s.getElementsByTagName('User'):
            if su.hasAttribute('Mode') and opt('i3d'):
                su.setAttribute('Mode', '4')
            for c in su.getElementsByTagName('ChannelSettings'):
                ic = int(c.getAttribute('InputChannel'))
                if ic in tpl:
                    for k, v in tpl[ic].items():
                        if c.hasAttribute(k):
                            c.setAttribute(k, v)
        if s.getAttribute('Id') == sn.getAttribute('ActiveID'):
            s.setAttribute('Name', spec.get('snapshot', name.replace('_', ''))[:10])

    out = '<?xml version="1.0" encoding="UTF-8"?> ' + root.toxml()
    assert all(ord(ch_) < 128 for ch_ in out), '비ASCII 문자 잔존'
    blob = zlib.compress(out.encode('utf-8'), 1)
    open(out_path, 'wb').write(blob)

    # 자체 검증
    d2 = minidom.parseString(zlib.decompress(open(out_path, 'rb').read()).decode())
    ul2 = [n for n in d2.documentElement.childNodes if n.nodeType == 1 and n.tagName == 'UserList'][0]
    us2 = [n for n in ul2.childNodes if n.nodeType == 1 and n.tagName == 'User']
    def sig(u):
        return tuple((c.getAttribute('InputChannel'), c.getAttribute('Name'), c.getAttribute('GroupID'),
                      c.getAttribute('Phi'), c.getAttribute('WidthOffset'), c.getAttribute('ChannelMode'))
                     for c in u.getElementsByTagName('ChannelSettings'))
    assert all(sig(u) == sig(us2[0]) for u in us2), '믹스 불일치'
    assert (not opt('i3d')) or all(u.getAttribute('Mode') == '4' for u in us2), 'i3D 아님'
    print(f'OK: {out_path} ({len(blob)} bytes, 12 mixes verified)')


if __name__ == '__main__':
    generate(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
