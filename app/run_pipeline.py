#!/usr/bin/env python3
"""원클릭 파이프라인: 채널시트 → (DM7|KLANG) 쇼파일 생성 + 배포
사용: python3 run_pipeline.py 시트경로 [--dm7] [--klang]
출력: 생성된 파일 경로들 (줄 단위) — 앱/스크립트가 결과 표시에 사용
"""
import sys, os, json, shutil, datetime, re

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sheet2spec, dm7_gen, klang_gen

PRESETS = os.path.expanduser('~/Library/Containers/com.klang.klangapp2/Data/Library/KLANGtechnologies/Presets')


def next_version(base_name, dirs, exts):
    """같은 이름 파일이 있으면 _V2, _V3… 자동 버전 업"""
    def exists(n):
        return any(os.path.exists(os.path.join(d, n + e)) for d in dirs for e in exts)
    if not exists(base_name):
        return base_name
    v = 2
    while exists(f'{base_name}_V{v}'):
        v += 1
    return f'{base_name}_V{v}'


def main():
    args = sys.argv[1:]
    sheet = args[0]
    want_dm7 = '--dm7' in args
    want_klang = '--klang' in args
    if not (want_dm7 or want_klang):
        want_dm7 = want_klang = True

    cfg = json.load(open(os.path.join(HERE, 'config.json')))
    dm7_dir = cfg.get('dm7_out_dir')
    klang_dir = cfg.get('klang_out_dir')
    os.makedirs(dm7_dir, exist_ok=True)
    os.makedirs(klang_dir, exist_ok=True)

    spec = sheet2spec.build_spec(sheet)
    spec['name'] = next_version(spec['name'], [dm7_dir, klang_dir], ['.dm7f', '.KLANGshow'])
    spec['snapshot'] = (spec.get('ascii_name') or spec['name']).replace('_', '')[:10]
    spec_path = '/tmp/showfile_spec.json'
    json.dump(spec, open(spec_path, 'w'), ensure_ascii=False)

    made = []
    if want_dm7:
        out = os.path.join(dm7_dir, spec['name'] + '.dm7f')
        dm7_gen.generate(spec_path, out)
        made.append(out)
    if want_klang:
        out = os.path.join(klang_dir, spec['name'] + '.KLANGshow')
        klang_gen.generate(spec_path, out)
        made.append(out)
        if cfg.get('klang_presets_copy') and os.path.isdir(PRESETS):
            shutil.copy(out, os.path.join(PRESETS, os.path.basename(out)))

    print('RESULT')
    for m in made:
        print(m)


if __name__ == '__main__':
    main()
