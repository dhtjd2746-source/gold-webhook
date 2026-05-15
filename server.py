"""
TradingView BB 터치 알림 → 김직선 분석 → SMS
Render.com 무료 배포용

흐름:
  TradingView (BB 닿을 때 알림) → 이 서버 → tradingview-ta로 분석 → SMS
"""

import os
import hmac
import hashlib
import time
import json
import math
import requests
from flask import Flask, request
from datetime import datetime, timezone, timedelta
from tradingview_ta import TA_Handler, Interval as TVInterval

app = Flask(__name__)

# ── 설정 ─────────────────────────────────────────────────
SOLAPI_KEY    = os.environ.get("SOLAPI_KEY",    "NCSRKRGVDS8CL5PP")
SOLAPI_SECRET = os.environ.get("SOLAPI_SECRET", "N32HBR7QSZUXE8SYYMKJWIXGWPORJ4G7")
FROM_NUMBER   = os.environ.get("FROM_NUMBER",   "01073030150")
TO_NUMBER     = os.environ.get("TO_NUMBER",     "01027460150")
KST           = timezone(timedelta(hours=9))

# TV 심볼 매핑 (TradingView 알림 메시지에 포함되는 ticker 기준)
TV_SYMBOLS = {
    "XAUUSD": {"symbol": "XAUUSD", "screener": "cfd",     "exchange": "OANDA",  "display": "골드",        "is_gold": True},
    "QQQ":    {"symbol": "QQQ",    "screener": "america", "exchange": "NASDAQ", "display": "나스닥(QQQ)", "is_gold": False},
}

ALERT_CODES = {
    "DOUBLE_SHORT", "SINGLE_SHORT", "BREAKOUT_SHORT", "REVERSAL_LONG",
    "DOUBLE_LONG",  "SINGLE_LONG",  "BREAKOUT_LONG",  "REVERSAL_SHORT",
}

# 쿨다운: 같은 심볼+신호 60분 내 중복 발송 방지
_last_sent = {}   # {"XAUUSD:DOUBLE_SHORT": datetime}

# ── SMS ──────────────────────────────────────────────────
def send_sms(text):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = str(int(time.time() * 1000))
    sig  = hmac.new(
        SOLAPI_SECRET.encode("utf-8"),
        (date + salt).encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    headers = {
        "Authorization": f"HMAC-SHA256 apiKey={SOLAPI_KEY}, date={date}, salt={salt}, signature={sig}",
        "Content-Type": "application/json"
    }
    body = {"message": {"to": TO_NUMBER, "from": FROM_NUMBER, "text": text}}
    res = requests.post("https://api.solapi.com/messages/v4/send",
                        headers=headers, json=body, timeout=10)
    print(f"[SMS] {res.status_code} | {text[:50]}")

# ── 지표 계산 ─────────────────────────────────────────────
def calc_bb(prices, n, mult):
    if len(prices) < n:
        return None, None
    subset = prices[-n:]
    sma = sum(subset) / n
    std = math.sqrt(sum((p - sma) ** 2 for p in subset) / n)
    return round(sma + mult * std, 1), round(sma - mult * std, 1)

def get_signal(price, bb20u, bb20l, bb4u, bb4l, direction, margin=4,
               cur_high=None, cur_low=None, cur_close=None, bb4_reliable=True):
    if direction == "neutral":
        return "NEUTRAL"

    chk_high  = cur_high  if cur_high  is not None else price
    chk_low   = cur_low   if cur_low   is not None else price
    chk_close = cur_close if cur_close is not None else price
    mid = (chk_high + chk_low) / 2

    u20 = chk_high >= bb20u - margin
    u4  = (chk_high >= bb4u - margin) if bb4_reliable else False
    d20 = chk_low  <= bb20l + margin
    d4  = (chk_low  <= bb4l + margin) if bb4_reliable else False

    if direction == "down":
        if chk_close <= bb20l:             return "BREAKOUT_SHORT"
        if u20 and u4:                     return "DOUBLE_SHORT"
        if u4  or u20:                     return "SINGLE_SHORT"
        if (d20 or d4) and chk_close >= mid: return "REVERSAL_LONG"
        return "WAIT_SHORT"
    else:
        if chk_close >= bb20u:             return "BREAKOUT_LONG"
        if d20 and d4:                     return "DOUBLE_LONG"
        if d4  or d20:                     return "SINGLE_LONG"
        if (u20 or u4) and chk_close <= mid: return "REVERSAL_SHORT"
        return "WAIT_LONG"

# ── 핵심: TV 데이터 가져와서 김직선 분석 ─────────────────
def analyze_and_sms(ticker):
    """
    ticker: 'XAUUSD' 또는 'QQQ'
    TV에서 BB 터치 알림이 오면 이 함수 실행.
    실제 신호(더블비/원비 등)이면 SMS 발송.
    """
    cfg = TV_SYMBOLS.get(ticker)
    if not cfg:
        print(f"[미지원 심볼] {ticker}")
        return

    now = datetime.now(KST)
    time_str = now.strftime("%m/%d %H:%M KST")

    # ── TV 데이터 수집 ────────────────────────
    try:
        handler = TA_Handler(
            symbol=cfg["symbol"],
            screener=cfg["screener"],
            exchange=cfg["exchange"],
            interval=TVInterval.INTERVAL_1_HOUR
        )
        ind = handler.get_analysis().indicators
    except Exception as e:
        print(f"[TV 오류] {ticker}: {e}")
        return

    price    = float(ind["close"])
    cur_high = float(ind["high"])
    cur_low  = float(ind["low"])
    cur_open = float(ind["open"])
    bb20u    = round(float(ind["BB.upper"]), 1)
    bb20l    = round(float(ind["BB.lower"]), 1)
    sma20    = float(ind["SMA20"])

    # ── SMA20 방향: close vs SMA20 위치로 빠르게 판단 ──
    # (히스토리 없는 서버 환경 대비 — close가 SMA 위/아래로 방향 추정)
    sma_gap = price - sma20
    band_half = (bb20u - bb20l) / 4   # 밴드 1/4 폭 기준
    if sma_gap < -band_half:   direction = "down"
    elif sma_gap > band_half:  direction = "up"
    else:                      direction = "neutral"

    # ── BB4/4 근사 (시가 기준 — 단일 봉 open 사용) ──────
    # 서버 히스토리 없을 때 BB4/4 ≈ open ± (bb20폭 * 0.7)
    span = (bb20u - bb20l) * 0.7
    bb4u = round(cur_open + span / 2, 1)
    bb4l = round(cur_open - span / 2, 1)
    bb4_reliable = True

    # ── 신호 판단 ─────────────────────────────
    code = get_signal(price, bb20u, bb20l, bb4u, bb4l, direction,
                      cur_high=cur_high, cur_low=cur_low,
                      cur_close=price, bb4_reliable=bb4_reliable)

    print(f"[분석] {ticker} | {price} | {direction} | {code}")

    # ── 진입 신호 아니면 종료 ──────────────────
    if code not in ALERT_CODES:
        print(f"[스킵] {code} — 진입 신호 아님")
        return

    # ── 쿨다운 체크 ───────────────────────────
    key = f"{ticker}:{code}"
    last = _last_sent.get(key)
    if last and (now - last).total_seconds() < 3600:
        print(f"[쿨다운] {key} — {int((now-last).total_seconds()//60)}분 경과")
        return

    # ── SMS 텍스트 조합 ───────────────────────
    is_gold = cfg["is_gold"]
    display = cfg["display"]
    ma_txt  = "하방" if direction == "down" else "상방"
    fmt     = lambda v: f"{v:,.0f}" if is_gold else f"{v:,.2f}"
    price_fmt = f"${price:,.1f}" if is_gold else f"${price:,.2f}"

    sl_hi = fmt(cur_high + (15 if is_gold else 2))
    sl_lo = fmt(cur_low  - (15 if is_gold else 2))
    tp_up = fmt(bb20u)
    tp_dn = fmt(bb20l)

    sig_map = {
        "DOUBLE_SHORT":   ("🔥더블비 숏",  f"SL {sl_hi} / TP {tp_dn}", "숏 진입해라"),
        "SINGLE_SHORT":   ("⚡원비 숏",    f"SL {sl_hi} / TP {tp_dn}", "숏 진입해라"),
        "BREAKOUT_SHORT": ("💥돌파 숏",    f"SL {fmt(bb20l+15 if is_gold else bb20l+1)} / TP {tp_dn}", "숏 홀드해라"),
        "REVERSAL_LONG":  ("🔄변곡 롱",    f"SL {sl_lo} / TP {tp_up}", "소량 롱. SMA 재확인"),
        "DOUBLE_LONG":    ("🔥더블비 롱",  f"SL {sl_lo} / TP {tp_up}", "롱 진입해라"),
        "SINGLE_LONG":    ("⚡원비 롱",    f"SL {sl_lo} / TP {tp_up}", "롱 진입해라"),
        "BREAKOUT_LONG":  ("💥돌파 롱",    f"SL {fmt(bb20u-15 if is_gold else bb20u-1)} / TP {tp_up}", "롱 홀드해라"),
        "REVERSAL_SHORT": ("🔄변곡 숏",    f"SL {sl_hi} / TP {tp_dn}", "소량 숏. SMA 재확인"),
    }
    sig_name, sl_tp, cmd = sig_map[code]

    text = (
        f"[{display}] {time_str}\n"
        f"{price_fmt} | {ma_txt}\n"
        f"{sig_name}\n"
        f"{sl_tp}\n"
        f"▶ {cmd}"
    )

    send_sms(text)
    _last_sent[key] = now

# ── 라우트 ───────────────────────────────────────────────
@app.route("/")
@app.route("/ping")
def ping():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    TradingView 알림 메시지 형식:
      {"ticker": "XAUUSD"}   또는
      {"ticker": "QQQ"}

    TradingView 알림 메시지 설정란에 입력할 내용:
      {"ticker": "{{ticker}}"}
    """
    try:
        raw  = request.get_data(as_text=True)
        print(f"[수신] {raw[:200]}")

        try:
            data = json.loads(raw)
        except Exception:
            data = {}

        # ticker 추출 (XAUUSD, QQQ 등)
        ticker = (
            data.get("ticker") or
            data.get("symbol") or
            ""
        ).upper().replace("/", "").replace("_", "")

        # 알려진 심볼로 정규화
        if "XAU" in ticker or "GOLD" in ticker:
            ticker = "XAUUSD"
        elif "QQQ" in ticker or "NAS" in ticker or "NDX" in ticker:
            ticker = "QQQ"

        if ticker:
            analyze_and_sms(ticker)
        else:
            print(f"[심볼 없음] {raw}")

        return "OK", 200

    except Exception as e:
        print(f"[오류] {e}")
        return "ERROR", 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
