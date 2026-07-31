#!/usr/bin/env python3
"""Waves SuperRack Performer .sprk 쇼파일 생성기.

사용법: python3 sprk_gen.py spec.json "출력.sprk" [템플릿.sprk]

spec.json 스키마:
{
  "template": "/path/디폴트 셋.sprk",     // 3번째 인자로 덮어쓰기 가능
  "racks": [
    {"rack": 1, "name": "OH", "ch": [1, 2], "chain": ["Pro-Q 4", "F6-RTA"]},
    {"rack": 2, "name": "Kick", "ch": [3], "chain": ["Pro-Q 4", "F6-RTA"]},
    {"rack": 21, "name": "Vox 1", "ch": [27], "chain": ["CrvEqtrL", "Pro-Q 4", "F6-RTA"]},
    {"rack": 17, "name": "Click 1", "ch": [22], "chain": []}
  ]
}
- rack: 1-based 랙 번호. ch: 1-based 드라이버(콘솔) 채널, 2개면 스테레오.
- chain: 템플릿 랙1(스테레오)/랙2(모노)에 존재하는 플러그인 이름만 사용 가능.
- spec에 없는 랙: 이름 "Rack N" 유지, 플러그인·라우팅 제거(빈 랙).

전제(디폴트 셋.sprk 기준): 랙1=스테레오, 랙2=모노, 두 랙이 사용할 모든
플러그인의 모노/스테레오 변형(4cc·preset)을 갖고 있어야 한다.
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path

INPUT_CLUSTER = 0     # cluster_type: Input rack
INPUTS_PROXY = 10     # cluster_type: Inputs (driver in)
OUTPUTS_PROXY = 11    # cluster_type: Outputs (driver out)
ACTIVE_SNAPSHOT = -1


def fail(msg):
    raise RuntimeError(msg)


def load_reference(cur, rack_obj_id):
    """템플릿 랙의 plug + snapshot_plugin + preset 정보를 plugin_name 키로 수집."""
    ref = {}
    for row in cur.execute(
        "select id, slot, plugin_name, plugin_4cc, additional_info, vendor_name,"
        " plug_role, ignore_latency from plug where chainer_id=? order by slot",
        (rack_obj_id,),
    ).fetchall():
        plug_id, _slot, name, fourcc, addinfo, vendor, role, ign = row
        sp = cur.execute(
            "select preset_id, setup_type_id, bypass, mute, docked, fw_posx, fw_posy,"
            " fw_scale_precentage, fw_always_on_top from snapshot_plugin"
            " where plug_id=? and snapshot_id=?",
            (plug_id, ACTIVE_SNAPSHOT),
        ).fetchall()
        presets = {}
        for p in sp:
            preset_id = p[0]
            presets[preset_id] = cur.execute(
                "select hash, preset from plugin_preset where id=?", (preset_id,)
            ).fetchone()
        ref[name] = {
            "fourcc": fourcc, "addinfo": addinfo, "vendor": vendor,
            "role": role, "ignore_latency": ign,
            "snapshot_plugins": sp, "presets": presets,
        }
    return ref


def main():
    if len(sys.argv) < 3:
        fail("사용법: sprk_gen.py spec.json 출력.sprk [템플릿.sprk]")
    spec = json.loads(Path(sys.argv[1]).read_text())
    out_path = Path(sys.argv[2])
    template = Path(sys.argv[3] if len(sys.argv) > 3 else spec["template"])
    if not template.is_file():
        fail(f"템플릿 없음: {template}")

    racks = spec["racks"]
    seen_racks, seen_chs = set(), set()
    for r in racks:
        if r["rack"] in seen_racks:
            fail(f"랙 {r['rack']} 중복")
        seen_racks.add(r["rack"])
        if len(r["ch"]) not in (1, 2):
            fail(f"랙 {r['rack']}: ch는 1개(모노) 또는 2개(스테레오)")
        for c in r["ch"]:
            if c in seen_chs:
                fail(f"드라이버 채널 {c} 중복")
            seen_chs.add(c)

    shutil.copyfile(template, out_path)
    db = sqlite3.connect(out_path)
    db.execute("PRAGMA foreign_keys=ON")
    cur = db.cursor()

    # 랙 object id 맵 (obj_index 0-based → id)
    rack_ids = dict(cur.execute(
        "select obj_index, id from object where obj_type=?", (INPUT_CLUSTER,)
    ).fetchall())
    num_racks = len(rack_ids)
    for r in racks:
        if r["rack"] - 1 not in rack_ids:
            fail(f"랙 {r['rack']}: 템플릿에 랙이 {num_racks}개뿐")

    # 레퍼런스: 랙1=스테레오, 랙2=모노
    ref_stereo = load_reference(cur, rack_ids[0])
    ref_mono = load_reference(cur, rack_ids[1])
    for r in racks:
        ref = ref_stereo if len(r["ch"]) == 2 else ref_mono
        for name in r["chain"]:
            if name not in ref:
                kind = "스테레오" if len(r["ch"]) == 2 else "모노"
                fail(f"랙 {r['rack']}: 템플릿에 {kind} 플러그인 '{name}' 없음")

    # 기존 plug 전부 삭제 전에 레퍼런스 preset 원본을 확보해 두었으므로,
    # 삭제(트리거가 ref_count 0 preset 자동 제거) 후 필요한 것만 재삽입한다.
    # FK 캐스케이드는 트리거를 안 태울 수 있어 snapshot_plugin을 명시적으로 먼저 지운다.
    cur.execute(
        "delete from snapshot_plugin where plug_id in"
        " (select id from plug where chainer_id in"
        "  (select id from object where obj_type=?))",
        (INPUT_CLUSTER,),
    )
    cur.execute(
        "delete from plug where chainer_id in (select id from object where obj_type=?)",
        (INPUT_CLUSTER,),
    )
    # 라우팅도 전부 재작성
    cur.execute(
        "delete from routes where src_cluster_type=? or dst_cluster_type=?",
        (INPUT_CLUSTER, INPUT_CLUSTER),
    )

    next_plug_id = (cur.execute("select coalesce(max(id),0) from plug").fetchone()[0]) + 1
    next_sp_id = (cur.execute("select coalesce(max(id),0) from snapshot_plugin").fetchone()[0]) + 1
    next_route_id = (cur.execute("select coalesce(max(id),0) from routes").fetchone()[0]) + 1

    def ensure_preset(preset_id, hash_, blob):
        row = cur.execute("select id from plugin_preset where hash=?", (hash_,)).fetchone()
        if row:
            return row[0]
        free = cur.execute("select 1 from plugin_preset where id=?", (preset_id,)).fetchone()
        if free:  # id 충돌 시 새 id
            preset_id = cur.execute("select max(id)+1 from plugin_preset").fetchone()[0]
        cur.execute(
            "insert into plugin_preset (id, hash, ref_count, preset) values (?,?,0,?)",
            (preset_id, hash_, blob),
        )
        return preset_id

    spec_by_rack = {r["rack"]: r for r in racks}
    for idx in sorted(rack_ids):  # obj_index 0-based
        obj_id = rack_ids[idx]
        r = spec_by_rack.get(idx + 1)
        stereo = bool(r) and len(r["ch"]) == 2
        name = r["name"] if r else f"Rack {idx + 1}"

        cur.execute(
            "update snapshot_chainer set name=? where chainer_id=? and snapshot_id=?",
            (name, obj_id, ACTIVE_SNAPSHOT),
        )
        n = 2 if stereo else 1
        fmt = 101 if stereo else 100
        cur.execute(
            "update chainer set num_inputs=?, num_outputs=?, input_stem_format=?,"
            " output_stem_format=? where obj_id=?",
            (n, n, fmt, fmt, obj_id),
        )
        if not r:
            continue

        ref = ref_stereo if stereo else ref_mono
        for slot, plug_name in enumerate(r["chain"], start=1):
            info = ref[plug_name]
            cur.execute(
                "insert into plug (id, chainer_id, slot, plugin_name, plugin_4cc,"
                " additional_info, disabled, recall_safe, side_chain, hot_plugin,"
                " vendor_name, plug_role, ignore_latency)"
                " values (?,?,?,?,?,?,0,0,0,-1,?,?,?)",
                (next_plug_id, obj_id, slot, plug_name, info["fourcc"],
                 info["addinfo"], info["vendor"], info["role"], info["ignore_latency"]),
            )
            for (preset_id, setup_type, bypass, mute, docked, px, py, scale, aot) in info["snapshot_plugins"]:
                hash_, blob = info["presets"][preset_id]
                pid = ensure_preset(preset_id, hash_, blob)
                cur.execute(
                    "insert into snapshot_plugin (id, plug_id, preset_id, snapshot_id,"
                    " setup_type_id, bypass, mute, docked, fw_posx, fw_posy,"
                    " fw_scale_precentage, fw_always_on_top)"
                    " values (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (next_sp_id, next_plug_id, pid, ACTIVE_SNAPSHOT,
                     setup_type, bypass, mute, docked, px, py, scale, aot),
                )
                next_sp_id += 1
            next_plug_id += 1

        # 라우팅 (인서트 방식: 드라이버 in ch == out ch)
        for lr, ch in enumerate(r["ch"]):
            drv = ch - 1
            cur.execute(
                "insert into routes values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (next_route_id, INPUTS_PROXY, 0, 0, -1, drv, 8, 1,
                 INPUT_CLUSTER, idx, 0, 0, lr, -1, 0, 4),
            )
            next_route_id += 1
            cur.execute(
                "insert into routes values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (next_route_id, INPUT_CLUSTER, idx, -1, 0, lr, -1, -1,
                 OUTPUTS_PROXY, 0, 0, -1, drv, -1, 0, 4),
            )
            next_route_id += 1

    db.commit()

    # ── 자체 검증 ──────────────────────────────────────────
    for r in racks:
        obj_id = rack_ids[r["rack"] - 1]
        got_name = cur.execute(
            "select name from snapshot_chainer where chainer_id=? and snapshot_id=?",
            (obj_id, ACTIVE_SNAPSHOT)).fetchone()[0]
        assert got_name == r["name"], (got_name, r["name"])
        ni, fmt = cur.execute(
            "select num_inputs, input_stem_format from chainer where obj_id=?",
            (obj_id,)).fetchone()
        assert (ni, fmt) == ((2, 101) if len(r["ch"]) == 2 else (1, 100)), r
        chain = [x[0] for x in cur.execute(
            "select plugin_name from plug where chainer_id=? order by slot", (obj_id,))]
        assert chain == r["chain"], (r["rack"], chain, r["chain"])
        for plug_id, in cur.execute("select id from plug where chainer_id=?", (obj_id,)):
            nsp = cur.execute(
                "select count(*) from snapshot_plugin where plug_id=?", (plug_id,)).fetchone()[0]
            assert nsp >= 1, f"랙 {r['rack']} plug {plug_id}: snapshot_plugin 없음"
        in_routes = cur.execute(
            "select src_channel_index from routes where dst_cluster_type=? and"
            " dst_cluster_type_index=? order by dst_channel_index",
            (INPUT_CLUSTER, r["rack"] - 1)).fetchall()
        out_routes = cur.execute(
            "select dst_channel_index from routes where src_cluster_type=? and"
            " src_cluster_type_index=? order by src_channel_index",
            (INPUT_CLUSTER, r["rack"] - 1)).fetchall()
        want = [(c - 1,) for c in r["ch"]]
        assert in_routes == want and out_routes == want, (r["rack"], in_routes, out_routes)
    # 고아 preset / ref_count 일관성
    bad = cur.execute(
        "select p.id, p.ref_count, count(sp.id) from plugin_preset p"
        " left join snapshot_plugin sp on sp.preset_id=p.id"
        " group by p.id having p.ref_count <> count(sp.id)").fetchall()
    assert not bad, f"preset ref_count 불일치: {bad}"
    db.close()

    n_st = sum(1 for r in racks if len(r["ch"]) == 2)
    print(f"OK: {out_path.name} — 랙 {len(racks)}개 사용(스테레오 {n_st}), "
          f"빈 랙 {num_racks - len(racks)}개, 검증 통과")


def generate(spec_path, out_path, template_path=None):
    """앱 서버용 in-process 호출 (실패 시 RuntimeError/AssertionError)"""
    old = sys.argv
    sys.argv = ["sprk_gen.py", spec_path, out_path] + ([template_path] if template_path else [])
    try:
        main()
    finally:
        sys.argv = old


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, AssertionError) as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)
