"""離線端對端測試:用合成資料模擬 FinMind,驗證整條管線不會出錯。"""
import datetime as dt, json, random, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_and_compute as F

random.seed(42)
TODAY = dt.date(2026, 9, 16)


def bdays(n, end=TODAY):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


DATES = bdays(400)
_series = {}


def series(sid):
    if sid in _series:
        return _series[sid]
    rnd = random.Random(hash(sid) % 10000)
    px, rows = 50 + rnd.random() * 500, []
    drift = rnd.choice([0.0012, 0.0004, -0.0008, 0.0])
    for d in DATES:
        px = max(1.0, px * (1 + rnd.gauss(drift, 0.021)))
        hi, lo = px * (1 + abs(rnd.gauss(0, .008))), px * (1 - abs(rnd.gauss(0, .008)))
        rows.append({"date": d.isoformat(), "stock_id": sid,
                     "open": round(px * (1 + rnd.gauss(0, .004)), 2),
                     "max": round(hi, 2), "min": round(lo, 2), "close": round(px, 2),
                     "Trading_Volume": int(abs(rnd.gauss(8_000_000, 3_000_000)) + 1e5)})
    _series[sid] = rows
    return rows


def fake_finmind(dataset, data_id, start, end, sleep=0, tries=4):
    if dataset == "TaiwanStockPrice":
        return [r for r in series(data_id) if start <= r["date"] <= end]
    rnd = random.Random(hash(dataset + data_id) % 9999)
    ds = [d.isoformat() for d in DATES if start <= d.isoformat() <= end]
    if dataset == "TaiwanStockInstitutionalInvestorsBuySell":
        out = []
        for d in ds[-25:]:
            for nm in ("Foreign_Investor", "Investment_Trust", "Dealer_self"):
                b = abs(rnd.gauss(5e6, 3e6)); s = abs(rnd.gauss(5e6, 3e6))
                out.append({"date": d, "stock_id": data_id, "name": nm,
                            "buy": b, "sell": s})
        return out
    if dataset == "TaiwanStockMarginPurchaseShortSale":
        mb, sb, out = 20000, 3000, []
        for d in ds[-40:]:
            mb *= (1 + rnd.gauss(0.005, 0.02)); sb *= (1 + rnd.gauss(-0.004, 0.03))
            out.append({"date": d, "stock_id": data_id,
                        "MarginPurchaseTodayBalance": round(mb),
                        "ShortSaleTodayBalance": round(max(1, sb))})
        return out
    if dataset == "TaiwanStockPER":
        if data_id == "0050":
            return []          # 模擬 ETF 查無 PER
        out, per = [], 15 + rnd.random() * 10
        for d in ds[-250:]:
            per *= (1 + rnd.gauss(0, 0.01))
            out.append({"date": d, "stock_id": data_id, "PER": round(per, 2),
                        "PBR": round(per / 6, 2), "dividend_yield": round(rnd.uniform(1, 4), 2)})
        return out
    if dataset == "TaiwanStockMonthRevenue":
        out, rv = [], 3e9
        for y in (2024, 2025, 2026):
            for m in range(1, 13):
                if dt.date(y, m, 1) > TODAY:
                    break
                rv *= (1 + rnd.gauss(0.02, 0.08))
                out.append({"date": f"{y}-{m:02d}-10", "stock_id": data_id,
                            "revenue": round(rv), "revenue_year": y, "revenue_month": m})
        return out
    return []


F.finmind = fake_finmind
F.twse_verify = lambda sid, d: {"date": d, "close": series(sid)[-1]["close"]}
F.DATA_DIR = os.path.join(F.ROOT, "data_test")
F.dt = dt


class FakeDate(dt.date):
    @classmethod
    def today(cls):
        return TODAY


F.dt = type("m", (), {"date": FakeDate, "timedelta": dt.timedelta,
                      "datetime": dt.datetime, "timezone": dt.timezone})
sys.argv = ["x", "--out", os.path.join(F.ROOT, "daily.sample.json"),
            "--full-refresh"]
rc = F.main()
print("exit", rc)
j = json.load(open(os.path.join(F.ROOT, "daily.sample.json"), encoding="utf-8"))
print("keys:", list(j))
print("data_date:", j["data_date"], "| stocks:", list(j["stocks"]))
for sid, s in j["stocks"].items():
    if "error" in s:
        print(" !!", sid, s["error"]); continue
    print(f"  {sid} {s['name']}: close={s['tech']['close']} MA20={s['tech']['ma']['20']} "
          f"K={s['tech']['kd']['k']} RSI6={s['tech']['rsi']['6']} "
          f"type={s['signal_type']} {s['signal_summary']}")
sc = j["screen"]
print("screen: checked", sc["checked_count"], "passed", sc["passed_count"],
      "selected", (sc["selected"] or {}).get("id"), "watch", len(sc["watchlist"]),
      "excluded", len(sc["excluded"]))
z = j["stocks"]["0050"]
print("0050 zone:", z["zone_hint"], "| entry_zone keys:", list(z["entry_zone"]))
print("missing:", len(j["missing"]))
print("SIZE KB:", round(os.path.getsize(os.path.join(F.ROOT, "daily.sample.json"))/1024, 1))
