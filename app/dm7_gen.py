#!/usr/bin/env python3
"""DM7 쇼파일 생성기 — 채널시트 스펙(JSON) → .dm7f
2026-07-21 리버싱 결과 기반 (콘솔 실기 검증 완료).
사용: python3 dm7_gen.py spec.json 출력경로.dm7f [베이스.dm7f]
"""
import zlib, re, uuid, struct, json, sys, os

DCA_SLOTS = {'OnAir':1, 'inst':2, 'Sings':3, 'Drums':4, 'AMBI':12}
COLOR_RE = re.compile(rb'(Blue|Orange|Red|Yellow|Green|Purple|Pink|White)\x00')
VALID_FLAGS = {b'\x00\x00\x00', b'\x01\x80\x01', b'\x01\x01\x01'}
MTRX_DELTA = 31048   # MIX1 이름 → MTRX1 이름 오프셋 (펌웨어 상수, 실측)
DCA_DELTA = 9188     # MTRX 앵커 → DCA 테이블 오프셋 (실측)


def _mix_positions(raw, ms):
    """믹스 1~48 이름 위치. MX 1 리네임된 베이스는 스트라이드 역산 폴백."""
    marks = [m.start() for m in re.finditer(b'nST/M', raw) if m.start() > ms[-1]]
    mixpos = {}
    for i, mk in enumerate(marks[:47]):
        c = COLOR_RE.search(raw, mk, mk + 0x300)
        if c:
            mixpos[i + 2] = c.start() - 0x40
    mx1 = raw.find(b'MX 1\x00', ms[-1])
    if mx1 < 0 and 2 in mixpos and 3 in mixpos:
        cand = 2 * mixpos[2] - mixpos[3]
        if raw[cand - 3:cand] in VALID_FLAGS:
            mx1 = cand
    if mx1 >= 0:
        mixpos[1] = mx1
    return mixpos


def _matrix_base(raw, ms, mixpos):
    """MTRX1 이름 위치. 커스텀(TOP/Main) → 공장(MT 1) → 델타 폴백."""
    for anchor in (b'\x01\x80\x01TOP', b'\x01\x80\x01Main'):
        tl = raw.find(anchor)
        if tl >= 0:
            return tl + 3
    t = raw.find(b'MT 1\x00', ms[-1])
    if t >= 0 and raw[t - 3:t] in VALID_FLAGS:
        return t
    if 1 in mixpos:
        cand = mixpos[1] + MTRX_DELTA
        if all(raw[cand + k * 0x206 - 3:cand + k * 0x206] in VALID_FLAGS for k in range(8)):
            return cand
    return -1


def _dca_base(raw, mtx_base):
    """DCA 이름 테이블 위치(\x01 플래그 포함). 커스텀(OnAir) → 공장(DCA 1) → 델타 폴백."""
    for anchor in (b'\x01OnAir', b'\x01DCA 1', b'\x01DCA  1'):
        loc = raw.find(anchor)
        if loc >= 0:
            return loc
    if mtx_base > 0:
        cand = (mtx_base - 3) + DCA_DELTA
        if raw[cand:cand + 1] == b'\x01':
            return cand
    return -1


def validate_base(path):
    """업로드된 리셋 씬이 생성기와 호환되는지 구조 검증."""
    try:
        data = open(path, 'rb').read()
    except OSError as e:
        return False, f'파일을 읽을 수 없습니다: {e}', {}
    info = {'sections': 0, 'channel_sections': 0, 'mix': False, 'matrix': False, 'dca': False}
    if b'#FILE' not in data or b'#END' not in data:
        return False, '.dm7f 형식이 아닙니다 (DM7 콘솔에서 저장한 파일인지 확인해 주세요)', info
    try:
        secs = find_sections(data)
    except Exception:
        secs = []
    info['sections'] = len(secs)
    for h, p, plen in secs:
        try:
            raw = zlib.decompress(data[p:])
        except Exception:
            continue
        ms = [x.start() for x in re.finditer(b'STEREO', raw)]
        if len(ms) != 120 or len({b - a for a, b in zip(ms, ms[1:])}) != 1:
            continue
        info['channel_sections'] += 1
        mixpos = _mix_positions(raw, ms)
        if len(mixpos) >= 40:
            info['mix'] = True
        mb = _matrix_base(raw, ms, mixpos)
        if mb > 0:
            info['matrix'] = True
            if _dca_base(raw, mb) > 0:
                info['dca'] = True
    if not info['channel_sections']:
        return False, '채널 구조(120채널)를 찾지 못했습니다 — DM7 리셋 씬이 맞는지, 콘솔 펌웨어를 확인해 주세요', info
    if not (info['mix'] and info['matrix']):
        return False, '믹스/매트릭스 구조를 찾지 못했습니다 — 이 리셋 씬은 호환되지 않습니다', info
    return True, 'OK', info


def find_sections(data):
    res = []
    for m in re.finditer(b'#FILE', data):
        h = m.start()
        for i in range(h + 28, h + 120):
            if data[i] == 0x78:
                o = zlib.decompressobj()
                try:
                    o.decompress(data[i:])
                    res.append((h, i, len(data) - i - len(o.unused_data)))
                    break
                except Exception:
                    continue
    return res


def wname(raw, off, name, width=64):
    nb = name.encode('utf-8')
    assert len(nb) < width, f'name too long: {name}'
    raw[off:off + width] = nb + b'\x00' * (width - len(nb))


def patch_blob(raw, spec, is_current):
    ms = [x.start() for x in re.finditer(b'STEREO', raw)]
    if len(ms) != 120 or len({b - a for a, b in zip(ms, ms[1:])}) != 1:
        return raw, False
    raw = bytearray(raw)

    # 채널명 (최대 12자 규칙은 스펙 작성 단계에서 보장)
    for c in spec['channels']:
        ch = c['ch']
        assert len(c['name']) <= 12, f"ch{ch} 이름 12자 초과: {c['name']}"
        wname(raw, ms[ch - 1] + 8, c['name'])

    # 스테레오 링크: STEREO 라벨 직전 2바이트 03 80 / 03 01
    # + 링크 페어에서 이름이 스펙에 없는 쪽(시트 빈 행)은 상대 채널 이름을 미러링
    have = {c['ch'] for c in spec['channels']}
    name_by = {c['ch']: c['name'] for c in spec['channels']}
    for a, b in spec.get('pairs', []):
        raw[ms[a - 1] - 2:ms[a - 1]] = b'\x03\x80'
        raw[ms[b - 1] - 2:ms[b - 1]] = b'\x03\x01'
        if a in name_by and b not in have:
            wname(raw, ms[b - 1] + 8, name_by[a])
        elif b in name_by and a not in have:
            wname(raw, ms[a - 1] + 8, name_by[b])

    if is_current:
        # DCA 어사인: u16 LE 마스크 @ 다음 레코드 STEREO -0x14, bit N = DCA N+1
        # Current에만 기록 (콘솔도 씬에는 안 씀)
        for c in spec['channels']:
            mask = 0
            for d in c.get('dca', []):
                mask |= 1 << (DCA_SLOTS[d] - 1)
            if mask:
                ch = c['ch']
                raw[ms[ch] - 0x14:ms[ch] - 0x12] = struct.pack('<H', mask)
        # DCA 이름 테이블 (레코드 0x58): AMBI 사용 시 DCA12 리네임 필수
        loc = _dca_base(raw, _matrix_base(raw, ms, _mix_positions(raw, ms)))
        if loc >= 0:
            for slot, nm in spec.get('dca_names', {}).items():
                s = loc + (int(slot) - 1) * 0x58
                raw[s + 1:s + 16] = nm.encode() + b'\x00' * (15 - len(nm.encode()))

    # MIX 레코드: nST/M 마커, 이름 = 컬러 문자열 -0x40, 링크+PanLink = 이름 -3 (01 80 01 / 01 01 01)
    mixpos = _mix_positions(raw, ms)
    for n, mx in spec.get('mixes', {}).items():
        n = int(n)
        if n in mixpos:
            wname(raw, mixpos[n], mx['name'], 0x40)
    for a, b in spec.get('mix_pairs', []):
        if a in mixpos and b in mixpos:
            raw[mixpos[a] - 3:mixpos[a]] = b'\x01\x80\x01'
            raw[mixpos[b] - 3:mixpos[b]] = b'\x01\x01\x01'

    # MATRIX: 고정 스트라이드 0x206 (커스텀/공장/델타 앵커 폴백)
    base = _matrix_base(raw, ms, mixpos)
    if base > 0:
        for n, mt in spec.get('matrix', {}).items():
            off = base + (int(n) - 1) * 0x206
            wname(raw, off, mt['name'], 0x40)
            if mt.get('mono'):
                raw[off - 3:off] = b'\x00\x00\x00'
            elif mt.get('link') == 'L':
                raw[off - 3:off] = b'\x01\x80\x01'
            elif mt.get('link') == 'R':
                raw[off - 3:off] = b'\x01\x01\x01'
    return bytes(raw), True


def generate(spec_path, out_path, base_path=None):
    spec = json.load(open(spec_path))
    if base_path is None:
        base_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'base', 'Reset.dm7f')
    src = open(base_path, 'rb').read()
    secs = find_sections(src)
    tail = src[src.rfind(b'#END'):]
    result = bytearray(src[:secs[0][0]])
    result[0x38:0x48] = uuid.uuid4().bytes  # 헤더 UUID (체크섬 아님)
    for h, p, plen in secs:
        name = src[h + 12:h + 28].split(b'\x00')[0].decode()
        header = bytearray(src[h:p])
        payload = src[p:p + plen]
        raw2, ok = patch_blob(zlib.decompress(payload), spec, name == 'Current')
        if ok:
            payload = zlib.compress(raw2, 1)  # 레벨 1 필수 (0x7801)
            idx = bytes(header).find(struct.pack('>I', plen))
            assert idx >= 0, 'size field not found'
            header[idx:idx + 4] = struct.pack('>I', len(payload))
        result += header
        result += payload
        result += b'\x00' * ((4 - len(result) % 4) % 4)  # 4바이트 정렬 필수
    result += tail
    open(out_path, 'wb').write(bytes(result))

    # 자체 검증
    chk = open(out_path, 'rb').read()
    n_ok = 0
    for h, p, plen in find_sections(chk):
        raw = zlib.decompress(chk[p:p + plen])
        ms = [x.start() for x in re.finditer(b'STEREO', raw)]
        if len(ms) == 120:
            for c in spec['channels']:
                got = raw[ms[c['ch'] - 1] + 8:ms[c['ch'] - 1] + 8 + 64].split(b'\x00')[0].decode()
                assert got == c['name'], (c['ch'], got, c['name'])
            n_ok += 1
        assert h % 4 == 0, 'section misaligned'
    assert chk.rfind(b'#END') % 4 == 0
    print(f'OK: {out_path} ({len(result)} bytes, {n_ok} channel sections verified)')


if __name__ == '__main__':
    generate(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
