from __future__ import annotations
import json
from datetime import date, timedelta
from fastapi import HTTPException
from app.config import ACTIVE_QUEUE
from app.services.core import business_day, is_sealed, now


def _open_count(conn, day: str) -> int:
    marks = ",".join("?" * len(ACTIVE_QUEUE))
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM tickets WHERE business_day=? AND status IN ({marks})",
        (day, *ACTIVE_QUEUE),
    ).fetchone()["c"]


def get_seal(conn) -> dict:
    day = business_day(conn)
    row = conn.execute(
        "SELECT * FROM day_seals WHERE business_day=?", (day,)
    ).fetchone()
    return {
        "营业日": day,
        "已封口": bool(row and row["sealed"]),
        "封口时刻": row["sealed_at"] if row else None,
        "备注": row["note"] if row else "",
        "未结号数": _open_count(conn, day),
    }


def seal_day(conn, force: bool = False) -> dict:
    day = business_day(conn)
    if is_sealed(conn, day):
        raise HTTPException(400, detail="当日已封口")
    open_count = _open_count(conn, day)
    if open_count and not force:
        raise HTTPException(400, detail=f"仍有{open_count}个未结号，须强制封口或先清队")
    note = "强制封口" if force and open_count else "正常封口"
    ts = now()
    exists = conn.execute(
        "SELECT id FROM day_seals WHERE business_day=?", (day,)
    ).fetchone()
    if exists:
        conn.execute(
            "UPDATE day_seals SET sealed=1, sealed_at=?, note=? WHERE business_day=?",
            (ts, note, day),
        )
    else:
        conn.execute(
            "INSERT INTO day_seals(business_day, sealed, sealed_at, note) VALUES (?,?,?,?)",
            (day, 1, ts, note),
        )
    return get_seal(conn)


def unseal_day(conn) -> dict:
    day = business_day(conn)
    if not is_sealed(conn, day):
        raise HTTPException(400, detail="当日未封口")
    conn.execute(
        "UPDATE day_seals SET sealed=0, sealed_at=NULL, note=? WHERE business_day=?",
        ("已解封", day),
    )
    return get_seal(conn)


def _snapshot(conn, day: str) -> dict:
    def cnt(status: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM tickets WHERE business_day=? AND status=?",
            (day, status),
        ).fetchone()["c"]

    wins = conn.execute(
        "SELECT id, name, current_ticket_id FROM windows ORDER BY id"
    ).fetchall()
    kinds = conn.execute(
        "SELECT id, name, prefix, seq FROM business_kinds ORDER BY id"
    ).fetchall()
    return {
        "营业日": day,
        "已封口": is_sealed(conn, day),
        "未结号数": _open_count(conn, day),
        "等候": cnt("等候"),
        "叫号中": cnt("叫号中"),
        "办理中": cnt("办理中"),
        "过号": cnt("过号"),
        "已办结": cnt("已办结"),
        "已作废": cnt("已作废"),
        "窗口": [
            {
                "id": w["id"],
                "名称": w["name"],
                "有在办": bool(w["current_ticket_id"]),
            }
            for w in wins
        ],
        "号段游标": [
            {"id": k["id"], "名称": k["name"], "前缀": k["prefix"], "序号": k["seq"]}
            for k in kinds
        ],
    }


def get_roll(conn, from_day: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM day_rolls WHERE from_day=?", (from_day,)
    ).fetchone()
    if not row:
        return None
    return {
        "回执编号": row["id"],
        "原营业日": row["from_day"],
        "营业日": row["to_day"],
        "切换时刻": row["rolled_at"],
        "作废号数": row["voided_count"],
        "清空窗口编号": [
            int(x) for x in row["cleared_window_ids"].split(",") if x.strip()
        ],
        "备注": row["note"],
        "回执": json.loads(row["receipt"]) if row["receipt"] else {},
    }


def latest_roll(conn) -> dict | None:
    row = conn.execute(
        "SELECT from_day FROM day_rolls ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return get_roll(conn, row["from_day"]) if row else None


def list_rolls(conn, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT from_day FROM day_rolls ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [get_roll(conn, r["from_day"]) for r in rows]


def roll_day(
    conn, target: str | None = None, expect_from: str | None = None
) -> dict:
    """切换营业日（全局唯一口径：强制作废旧日未结）。

    事务内一次完成：旧日全部未结号置为已作废并逐条留审计、
    窗口当前号指针清空、号段游标归零、切日回执落库、营业日落新日。
    任一步异常整笔回滚。
    可重放：某次切日已把 expect_from 推至 nxt 时，重复请求原样返回其回执，
    不再变更任何状态（响应丢失后的重试幂等）。
    """
    cur = business_day(conn)
    if target is not None:
        try:
            nxt = date.fromisoformat(target).isoformat()
        except ValueError:
            raise HTTPException(400, detail="新营业日格式须为 年-月-日")
    else:
        nxt = (date.fromisoformat(cur) + timedelta(days=1)).isoformat()

    prior = get_roll(conn, expect_from) if expect_from else None
    if prior:
        # 该原营业日的切日已发生：显式目标须一致（未给目标则视为同一次重试），
        # 原样返回回执，不再变更任何状态。
        if target is not None and prior["营业日"] != nxt:
            raise HTTPException(
                400,
                detail=f"营业日已由 {prior['原营业日']} 切至 {prior['营业日']}，"
                       f"不可再切至 {nxt}",
            )
        return {
            "原营业日": prior["原营业日"],
            "营业日": prior["营业日"],
            "切换时刻": prior["切换时刻"],
            "作废号数": prior["作废号数"],
            "清空窗口编号": prior["清空窗口编号"],
            "回执": prior["回执"],
            "重放": True,
        }
    if expect_from is not None and expect_from != cur:
        raise HTTPException(
            409, detail=f"营业日已由 {expect_from} 变为 {cur}，请刷新后重试"
        )
    if nxt <= cur:
        raise HTTPException(400, detail="新营业日须晚于当前营业日")

    ts = now()

    # 0) 前置条件：旧营业日须已封口（重放命中已在上方提前返回）
    if not is_sealed(conn, cur):
        raise HTTPException(400, detail="须先封口当日队列再切日")

    # 1) 旧日未结号统一作废，逐条写审计明细
    act_marks = ",".join("?" * len(ACTIVE_QUEUE))
    rows = conn.execute(
        f"""SELECT * FROM tickets
            WHERE business_day=? AND status IN ({act_marks})
            ORDER BY queue_pos, id""",
        (cur, *ACTIVE_QUEUE),
    ).fetchall()
    cur_roll = conn.execute(
        """INSERT INTO day_rolls(from_day, to_day, rolled_at, voided_count,
                                 cleared_window_ids, receipt, note)
           VALUES (?,?,?,?,?,?,?)""",
        (cur, nxt, ts, 0, "", "", "强制作废未结后切日"),
    )
    roll_id = cur_roll.lastrowid
    voided = []
    for r in rows:
        upd = conn.execute(
            "UPDATE tickets SET status='已作废', finished_at=? "
            "WHERE id=? AND business_day=? AND status=?",
            (ts, r["id"], cur, r["status"]),
        )
        if upd.rowcount != 1:
            raise HTTPException(409, detail="未结号状态已变化，切日已回滚")
        conn.execute(
            """INSERT INTO day_roll_voids(roll_id, ticket_id, slip_no, kind_id,
                                          window_id, prev_status, voided_at, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (roll_id, r["id"], r["slip_no"], r["kind_id"], r["window_id"],
             r["status"], ts, "切日作废"),
        )
        if r["window_id"]:
            conn.execute(
                """INSERT INTO call_events(ticket_id, window_id, action, created_at, note)
                   VALUES (?,?,?,?,?)""",
                (r["id"], r["window_id"], "切日作废", ts, f"{cur}→{nxt}"),
            )
        voided.append(
            {
                "号牌": r["slip_no"],
                "原状态": r["status"],
                "窗口编号": r["window_id"],
            }
        )

    # 2) 窗口当前号指针强制清空（含任何隔日残留指针）
    cleared = conn.execute(
        "SELECT id FROM windows WHERE current_ticket_id IS NOT NULL ORDER BY id"
    ).fetchall()
    conn.execute("UPDATE windows SET current_ticket_id=NULL")

    # 3) 号段游标归零
    conn.execute("UPDATE business_kinds SET seq=0")

    # 4) 营业日切换 + 新日封口台账
    conn.execute(
        "INSERT INTO settings(key, value) VALUES ('营业日', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (nxt,),
    )
    exists = conn.execute(
        "SELECT id FROM day_seals WHERE business_day=?", (nxt,)
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO day_seals(business_day, sealed, sealed_at, note) VALUES (?,0,NULL,?)",
            (nxt, "新营业日"),
        )

    # 5) 回执：冻结切换后的同刷快照，便于重放核验
    receipt = {
        "回执编号": roll_id,
        "原营业日": cur,
        "营业日": nxt,
        "切换时刻": ts,
        "口径": "强制作废未结",
        "作废": {"数量": len(voided), "明细": voided},
        "清空窗口编号": [w["id"] for w in cleared],
        "原营业日快照": _snapshot(conn, cur),
        "新营业日快照": _snapshot(conn, nxt),
    }
    conn.execute(
        "UPDATE day_rolls SET voided_count=?, cleared_window_ids=?, receipt=? WHERE id=?",
        (
            len(voided),
            ",".join(str(w["id"]) for w in cleared),
            json.dumps(receipt, ensure_ascii=False),
            roll_id,
        ),
    )
    return {
        "原营业日": cur,
        "营业日": nxt,
        "切换时刻": ts,
        "作废号数": len(voided),
        "清空窗口编号": [w["id"] for w in cleared],
        "回执": receipt,
        "重放": False,
    }


def overview(conn) -> dict:
    day = business_day(conn)

    def cnt(status: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM tickets WHERE business_day=? AND status=?",
            (day, status),
        ).fetchone()["c"]

    wins = conn.execute(
        "SELECT id, name, open, current_ticket_id FROM windows ORDER BY id"
    ).fetchall()
    last = latest_roll(conn)
    return {
        "营业日": day,
        "已封口": is_sealed(conn, day),
        "等候": cnt("等候"),
        "叫号中": cnt("叫号中"),
        "办理中": cnt("办理中"),
        "过号": cnt("过号"),
        "已办结": cnt("已办结"),
        "已作废": cnt("已作废"),
        "未结号数": _open_count(conn, day),
        "开放窗口": sum(1 for w in wins if w["open"]),
        "窗口": [
            {
                "id": w["id"],
                "名称": w["name"],
                "开放": bool(w["open"]),
                "有在办": bool(w["current_ticket_id"]),
            }
            for w in wins
        ],
        "上次切日": (
            {
                "原营业日": last["原营业日"],
                "营业日": last["营业日"],
                "切换时刻": last["切换时刻"],
                "作废号数": last["作废号数"],
            }
            if last
            else None
        ),
    }

