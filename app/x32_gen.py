#!/usr/bin/env python3
"""Behringer X32 쇼파일 생성기 — 채널시트 스펙(JSON) → .x32
채널명, 라우팅, 입출력 할당을 자동 생성.
사용: python3 x32_gen.py spec.json 출력경로.x32
"""
import json, sys, os, xml.etree.ElementTree as ET
from xml.dom import minidom


def _create_base_x32():
    """X32 기본 쇼파일 구조 생성"""
    root = ET.Element('X32', attrib={'revision': '1'})

    # 채널 (32개)
    ch_elem = ET.SubElement(root, 'Channel')
    for i in range(32):
        ch_num = i + 1
        ch_name = f'Ch {ch_num:02d}'
        ET.SubElement(ch_elem, 'Ch', attrib={
            'index': str(i),
            'name': ch_name,
            'on': '1',
            'fader': '0.0',
            'pan': '0.0',
            'color': '0',
        })

    # 보조 입력 (6개)
    aux_elem = ET.SubElement(root, 'Auxin')
    for i in range(6):
        aux_num = i + 1
        aux_name = f'Aux {aux_num}'
        ET.SubElement(aux_elem, 'Aux', attrib={
            'index': str(i),
            'name': aux_name,
            'on': '1',
            'fader': '0.0',
            'pan': '0.0',
        })

    # 믹스 (16개)
    mix_elem = ET.SubElement(root, 'Mix')
    for i in range(16):
        mix_num = i + 1
        mix_name = f'Mix {mix_num}'
        ET.SubElement(mix_elem, 'Mix', attrib={
            'index': str(i),
            'name': mix_name,
            'on': '1',
            'fader': '0.0',
            'pan': '0.0',
        })

    # 매트릭스 (6개)
    matrix_elem = ET.SubElement(root, 'Matrix')
    for i in range(6):
        matrix_num = i + 1
        matrix_name = f'Matrix {matrix_num}'
        ET.SubElement(matrix_elem, 'Mtrx', attrib={
            'index': str(i),
            'name': matrix_name,
            'on': '1',
            'fader': '0.0',
        })

    # DCA 그룹 (8개)
    dca_elem = ET.SubElement(root, 'DCA')
    for i in range(8):
        dca_num = i + 1
        dca_name = f'DCA {dca_num}'
        ET.SubElement(dca_elem, 'Group', attrib={
            'index': str(i),
            'name': dca_name,
            'on': '1',
            'fader': '0.0',
        })

    return root


def _prettify_xml(elem):
    """XML 예쁘게 포매팅"""
    rough_string = ET.tostring(elem, encoding='utf-8')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent='  ', encoding='utf-8')


def patch_x32(root, spec):
    """스펙에 따라 X32 쇼파일 패치"""

    # 채널명 업데이트
    ch_elem = root.find('Channel')
    if ch_elem is not None:
        for c in spec.get('channels', []):
            ch_idx = c['ch'] - 1
            if ch_idx < 32:
                ch = ch_elem.findall('Ch')[ch_idx]
                ch.set('name', c['name'][:16])  # X32는 채널명 16자 제한

    # 믹스명 업데이트
    mix_elem = root.find('Mix')
    if mix_elem is not None:
        for n, mx in spec.get('mixes', {}).items():
            n = int(n)
            if n >= 1 and n <= 16:
                mix = mix_elem.findall('Mix')[n - 1]
                mix.set('name', mx['name'][:12])  # 믹스명 12자 제한

    # 매트릭스명 업데이트
    matrix_elem = root.find('Matrix')
    if matrix_elem is not None:
        for n, mt in spec.get('matrix', {}).items():
            n = int(n)
            if n >= 1 and n <= 6:
                mtrx = matrix_elem.findall('Mtrx')[n - 1]
                mtrx.set('name', mt['name'][:12])

    # DCA 그룹명 업데이트
    dca_elem = root.find('DCA')
    if dca_elem is not None:
        dca_names = spec.get('dca_names', {})
        for slot, nm in dca_names.items():
            slot_idx = int(slot) - 1
            if slot_idx >= 0 and slot_idx < 8:
                dca = dca_elem.findall('Group')[slot_idx]
                dca.set('name', nm[:12])

    return root


def generate(spec_path, out_path):
    """스펙 JSON → X32 쇼파일 생성"""
    spec = json.load(open(spec_path))

    # 기본 구조 생성
    root = _create_base_x32()

    # 스펙에 따라 패치
    root = patch_x32(root, spec)

    # XML 작성
    xml_str = _prettify_xml(root)
    with open(out_path, 'wb') as f:
        f.write(xml_str)

    print(f'OK: {out_path} (Behringer X32 쇼파일 생성됨)')


if __name__ == '__main__':
    generate(sys.argv[1], sys.argv[2])
