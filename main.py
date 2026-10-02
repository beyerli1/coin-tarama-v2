#!/usr/bin/env python3
"""
Kripto Tarama Botu v4 – Agresif + Muhafazakâr
Borsalar: Binance Global, OKX, Gate, KuCoin, MEXC, Bitget, HTX
Odak: Alttan birikim + patlamaya hazır coinler (1- birkaç gün, %0-20)
Telegram: /tarama, /otomatik_ac, /otomatik_kapat, /yardim
Otomatik: her 1 saat (Türkiye saati UTC+3)
"""

import requests
import pandas as pd
import numpy as np
import time
import argparse
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple

TZ = ZoneInfo("Europe/Istanbul")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CryptoScanner/4.0)",
    "Accept": "application/json"
}

MIN_VOL = 2_000_000
MIN_CHG = 0.5
MAX_CHG = 25.0
TOP_N = 10
DEFAULT_INTERVAL_MIN = 60

AUTO_ENABLED = True
LAST_UPDATE_ID = 0


def now_tr() -> str:
    return datetime.now(TZ).strftime("%H:%M")


def now_tr_full() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()


def rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0)
    l = -d.clip(upper=0)
    ag = g.ewm(alpha=1/p, min_periods=p, adjust=False).mean()
    al = l.ewm(alpha=1/p, min_periods=p, adjust=False).mean()
    rs = ag / al
    return 100 - (100 / (1 + rs))


def macd(s: pd.Series):
    ef = ema(s, 12)
    es = ema(s, 26)
    line = ef - es
    sig = ema(line, 9)
    return line, sig, line - sig


def bollinger(s: pd.Series, p: int = 20):
    mid = s.rolling(p).mean()
    std = s.rolling(p).std()
    return mid + 2*std, mid, mid - 2*std


def find_levels(df: pd.DataFrame, lookback: int = 48) -> Tuple[List[float], List[float]]:
    if len(df) < lookback:
        lookback = len(df)
    window = df.tail(lookback)
    highs = window["high"].values
    lows = window["low"].values
    last = float(window["close"].iloc[-1])

    resist = []
    support = []
    for i in range(2, len(highs) - 2):
        if highs[i] >= highs[i-1] and highs[i] >= highs[i-2] and highs[i] >= highs[i+1] and highs[i] >= highs[i+2]:
            if highs[i] > last:
                resist.append(float(highs[i]))
        if lows[i] <= lows[i-1] and lows[i] <= lows[i-2] and lows[i] <= lows[i+1] and lows[i] <= lows[i+2]:
            if lows[i] < last:
                support.append(float(lows[i]))

    resist = sorted(set([round(x, 8) for x in resist]))[:2] if resist else []
    support = sorted(set([round(x, 8) for x in support]), reverse=True)[:2] if support else []

    if len(resist) < 2:
        for h in sorted(window["high"].nlargest(4).tolist(), reverse=True):
            if h > last and round(h, 8) not in resist:
                resist.append(round(float(h), 8))
            if len(resist) >= 2:
                break
    if len(support) < 2:
        for l in sorted(window["low"].nsmallest(4).tolist()):
            if l < last and round(l, 8) not in support:
                support.append(round(float(l), 8))
            if len(support) >= 2:
                break
    return support[:2], resist[:2]


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            r = requests.post(url, json={
                "chat_id": chat_id, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True
            }, timeout=15)
            if r.status_code != 200:
                print(f"[TG hata] {r.status_code}: {r.text[:100]}")
                return False
            time.sleep(0.25)
        except Exception as e:
            print(f"[TG hata] {e}")
            return False
    return True


def get_updates(token: str, offset: int = 0):
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": offset, "timeout": 8, "limit": 20}, timeout=15)
        data = r.json()
        if not data.get("ok"):
            return [], offset
        ups = data.get("result", [])
        nxt = ups[-1]["update_id"] + 1 if ups else offset
        return ups, nxt
    except Exception as e:
        print(f"[getUpdates] {e}")
        return [], offset


def parse_cmd(upd: dict, chat_id: str) -> Optional[str]:
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return None
    if str(msg.get("chat", {}).get("id")) != str(chat_id):
        return None
    text = (msg.get("text") or "").strip().lower()
    if not text:
        return None
    return text.split("@")[0].strip()


STABLES = {"USDC", "USDT", "DAI", "TUSD", "FDUSD", "USDE", "BUSD", "USD1", "USDP"}


def fetch_binance() -> List[Dict]:
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/24hr", headers=HEADERS, timeout=15)
        out = []
        for t in r.json():
            sym = t.get("symbol", "")
            if sym.endswith("USDT"):
                base, quote = sym[:-4], "USDT"
            elif sym.endswith("TRY"):
                base, quote = sym[:-3], "TRY"
            else:
                continue
            if base in STABLES:
                continue
            try:
                last = float(t["lastPrice"])
                chg = float(t.get("priceChangePercent", 0) or 0)
                vol = float(t.get("quoteVolume", 0) or 0)
                if quote == "TRY":
                    vol /= 34.0
                if last <= 0 or vol < 500:
                    continue
                out.append({"exchange": "Binance", "symbol": base, "pair": sym, "quote": quote,
                            "last": last, "change_pct": chg, "vol_usdt": vol})
            except:
                continue
        return out
    except Exception as e:
        print(f"[Binance] {e}")
        return []


def fetch_okx() -> List[Dict]:
    try:
        r = requests.get("https://www.okx.com/api/v5/market/tickers?instType=SPOT", headers=HEADERS, timeout=12)
        data = r.json()
        if data.get("code") != "0":
            return []
        out = []
        for t in data.get("data", []):
            inst = t.get("instId", "")
            if inst.endswith("-USDT"):
                base, quote = inst.replace("-USDT", ""), "USDT"
            elif inst.endswith("-TRY"):
                base, quote = inst.replace("-TRY", ""), "TRY"
            else:
                continue
            if base in STABLES:
                continue
            try:
                last = float(t["last"])
                o24 = float(t["open24h"])
                vol = float(t.get("volCcy24h", 0) or 0)
                if quote == "TRY":
                    vol /= 34.0
                if last <= 0 or o24 <= 0 or vol < 500:
                    continue
                chg = ((last - o24) / o24) * 100
                out.append({"exchange": "OKX", "symbol": base, "pair": inst, "quote": quote,
                            "last": last, "change_pct": chg, "vol_usdt": vol})
            except:
                continue
        return out
    except Exception as e:
        print(f"[OKX] {e}")
        return []


def fetch_gate() -> List[Dict]:
    try:
        r = requests.get("https://api.gateio.ws/api/v4/spot/tickers", headers=HEADERS, timeout=12)
        out = []
        for t in r.json():
            pair = t.get("currency_pair", "")
            if not pair.endswith("_USDT"):
                continue
            base = pair.replace("_USDT", "")
            if base in STABLES:
                continue
            try:
                last = float(t["last"])
                chg = float(t.get("change_percentage", 0) or 0)
                vol = float(t.get("quote_volume", 0) or 0)
                if last <= 0 or vol < 500:
                    continue
                out.append({"exchange": "Gate", "symbol": base, "pair": pair, "quote": "USDT",
                            "last": last, "change_pct": chg, "vol_usdt": vol})
            except:
                continue
        return out
    except Exception as e:
        print(f"[Gate] {e}")
        return []


def fetch_kucoin() -> List[Dict]:
    try:
        r = requests.get("https://api.kucoin.com/api/v1/market/allTickers", headers=HEADERS, timeout=12)
        data = r.json()
        if data.get("code") != "200000":
            return []
        out = []
        for t in data.get("data", {}).get("ticker", []):
            sym = t.get("symbol", "")
            if not sym.endswith("-USDT"):
                continue
            base = sym.replace("-USDT", "")
            if base in STABLES:
                continue
            try:
                last = float(t["last"])
                chg = float(t.get("changeRate", 0) or 0) * 100
                vol = float(t.get("volValue", 0) or 0)
                if last <= 0 or vol < 500:
                    continue
                out.append({"exchange": "KuCoin", "symbol": base, "pair": sym, "quote": "USDT",
                            "last": last, "change_pct": chg, "vol_usdt": vol})
            except:
                continue
        return out
    except Exception as e:
        print(f"[KuCoin] {e}")
        return []


def fetch_mexc() -> List[Dict]:
    try:
        r = requests.get("https://api.mexc.com/api/v3/ticker/24hr", headers=HEADERS, timeout=12)
        out = []
        for t in r.json():
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in STABLES:
                continue
            try:
                last = float(t["lastPrice"])
                chg = float(t.get("priceChangePercent", 0) or 0)
                vol = float(t.get("quoteVolume", 0) or 0)
                if last <= 0 or vol < 500:
                    continue
                out.append({"exchange": "MEXC", "symbol": base, "pair": sym, "quote": "USDT",
                            "last": last, "change_pct": chg, "vol_usdt": vol})
            except:
                continue
        return out
    except Exception as e:
        print(f"[MEXC] {e}")
        return []


def fetch_bitget() -> List[Dict]:
    try:
        r = requests.get("https://api.bitget.com/api/v2/spot/market/tickers", headers=HEADERS, timeout=12)
        data = r.json()
        if data.get("code") != "00000":
            return []
        real_r = {"RENDER", "RONIN", "RARE", "RSR", "ROSE", "RAY", "RATS", "RIF", "REZ", "RNDR", "RUNE", "RVN", "REQ", "RPL", "RON"}
        out = []
        for t in data.get("data", []):
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in STABLES:
                continue
            if len(base) > 12 or (base.startswith("R") and base.isupper() and base not in real_r):
                continue
            try:
                last = float(t["lastPr"])
                chg = float(t.get("change24h", 0) or 0) * 100
                vol = float(t.get("quoteVolume", 0) or 0)
                if last <= 0 or vol < 500:
                    continue
                out.append({"exchange": "Bitget", "symbol": base, "pair": sym, "quote": "USDT",
                            "last": last, "change_pct": chg, "vol_usdt": vol})
            except:
                continue
        return out
    except Exception as e:
        print(f"[Bitget] {e}")
        return []


def fetch_htx() -> List[Dict]:
    try:
        r = requests.get("https://api.huobi.pro/market/tickers", headers=HEADERS, timeout=12)
        data = r.json()
        if data.get("status") != "ok":
            return []
        out = []
        for t in data.get("data", []):
            sym = t.get("symbol", "").upper()
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in STABLES:
                continue
            try:
                last = float(t["close"])
                o = float(t["open"])
                vol = float(t.get("amount", 0) or 0)
                if vol <= 0:
                    vol = float(t.get("vol", 0) or 0) * last
                if last <= 0 or o <= 0 or vol < 500 or vol > 2e9:
                    continue
                chg = ((last - o) / o) * 100
                out.append({"exchange": "HTX", "symbol": base, "pair": sym, "quote": "USDT",
                            "last": last, "change_pct": chg, "vol_usdt": vol})
            except:
                continue
        return out
    except Exception as e:
        print(f"[HTX] {e}")
        return []


def candles_binance(pair: str, interval: str = "1h", limit: int = 100) -> Optional[pd.DataFrame]:
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/klines",
                         params={"symbol": pair, "interval": interval, "limit": limit},
                         headers=HEADERS, timeout=12)
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","ct","qv","n","tb","tq","i"])
        for c in ["open","high","low","close","vol"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["close"])
    except:
        return None


def candles_okx(pair: str, bar: str = "1H", limit: int = 100) -> Optional[pd.DataFrame]:
    try:
        r = requests.get("https://www.okx.com/api/v5/market/candles",
                         params={"instId": pair, "bar": bar, "limit": str(limit)},
                         headers=HEADERS, timeout=12)
        data = r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        rows = list(reversed(data["data"]))
        df = pd.DataFrame(rows, columns=["ts","open","high","low","close","vol","volCcy","volCcyQuote","confirm"])
        for c in ["open","high","low","close","vol"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.dropna(subset=["close"])
    except:
        return None


def candles_gate(pair: str, interval: str = "1h", limit: int = 100) -> Optional[pd.DataFrame]:
    try:
        r = requests.get("https://api.gateio.ws/api/v4/spot/candlesticks",
                         params={"currency_pair": pair, "interval": interval, "limit": limit},
                         headers=HEADERS, timeout=12)
        rows = r.json()
        if not isinstance(rows, list) or not rows:
            return None
        df = pd.DataFrame(rows, columns=["ts","qv","close","high","low","open","vol","closed"])
        for c in ["open","high","low","close","vol"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("ts").reset_index(drop=True)
        return df.dropna(subset=["close"])
    except:
        return None


def get_candles(ex: str, pair: str, tf: str = "1h") -> Optional[pd.DataFrame]:
    if ex == "Binance":
        return candles_binance(pair, interval=tf)
    if ex == "OKX":
        return candles_okx(pair, bar="1H" if tf == "1h" else "4H")
    if ex == "Gate":
        return candles_gate(pair, interval=tf)
    return candles_binance(pair.replace("-", "").replace("_", ""), interval=tf) or \
           candles_okx(pair if "-" in pair else f"{pair}-USDT")


def calc_ind(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) < 50:
        return df
    c = df["close"]
    df["rsi"] = rsi(c)
    ml, ms, mh = macd(c)
    df["macd"] = ml
    df["macd_sig"] = ms
    df["macd_hist"] = mh
    df["ema9"] = ema(c, 9)
    df["ema21"] = ema(c, 21)
    df["ema50"] = ema(c, 50)
    bh, bm, bl = bollinger(c)
    df["bb_h"] = bh
    df["bb_m"] = bm
    df["bb_l"] = bl
    df["bb_w"] = (bh - bl) / bm
    df["vol_sma"] = df["vol"].rolling(20).mean()
    df["vol_ratio"] = df["vol"] / df["vol_sma"]
    df["range_10"] = (df["high"].rolling(10).max() - df["low"].rolling(10).min()) / c
    df["range_30"] = (df["high"].rolling(30).max() - df["low"].rolling(30).min()) / c
    return df


def score_coin(df: pd.DataFrame, ticker: Dict) -> Dict:
    if df is None or len(df) < 50:
        return {"agg": 0, "cons": 0, "signals": [], "support": [], "resist": []}

    last = df.iloc[-1]
    prev = df.iloc[-2]
    signals = []
    agg = 0.0
    cons = 0.0

    if pd.notna(last.get("ema9")) and pd.notna(last.get("ema21")):
        if last["close"] > last["ema9"] > last["ema21"]:
            agg += 1.5
            cons += 2.0
            signals.append("EMA9>EMA21")
        if pd.notna(last.get("ema50")) and last["close"] > last["ema50"]:
            cons += 1.5
            agg += 0.8
            signals.append(">EMA50")

    rv = last.get("rsi")
    if pd.notna(rv):
        if 40 <= rv <= 65:
            cons += 2.0
            agg += 1.0
            signals.append(f"RSI sağlıklı ({rv:.0f})")
        elif 30 <= rv < 40:
            agg += 1.5
            cons += 0.5
            signals.append(f"RSI düşük ({rv:.0f})")
        elif rv > 72:
            agg -= 1.5
            cons -= 2.0
            signals.append(f"RSI yüksek ({rv:.0f})")

    if pd.notna(last.get("macd_hist")) and pd.notna(prev.get("macd_hist")):
        if last["macd_hist"] > 0 and last["macd_hist"] > prev["macd_hist"]:
            agg += 2.0
            cons += 1.5
            signals.append("MACD hist+")
        if last["macd"] > last["macd_sig"] and prev["macd"] <= prev["macd_sig"]:
            agg += 1.5
            signals.append("MACD cross")

    vr = last.get("vol_ratio")
    if pd.notna(vr):
        if vr >= 2.0:
            agg += 2.5
            cons += 0.8
            signals.append(f"Hacim x{vr:.1f}")
        elif vr >= 1.3:
            agg += 1.2
            cons += 1.0
            signals.append(f"Hacim x{vr:.1f}")

    if pd.notna(last.get("bb_w")) and pd.notna(last.get("range_10")) and pd.notna(last.get("range_30")):
        if last["range_10"] < last["range_30"] * 0.55 and vr and vr > 1.2:
            agg += 2.0
            cons += 1.0
            signals.append("Sıkışma + hacim")
        if last["bb_w"] > df["bb_w"].tail(8).mean() * 1.2 and last["close"] > last["bb_m"]:
            agg += 1.2
            signals.append("BB genişliyor")

    chg = ticker.get("change_pct", 0)
    if 3 <= chg <= 12:
        agg += 1.5
        cons += 1.2
        signals.append(f"+%{chg:.1f}")
    elif 12 < chg <= 20:
        agg += 1.0
        signals.append(f"+%{chg:.1f} dikkat")
    elif chg > 20:
        agg -= 0.5
        cons -= 1.0

    vol = ticker.get("vol_usdt", 0)
    if vol > 50_000_000:
        cons += 1.5
    elif vol > 15_000_000:
        cons += 0.8

    support, resist = find_levels(df)
    return {
        "agg": round(agg, 1), "cons": round(cons, 1),
        "signals": signals[:6], "support": support, "resist": resist,
        "rsi": round(float(rv), 1) if pd.notna(rv) else None,
        "vol_ratio": round(float(vr), 2) if pd.notna(vr) else None,
    }


def aggregate() -> List[Dict]:
    print(f"[{now_tr()}] Ticker'lar çekiliyor...")
    all_t = []
    for name, fn in [("Binance", fetch_binance), ("OKX", fetch_okx), ("Gate", fetch_gate),
                     ("KuCoin", fetch_kucoin), ("MEXC", fetch_mexc), ("Bitget", fetch_bitget), ("HTX", fetch_htx)]:
        print(f"  → {name}...", end=" ", flush=True)
        tks = fn()
        print(f"{len(tks)}")
        all_t.extend(tks)
        time.sleep(0.1)
    prio = {"Binance": 7, "OKX": 6, "Gate": 5, "KuCoin": 4, "MEXC": 3, "Bitget": 2, "HTX": 1}
    best = {}
    for t in all_t:
        key = f"{t['symbol'].upper()}-{t.get('quote','USDT')}"
        if key not in best or t["vol_usdt"] > best[key]["vol_usdt"] * 1.1:
            best[key] = t
        elif abs(t["vol_usdt"] - best[key]["vol_usdt"]) / max(best[key]["vol_usdt"], 1) < 0.2:
            if prio.get(t["exchange"], 0) > prio.get(best[key]["exchange"], 0):
                best[key] = t
    print(f"  Unique: {len(best)}")
    return list(best.values())


def run_scan(min_vol=MIN_VOL, min_chg=MIN_CHG, max_chg=MAX_CHG, top_cand=45):
    print("=" * 60)
    print(f"  TARAMA  |  {now_tr_full()} (TR)")
    print("=" * 60)
    all_u = aggregate()
    cands = [t for t in all_u if t["vol_usdt"] >= min_vol and min_chg <= t["change_pct"] <= max_chg]
    cands.sort(key=lambda x: x["vol_usdt"], reverse=True)
    cands = cands[:top_cand]
    print(f"Aday: {len(cands)}\n")

    scored = []
    for i, t in enumerate(cands, 1):
        print(f"  ({i}/{len(cands)}) {t['symbol']}/{t.get('quote','USDT')} [{t['exchange']}] ...", end=" ", flush=True)
        df = get_candles(t["exchange"], t["pair"], "1h")
        time.sleep(0.08)
        if df is None or len(df) < 50:
            for fb, fp in [("Binance", f"{t['symbol']}USDT"), ("OKX", f"{t['symbol']}-USDT")]:
                df = get_candles(fb, fp, "1h")
                time.sleep(0.06)
                if df is not None and len(df) >= 50:
                    t = {**t, "exchange": t["exchange"] + f"→{fb}"}
                    break
        if df is None or len(df) < 50:
            print("yok")
            continue
        df = calc_ind(df)
        sc = score_coin(df, t)
        if sc["agg"] >= 3.5 or sc["cons"] >= 3.5:
            scored.append({**t, **sc})
            print(f"A:{sc['agg']} C:{sc['cons']}")
        else:
            print("düşük")

    aggressive = sorted(scored, key=lambda x: x["agg"], reverse=True)[:TOP_N]
    conservative = sorted(scored, key=lambda x: x["cons"], reverse=True)[:TOP_N]
    return aggressive, conservative


def fmt_price(p) -> str:
    if p is None:
        return "-"
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


def format_message(agg: List[Dict], cons: List[Dict]) -> str:
    lines = [f"🚀 <b>KRİPTO TARAMA</b> — {now_tr()} (TR)\n"]
    lines.append("<b>⚡ AGRESİF TOP 10</b> (daha volatil / patlamaya yakın)")
    if not agg:
        lines.append("  Sonuç yok\n")
    else:
        for i, r in enumerate(agg, 1):
            q = r.get("quote", "USDT")
            sup = " / ".join(fmt_price(x) for x in r.get("support", [])) or "-"
            res = " / ".join(fmt_price(x) for x in r.get("resist", [])) or "-"
            lines.append(
                f"{i}. <b>{r['symbol']}/{q}</b> [{r['exchange']}]\n"
                f"   Fiyat: <code>{fmt_price(r['last'])}</code>  24s: +%{r['change_pct']:.1f}  Skor: {r['agg']}\n"
                f"   Destek: {sup}\n"
                f"   Direnç: {res}\n"
            )
    lines.append("\n<b>🛡️ MUHAFAZAKÂR TOP 10</b> (daha temiz / yavaş)")
    if not cons:
        lines.append("  Sonuç yok\n")
    else:
        for i, r in enumerate(cons, 1):
            q = r.get("quote", "USDT")
            sup = " / ".join(fmt_price(x) for x in r.get("support", [])) or "-"
            res = " / ".join(fmt_price(x) for x in r.get("resist", [])) or "-"
            lines.append(
                f"{i}. <b>{r['symbol']}/{q}</b> [{r['exchange']}]\n"
                f"   Fiyat: <code>{fmt_price(r['last'])}</code>  24s: +%{r['change_pct']:.1f}  Skor: {r['cons']}\n"
                f"   Destek: {sup}\n"
                f"   Direnç: {res}\n"
            )
    lines.append("\n⚠️ Yatırım tavsiyesi değildir. Stop kullan.")
    return "\n".join(lines)


def do_scan_and_send(token: str, chat_id: str, args):
    agg, cons = run_scan(min_vol=args.min_volume, min_chg=args.min_change,
                         max_chg=args.max_change, top_cand=args.top_candidates)
    msg = format_message(agg, cons)
    print(msg[:400])
    if token and chat_id:
        print("[TG] Gönderiliyor...")
        ok = send_telegram(token, chat_id, msg)
        print("[TG] ✓" if ok else "[TG] ✗")
    return agg, cons


def main():
    global AUTO_ENABLED, LAST_UPDATE_ID
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-volume", type=float, default=MIN_VOL)
    parser.add_argument("--min-change", type=float, default=MIN_CHG)
    parser.add_argument("--max-change", type=float, default=MAX_CHG)
    parser.add_argument("--top-candidates", type=int, default=45)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_MIN)
    parser.add_argument("--telegram-token", type=str, default="")
    parser.add_argument("--telegram-chat", type=str, default="")
    args = parser.parse_args()

    token = args.telegram_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = args.telegram_chat or os.environ.get("TELEGRAM_CHAT_ID", "")

    if not args.loop:
        do_scan_and_send(token, chat_id, args)
        return

    print(f"\n🔄 Bot başladı | Aralık: {args.interval} dk | TR saati")
    print("Komutlar: /tarama  /otomatik_ac  /otomatik_kapat  /yardim\n")

    last_scan = 0.0
    interval_sec = args.interval * 60

    try:
        do_scan_and_send(token, chat_id, args)
        last_scan = time.time()
    except Exception as e:
        print(f"[İlk tarama] {e}")

    while True:
        try:
            if token and chat_id:
                ups, LAST_UPDATE_ID = get_updates(token, LAST_UPDATE_ID)
                for u in ups:
                    cmd = parse_cmd(u, chat_id)
                    if not cmd:
                        continue
                    print(f"📩 Komut: {cmd}")
                    if cmd in ("/tarama", "/scan", "tarama", "scan"):
                        send_telegram(token, chat_id, "⏳ Tarama başladı, 1-3 dk bekle...")
                        do_scan_and_send(token, chat_id, args)
                        last_scan = time.time()
                    elif cmd in ("/otomatik_ac", "/auto_on"):
                        AUTO_ENABLED = True
                        send_telegram(token, chat_id, f"✅ Otomatik tarama AÇIK (her {args.interval} dk)")
                    elif cmd in ("/otomatik_kapat", "/auto_off"):
                        AUTO_ENABLED = False
                        send_telegram(token, chat_id, "⏸ Otomatik tarama KAPALI\nManuel için /tarama yaz")
                    elif cmd in ("/yardim", "/help", "/start"):
                        help_msg = (
                            "📋 <b>Komutlar</b>\n\n"
                            "/tarama — Hemen tarama yap\n"
                            "/otomatik_ac — Saatlik mesajı aç\n"
                            "/otomatik_kapat — Saatlik mesajı kapat\n"
                            "/yardim — Bu liste\n\n"
                            f"Otomatik: {'AÇIK' if AUTO_ENABLED else 'KAPALI'} | Aralık: {args.interval} dk\n"
                            "Saat dilimi: Türkiye (UTC+3)"
                        )
                        send_telegram(token, chat_id, help_msg)

            now = time.time()
            if AUTO_ENABLED and (now - last_scan) >= interval_sec:
                print(f"\n⏰ Otomatik tarama {now_tr()}")
                do_scan_and_send(token, chat_id, args)
                last_scan = time.time()
            time.sleep(4)
        except KeyboardInterrupt:
            print("\nDurduruldu.")
            break
        except Exception as e:
            print(f"[Hata] {e}")
            time.sleep(20)


if __name__ == "__main__":
    main()
