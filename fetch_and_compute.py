#!/usr/bin/env python3
"""
台股盤勢資料管線 — 在 GitHub Actions 上執行。

負責:抓取原始資料 → 計算所有技術/籌碼/評價指標 → 預先判定訊號
     → 輸出一份精簡的 daily.json(數 KB)。

設計目的:讓原始資料(數百 KB 的日線 JSON)完全不經過語言模型的 context,
        排程端只讀這份算好的 JSON。

用法:
    python fetch_and_compute.py                # 正常增量更新
    python fetch_and_compute.py --full-refresh # 忽略快取,重抓完整區間
    python fetch_and_compute.py --selftest     # 僅跑指標自我驗算,不連網
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import indicators as ind

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
FINMIND = "https://api.finmindtrade.com/api/v4/data"
TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
UA = "twstock-pipeline/1.0 (+github actions)"

MISSING: list[str] = []
NOTES: list[str] = []


def _scrub(x) -> str:
    """任何進到日誌的字串都先把 token 抹掉(Actions 日誌在公開 repo 是公開的)。"""
    t = str(x)
    if TOKEN:
        t = t.replace(TOKEN, "***")
    return t


def log(*a):
    print(*[_scrub(x) for x in a], file=sys.stderr, flush=True)


def note_missing(msg: str):
    if msg not in MISSING:
        MISSING.append(msg)


# ------------------------------------------------------------ FinMind client
class RateLimited(Exception):
    pass


def _get(url: str, timeout: int = 40) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def finmind(dataset: str, data_id: str, start: str, end: str,
            sleep: float = 1.2, tries: int = 4) -> list[dict]:
    """回傳 data list;失敗回空 list 並記錄到 MISSING。"""
    q = {"dataset": dataset, "data_id": data_id,
         "start_date": start, "end_date": end}
    if TOKEN:
        q["token"] = TOKEN
    url = FINMIND + "?" + urllib.parse.urlencode(q)
    delay = sleep
    for attempt in range(tries):
        try:
            j = _get(url)
            st = j.get("status")
            if st == 200:
                time.sleep(sleep)
                return j.get("data") or []
            if st in (402, 429):          # FinMind 流量限制
                raise RateLimited(j.get("msg", "rate limited"))
            note_missing(f"{dataset}/{data_id}: FinMind 回應 status={st} msg={j.get('msg')}")
            return []
        except RateLimited as e:
            wait = min(90, delay * (2 ** attempt) + 15)
            log(f"  rate limited ({_scrub(e)}), sleep {wait:.0f}s")
            time.sleep(wait)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            wait = min(45, delay * (2 ** attempt) + 3)
            log(f"  {dataset}/{data_id} 連線失敗 {_scrub(e)},{wait:.0f}s 後重試")
            time.sleep(wait)
        except json.JSONDecodeError as e:
            note_missing(f"{dataset}/{data_id}: 回應非 JSON ({_scrub(e)})")
            return []
    note_missing(f"{dataset}/{data_id}: 重試 {tries} 次仍失敗")
    return []


# ------------------------------------------------------------ price cache
PRICE_COLS = ["date", "open", "high", "low", "close", "volume"]


def cache_path(sid: str) -> str:
    safe = "".join(ch for ch in sid if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError(f"不合法的股票代號: {sid!r}")
    return os.path.join(DATA_DIR, f"price_{safe}.csv")


def load_cache(sid: str) -> list[dict]:
    p = cache_path(sid)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                out.append({"date": row["date"],
                            "open": float(row["open"]), "high": float(row["high"]),
                            "low": float(row["low"]), "close": float(row["close"]),
                            "volume": float(row["volume"])})
            except (ValueError, KeyError):
                continue
    return out


def save_cache(sid: str, rows: list[dict]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(cache_path(sid), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PRICE_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in PRICE_COLS})


def get_prices(sid: str, days: int, today: dt.date, sleep: float,
               full: bool = False) -> list[dict]:
    """增量更新:只抓快取最後一天之後的資料。回傳依日期排序的 OHLCV。"""
    start_needed = today - dt.timedelta(days=days)
    rows = [] if full else load_cache(sid)
    rows = [r for r in rows if r["date"] >= start_needed.isoformat()]
    if rows:
        last = dt.date.fromisoformat(rows[-1]["date"])
        # 快取太舊(超過 30 天沒更新)視為不可靠,重抓
        if (today - last).days > 30:
            rows, start = [], start_needed
        else:
            start = last + dt.timedelta(days=1)
    else:
        start = start_needed
    if start <= today:
        raw = finmind("TaiwanStockPrice", sid, start.isoformat(),
                      today.isoformat(), sleep=sleep)
        seen = {r["date"] for r in rows}
        for d in raw:
            if d["date"] in seen:
                continue
            try:
                row = {"date": d["date"], "open": float(d["open"]),
                       "high": float(d["max"]), "low": float(d["min"]),
                       "close": float(d["close"]),
                       "volume": float(d.get("Trading_Volume") or 0)}
            except (TypeError, ValueError, KeyError):
                continue
            if row["close"] <= 0:
                continue
            rows.append(row)
    rows.sort(key=lambda r: r["date"])
    if rows:
        save_cache(sid, rows)
    return rows


def detect_price_gap(rows: list[dict], thr_pct: float) -> str | None:
    """偵測疑似未還原的除權息/面額變更斷層。"""
    for i in range(max(1, len(rows) - 60), len(rows)):
        prev, cur = rows[i - 1]["close"], rows[i]["close"]
        if prev <= 0:
            continue
        chg = (cur / prev - 1) * 100
        if abs(chg) > thr_pct:
            return f"{rows[i]['date']} 單日 {chg:+.1f}%,疑似未還原價格斷層"
    return None


# ------------------------------------------------------------ metrics
def r2(x, nd=2):
    return None if x is None else round(float(x), nd)


def pct_rank(series: list[float], value: float) -> float | None:
    """value 在 series 中的百分位(0~100)。"""
    s = [v for v in series if v is not None]
    if not s:
        return None
    return round(sum(1 for v in s if v <= value) / len(s) * 100.0, 1)


def compute_tech(rows: list[dict], bench: dict | None = None) -> dict:
    close = [r["close"] for r in rows]
    high = [r["high"] for r in rows]
    low = [r["low"] for r in rows]
    vol = [r["volume"] for r in rows]
    n = len(close)
    if n < 30:
        return {"error": f"日線資料僅 {n} 筆,不足以計算指標"}

    ok, bad = ind.cross_check(high, low, close)
    if not ok:
        return {"error": "指標交叉驗算不一致: " + "; ".join(bad[:3])}

    ma, bias = {}, {}
    for p in (5, 10, 20, 60, 120):
        if n >= p:
            v = ind.sma_a(close, p)[-1]
            ma[str(p)] = r2(v)
            bias[str(p)] = r2((close[-1] / v - 1) * 100) if v else None
        else:
            ma[str(p)] = None
            bias[str(p)] = None

    def slope(period, look=10):
        """均線斜率:與 look 日前相比的變化%(正 = 上彎)"""
        if n < period + look:
            return None
        s = ind.sma_a(close, period)
        return r2((s[-1] / s[-1 - look] - 1) * 100, 3) if s[-1 - look] else None

    k, d = ind.kd_a(high, low, close)
    dif, sig, osc = ind.macd_a(close)
    rsi6, rsi12 = ind.rsi_a(close, 6), ind.rsi_a(close, 12)

    def ret(days):
        return r2((close[-1] / close[-1 - days] - 1) * 100) if n > days else None

    vol_ma20 = sum(vol[-20:]) / 20 if n >= 20 else None
    m5, m10, m20, m60 = (ma.get("5"), ma.get("10"), ma.get("20"), ma.get("60"))
    if None not in (m5, m10, m20, m60):
        if m5 > m10 > m20 > m60:
            order = "多頭排列"
        elif m5 < m10 < m20 < m60:
            order = "空頭排列"
        else:
            spread = (max(m5, m10, m20) - min(m5, m10, m20)) / close[-1] * 100
            order = "均線糾結" if spread < 2.0 else "排列混亂"
    else:
        order = "資料不足"

    # 支撐壓力:近 20/60 日高低 + 均線
    support = sorted({x for x in [r2(min(low[-20:])), r2(min(low[-60:]) if n >= 60 else min(low)),
                                  ma.get("20"), ma.get("60")]
                      if x is not None and x < close[-1]}, reverse=True)[:3]
    resistance = sorted({x for x in [r2(max(high[-20:])), r2(max(high[-60:]) if n >= 60 else max(high)),
                                     ma.get("20"), ma.get("60")]
                         if x is not None and x > close[-1]})[:3]

    # K 線型態
    o, h, l, c = rows[-1]["open"], high[-1], low[-1], close[-1]
    po, pc = rows[-2]["open"], close[-2]
    body = abs(c - o) / c * 100
    patterns = []
    if body >= 3 and c > o:
        patterns.append("長紅")
    if body >= 3 and c < o:
        patterns.append("長黑")
    if l > high[-2]:
        patterns.append("向上跳空")
    if h < low[-2]:
        patterns.append("向下跳空")
    if c > o and pc < po and c >= po and o <= pc:
        patterns.append("多頭吞噬")
    if c < o and pc > po and c <= po and o >= pc:
        patterns.append("空頭吞噬")
    if (min(o, c) - l) / c * 100 >= 2 and body < 1.5:
        patterns.append("長下影線")

    daily_ret = [(close[i] / close[i - 1] - 1) * 100 for i in range(max(1, n - 20), n)]
    mean = sum(daily_ret) / len(daily_ret)
    std20 = (sum((x - mean) ** 2 for x in daily_ret) / (len(daily_ret) - 1)) ** 0.5 \
        if len(daily_ret) > 1 else None

    # 近一年 60MA 乖離序列(0050 進場區用)
    bias60_series = []
    if n >= 60:
        s60 = ind.sma_a(close, 60)
        for i in range(max(60, n - 240), n):
            if s60[i]:
                bias60_series.append((close[i] / s60[i] - 1) * 100)

    hi52 = max(high[-240:]) if n >= 20 else max(high)
    lo52 = min(low[-240:]) if n >= 20 else min(low)

    out = {
        "date": rows[-1]["date"],
        "close": r2(c), "open": r2(o), "high": r2(h), "low": r2(l),
        "chg_pct": r2((c / close[-2] - 1) * 100),
        "volume_lots": r2(vol[-1] / 1000, 0),
        "vol_ma20_lots": r2(vol_ma20 / 1000, 0) if vol_ma20 else None,
        "vol_ratio": r2(vol[-1] / vol_ma20, 2) if vol_ma20 else None,
        "ma": ma, "bias": bias, "ma_order": order,
        "ma20_slope10": slope(20), "ma60_slope10": slope(60),
        "kd": {"k": r2(k[-1]), "d": r2(d[-1]),
               "k_prev": r2(k[-2]), "d_prev": r2(d[-2])},
        "macd": {"dif": r2(dif[-1], 3), "macd": r2(sig[-1], 3), "osc": r2(osc[-1], 3),
                 "dif_above_macd": (None if dif[-1] is None or sig[-1] is None
                                    else dif[-1] > sig[-1])},
        "rsi": {"6": r2(rsi6[-1]), "12": r2(rsi12[-1])},
        "ret": {"5d": ret(5), "7d": ret(7), "20d": ret(20), "30d": ret(30),
                "60d": ret(60)},
        "std20_daily_ret": r2(std20),
        "range52w": {"high": r2(hi52), "low": r2(lo52),
                     "pct_rank": r2((c - lo52) / (hi52 - lo52) * 100, 1)
                     if hi52 > lo52 else None},
        "support": support, "resistance": resistance,
        "candle": patterns,
        "bars": n,
    }
    if bias60_series:
        out["bias60_pctile_1y"] = pct_rank(bias60_series, bias60_series[-1])
        srt = sorted(bias60_series)
        out["bias60_p20"] = r2(srt[max(0, int(len(srt) * 0.20) - 1)])
        out["bias60_p80"] = r2(srt[min(len(srt) - 1, int(len(srt) * 0.80))])
    # 連續跌破月線天數 / 站上月線天數
    if ma.get("20") and n >= 21:
        s20 = ind.sma_a(close, 20)
        streak, above = 0, close[-1] > s20[-1]
        for i in range(n - 1, 59, -1):
            if s20[i] is None:
                break
            if (close[i] > s20[i]) == above:
                streak += 1
            else:
                break
        out["ma20_side"] = "站上" if above else "跌破"
        out["ma20_streak_days"] = streak
    if bench and bench.get("ret", {}).get("20d") is not None and out["ret"]["20d"] is not None:
        out["excess_ret_20d"] = r2(out["ret"]["20d"] - bench["ret"]["20d"])
    return out


def compute_inst(sid: str, today: dt.date, sleep: float) -> dict:
    rows = finmind("TaiwanStockInstitutionalInvestorsBuySell", sid,
                   (today - dt.timedelta(days=30)).isoformat(),
                   today.isoformat(), sleep=sleep)
    if not rows:
        note_missing(f"{sid}: 三大法人買賣超查無資料")
        return {}
    by_date: dict[str, dict[str, float]] = {}
    for r in rows:
        try:
            net = (float(r["buy"]) - float(r["sell"])) / 1000.0   # 張
        except (TypeError, ValueError, KeyError):
            continue
        name = r.get("name", "")
        key = ("foreign" if name.startswith("Foreign_Investor") else
               "trust" if name.startswith("Investment_Trust") else
               "dealer" if name.startswith("Dealer") else
               "foreign_dealer" if name.startswith("Foreign_Dealer") else None)
        if key is None:
            continue
        slot = by_date.setdefault(r["date"], {})
        slot[key] = slot.get(key, 0.0) + net
    dates = sorted(by_date)
    if not dates:
        return {}
    last5 = dates[-5:]

    def cum(key, ds):
        return r2(sum(by_date[d].get(key, 0.0) for d in ds), 0)

    # 外資連續賣超天數(由最新日往回)
    streak = 0
    for d in reversed(dates):
        if by_date[d].get("foreign", 0.0) < 0:
            streak += 1
        else:
            break
    # 近5日視窗內是否曾出現連3日以上賣超
    had3, run, run_dates, found = False, 0, [], []
    for d in last5:
        if by_date[d].get("foreign", 0.0) < 0:
            run += 1
            run_dates.append(d)
            if run >= 3:
                had3, found = True, list(run_dates)
        else:
            run, run_dates = 0, []
    return {
        "date": dates[-1],
        "today": {k: r2(by_date[dates[-1]].get(k, 0.0), 0)
                  for k in ("foreign", "trust", "dealer")},
        "cum5": {k: cum(k, last5) for k in ("foreign", "trust", "dealer")},
        "cum5_dates": [last5[0], last5[-1]],
        "foreign_sell_streak_now": streak,
        "foreign_3d_sell_in_5d_window": had3,
        "foreign_3d_sell_dates": found,
    }


def compute_margin(sid: str, today: dt.date, sleep: float) -> dict:
    rows = finmind("TaiwanStockMarginPurchaseShortSale", sid,
                   (today - dt.timedelta(days=45)).isoformat(),
                   today.isoformat(), sleep=sleep)
    if not rows:
        note_missing(f"{sid}: 融資融券查無資料")
        return {}
    rows = sorted(rows, key=lambda r: r["date"])
    try:
        mb = [float(r["MarginPurchaseTodayBalance"]) for r in rows]
        sb = [float(r["ShortSaleTodayBalance"]) for r in rows]
    except (TypeError, ValueError, KeyError):
        note_missing(f"{sid}: 融資融券欄位格式異常")
        return {}

    def chg(series, back):
        if len(series) <= back or series[-1 - back] == 0:
            return None
        return r2((series[-1] / series[-1 - back] - 1) * 100)

    return {
        "date": rows[-1]["date"],
        "margin_balance_lots": r2(mb[-1], 0),
        "margin_chg_1d_pct": chg(mb, 1),
        "margin_chg_5d_pct": chg(mb, 5),
        "margin_chg_9d_pct": chg(mb, 9),
        "short_balance_lots": r2(sb[-1], 0),
        "short_chg_5d_pct": chg(sb, 5),
        "short_chg_9d_pct": chg(sb, 9),
    }


def compute_per(sid: str, today: dt.date, sleep: float) -> dict:
    rows = finmind("TaiwanStockPER", sid,
                   (today - dt.timedelta(days=400)).isoformat(),
                   today.isoformat(), sleep=sleep)
    if not rows:
        note_missing(f"{sid}: TaiwanStockPER 查無資料(ETF 通常無此資料)")
        return {}
    rows = sorted(rows, key=lambda r: r["date"])
    pers = []
    for r in rows:
        try:
            v = float(r["PER"])
        except (TypeError, ValueError, KeyError):
            continue
        if v > 0:
            pers.append(v)
    if not pers:
        note_missing(f"{sid}: PER 數值全為 0 或無效")
        return {}
    last = rows[-1]
    return {
        "date": last["date"],
        "per": r2(float(last.get("PER") or 0)),
        "pbr": r2(float(last.get("PBR") or 0)),
        "dividend_yield": r2(float(last.get("dividend_yield") or 0)),
        "per_pctile_1y": pct_rank(pers, pers[-1]),
        "per_1y_min": r2(min(pers)), "per_1y_max": r2(max(pers)),
        "samples": len(pers),
    }


def compute_revenue(sid: str, today: dt.date, sleep: float) -> dict:
    rows = finmind("TaiwanStockMonthRevenue", sid,
                   (today - dt.timedelta(days=800)).isoformat(),
                   today.isoformat(), sleep=sleep)
    if not rows:
        note_missing(f"{sid}: 月營收查無資料")
        return {}
    rows = sorted(rows, key=lambda r: (r["revenue_year"], r["revenue_month"]))
    by_key = {(int(r["revenue_year"]), int(r["revenue_month"])): float(r["revenue"])
              for r in rows if r.get("revenue") is not None}
    if not by_key:
        return {}
    ky = max(by_key)
    cur = by_key[ky]
    prev_y = by_key.get((ky[0] - 1, ky[1]))
    prev_m = by_key.get((ky[0], ky[1] - 1) if ky[1] > 1 else (ky[0] - 1, 12))
    recent = []
    keys = sorted(by_key)[-6:]
    for k in keys:
        py = by_key.get((k[0] - 1, k[1]))
        recent.append({"m": f"{k[0]}-{k[1]:02d}",
                       "yoy": r2((by_key[k] / py - 1) * 100) if py else None})
    return {
        "month": f"{ky[0]}-{ky[1]:02d}",
        "revenue_e8": r2(cur / 1e8),
        "yoy_pct": r2((cur / prev_y - 1) * 100) if prev_y else None,
        "mom_pct": r2((cur / prev_m - 1) * 100) if prev_m else None,
        "recent_yoy": recent,
    }


# ------------------------------------------------------------ 訊號預判
def S(hit, why=None, needs_news=False):
    """hit: True/False/None(資料不足)"""
    return {"hit": hit, "data": why, "needs_news": needs_news}


def reduce_signals(t: dict, inst: dict, mg: dict, per: dict,
                   rev: dict, bench: dict) -> dict:
    """中長線減碼訊號 — 機械可判定的部分先算好,需新聞判讀的標 needs_news。"""
    s = {}
    ma20 = t["ma"].get("20")
    c = t["close"]

    s["跌破月線且無法快速站回"] = S(
        bool(ma20 and c < ma20 and t.get("ma20_streak_days", 0) >= 3) if ma20 else None,
        f"收盤 {c} / MA20 {ma20} / {t.get('ma20_side')}已連 {t.get('ma20_streak_days')} 日")

    m5, m10, m20 = t["ma"].get("5"), t["ma"].get("10"), t["ma"].get("20")
    if None in (m5, m10, m20):
        s["均線轉糾結或死亡交叉"] = S(None, "均線資料不足")
    else:
        s["均線轉糾結或死亡交叉"] = S(
            (m5 < m10 < m20) or t["ma_order"] == "均線糾結",
            f"MA5 {m5} / MA10 {m10} / MA20 {m20}({t['ma_order']})")

    vr = t.get("vol_ratio")
    near_high = not t["resistance"]          # 無上方壓力 = 已在相對高檔
    s["高檔爆量但價格不再創新高"] = S(
        bool(vr and vr >= 2.0 and not near_high) if vr is not None else None,
        f"量能比 {vr}x 20日均量;上方壓力 {t['resistance'][:1] or '無(已在近期高點)'};"
        f"今日 {t['chg_pct']}%")

    s["頂背離(價創高但指標未同步)"] = S(
        t.get("_divergence_bear"), t.get("_divergence_bear_why"))

    m60 = t["ma"].get("60")
    s["長期上升趨勢線被跌破(以季線+斜率代理)"] = S(
        bool(m60 and c < m60 and (t.get("ma60_slope10") or 0) < 0) if m60 else None,
        f"收盤 {c} / MA60 {m60} / 季線10日斜率 {t.get('ma60_slope10')}%")

    if inst:
        tot5 = sum(v for v in inst["cum5"].values() if v is not None)
        s["三大法人由買轉賣且連續賣超"] = S(
            tot5 < 0 and inst["foreign_sell_streak_now"] >= 2,
            f"近5日合計 {tot5:+.0f} 張(外資 {inst['cum5']['foreign']:+.0f}/投信 "
            f"{inst['cum5']['trust']:+.0f}/自營 {inst['cum5']['dealer']:+.0f});"
            f"外資目前連 {inst['foreign_sell_streak_now']} 日賣超")
    else:
        s["三大法人由買轉賣且連續賣超"] = S(None, "法人資料不足")

    if mg:
        up = mg.get("margin_chg_5d_pct")
        dn = mg.get("short_chg_5d_pct")
        s["融資大增融券大減(散戶追價過熱)"] = S(
            bool(up is not None and dn is not None and up >= 10 and dn <= -10),
            f"融資5日 {up}% / 9日 {mg.get('margin_chg_9d_pct')}%;"
            f"融券5日 {dn}% / 9日 {mg.get('short_chg_9d_pct')}%")
    else:
        s["融資大增融券大減(散戶追價過熱)"] = S(None, "融資券資料不足")

    s["主力分點由買轉賣"] = S(None, "FinMind 免費版無分點進出資料,未串接")

    if rev and rev.get("recent_yoy"):
        ys = [x["yoy"] for x in rev["recent_yoy"] if x["yoy"] is not None]
        if len(ys) >= 2:
            s["營收/獲利成長率趨緩"] = S(
                ys[-1] < ys[-2],
                f"{rev['month']} 營收 YoY {ys[-1]}%(前月 {ys[-2]}%);近期序列 {ys}")
        else:
            s["營收/獲利成長率趨緩"] = S(None, "月營收序列不足")
    else:
        s["營收/獲利成長率趨緩"] = S(None, "月營收查無資料")

    s["產業競爭格局改變/訂單能見度下降"] = S(None, None, needs_news=True)

    if per:
        s["本益比明顯高於歷史區間"] = S(
            (per.get("per_pctile_1y") or 0) >= 80,
            f"PER {per['per']}(近一年百分位 {per.get('per_pctile_1y')}%,"
            f"區間 {per.get('per_1y_min')}~{per.get('per_1y_max')})")
    else:
        s["本益比明顯高於歷史區間"] = S(None, "TaiwanStockPER 查無資料")

    s["訂單/產能/題材出現變化"] = S(None, None, needs_news=True)

    b60 = t["bias"].get("60")
    pc = t.get("bias60_pctile_1y")
    s["季線乖離率過大(歷史偏高)"] = S(
        bool(pc is not None and pc >= 80) if pc is not None else None,
        f"季線乖離 {b60}%(近一年百分位 {pc}%,P80={t.get('bias60_p80')}%)")

    s["市場氛圍過度樂觀、新聞熱度極高"] = S(None, None, needs_news=True)

    yoy = (rev or {}).get("yoy_pct")
    r20 = t["ret"].get("20d")
    if yoy is None or r20 is None:
        s["短期漲幅超過基本面支撐速度"] = S(
            None, f"近20日漲幅 {r20}% / 營收YoY {yoy}% — 其一查無資料")
    else:
        s["短期漲幅超過基本面支撐速度"] = S(
            r20 > yoy,
            f"近7日 {t['ret'].get('7d')}% / 20日 {r20}% / 30日 {t['ret'].get('30d')}%"
            f" vs {rev['month']} 營收YoY {yoy}%")

    ex = t.get("excess_ret_20d")
    if ex is None:
        s["資金撤出族群(相對大盤轉弱)"] = S(None, "大盤比較資料不足")
    else:
        s["資金撤出族群(相對大盤轉弱)"] = S(
            ex < 0 and bool(ma20 and c < ma20),
            f"近20日相對大盤超額報酬 {ex:+.2f}%(個股 {t['ret']['20d']}% vs "
            f"TAIEX {bench.get('ret', {}).get('20d')}%)")

    s["總經環境(升息/匯率/地緣)轉為不利"] = S(None, None, needs_news=True)
    s["供應鏈或國際同業出現警訊"] = S(None, None, needs_news=True)
    return s


def entry_signals(t: dict, inst: dict, mg: dict, per: dict, bench: dict) -> dict:
    """0050 專用:進場訊號(打勾 = 對進場有利)"""
    s = {}
    c = t["close"]
    ma20, ma60 = t["ma"].get("20"), t["ma"].get("60")
    m5, m10 = t["ma"].get("5"), t["ma"].get("10")

    s["站上月線並守住/回測不破留長下影"] = S(
        bool(ma20 and ((c > ma20 and t.get("ma20_streak_days", 0) >= 2)
                       or "長下影線" in t["candle"])) if ma20 else None,
        f"收盤 {c} / MA20 {ma20};{t.get('ma20_side')}連 {t.get('ma20_streak_days')} 日;"
        f"K線 {t['candle']}")

    if None in (m5, m10, ma20):
        s["均線轉多頭排列或黃金交叉"] = S(None, "均線資料不足")
    else:
        s["均線轉多頭排列或黃金交叉"] = S(
            m5 > m10 > ma20, f"MA5 {m5} / MA10 {m10} / MA20 {ma20}({t['ma_order']})")

    vr = t.get("vol_ratio")
    s["帶量突破前波壓力"] = S(
        bool(vr and vr >= 1.5 and (t["chg_pct"] or 0) > 0
             and (not t["resistance"] or c > t["resistance"][0])) if vr else None,
        f"量能比 {vr}x、漲跌 {t['chg_pct']}%、壓力 {t['resistance'][:1]}")

    s["底背離(價創低但指標未同步創低)"] = S(
        t.get("_divergence_bull"), t.get("_divergence_bull_why"))

    s["突破下降趨勢線/收復季線"] = S(
        bool(ma60 and c > ma60 and (t.get("ma60_slope10") or 0) > -0.5) if ma60 else None,
        f"收盤 {c} / MA60 {ma60} / 季線10日斜率 {t.get('ma60_slope10')}%")

    if inst:
        s["三大法人由賣轉買且連續買超"] = S(
            inst["cum5"]["foreign"] > 0 and inst["foreign_sell_streak_now"] == 0,
            f"外資近5日 {inst['cum5']['foreign']:+.0f} 張、投信 {inst['cum5']['trust']:+.0f}、"
            f"自營 {inst['cum5']['dealer']:+.0f};外資連續賣超 {inst['foreign_sell_streak_now']} 日")
        s["自營商/投信同步回補"] = S(
            inst["cum5"]["trust"] > 0 and inst["cum5"]["dealer"] > 0,
            f"投信 {inst['cum5']['trust']:+.0f} / 自營 {inst['cum5']['dealer']:+.0f} 張")
    else:
        s["三大法人由賣轉買且連續買超"] = S(None, "法人資料不足")
        s["自營商/投信同步回補"] = S(None, "法人資料不足")

    if mg:
        up, dn = mg.get("margin_chg_5d_pct"), mg.get("short_chg_5d_pct")
        s["融資大減融券增(浮額清洗)"] = S(
            bool(up is not None and dn is not None and up <= -5 and dn >= 5),
            f"融資5日 {up}%、融券5日 {dn}%")
    else:
        s["融資大減融券增(浮額清洗)"] = S(None, "融資券資料不足")

    if per:
        s["本益比位於近一年偏低區間(P30以下)"] = S(
            (per.get("per_pctile_1y") or 100) <= 30,
            f"PER {per['per']},近一年百分位 {per.get('per_pctile_1y')}%")
        dy = per.get("dividend_yield")
        s["殖利率位於近一年偏高區間"] = S(
            None if not dy else None,
            f"最新殖利率 {dy}%(近一年百分位未計算:FinMind 殖利率序列未另抓)")
    else:
        s["本益比位於近一年偏低區間(P30以下)"] = S(None, "TaiwanStockPER 查無 ETF 資料")
        s["殖利率位於近一年偏高區間"] = S(None, "查無資料")

    s["ETF 折溢價出現折價或溢價收斂"] = S(None, "未串接 ETF 淨值/折溢價來源,查無資料")

    pc = t.get("bias60_pctile_1y")
    s["負乖離過大(季線乖離近一年P20以下)"] = S(
        bool(pc is not None and pc <= 20) if pc is not None else None,
        f"季線乖離 {t['bias'].get('60')}%,近一年百分位 {pc}%(P20={t.get('bias60_p20')}%)")

    s["市場氛圍過度悲觀、恐慌性殺盤"] = S(None, None, needs_news=True)

    k, d = t["kd"]["k"], t["kd"]["d"]
    kp, dp = t["kd"]["k_prev"], t["kd"]["d_prev"]
    r6 = t["rsi"]["6"]
    if None in (k, d, kp, dp, r6):
        s["KD/RSI 低檔區並黃金交叉"] = S(None, "KD/RSI 資料不足")
    else:
        s["KD/RSI 低檔區並黃金交叉"] = S(
            (r6 < 30 or k < 20) and k > d and kp <= dp,
            f"RSI6 {r6}、K {k}(前 {kp})、D {d}(前 {dp})")

    bc, bma20 = bench.get("close"), bench.get("ma", {}).get("20")
    s["大盤站回均線之上、資金回流"] = S(
        bool(bc and bma20 and bc > bma20) if (bc and bma20) else None,
        f"TAIEX {bc} / MA20 {bma20} / MA60 {bench.get('ma', {}).get('60')};"
        f"大盤近20日 {bench.get('ret', {}).get('20d')}%")
    s["降息/匯率/地緣政治轉為有利"] = S(None, None, needs_news=True)
    s["國際股市(費半/那斯達克)止穩轉強"] = S(None, None, needs_news=True)
    return s


def divergence(rows: list[dict], t: dict) -> None:
    """在 t 上標記頂/底背離(以近 60 日前一個波峰/波谷為比較基準)。"""
    close = [r["close"] for r in rows]
    high = [r["high"] for r in rows]
    low = [r["low"] for r in rows]
    n = len(close)
    if n < 80:
        t["_divergence_bear"] = None
        t["_divergence_bear_why"] = "資料不足"
        t["_divergence_bull"] = None
        t["_divergence_bull_why"] = "資料不足"
        return
    k, _ = ind.kd_a(high, low, close)
    r12 = ind.rsi_a(close, 12)
    win = close[-60:-5]
    # 頂背離
    pi = len(close) - 60 + win.index(max(win))
    if close[-1] >= max(win) and k[pi] is not None and k[-1] is not None:
        bear = k[-1] < k[pi] or (r12[-1] is not None and r12[pi] is not None
                                 and r12[-1] < r12[pi])
        t["_divergence_bear"] = bear
        t["_divergence_bear_why"] = (
            f"今日收 {close[-1]} 高於前波峰 {round(max(win),2)}({rows[pi]['date']}),"
            f"K {round(k[-1],1)} vs {round(k[pi],1)}、RSI12 {r12[-1] and round(r12[-1],1)} "
            f"vs {r12[pi] and round(r12[pi],1)}")
    else:
        t["_divergence_bear"] = False
        t["_divergence_bear_why"] = (
            f"今日收 {close[-1]} 未創近60日新高(前高 {round(max(win),2)}),不構成頂背離")
    # 底背離
    ti = len(close) - 60 + win.index(min(win))
    if close[-1] <= min(win) and k[ti] is not None and k[-1] is not None:
        bull = k[-1] > k[ti] or (r12[-1] is not None and r12[ti] is not None
                                 and r12[-1] > r12[ti])
        t["_divergence_bull"] = bull
        t["_divergence_bull_why"] = (
            f"今日收 {close[-1]} 低於前波谷 {round(min(win),2)}({rows[ti]['date']}),"
            f"K {round(k[-1],1)} vs {round(k[ti],1)}")
    else:
        t["_divergence_bull"] = False
        t["_divergence_bull_why"] = (
            f"今日收 {close[-1]} 未創近60日新低(前低 {round(min(win),2)}),不構成底背離")


# ------------------------------------------------------------ 每日篩選
def trend_gate(t: dict) -> tuple[str | None, str]:
    m5, m10, m20, m60 = (t["ma"].get("5"), t["ma"].get("10"),
                         t["ma"].get("20"), t["ma"].get("60"))
    c = t["close"]
    slope60 = t.get("ma60_slope10")
    if None in (m5, m10, m20, m60) or slope60 is None:
        return None, "均線資料不足"
    if slope60 <= 0:
        return None, f"季線10日斜率 {slope60}% 未上彎"
    if t.get("vol_ratio") and t["vol_ratio"] >= 2.5 and (t["chg_pct"] or 0) < 0:
        return None, f"爆量收黑(量能比 {t['vol_ratio']}x、{t['chg_pct']}%),疑似出貨"
    if m5 > m10 > m20 > m60 and c > m60:
        return "A", f"多頭排列 MA5 {m5}>MA10 {m10}>MA20 {m20}>MA60 {m60},站上季線"
    if (m20 > m60 and c > m20 and c > m60
            and (c <= m10 * 1.03 or c <= m20 * 1.05)):
        return "B", (f"回檔整理型:MA20 {m20}>MA60 {m60}、季線上彎 {slope60}%,"
                     f"股價 {c} 站上月/季線且回靠 MA10 {m10}")
    return None, f"不符路徑A/B(MA5 {m5}/MA10 {m10}/MA20 {m20}/MA60 {m60}、收盤 {c})"


def risk_price_checks(t: dict, cfg: dict) -> list[dict]:
    sc = cfg["screen"]
    out = []
    b60 = t["bias"].get("60")
    out.append({"id": 1, "name": f"季線乖離 ≤ +{sc['max_bias60_pct']}%",
                "value": b60, "pass": (b60 is not None and b60 <= sc["max_bias60_pct"])})
    r20 = t["ret"].get("20d")
    out.append({"id": 2, "name": f"近20日漲幅 ≤ +{sc['max_ret20_pct']}%",
                "value": r20, "pass": (r20 is not None and r20 <= sc["max_ret20_pct"])})
    b10 = t["bias"].get("10")
    out.append({"id": 3, "name": f"距10日均線 ≤ +{sc['max_above_ma10_pct']}%",
                "value": b10, "pass": (b10 is not None and b10 <= sc["max_above_ma10_pct"])})
    r6, k = t["rsi"].get("6"), t["kd"].get("k")
    out.append({"id": 4, "name": f"RSI6 ≤ {sc['max_rsi6']} 且 K ≤ {sc['max_k']}",
                "value": {"rsi6": r6, "k": k},
                "pass": (r6 is not None and k is not None
                         and r6 <= sc["max_rsi6"] and k <= sc["max_k"])})
    vr = t.get("vol_ratio")
    out.append({"id": 5, "name": f"成交量 ≤ {sc['max_vol_ratio']}x 20日均量",
                "value": vr, "pass": (vr is not None and vr <= sc["max_vol_ratio"])})
    return out


def pullback_needed(t: dict, cfg: dict) -> dict:
    """反推需回檔幾 % 才進入合格區(負數 = 需下跌)。"""
    sc, c = cfg["screen"], t["close"]
    out = {}
    m60, m10 = t["ma"].get("60"), t["ma"].get("10")
    if m60:
        tgt = m60 * (1 + sc["max_bias60_pct"] / 100)
        out["季線乖離達標價"] = {"price": r2(tgt), "need_pct": r2((tgt / c - 1) * 100)}
    if m10:
        tgt = m10 * (1 + sc["max_above_ma10_pct"] / 100)
        out["距10日線達標價"] = {"price": r2(tgt), "need_pct": r2((tgt / c - 1) * 100)}
    if t.get("_close_20d_ago"):
        tgt = t["_close_20d_ago"] * (1 + sc["max_ret20_pct"] / 100)
        out["近20日漲幅達標價"] = {"price": r2(tgt), "need_pct": r2((tgt / c - 1) * 100)}
    return out


def run_screen(cfg: dict, today: dt.date, bench: dict, exclude: set[str],
               sleep: float, full: bool) -> dict:
    checked, passed, watch, excluded = [], [], [], []
    names = cfg.get("candidate_names", {})
    stage1 = []
    for sid in cfg["candidates"]:
        if sid in exclude:
            continue
        checked.append(sid)
        rows = get_prices(sid, cfg["history_days"], today, sleep, full)
        if len(rows) < 80:
            excluded.append({"id": sid, "name": names.get(sid, sid),
                             "reason": f"日線僅 {len(rows)} 筆,資料不足"})
            continue
        gap = detect_price_gap(rows, cfg["screen"]["price_gap_abs_pct"])
        if gap:
            excluded.append({"id": sid, "name": names.get(sid, sid),
                             "reason": f"排除:{gap}"})
            continue
        t = compute_tech(rows, bench)
        if "error" in t:
            excluded.append({"id": sid, "name": names.get(sid, sid), "reason": t["error"]})
            continue
        t["_close_20d_ago"] = rows[-21]["close"] if len(rows) > 21 else None
        path, why = trend_gate(t)
        if path is None:
            continue
        checks = risk_price_checks(t, cfg)
        failed = [c_["name"] for c_ in checks if not c_["pass"]]
        stage1.append({"id": sid, "name": names.get(sid, sid), "path": path,
                       "trend_why": why, "t": t, "checks": checks, "failed": failed})

    log(f"  篩選第一階段:{len(checked)} 檔檢核,{len(stage1)} 檔通過趨勢門檻")
    for item in stage1:
        t, sid = item["t"], item["id"]
        if item["failed"]:
            watch.append({
                "id": sid, "name": item["name"], "path": item["path"],
                "trend_why": item["trend_why"],
                "blocked_by": item["failed"],
                "close": t["close"],
                "key_values": {"bias60": t["bias"].get("60"), "ret20d": t["ret"].get("20d"),
                               "bias10": t["bias"].get("10"), "rsi6": t["rsi"].get("6"),
                               "k": t["kd"].get("k"), "vol_ratio": t.get("vol_ratio")},
                "pullback_needed": pullback_needed(t, cfg)})
            continue
        # 第二階段:只對通過價格面的候選抓法人與本益比
        inst = compute_inst(sid, today, sleep)
        per = compute_per(sid, today, sleep)
        c6 = {"id": 6, "name": "本益比未達近一年P90"}
        if per and per.get("per_pctile_1y") is not None:
            c6["value"] = per["per_pctile_1y"]
            c6["pass"] = per["per_pctile_1y"] < cfg["screen"]["per_percentile_block"]
        else:
            c6["value"] = None
            c6["pass"] = True
            c6["skipped"] = "TaiwanStockPER 查無資料,略過此項"
        c7 = {"id": 7, "name": "近5日投信或外資買超,且外資非處於連3日以上賣超"}
        if inst:
            buy_ok = inst["cum5"]["foreign"] > 0 or inst["cum5"]["trust"] > 0
            streak_ok = inst["foreign_sell_streak_now"] < 3
            c7["value"] = {"foreign5": inst["cum5"]["foreign"], "trust5": inst["cum5"]["trust"],
                           "foreign_sell_streak_now": inst["foreign_sell_streak_now"],
                           "had_3d_sell_in_window": inst["foreign_3d_sell_in_5d_window"],
                           "had_3d_sell_dates": inst["foreign_3d_sell_dates"]}
            c7["pass"] = buy_ok and streak_ok
        else:
            c7["value"] = None
            c7["pass"] = False
            c7["skipped"] = "法人資料查無,無法確認,視為不通過"
        item["checks"] += [c6, c7]
        f2 = [c_["name"] for c_ in (c6, c7) if not c_["pass"]]
        if f2:
            watch.append({"id": sid, "name": item["name"], "path": item["path"],
                          "trend_why": item["trend_why"], "blocked_by": f2,
                          "close": t["close"],
                          "key_values": {"per_pctile": c6.get("value"), "inst": c7.get("value")},
                          "pullback_needed": pullback_needed(t, cfg)})
            continue
        item["inst"], item["per"] = inst, per
        passed.append(item)

    # 排序:趨勢健康度高、風險低者優先
    def score(it):
        t = it["t"]
        return ((t.get("ma60_slope10") or 0) * 2
                - (t["bias"].get("60") or 0) * 0.5
                - (t["ret"].get("20d") or 0) * 0.3
                + (0 if it["path"] == "A" else 1))
    passed.sort(key=score, reverse=True)

    sel = None
    if passed:
        it = passed[0]
        t = it["t"]
        sel = {
            "id": it["id"], "name": it["name"], "path": it["path"],
            "trend_why": it["trend_why"],
            "checks": it["checks"],
            "tech": {k: v for k, v in t.items() if not k.startswith("_")},
            "inst": it.get("inst"), "per": it.get("per"),
            "risk_panel": {
                "季線乖離率%": t["bias"].get("60"),
                "近20日漲幅%": t["ret"].get("20d"),
                "回到月線需跌%": r2((t["ma"]["20"] / t["close"] - 1) * 100) if t["ma"].get("20") else None,
                "回到季線需跌%": r2((t["ma"]["60"] / t["close"] - 1) * 100) if t["ma"].get("60") else None,
                "近20日日報酬標準差%": t.get("std20_daily_ret"),
                "本益比": (it.get("per") or {}).get("per"),
                "本益比近一年百分位%": (it.get("per") or {}).get("per_pctile_1y"),
            },
        }
    return {"checked": checked, "checked_count": len(checked),
            "selected": sel, "passed_count": len(passed),
            "also_passed": [{"id": p["id"], "name": p["name"]} for p in passed[1:]],
            "watchlist": watch[:2], "excluded": excluded,
            "note": "本篩選為候選名單逐一檢核,非全市場掃描"}


# ------------------------------------------------------------ TWSE 二次驗證
def twse_verify(sid: str, d: str) -> dict | None:
    """用證交所官方端點驗證上市股票最新收盤價。失敗不致命。"""
    ymd = d.replace("-", "")
    url = ("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
           f"?date={ymd}&stockNo={sid}&response=json")
    try:
        j = _get(url, timeout=25)
    except Exception as e:                                    # noqa: BLE001
        NOTES.append(f"{sid}: TWSE 二次驗證未取得({type(e).__name__})")
        return None
    rows = j.get("data") or []
    if not rows:
        return None
    for row in reversed(rows):
        try:
            roc = row[0].split("/")
            iso = f"{int(roc[0]) + 1911}-{int(roc[1]):02d}-{int(roc[2]):02d}"
        except (ValueError, IndexError):
            continue
        if iso == d:
            try:
                return {"date": iso, "close": float(row[6].replace(",", ""))}
            except (ValueError, IndexError):
                return None
    return None


# ------------------------------------------------------------ 0050 進場區
def etf_entry_zone(t: dict) -> dict:
    c = t["close"]
    out = {}
    for p in (20, 60):
        m = t["ma"].get(str(p))
        out[f"回到MA{p}"] = ({"price": m, "need_pct": r2((m / c - 1) * 100)}
                           if m else {"price": None, "need_pct": None})
    m60, p20 = t["ma"].get("60"), t.get("bias60_p20")
    if m60 is not None and p20 is not None:
        tgt = m60 * (1 + p20 / 100)
        out["季線乖離達近一年P20"] = {"price": r2(tgt), "need_pct": r2((tgt / c - 1) * 100),
                                "bias60_p20_pct": p20,
                                "目前乖離百分位": t.get("bias60_pctile_1y")}
    else:
        out["季線乖離達近一年P20"] = {"price": None, "need_pct": None,
                                "note": "乖離序列不足一年,無法計算"}
    out["近20日日報酬標準差%"] = t.get("std20_daily_ret")
    out["近一年高低區間百分位%"] = t["range52w"].get("pct_rank")
    out["近一年高/低"] = [t["range52w"].get("high"), t["range52w"].get("low")]
    return out


def zone_hint(t: dict, hits: int, total: int) -> str:
    """機械式初判,最終判斷仍由報告端依訊號與風險綜合說明。"""
    pr = t["range52w"].get("pct_rank")
    pc = t.get("bias60_pctile_1y")
    if pr is None:
        return "資料不足"
    if hits >= 6 and (pc is not None and pc <= 30):
        return "積極進場區"
    if hits >= 4 and pr <= 70:
        return "分批布局區"
    if pr >= 85 and (pc is not None and pc >= 70):
        return "追高風險區"
    return "觀望等待區"


def count_hits(sig: dict) -> tuple[int, int, int]:
    hit = sum(1 for v in sig.values() if v["hit"] is True)
    nd = sum(1 for v in sig.values() if v["hit"] is None)
    return hit, nd, len(sig)


def validate_config(cfg: dict) -> list[str]:
    """檢查 config.json 結構,讓打錯的設定在抓資料前就失敗,而不是產出壞報告。"""
    errs = []
    for key in ("fixed", "reverse", "candidates", "benchmark", "screen",
                "history_days"):
        if key not in cfg:
            errs.append(f"缺少必要欄位 {key}")
    seen = {}
    for group in ("fixed", "reverse"):
        for i, item in enumerate(cfg.get(group, [])):
            if not isinstance(item, dict):
                errs.append(f"{group}[{i}] 必須是物件")
                continue
            for k in ("id", "name"):
                if not item.get(k):
                    errs.append(f"{group}[{i}] 缺少 {k}")
            sid = str(item.get("id", ""))
            if sid and not sid.replace("-", "").isalnum():
                errs.append(f"{group}[{i}] 代號 {sid!r} 含不合法字元")
            if sid in seen:
                errs.append(f"代號 {sid} 在 {seen[sid]} 與 {group} 重複")
            seen[sid] = group
    for sid in cfg.get("candidates", []):
        if not str(sid).isalnum():
            errs.append(f"candidates 的 {sid!r} 含不合法字元")
        if sid in seen:
            errs.append(f"candidates 的 {sid} 與固定追蹤重複(篩選時會自動排除,可移除)")
    missing_names = [s_ for s_ in cfg.get("candidates", [])
                     if s_ not in cfg.get("candidate_names", {})]
    if missing_names:
        errs.append(f"candidate_names 缺少這些代號的名稱: {missing_names}"
                    "(不影響執行,但報告只會顯示代號)")
    if not cfg.get("reverse"):
        errs.append("reverse 為空:不會有任何進場訊號檢核(若是刻意的可忽略)")
    sc = cfg.get("screen", {})
    for k in ("max_bias60_pct", "max_ret20_pct", "max_above_ma10_pct",
              "max_rsi6", "max_k", "max_vol_ratio", "per_percentile_block",
              "price_gap_abs_pct"):
        if not isinstance(sc.get(k), (int, float)):
            errs.append(f"screen.{k} 必須是數字")
    if cfg.get("history_days", 0) < 400:
        errs.append("history_days 建議至少 400(要算滿一年的乖離百分位)")
    return errs


def sanitize(o):
    """把 NaN/Inf 轉成 None,確保輸出是嚴格合法的 JSON。"""
    import math as _m
    if isinstance(o, float):
        return None if (_m.isnan(o) or _m.isinf(o)) else o
    if isinstance(o, dict):
        return {k: sanitize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [sanitize(v) for v in o]
    return o


# ------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-refresh", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check-config", action="store_true",
                    help="只驗證 config.json,不連網")
    ap.add_argument("--out", default=os.path.join(ROOT, "daily.json"))
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    a = ap.parse_args()

    if a.selftest:
        import random
        random.seed(1)
        close = [100.0]
        for _ in range(319):
            close.append(max(1.0, close[-1] * (1 + random.gauss(0, 0.02))))
        high = [c * 1.01 for c in close]
        low = [c * 0.99 for c in close]
        ok, bad = ind.cross_check(high, low, close)
        print("指標交叉驗算:", "通過" if ok else "不一致 " + str(bad[:5]))
        return 0 if ok else 1

    cfg = json.load(open(a.config, encoding="utf-8"))
    errs = validate_config(cfg)
    if a.check_config:
        for e in errs:
            print("設定問題:", e)
        print("config.json 檢查完成" + ("(無問題)" if not errs else
                                    f",共 {len(errs)} 項需確認"))
        return 0
    for e in errs:
        log("設定警告:", e)
    sleep = cfg.get("request_sleep_sec", 1.2)
    today = dt.date.today()
    log(f"執行日 {today},FinMind token: {'有' if TOKEN else '無(匿名,流量較低)'}")

    # 大盤
    brows = get_prices(cfg["benchmark"], cfg["history_days"], today, sleep, a.full_refresh)
    if len(brows) < 30:
        # 仍寫出檔案,讓排程端讀得到失敗原因而不是讀到昨天的舊檔
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "run_date": today.isoformat(),
                       "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       "pipeline_error": "TAIEX 日線取得失敗,本次未產生任何盤勢資料",
                       "missing": MISSING}, f, ensure_ascii=False)
        log("TAIEX 取得失敗,已寫出 pipeline_error")
        return 2
    bench = compute_tech(brows)
    data_date = bench["date"]
    log(f"最近交易日 {data_date}(大盤 {bench['close']}, {bench['chg_pct']:+.2f}%)")

    out: dict = {
        "schema": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "run_date": today.isoformat(),
        "data_date": data_date,
        "is_fresh_session": data_date == today.isoformat(),
        "benchmark": {"id": "TAIEX", **{k: v for k, v in bench.items()
                                        if not k.startswith("_")}},
        "stocks": {}, "screen": {}, "missing": [], "notes": [],
    }

    tracked = cfg["fixed"] + cfg["reverse"]
    for item in tracked:
        sid, is_etf = item["id"], item.get("type") == "etf"
        log(f"處理 {sid} {item['name']}")
        rows = get_prices(sid, cfg["history_days"], today, sleep, a.full_refresh)
        if len(rows) < 30:
            out["stocks"][sid] = {"name": item["name"],
                                  "error": f"日線資料僅 {len(rows)} 筆,無法計算"}
            note_missing(f"{sid}: 日線資料不足")
            continue
        gap = detect_price_gap(rows, cfg["screen"]["price_gap_abs_pct"])
        t = compute_tech(rows, bench)
        if "error" in t:
            out["stocks"][sid] = {"name": item["name"], "error": t["error"]}
            continue
        divergence(rows, t)
        inst = compute_inst(sid, today, sleep)
        mg = compute_margin(sid, today, sleep)
        per = compute_per(sid, today, sleep)
        rev = {} if is_etf else compute_revenue(sid, today, sleep)

        rec: dict = {
            "name": item["name"], "market": item.get("market"),
            "note": item.get("note", ""),
            "single_source": item.get("market") == "tpex",
            "price_gap_warning": gap,
            "tech": {k: v for k, v in t.items() if not k.startswith("_")},
            "inst": inst or None, "margin": mg or None, "per": per or None,
            "revenue": (None if is_etf else (rev or None)),
        }
        if is_etf:
            rec["revenue_note"] = "ETF 無月營收,不適用"
            sig = entry_signals(t, inst, mg, per, bench)
            hits, nd, tot = count_hits(sig)
            rec["signal_type"] = "entry"
            rec["signals"] = sig
            rec["signal_summary"] = {"hit": hits, "no_data": nd, "total": tot}
            rec["entry_zone"] = etf_entry_zone(t)
            rec["zone_hint"] = zone_hint(t, hits, tot)
        else:
            sig = reduce_signals(t, inst, mg, per, rev, bench)
            hits, nd, tot = count_hits(sig)
            rec["signal_type"] = "reduce"
            rec["signals"] = sig
            rec["signal_summary"] = {"hit": hits, "no_data": nd, "total": tot}
        if item.get("market") == "twse":
            v = twse_verify(sid, t["date"])
            if v:
                rec["twse_verify"] = {"close": v["close"],
                                      "match": abs(v["close"] - t["close"]) < 0.02}
            else:
                rec["twse_verify"] = None
        out["stocks"][sid] = rec

    exclude = {i["id"] for i in tracked}
    log("開始每日篩選")
    scr = run_screen(cfg, today, bench, exclude, sleep, a.full_refresh)

    # 每日新選標的同樣要做減碼訊號檢核,並併入 stocks 讓報告端處理方式一致
    if scr.get("selected"):
        sel = scr["selected"]
        sid = sel["id"]
        log(f"新選標的 {sid} {sel['name']},補抓融資券與月營收")
        rows = get_prices(sid, cfg["history_days"], today, sleep, False)
        t = compute_tech(rows, bench)
        if "error" not in t:
            divergence(rows, t)
            mg = compute_margin(sid, today, sleep)
            rev = compute_revenue(sid, today, sleep)
            sig = reduce_signals(t, sel.get("inst") or {}, mg, sel.get("per") or {},
                                 rev, bench)
            hits, nd, tot = count_hits(sig)
            out["stocks"][sid] = {
                "name": sel["name"], "market": "twse/tpex(篩選標的)",
                "note": f"每日新選標的(路徑{sel['path']})",
                "is_daily_pick": True,
                "single_source": False,
                "price_gap_warning": detect_price_gap(rows, cfg["screen"]["price_gap_abs_pct"]),
                "tech": {k: v for k, v in t.items() if not k.startswith("_")},
                "inst": sel.get("inst"), "margin": mg or None,
                "per": sel.get("per"), "revenue": rev or None,
                "signal_type": "reduce", "signals": sig,
                "signal_summary": {"hit": hits, "no_data": nd, "total": tot},
            }
            sel.pop("tech", None)      # 已併入 stocks,避免重複佔空間
            sel.pop("inst", None)
            sel.pop("per", None)
    out["screen"] = scr

    out["missing"] = MISSING
    out["notes"] = NOTES + [
        "上櫃標的(3324)僅 FinMind 單一來源,未經官方二次驗證",
        "技術指標由本管線自行計算,並以兩套獨立實作交叉驗算後才輸出",
        "需新聞判讀的檢核項標記 needs_news,由報告端補充或註明資料不足",
    ]
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(sanitize(out), f, ensure_ascii=False, separators=(",", ":"),
                  allow_nan=False)
    size = os.path.getsize(a.out)
    log(f"完成:{a.out} ({size/1024:.1f} KB),未取得資料 {len(MISSING)} 項")
    print(f"OK data_date={data_date} size={size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
