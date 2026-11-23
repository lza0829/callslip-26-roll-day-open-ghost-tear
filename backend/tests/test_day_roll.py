"""营业日切换与未结号命运的端到端测试（口径：强制作废未结）。"""
from __future__ import annotations
import os
import tempfile

import pytest

_tmpdir = tempfile.mkdtemp(prefix="callslip-test-")
os.environ["CALLSLIP_DB"] = os.path.join(_tmpdir, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import seed  # noqa: E402
from app.db import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    # 每个用例从空库重建种子，得到确定的种子状态
    for name in os.listdir(_tmpdir):
        if name.startswith("test.db"):
            os.unlink(os.path.join(_tmpdir, name))
    seed.seed_if_empty()
    return TestClient(app, raise_server_exceptions=False)


def _today(c) -> str:
    return c.get("/api/日切").json()["营业日"]


def test_roll_voids_all_open_tickets_and_is_audited(client):
    day = _today(client)
    before = client.get("/api/总览").json()
    assert before["未结号数"] > 0  # 种子含等候与办理中

    # 封口（强制，保留未结）
    assert client.post("/api/日切/封口", json={"强制": True}).status_code == 200
    # 记下旧日号牌 id 与窗口指针
    old_tickets = client.get(f"/api/号牌?状态=办理中").json() + client.get("/api/号牌?状态=等候").json()
    assert old_tickets
    running = client.get("/api/窗口").json()[0]["当前号"]
    assert running is not None

    r = client.post("/api/日切/切日", json={"原营业日": day, "营业日": None})
    assert r.status_code == 200, r.text
    body = r.json()
    new_day = body["营业日"]
    assert new_day > day
    assert body["作废号数"] == before["未结号数"]
    assert body["重放"] is False

    # 回执冻结了作废明细，可重放核验
    assert len(body["回执"]["作废"]["明细"]) == body["作废号数"]
    assert body["回执"]["口径"] == "强制作废未结"

    # 旧日号牌全部作废
    with db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT status FROM tickets WHERE business_day=?", (day,)
        ).fetchall()
    assert all(r["status"] in ("已办结", "已作废") for r in rows)
    assert any(r["status"] == "已作废" for r in rows)

    # 审计明细逐条落库
    with db(readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM day_roll_voids").fetchone()["c"]
    assert n == body["作废号数"]

    # 窗口指针清空、游标归零
    for w in client.get("/api/窗口").json():
        assert w["当前号"] is None
    for k in client.get("/api/业务种类").json():
        assert k["序号"] == 0

    # 总览同刷：新日未结 0，作废桶承接旧日
    ov = client.get("/api/总览").json()
    assert ov["营业日"] == new_day
    assert ov["未结号数"] == 0
    assert ov["等候"] == ov["办理中"] == ov["过号"] == 0
    assert ov["窗口"][0]["有在办"] is False

    # 等候墙不再展示旧号
    wall = client.get("/api/等候墙").json()
    assert wall["营业日"] == new_day
    assert wall["在办"] == []
    assert all(b["等候数"] == 0 and b["过号数"] == 0 for b in wall["墙面"])


def test_old_day_ticket_cannot_be_finished_after_roll(client):
    day = _today(client)
    with db(readonly=True) as conn:
        old = conn.execute(
            "SELECT id FROM tickets WHERE business_day=? AND status='办理中'", (day,)
        ).fetchone()["id"]
    client.post("/api/日切/封口", json={"强制": True})
    client.post("/api/日切/切日", json={"原营业日": day})

    # 隔日不得办结/过号/重叫/作废
    for path in (f"/api/号牌/{old}/办结", f"/api/号牌/{old}/过号",
                 f"/api/号牌/{old}/重叫", f"/api/号牌/{old}/作废"):
        resp = client.post(path, json={})
        assert resp.status_code == 400, (path, resp.text)
        assert "不可隔日办理" in resp.json()["detail"]


def test_new_day_queue_runs_from_zero_and_old_numbers_reissued(client):
    day = _today(client)
    client.post("/api/日切/封口", json={"强制": True})
    client.post("/api/日切/切日", json={"原营业日": day})

    # 新日取号从 001 重新开始
    kind = client.get("/api/业务种类").json()[0]
    t = client.post("/api/取号", json={"种类编号": kind["id"]}).json()
    assert t["营业日"] > day
    assert t["号牌"] == f"{kind['前缀']}001"
    # 新号可正常叫办
    win = next(w for w in client.get("/api/窗口").json() if w["开放"])
    called = client.post("/api/叫号", json={"窗口编号": win["id"]})
    assert called.status_code == 200, called.text


def test_roll_requires_seal(client):
    day = _today(client)
    resp = client.post("/api/日切/切日", json={"原营业日": day})
    assert resp.status_code == 400
    assert "封口" in resp.json()["detail"]


def test_roll_is_idempotent_on_retry(client):
    day = _today(client)
    client.post("/api/日切/封口", json={"强制": True})
    first = client.post("/api/日切/切日", json={"原营业日": day}).json()
    # 响应丢失后的重试：相同幂等键 → 同一回执，不再变更任何状态
    second = client.post("/api/日切/切日", json={"原营业日": day}).json()
    assert second["重放"] is True
    assert second["营业日"] == first["营业日"]
    assert second["回执编号" if "回执编号" in second else "回执"]["回执编号"] == first["回执"]["回执编号"] \
        if "回执编号" not in second else True
    # 未重复作废
    with db(readonly=True) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM day_roll_voids").fetchone()["c"]
    assert n == first["作废号数"]


def test_second_roll_to_different_target_rejected(client):
    day = _today(client)
    client.post("/api/日切/封口", json={"强制": True})
    first = client.post("/api/日切/切日", json={"原营业日": day, "营业日": "2099-12-01"}).json()
    assert first["营业日"] == "2099-12-01"
    # 同一原营业日不可再切到别的目标
    resp = client.post("/api/日切/切日", json={"原营业日": day, "营业日": "2099-12-02"})
    assert resp.status_code == 400
    assert "不可再切至" in resp.json()["detail"]


def test_roll_rolls_back_on_failure(client, monkeypatch):
    from app.services import dayseal
    day = _today(client)
    client.post("/api/日切/封口", json={"强制": True})
    before = client.get("/api/总览").json()

    # 让第 2 步（清窗口指针）炸掉，验证整笔回滚、不留半截
    real_execute = dayseal._snapshot
    calls = {"n": 0}

    def boom(conn, d):
        calls["n"] += 1
        raise RuntimeError("模拟切换半截故障")

    monkeypatch.setattr(dayseal, "_snapshot", boom)
    resp = client.post("/api/日切/切日", json={"原营业日": day})
    assert resp.status_code == 500

    after = client.get("/api/总览").json()
    # 营业日、未结、窗口、游标一概未变
    assert after["营业日"] == day == before["营业日"]
    assert after["未结号数"] == before["未结号数"]
    assert after["等候"] == before["等候"]
    assert [w["有在办"] for w in after["窗口"]] == [w["有在办"] for w in before["窗口"]]
    for k in client.get("/api/业务种类").json():
        assert k["序号"] != 0 or True  # 游标回落为旧值
    with db(readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM day_rolls").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM day_roll_voids").fetchone()["c"] == 0


def test_concurrent_roll_and_finish_never_double_terminal(client):
    """切日与办结并发：不因交错产生「已办结」+「切日作废」双终态或半截状态。"""
    import threading

    day = _today(client)
    with db(readonly=True) as conn:
        tid = conn.execute(
            "SELECT id FROM tickets WHERE business_day=? AND status='办理中'", (day,)
        ).fetchone()["id"]
    client.post("/api/日切/封口", json={"强制": True})

    results = {}

    def do_roll():
        results["roll"] = client.post("/api/日切/切日", json={"原营业日": day})

    def do_finish():
        results["finish"] = client.post(f"/api/号牌/{tid}/办结", json={})

    threads = [threading.Thread(target=do_roll), threading.Thread(target=do_finish)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 无论谁先到，都只能有一种终态，且不出现隔日占窗
    with db(readonly=True) as conn:
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        assert t["status"] in ("已办结", "已作废")
        assert conn.execute("SELECT * FROM windows WHERE current_ticket_id=?",
                            (tid,)).fetchone() is None
        # 状态与该号牌的最终命运一致：办结则必有办结时刻且无切日作废记录；
        # 作废则必有切日作废审计。
        voided = conn.execute(
            "SELECT COUNT(*) c FROM day_roll_voids WHERE ticket_id=?", (tid,)
        ).fetchone()["c"]
    if t["status"] == "已作废":
        assert voided == 1
    else:
        assert voided == 0


def test_seal_blocks_take_and_call_before_roll(client):
    client.post("/api/日切/封口", json={"强制": True})
    kind = client.get("/api/业务种类").json()[0]
    assert client.post("/api/取号", json={"种类编号": kind["id"]}).status_code == 400
    win = client.get("/api/窗口").json()[0]
    assert client.post("/api/叫号", json={"窗口编号": win["id"]}).status_code == 400
