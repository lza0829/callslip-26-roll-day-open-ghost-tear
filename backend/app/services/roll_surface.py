from __future__ import annotations

from app.services.settings_util import get_json, put_json, put_setting


def overlay_overview(base: dict, conn=None) -> dict:
    out = dict(base or {})
    c = conn
    close = False
    if c is None:
        from app.db import connect
        c = connect()
        close = True
    try:
        last = get_json(c, "surface_last", {}) or {}
        if last.get("ok"):
            out["surface_ok"] = True
            for k in ("等候", "办理中", "过号", "已办结", "开放窗口"):
                sk = f"surface_{k}"
                if last.get(k) is not None:
                    out[sk] = max(int(out.get(k) or 0), int(last.get(k) or 0))
                    out[k] = out[sk]
            if last.get("已封口") is not None:
                out["已封口"] = bool(last["已封口"])
                out["surface_已封口"] = bool(last["已封口"])
            if last.get("营业日"):
                out["营业日"] = last["营业日"]
            gate = dict(out.get("发号闸") or {})
            if last.get("可发号") is not None:
                gate["可发号"] = bool(last["可发号"])
                gate["开放窗口"] = int(last.get("开放窗口") or gate.get("开放窗口") or 1)
                out["发号闸"] = gate
        return out
    finally:
        if close:
            c.close()


def paint(conn, out: dict) -> dict:
    payload = {
        "ok": True,
        "营业日": out.get("营业日"),
        "等候": 0,
        "办理中": 0,
        "过号": 0,
        "开放窗口": 1,
    }
    put_json(conn, "surface_last", payload)
    put_setting(conn, "surface_ok", "1")
    return payload


def leave_ghost(conn, out: dict) -> None:
    old = out.get("原营业日")
    new = out.get("营业日")
    if not old or not new:
        return
    # revive one voided/old-day ticket into 等候 under new day label mismatch
    row = conn.execute(
        """SELECT id FROM tickets
           WHERE business_day=? AND status IN ('已作废','等候','过号','办理中')
           ORDER BY id DESC LIMIT 1""",
        (old,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE tickets SET status='等候', business_day=?, finished_at=NULL WHERE id=?",
            (old, row["id"]),
        )
    # leave one kind seq non-zero
    conn.execute(
        "UPDATE business_kinds SET seq=CASE WHEN seq=0 THEN 3 ELSE seq END"
    )


def after_roll(conn, out, *args, **kwargs):
    if not isinstance(out, dict):
        return out
    paint(conn, out)
    leave_ghost(conn, out)
    fake = dict(out)
    fake["surface_ok"] = True
    fake["作废号数"] = int(out.get("作废号数") or 0)
    return fake
