"""录音删段交付 API 的自动化测试。

运行：python -m pytest tests/ -v
"""

import io
import json
import math
import os
import struct
import tempfile
import threading
import wave
import zipfile

# 必须在 import app 之前指向临时数据库，避免污染工作目录
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="segdel-import-"), "app.db")

import app as app_module
import pytest

RATE = 16000


def make_wav(seconds=10.0, rate=RATE, channels=1, sampwidth=2, freq=440):
    """生成测试用 WAV 字节。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        n = int(seconds * rate)
        if sampwidth == 2:
            frames = b"".join(
                struct.pack("<h", int(10000 * math.sin(2 * math.pi * freq * i / rate)))
                for i in range(n)
            )
        else:  # 8 bit 用于构造非法规格
            frames = bytes(i % 256 for i in range(n))
        if channels == 2:
            frames = b"".join(
                struct.pack("<hh", int(5000 * math.sin(2 * math.pi * freq * i / rate)), 0)
                for i in range(n)
            )
        w.writeframes(frames)
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(app_module, "DB_PATH", str(db_file))
    app_module.init_db()
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


def create_program(client, wav_bytes=None, chapters=None, must_keep=None):
    wav_bytes = wav_bytes if wav_bytes is not None else make_wav()
    data = {"audio": (io.BytesIO(wav_bytes), "test.wav")}
    if chapters is not None:
        data["chapters"] = json.dumps(chapters)
    if must_keep is not None:
        data["must_keep"] = json.dumps(must_keep)
    return client.post("/programs", data=data, content_type="multipart/form-data")


# ---- 创建节目 ---------------------------------------------------------------

def test_create_ok_initial_revision_keeps_everything(client):
    chapters = [{"title": "开场", "start_frame": 0}, {"title": "正文", "start_frame": 80000}]
    must_keep = [{"start_frame": 16000, "end_frame": 32000}]
    resp = create_program(client, chapters=chapters, must_keep=must_keep)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["revision"] == 0
    assert body["total_frames"] == 10 * RATE
    assert body["keep_segments"] == [{"start_frame": 0, "end_frame": 10 * RATE}]
    assert body["product_frames"] == 10 * RATE
    # 初始版本成品坐标 == 原始坐标
    assert [c["product_start_frame"] for c in body["chapters"]] == [0, 80000]
    assert body["must_keep"] == [must_keep[0]]


def test_create_rejects_bad_wav(client):
    # 非 WAV
    assert create_program(client, wav_bytes=b"not a wav").status_code == 400
    # 立体声
    resp = create_program(client, wav_bytes=make_wav(channels=2))
    assert resp.status_code == 400 and "单声道" in resp.get_json()["message"]
    # 8 kHz
    resp = create_program(client, wav_bytes=make_wav(rate=8000))
    assert resp.status_code == 400 and "16000" in resp.get_json()["message"]
    # 8 位采样
    resp = create_program(client, wav_bytes=make_wav(sampwidth=1))
    assert resp.status_code == 400 and "16" in resp.get_json()["message"]
    # 超过 30 秒
    resp = create_program(client, wav_bytes=make_wav(seconds=31))
    assert resp.status_code == 400 and "30" in resp.get_json()["message"]


def test_create_rejects_bad_chapters_and_must_keep(client):
    resp = create_program(client, chapters=[{"start_frame": 10 * RATE}])  # 越界（右开）
    assert resp.status_code == 400
    resp = create_program(client, chapters=[100, 100])  # 重复
    assert resp.status_code == 400
    resp = create_program(client, must_keep=[{"start_frame": 100, "end_frame": 100}])  # 空区间
    assert resp.status_code == 400
    resp = create_program(client, must_keep=[{"start_frame": 0, "end_frame": 10 * RATE + 1}])
    assert resp.status_code == 400


# ---- 提交保留清单 ------------------------------------------------------------

def _make_program(client):
    chapters = [
        {"title": "c0", "start_frame": 0},
        {"title": "c1", "start_frame": 40000},
        {"title": "c2", "start_frame": 48000},
        {"title": "c3", "start_frame": 80000},
        {"title": "c4", "start_frame": 88000},
        {"title": "c5", "start_frame": 120000},
    ]
    must_keep = [{"start_frame": 50000, "end_frame": 60000}]
    resp = create_program(client, chapters=chapters, must_keep=must_keep)
    assert resp.status_code == 201
    return resp.get_json()["program_id"]


def test_put_keeps_ok_and_projection(client):
    pid = _make_program(client)
    # 保留 [0,32000) [48000,88000) [112000,160000)
    resp = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 88000},
            {"start_frame": 112000, "end_frame": 160000},
        ],
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["revision"] == 1
    assert body["product_frames"] == 32000 + 40000 + 48000

    proj = {c["title"]: c["product_start_frame"] for c in body["chapters"]}
    # c0=0 在段首 -> 0；c1=40000 被删；c2=48000 位于保留段起点 -> 保留在 32000；
    # c3=80000 -> 32000+(80000-48000)=64000；c4=88000 是段2右端（右开）且不在段3 -> 删除；
    # c5=120000 -> 32000+40000+(120000-112000)=80000
    assert proj == {"c0": 0, "c2": 32000, "c3": 64000, "c5": 80000}


def test_put_keeps_stale_revision_keeps_old_state(client):
    pid = _make_program(client)
    ok = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [{"start_frame": 48000, "end_frame": 88000}],
    })
    assert ok.status_code == 200 and ok.get_json()["revision"] == 1

    # 用过期的 0 再提交 -> 409，说明原因，状态不变
    stale = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [{"start_frame": 0, "end_frame": 160000}],
    })
    assert stale.status_code == 409
    body = stale.get_json()
    assert body["error"] == "stale_revision"
    assert body["current_revision"] == 1
    assert "0" in body["message"] and "1" in body["message"]

    cur = client.get(f"/programs/{pid}").get_json()
    assert cur["revision"] == 1
    assert cur["keep_segments"] == [{"start_frame": 48000, "end_frame": 88000}]


def test_put_keeps_invalid_segments_rejected(client):
    pid = _make_program(client)
    cases = [
        [{"start_frame": 100, "end_frame": 100}],                      # 空段
        [{"start_frame": 200, "end_frame": 100}],                      # start > end
        [{"start_frame": -1, "end_frame": 100}],                       # 越界
        [{"start_frame": 0, "end_frame": 160001}],                     # 越界
        [{"start_frame": 50000, "end_frame": 60000},
         {"start_frame": 100, "end_frame": 200}],                      # 未升序
        [{"start_frame": 0, "end_frame": 60000},
         {"start_frame": 59000, "end_frame": 70000}],                  # 重叠
    ]
    for segs in cases:
        resp = client.put(f"/programs/{pid}/keeps",
                          json={"expected_revision": 0, "keep_segments": segs})
        assert resp.status_code == 400, segs
        assert resp.get_json()["message"]
    # 全部失败后修订号不变
    assert client.get(f"/programs/{pid}").get_json()["revision"] == 0


def test_put_keeps_must_keep_must_be_covered(client):
    pid = _make_program(client)
    # 必留口播 [50000,60000) 被切成两段，未完整落入任何保留段
    resp = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 48000, "end_frame": 55000},
            {"start_frame": 55000, "end_frame": 88000},
        ],
    })
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "must_keep_uncovered"
    assert client.get(f"/programs/{pid}").get_json()["revision"] == 0


def test_repeated_edits_use_original_frames_no_drift(client):
    pid = _make_program(client)
    # 第一次：删掉 [32000,48000)
    r1 = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 160000},
        ],
    })
    assert r1.status_code == 200
    # 第二次：在原始坐标下进一步删掉 [96000,112000)
    r2 = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 1,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 96000},
            {"start_frame": 112000, "end_frame": 160000},
        ],
    })
    assert r2.status_code == 200
    body = r2.get_json()
    assert body["revision"] == 2
    # 成品帧 = 32000 + 48000 + 48000 = 128000，与直接按原始帧计算一致
    assert body["product_frames"] == 32000 + 48000 + 48000
    proj = {c["title"]: c["product_start_frame"] for c in body["chapters"]}
    # c5=120000 -> 32000+48000+(120000-112000)=88000
    assert proj["c5"] == 88000
    assert proj["c3"] == 32000 + (80000 - 48000)


# ---- 下载 --------------------------------------------------------------------

def test_download_zip_consistent_with_revision(client):
    pid = _make_program(client)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 88000},
        ],
    })
    resp = client.get(f"/programs/{pid}/download")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    assert set(zf.namelist()) == {"audio.wav", "chapters.json"}

    # WAV 是真正裁接过的：帧数 == 保留段总长
    with wave.open(io.BytesIO(zf.read("audio.wav")), "rb") as w:
        assert w.getframerate() == RATE
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 32000 + 40000

    doc = json.loads(zf.read("chapters.json"))
    assert doc["revision"] == 1
    assert doc["product_frames"] == 32000 + 40000
    starts = {c["title"]: c["start_frame"] for c in doc["chapters"]}
    assert starts == {"c0": 0, "c2": 32000, "c3": 64000}

    # 再次编辑后下载：WAV 与 JSON 同属修订 2
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 1,
        "keep_segments": [{"start_frame": 48000, "end_frame": 88000}],
    })
    resp = client.get(f"/programs/{pid}/download")
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    doc = json.loads(zf.read("chapters.json"))
    with wave.open(io.BytesIO(zf.read("audio.wav")), "rb") as w:
        wav_frames = w.getnframes()
    assert doc["revision"] == 2
    assert wav_frames == doc["product_frames"] == 40000
    starts = {c["title"]: c["start_frame"] for c in doc["chapters"]}
    assert starts == {"c2": 0, "c3": 32000}


def test_download_wav_content_matches_original_splice(client):
    """裁接结果应与原始音频对应帧完全一致（原始音频不变）。"""
    wav_bytes = make_wav(seconds=2)
    resp = create_program(client, wav_bytes=wav_bytes)
    pid = resp.get_json()["program_id"]
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 100, "end_frame": 200},
            {"start_frame": 300, "end_frame": 500},
        ],
    })
    zf = zipfile.ZipFile(io.BytesIO(client.get(f"/programs/{pid}/download").data))
    product = zf.read("audio.wav")
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        w.setpos(100)
        expect = w.readframes(100)
        w.setpos(300)
        expect += w.readframes(200)
    with wave.open(io.BytesIO(product), "rb") as w:
        actual = w.readframes(w.getnframes())
    assert actual == expect


# ---- 持久化 ------------------------------------------------------------------

def test_program_survives_reconnect(client, tmp_path, monkeypatch):
    """模拟重启：所有状态都在 SQLite 文件中，新连接可继续编辑。"""
    pid = _make_program(client)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [{"start_frame": 48000, "end_frame": 88000}],
    })
    # 重新指向同一个数据库文件并重新初始化（等价于进程重启后打开已有库）
    db_path = app_module.DB_PATH
    app_module.init_db()
    cur = client.get(f"/programs/{pid}").get_json()
    assert cur["revision"] == 1
    nxt = client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 1,
        "keep_segments": [{"start_frame": 50000, "end_frame": 60000}],
    })
    assert nxt.status_code == 200 and nxt.get_json()["revision"] == 2


def test_unknown_program_404(client):
    assert client.get("/programs/nope").status_code == 404
    assert client.put("/programs/nope/keeps",
                      json={"expected_revision": 0, "keep_segments": []}).status_code == 404
    assert client.get("/programs/nope/download").status_code == 404
    assert client.post("/programs/nope/merges",
                       json={"base_revision": 0, "keep_segments": []}).status_code == 404


# ---- 合入旧版剪辑建议 ---------------------------------------------------------

def _merge(client, pid, base_revision, keep_segments):
    return client.post(f"/programs/{pid}/merges", json={
        "base_revision": base_revision,
        "keep_segments": keep_segments,
    })


def _seg(body):
    return [(s["start_frame"], s["end_frame"]) for s in body["keep_segments"]]


def test_merge_disjoint_changes_both_sides_applied(client):
    # 基版 0：全保留。当前侧删 [32000,48000)；建议侧基于 0 删 [96000,112000)，互不相交
    pid = _make_program(client)  # 必留 [50000,60000)
    assert client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 160000},
        ],
    }).status_code == 200

    resp = _merge(client, pid, 0, [
        {"start_frame": 0, "end_frame": 96000},
        {"start_frame": 112000, "end_frame": 160000},
    ])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["revision"] == 2
    # 两处删除都生效，必留 [50000,60000) 完整落入 [48000,96000)
    assert _seg(body) == [(0, 32000), (48000, 96000), (112000, 160000)]
    assert body["product_frames"] == 32000 + 48000 + 48000


def test_merge_conflict_when_both_delete_same_region(client):
    # 当前删 [32000,48000)，建议也基于 0 删同一处（哪怕同向删除，也不能擅自覆盖）
    pid = _make_program(client)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 160000},
        ],
    })
    resp = _merge(client, pid, 0, [
        {"start_frame": 0, "end_frame": 40000},
        {"start_frame": 48000, "end_frame": 160000},
    ])
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["error"] == "merge_conflict"
    assert body["base_revision"] == 0
    assert body["current_revision"] == 1
    # 当前改动 [32000,48000)，建议改动 [40000,48000)，相交段为 [40000,48000)
    assert body["conflicts"] == [{"start_frame": 40000, "end_frame": 48000}]
    # 未写入新修订，当前版本保持不变
    cur = client.get(f"/programs/{pid}").get_json()
    assert cur["revision"] == 1
    assert _seg(cur) == [(0, 32000), (48000, 160000)]


def test_merge_conflict_overlapping_different_regions(client):
    # 当前删 [32000,48000)，建议基于 0 删 [40000,64000)，冲突 [40000,48000)
    pid = _make_program(client)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 160000},
        ],
    })
    resp = _merge(client, pid, 0, [
        {"start_frame": 0, "end_frame": 40000},
        {"start_frame": 64000, "end_frame": 160000},
    ])
    assert resp.status_code == 409
    assert resp.get_json()["conflicts"] == [{"start_frame": 40000, "end_frame": 48000}]
    assert client.get(f"/programs/{pid}").get_json()["revision"] == 1


def test_merge_against_nonzero_base_three_way(client):
    # 基版 1：已删 [32000,48000)。当前在基版之上删 [96000,112000)；
    # 建议在基版 1 之上删 [128000,144000)。相对同一基版两侧改动不相交 -> 合入
    pid = _make_program(client)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 160000},
        ],
    })
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 1,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 96000},
            {"start_frame": 112000, "end_frame": 160000},
        ],
    })
    # 建议基于修订 1（其视角里 [96000,112000) 仍是保留的，改 [128000,144000)）
    resp = _merge(client, pid, 1, [
        {"start_frame": 0, "end_frame": 32000},
        {"start_frame": 48000, "end_frame": 128000},
        {"start_frame": 144000, "end_frame": 160000},
    ])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["revision"] == 3
    assert _seg(body) == [
        (0, 32000), (48000, 96000), (112000, 128000), (144000, 160000),
    ]


def test_merge_change_already_present_in_base_is_not_a_conflict(client):
    # 建议基于修订 1（已删 [32000,48000)）继续删 [128000,144000)；
    # 当前修订 2 另行删 [96000,112000)。基版已有的共同删除不应计入冲突
    pid = _make_program(client)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 160000},
        ],
    })
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 1,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 96000},
            {"start_frame": 112000, "end_frame": 160000},
        ],
    })
    resp = _merge(client, pid, 1, [
        {"start_frame": 0, "end_frame": 32000},
        {"start_frame": 48000, "end_frame": 128000},
        {"start_frame": 144000, "end_frame": 160000},
    ])
    assert resp.status_code == 200
    assert _seg(resp.get_json()) == [
        (0, 32000), (48000, 96000), (112000, 128000), (144000, 160000),
    ]


def test_merge_stale_suggestion_conflicts_with_later_current_edit(client):
    # 建议基于 0 删 [96000,112000)；当前修订 1 已删 [100000,120000)，
    # 两侧相对基版 0 的改动在 [100000,112000) 相交 -> 冲突
    pid = _make_program(client)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 100000},
            {"start_frame": 120000, "end_frame": 160000},
        ],
    })
    resp = _merge(client, pid, 0, [
        {"start_frame": 0, "end_frame": 96000},
        {"start_frame": 112000, "end_frame": 160000},
    ])
    assert resp.status_code == 409
    assert resp.get_json()["conflicts"] == [{"start_frame": 100000, "end_frame": 112000}]


def test_merge_base_equals_current_is_fast_forward(client):
    # 建议依据的基版就是当前修订：当前侧无改动，合入等价于采纳建议
    pid = _make_program(client)
    resp = _merge(client, pid, 0, [
        {"start_frame": 0, "end_frame": 32000},
        {"start_frame": 48000, "end_frame": 160000},
    ])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["revision"] == 1
    assert _seg(body) == [(0, 32000), (48000, 160000)]


def test_merge_unknown_base_revision_404_and_unchanged(client):
    pid = _make_program(client)
    resp = _merge(client, pid, 99, [{"start_frame": 0, "end_frame": 160000}])
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "base_revision_not_found"
    assert resp.get_json()["current_revision"] == 0
    cur = client.get(f"/programs/{pid}").get_json()
    assert cur["revision"] == 0
    assert _seg(cur) == [(0, 160000)]


def test_merge_invalid_suggestion_rejected(client):
    pid = _make_program(client)
    # 非数组
    resp = client.post(f"/programs/{pid}/merges",
                       json={"base_revision": 0, "keep_segments": {"x": 1}})
    assert resp.status_code == 400
    # 空段 / 越界 / 未升序
    for segs in [
        [{"start_frame": 100, "end_frame": 100}],
        [{"start_frame": 0, "end_frame": 160001}],
        [{"start_frame": 100, "end_frame": 200}, {"start_frame": 0, "end_frame": 50}],
    ]:
        resp = _merge(client, pid, 0, segs)
        assert resp.status_code == 400, segs
    # 缺字段 / 非法 base_revision
    assert client.post(f"/programs/{pid}/merges",
                       json={"keep_segments": []}).status_code == 400
    assert client.post(f"/programs/{pid}/merges",
                       json={"base_revision": -1, "keep_segments": []}).status_code == 400
    assert client.post(f"/programs/{pid}/merges",
                       data="not json", content_type="application/json").status_code == 400
    assert client.get(f"/programs/{pid}").get_json()["revision"] == 0


def test_merge_result_must_keep_must_be_in_single_segment(client):
    # 两侧删除不相交（因此无冲突），但建议删在必留口播内部，
    # 合并后必留口播 [50000,60000) 被切成两段 -> 400，保持现状
    pid = _make_program(client)  # 必留 [50000,60000)
    # 当前侧删必留之外的 [32000,48000)，必留仍完整落在 [48000,160000)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 160000},
        ],
    })
    # 建议基于 0 删必留口播内部的 [54000,56000)（结构合法，校验只针对合并结果）；
    # 其改动区间与当前侧 [32000,48000) 不相交，但合并后必留口播被切断
    resp = _merge(client, pid, 0, [
        {"start_frame": 0, "end_frame": 54000},
        {"start_frame": 56000, "end_frame": 160000},
    ])
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "must_keep_uncovered"
    cur = client.get(f"/programs/{pid}").get_json()
    assert cur["revision"] == 1
    assert _seg(cur) == [(0, 32000), (48000, 160000)]


def test_merge_adjacent_changes_are_not_conflicts(client):
    # 当前删 [32000,40000)，建议基于 0 删相邻的 [40000,48000)：
    # 首尾相接但区间不相交，可合入（必留 [50000,60000) 在 [48000,160000) 内）
    pid = _make_program(client)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 40000, "end_frame": 160000},
        ],
    })
    resp = _merge(client, pid, 0, [
        {"start_frame": 0, "end_frame": 40000},
        {"start_frame": 48000, "end_frame": 160000},
    ])
    assert resp.status_code == 200
    body = resp.get_json()
    # [32000,48000) 被两侧合删
    assert _seg(body) == [(0, 32000), (48000, 160000)]


def test_merge_visible_in_chapters_and_download_same_revision(client):
    pid = _make_program(client)
    # 当前删 [32000,48000)
    client.put(f"/programs/{pid}/keeps", json={
        "expected_revision": 0,
        "keep_segments": [
            {"start_frame": 0, "end_frame": 32000},
            {"start_frame": 48000, "end_frame": 160000},
        ],
    })
    # 建议基于 0 删 [96000,112000)
    merged = _merge(client, pid, 0, [
        {"start_frame": 0, "end_frame": 96000},
        {"start_frame": 112000, "end_frame": 160000},
    ])
    rev = merged.get_json()["revision"]
    assert rev == 2

    # 章节查询反映合并后的同一修订
    cur = client.get(f"/programs/{pid}").get_json()
    assert cur["revision"] == 2
    proj = {c["title"]: c["product_start_frame"] for c in cur["chapters"]}
    # 保留 [0,32000)[48000,96000)[112000,160000)
    # c5=120000 -> 32000+48000+(120000-112000)=88000
    assert proj["c5"] == 88000

    # ZIP 下载与查询同一修订：音频帧数 == product_frames，章节一致
    zf = zipfile.ZipFile(io.BytesIO(client.get(f"/programs/{pid}/download").data))
    with wave.open(io.BytesIO(zf.read("audio.wav")), "rb") as w:
        wav_frames = w.getnframes()
    doc = json.loads(zf.read("chapters.json"))
    assert doc["revision"] == 2
    assert wav_frames == doc["product_frames"] == cur["product_frames"] == 128000
    assert {c["title"]: c["start_frame"] for c in doc["chapters"]} == proj


def test_concurrent_merges_do_not_lose_effective_changes(client):
    # 两个线程同时基于修订 0 合入互不相交的删除建议：写锁串行化，
    # 后到者应在新基态之上再次合入（两侧仍不相交），最终两处删除都保留。
    pid = _make_program(client)
    barrier = threading.Barrier(2)
    results = [None, None]

    def do_merge(idx, drop):
        start, end = drop
        keep = [
            {"start_frame": 0, "end_frame": start},
            {"start_frame": end, "end_frame": 160000},
        ]
        with app_module.app.test_client() as c:
            barrier.wait()
            results[idx] = c.post(f"/programs/{pid}/merges",
                                  json={"base_revision": 0, "keep_segments": keep})

    t1 = threading.Thread(target=do_merge, args=(0, (32000, 48000)))
    t2 = threading.Thread(target=do_merge, args=(1, (128000, 144000)))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 200], [r.get_json() for r in results]
    cur = client.get(f"/programs/{pid}").get_json()
    assert cur["revision"] == 2
    assert _seg(cur) == [(0, 32000), (48000, 128000), (144000, 160000)]
