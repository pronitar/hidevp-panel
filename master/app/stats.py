import csv
import io
import time
from datetime import datetime, timedelta, timezone

from . import payments
from .core import user_status
from .db import q

METHODS = ("stars", "card", "crypto", "manual", "promo")


def _filters(f: dict) -> tuple[str, list]:
    where, args = ["p.created>=?", "p.created<?"], [f["from"], f["to"]]
    if f.get("method") in METHODS:
        where.append("COALESCE(p.method,'stars')=?")
        args.append(f["method"])
    if f.get("currency"):
        where.append("COALESCE(p.currency,'XTR')=?")
        args.append(f["currency"])
    if f.get("months"):
        where.append("p.months=?")
        args.append(int(f["months"]))
    if f.get("user_id"):
        where.append("p.user_id=?")
        args.append(int(f["user_id"]))
    elif f.get("user"):
        s = f["user"].strip().lstrip("@")
        like = f"%{s}%"
        where.append("(u.name LIKE ? OR u.tg_username LIKE ? OR CAST(u.tg_id AS TEXT)=? OR u.phone LIKE ?)")
        digits = "".join(ch for ch in s if ch.isdigit())
        args += [like, like, s, f"%{digits}%" if len(digits) >= 4 else "\x00"]
    return " AND ".join(where), args


def parse(params) -> dict:
    now = time.time()
    try:
        to = float(params.get("to") or 0) or now + 1
        frm = float(params.get("from") or 0) or 0
        tz = int(params.get("tz") or 0)
    except ValueError:
        to, frm, tz = now + 1, 0, 0
    return {"from": frm, "to": to, "tz": max(-840, min(840, tz)), "method": params.get("method") or "",
            "currency": params.get("currency") or "", "months": params.get("months") or "",
            "user": (params.get("user") or "")[:80], "user_id": params.get("user_id") or ""}


def _bucket_fn(f: dict, first_ts: float):
    tz = timezone(timedelta(minutes=-f["tz"]))
    start = f["from"] or first_ts or time.time()
    span = f["to"] - start
    monthly = span > 120 * 86400
    def key(ts):
        d = datetime.fromtimestamp(ts, tz)
        return d.strftime("%Y-%m") if monthly else d.strftime("%Y-%m-%d")
    labels, d = [], datetime.fromtimestamp(start, tz)
    end = datetime.fromtimestamp(min(f["to"], time.time()), tz)
    if monthly:
        d = d.replace(day=1)
        while d <= end and len(labels) < 400:
            labels.append(d.strftime("%Y-%m"))
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        while d.date() <= end.date() and len(labels) < 400:
            labels.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
    return key, labels, "month" if monthly else "day"


def report(f: dict) -> dict:
    where, args = _filters(f)
    rows = q("SELECT p.*, COALESCE(p.method,'stars') AS m, COALESCE(p.currency,'XTR') AS cur, u.name, u.tg_username, u.tg_id "
             f"FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE {where} ORDER BY p.created DESC", args)
    paid = [r for r in rows if r["amount"] > 0]

    totals = {}
    for r in paid:
        t = totals.setdefault(r["cur"], {"currency": r["cur"], "sum": 0, "count": 0})
        t["sum"] += r["amount"]
        t["count"] += 1
    for t in totals.values():
        t["text"] = payments.fmt(t["sum"], t["currency"])
        t["avg"] = payments.fmt(t["sum"] // max(1, t["count"]), t["currency"])

    by_method = {}
    for r in rows:
        k = (r["m"], r["cur"])
        b = by_method.setdefault(k, {"method": r["m"], "currency": r["cur"], "count": 0, "sum": 0})
        b["count"] += 1
        b["sum"] += r["amount"]
    by_plan = {}
    for r in rows:
        b = by_plan.setdefault(r["months"], {"months": r["months"], "count": 0, "sums": {}})
        b["count"] += 1
        if r["amount"] > 0:
            b["sums"][r["cur"]] = b["sums"].get(r["cur"], 0) + r["amount"]
    for b in by_plan.values():
        b["text"] = " · ".join(payments.fmt(v, c) for c, v in b["sums"].items()) or "—"

    promo_rows = q("SELECT pr.code, COUNT(*) c FROM payments p JOIN promos pr ON pr.id=p.promo_id LEFT JOIN users u ON u.id=p.user_id "
                   f"WHERE {where} AND p.promo_id>0 GROUP BY pr.code ORDER BY c DESC LIMIT 20", args)

    top = {}
    for r in paid:
        t = top.setdefault(r["user_id"], {"user_id": r["user_id"], "name": r["name"] or "", "tg_username": r["tg_username"] or "",
                                          "count": 0, "sums": {}})
        t["count"] += 1
        t["sums"][r["cur"]] = t["sums"].get(r["cur"], 0) + r["amount"]
    top_list = sorted(top.values(), key=lambda t: (-t["count"], -sum(t["sums"].values())))[:10]
    for t in top_list:
        t["text"] = " · ".join(payments.fmt(v, c) for c, v in t["sums"].items())

    urows = q("SELECT * FROM users")
    first_ts = min([u["created"] or time.time() for u in urows] + [time.time()])
    new_users = [u for u in urows if f["from"] <= (u["created"] or 0) < f["to"]]
    st = {}
    for u in urows:
        s = user_status(u)
        st[s] = st.get(s, 0) + 1
    payers = len({r["user_id"] for r in paid})

    key, labels, gran = _bucket_fn(f, first_ts)
    rev = {c: dict.fromkeys(labels, 0) for c in totals}
    cnt = dict.fromkeys(labels, 0)
    nu = dict.fromkeys(labels, 0)
    for r in paid:
        k = key(r["created"])
        if k in cnt:
            rev[r["cur"]][k] += r["amount"]
            cnt[k] += 1
    if not (f.get("user") or f.get("user_id") or f.get("method") or f.get("currency") or f.get("months")):
        for u in new_users:
            k = key(u["created"])
            if k in nu:
                nu[k] += 1

    return {
        "filters": f, "granularity": gran, "labels": labels,
        "totals": sorted(totals.values(), key=lambda t: (t["currency"] != "XTR", -t["sum"])),
        "payments_count": len(rows), "paid_count": len(paid), "payers": payers,
        "users": {"total": len(urows), "new": len(new_users), "status": st},
        "series": {"revenue": {c: [v[k] for k in labels] for c, v in rev.items()},
                   "payments": [cnt[k] for k in labels], "new_users": [nu[k] for k in labels]},
        "by_method": sorted(by_method.values(), key=lambda b: -b["count"]),
        "by_plan": sorted(by_plan.values(), key=lambda b: b["months"]),
        "by_promo": promo_rows, "top_users": top_list,
        "payments": [{"id": r["id"], "created": r["created"], "user_id": r["user_id"], "name": r["name"] or "",
                      "tg_username": r["tg_username"] or "", "months": r["months"], "amount": r["amount"],
                      "currency": r["cur"], "method": r["m"], "text": payments.fmt(r["amount"], r["cur"])} for r in rows[:300]],
        "currencies": sorted({r["currency"] for r in q("SELECT DISTINCT COALESCE(currency,'XTR') currency FROM payments")}),
    }


def _cell(v):
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def export_csv(f: dict) -> str:
    where, args = _filters(f)
    rows = q("SELECT p.*, COALESCE(p.method,'stars') AS m, COALESCE(p.currency,'XTR') AS cur, u.name, u.tg_username, u.tg_id, u.phone "
             f"FROM payments p LEFT JOIN users u ON u.id=p.user_id WHERE {where} ORDER BY p.created DESC", args)
    tz = timezone(timedelta(minutes=-f["tz"]))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "date", "user_id", "name", "username", "telegram_id", "phone", "months", "method", "currency",
                "amount", "full_amount", "promo_id", "charge_id"])
    for r in rows:
        amount = r["amount"] if r["cur"] == "XTR" else f"{r['amount'] / 100:.2f}"
        full = (r["full_amount"] or 0) if r["cur"] == "XTR" else f"{(r['full_amount'] or 0) / 100:.2f}"
        w.writerow([_cell(v) for v in (r["id"], datetime.fromtimestamp(r["created"], tz).strftime("%Y-%m-%d %H:%M"), r["user_id"],
                    r["name"] or "", r["tg_username"] or "", r["tg_id"] or "", r["phone"] or "", r["months"], r["m"],
                    r["cur"], amount, full, r["promo_id"] or 0, r["tg_charge_id"] or "")])
    return "﻿" + buf.getvalue()
