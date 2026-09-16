"""
technical indicators — two independent implementations for cross-checking.

impl A: explicit pure-python loops (no pandas)
impl B: pandas/numpy vectorised primitives

both follow the same definitions; the cross-check catches coding bugs
(off-by-one, wrong window, NaN handling), not definitional disagreements.
conventions used (Taiwan market standard):
  KD   : 9-day RSV, K = 2/3*K[-1] + 1/3*RSV, D = 2/3*D[-1] + 1/3*K, seed K=D=50
  MACD : EMA12 / EMA26, DIF = EMA12-EMA26, signal = EMA9(DIF), OSC = DIF-signal
         EMAs seeded with the SMA of the first n values
  RSI  : Wilder smoothing, seeded with the SMA of the first n gains/losses
"""
from __future__ import annotations
import math

# ---------------------------------------------------------------- impl A


def _ema_a(vals, n):
    """EMA seeded with SMA of first n."""
    if len(vals) < n:
        return []
    out = [None] * (n - 1)
    sma = sum(vals[:n]) / n
    out.append(sma)
    k = 2.0 / (n + 1.0)
    prev = sma
    for v in vals[n:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def sma_a(vals, n):
    out = []
    for i in range(len(vals)):
        if i + 1 < n:
            out.append(None)
        else:
            out.append(sum(vals[i + 1 - n:i + 1]) / n)
    return out


def kd_a(high, low, close, n=9, seed=50.0):
    k_list, d_list = [], []
    k, d = seed, seed
    for i in range(len(close)):
        if i + 1 < n:
            k_list.append(None)
            d_list.append(None)
            continue
        hh = max(high[i + 1 - n:i + 1])
        ll = min(low[i + 1 - n:i + 1])
        rsv = 50.0 if hh == ll else (close[i] - ll) / (hh - ll) * 100.0
        k = (2.0 / 3.0) * k + (1.0 / 3.0) * rsv
        d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
        k_list.append(k)
        d_list.append(d)
    return k_list, d_list


def macd_a(close, fast=12, slow=26, sig=9):
    ef = _ema_a(close, fast)
    es = _ema_a(close, slow)
    if not es:
        return [], [], []
    dif = [None] * len(close)
    for i in range(len(close)):
        if i < len(ef) and i < len(es) and ef[i] is not None and es[i] is not None:
            dif[i] = ef[i] - es[i]
    valid = [v for v in dif if v is not None]
    start = len(dif) - len(valid)
    sg_valid = _ema_a(valid, sig)
    signal = [None] * len(close)
    for j, v in enumerate(sg_valid):
        if v is not None:
            signal[start + j] = v
    osc = [None if (dif[i] is None or signal[i] is None) else dif[i] - signal[i]
           for i in range(len(close))]
    return dif, signal, osc


def rsi_a(close, n=14):
    out = [None] * len(close)
    if len(close) <= n:
        return out
    gains, losses = [], []
    for i in range(1, len(close)):
        ch = close[i] - close[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


# ---------------------------------------------------------------- impl B
def _pd():
    import pandas as pd
    return pd


def _ema_b(s, n):
    pd = _pd()
    s = pd.Series(s, dtype="float64")
    if len(s) < n:
        return pd.Series([float("nan")] * len(s))
    seeded = s.copy()
    seeded.iloc[n - 1] = s.iloc[:n].mean()
    seeded.iloc[:n - 1] = float("nan")
    return seeded.ewm(span=n, adjust=False, ignore_na=False).mean().where(
        pd.Series(range(len(s))).ge(n - 1).values)


def sma_b(vals, n):
    pd = _pd()
    return pd.Series(vals, dtype="float64").rolling(n).mean()


def kd_b(high, low, close, n=9, seed=50.0):
    pd = _pd()
    h = pd.Series(high, dtype="float64")
    l = pd.Series(low, dtype="float64")
    c = pd.Series(close, dtype="float64")
    hh = h.rolling(n).max()
    ll = l.rolling(n).min()
    rng = (hh - ll)
    rsv = ((c - ll) / rng * 100.0).where(rng != 0, 50.0)
    rsv = rsv.where(hh.notna())
    k = pd.Series([float("nan")] * len(c))
    d = pd.Series([float("nan")] * len(c))
    pk, pdv = seed, seed
    idx = rsv.notna().to_numpy()
    rv = rsv.to_numpy()
    kv = k.to_numpy().copy()
    dv = d.to_numpy().copy()
    for i in range(len(rv)):
        if not idx[i]:
            continue
        pk = (2.0 / 3.0) * pk + (1.0 / 3.0) * rv[i]
        pdv = (2.0 / 3.0) * pdv + (1.0 / 3.0) * pk
        kv[i], dv[i] = pk, pdv
    return pd.Series(kv), pd.Series(dv)


def macd_b(close, fast=12, slow=26, sig=9):
    pd = _pd()
    ef = _ema_b(close, fast)
    es = _ema_b(close, slow)
    dif = ef - es
    valid = dif.dropna()
    sg_valid = _ema_b(valid.tolist(), sig)
    signal = pd.Series([float("nan")] * len(dif))
    if len(valid):
        signal.iloc[valid.index[0]:] = sg_valid.to_numpy()
    return dif, signal, dif - signal


def rsi_b(close, n=14):
    pd = _pd()
    c = pd.Series(close, dtype="float64")
    ch = c.diff()
    gain = ch.clip(lower=0)
    loss = (-ch).clip(lower=0)
    ag = gain.copy() * float("nan")
    al = ag.copy()
    if len(c) > n:
        ag.iloc[n] = gain.iloc[1:n + 1].mean()
        al.iloc[n] = loss.iloc[1:n + 1].mean()
        for i in range(n + 1, len(c)):
            ag.iloc[i] = (ag.iloc[i - 1] * (n - 1) + gain.iloc[i]) / n
            al.iloc[i] = (al.iloc[i - 1] * (n - 1) + loss.iloc[i]) / n
    rs = ag / al
    out = 100.0 - 100.0 / (1 + rs)
    return out.where(al != 0, 100.0).where(ag.notna())


# ---------------------------------------------------------------- compare
def _close(a, b, tol=1e-6):
    if a is None and (b is None or (isinstance(b, float) and math.isnan(b))):
        return True
    if a is None or b is None:
        return False
    if isinstance(b, float) and math.isnan(b):
        return False
    return abs(a - b) <= tol * max(1.0, abs(a))


def cross_check(high, low, close, tol=1e-6):
    """run both implementations, return (ok, list_of_mismatch_descriptions)"""
    bad = []
    ka, da = kd_a(high, low, close)
    kb, db = kd_b(high, low, close)
    difa, siga, osca = macd_a(close)
    difb, sigb, oscb = macd_b(close)
    pairs = [("K", ka, kb), ("D", da, db), ("DIF", difa, difb),
             ("MACD", siga, sigb), ("OSC", osca, oscb),
             ("RSI6", rsi_a(close, 6), rsi_b(close, 6)),
             ("RSI12", rsi_a(close, 12), rsi_b(close, 12))]
    for n in (5, 10, 20, 60, 120):
        if len(close) >= n:
            pairs.append((f"MA{n}", sma_a(close, n), list(sma_b(close, n))))
    for name, xa, xb in pairs:
        xb = list(xb)
        for i in range(len(close)):
            va = xa[i] if i < len(xa) else None
            vb = xb[i] if i < len(xb) else None
            if not _close(va, vb, tol):
                bad.append(f"{name}[{i}]: A={va!r} B={vb!r}")
                break
    return (len(bad) == 0), bad
