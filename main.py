import os, hmac, hashlib, time, json, math, requests
from flask import Flask, request
from datetime import datetime, timezone, timedelta
from tradingview_ta import TA_Handler, Interval as TVInterval

app = Flask(__name__)

SOLAPI_KEY    = "NCSRKRGVDS8CL5PP"
SOLAPI_SECRET = "N32HBR7QSZUXE8SYYMKJWIXGWPORJ4G7"
FROM_NUMBER   = "01073030150"
TO_NUMBER     = "01027460150"
KST           = timezone(timedelta(hours=9))

TV_SYMBOLS = {
    "XAUUSD": {"symbol":"XAUUSD","screener":"cfd",     "exchange":"OANDA",  "display":"골드",     "is_gold":True},
    "NAS100": {"symbol":"NDX",   "screener":"america", "exchange":"NASDAQ", "display":"나스닥100", "is_gold":False},
}
ALERT_CODES = {"DOUBLE_SHORT","SINGLE_SHORT","BREAKOUT_SHORT","REVERSAL_LONG",
               "DOUBLE_LONG","SINGLE_LONG","BREAKOUT_LONG","REVERSAL_SHORT"}
_last_sent = {}

def send_sms(text):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = str(int(time.time()*1000))
    sig  = hmac.new(SOLAPI_SECRET.encode(), (date+salt).encode(), hashlib.sha256).hexdigest()
    headers = {"Authorization":f"HMAC-SHA256 apiKey={SOLAPI_KEY}, date={date}, salt={salt}, signature={sig}",
               "Content-Type":"application/json"}
    body = {"message":{"to":TO_NUMBER,"from":FROM_NUMBER,"text":text}}
    res = requests.post("https://api.solapi.com/messages/v4/send", headers=headers, json=body, timeout=10)
    print("SMS:", res.status_code, text[:40])

def get_signal(price, bb20u, bb20l, bb4u, bb4l, direction, margin=4,
               cur_high=None, cur_low=None, bb4_reliable=True):
    if direction == "neutral": return "NEUTRAL"
    h = cur_high or price; l = cur_low or price; mid = (h+l)/2
    u20 = h >= bb20u-margin; u4 = (h >= bb4u-margin) if bb4_reliable else False
    d20 = l <= bb20l+margin; d4 = (l <= bb4l+margin) if bb4_reliable else False
    if direction == "down":
        if price <= bb20l:          return "BREAKOUT_SHORT"
        if u20 and u4:              return "DOUBLE_SHORT"
        if u20 or u4:               return "SINGLE_SHORT"
        if (d20 or d4) and price >= mid: return "REVERSAL_LONG"
        return "WAIT_SHORT"
    else:
        if price >= bb20u:          return "BREAKOUT_LONG"
        if d20 and d4:              return "DOUBLE_LONG"
        if d20 or d4:               return "SINGLE_LONG"
        if (u20 or u4) and price <= mid: return "REVERSAL_SHORT"
        return "WAIT_LONG"

def analyze_and_sms(ticker):
    cfg = TV_SYMBOLS.get(ticker)
    if not cfg: return
    now = datetime.now(KST)
    time_str = now.strftime("%m/%d %H:%M KST")
    try:
        ind = TA_Handler(symbol=cfg["symbol"], screener=cfg["screener"],
                         exchange=cfg["exchange"],
                         interval=TVInterval.INTERVAL_1_HOUR).get_analysis().indicators
    except Exception as e:
        print("TV error:", e); return

    price    = float(ind["close"])
    cur_high = float(ind["high"])
    cur_low  = float(ind["low"])
    bb20u    = round(float(ind["BB.upper"]),1)
    bb20l    = round(float(ind["BB.lower"]),1)
    sma20    = float(ind["SMA20"])

    gap = price - sma20; half = (bb20u-bb20l)/4
    direction = "down" if gap < -half else "up" if gap > half else "neutral"

    span = (bb20u-bb20l)*0.7
    bb4u = round(float(ind["open"])+span/2, 1)
    bb4l = round(float(ind["open"])-span/2, 1)

    code = get_signal(price, bb20u, bb20l, bb4u, bb4l, direction,
                      cur_high=cur_high, cur_low=cur_low)
    print(f"[{ticker}] {price} | {direction} | {code}")

    if code not in ALERT_CODES: return

    key = f"{ticker}:{code}"
    last = _last_sent.get(key)
    if last and (now-last).total_seconds() < 3600: return

    is_gold = cfg["is_gold"]
    display = cfg["display"]
    ma_txt  = "하방" if direction=="down" else "상방"
    fmt     = lambda v: f"{v:,.0f}"   # 골드/나스닥100 모두 정수 표시
    pfmt    = f"${price:,.1f}" if is_gold else f"{price:,.0f}pt"
    sl_hi = fmt(cur_high+(15 if is_gold else 80))
    sl_lo = fmt(cur_low -(15 if is_gold else 80))

    sig_map = {
        "DOUBLE_SHORT":   (f"SL {sl_hi} / TP {fmt(bb20l)}", "숏 진입해라",    "🔥더블비 숏"),
        "SINGLE_SHORT":   (f"SL {sl_hi} / TP {fmt(bb20l)}", "숏 진입해라",    "⚡원비 숏"),
        "BREAKOUT_SHORT": (f"SL {fmt(bb20l+(15 if is_gold else 1))} / TP {fmt(bb20l)}", "숏 홀드해라", "💥돌파 숏"),
        "REVERSAL_LONG":  (f"SL {sl_lo} / TP {fmt(bb20u)}", "소량 롱. SMA 재확인", "🔄변곡 롱"),
        "DOUBLE_LONG":    (f"SL {sl_lo} / TP {fmt(bb20u)}", "롱 진입해라",    "🔥더블비 롱"),
        "SINGLE_LONG":    (f"SL {sl_lo} / TP {fmt(bb20u)}", "롱 진입해라",    "⚡원비 롱"),
        "BREAKOUT_LONG":  (f"SL {fmt(bb20u-(15 if is_gold else 1))} / TP {fmt(bb20u)}", "롱 홀드해라", "💥돌파 롱"),
        "REVERSAL_SHORT": (f"SL {sl_hi} / TP {fmt(bb20l)}", "소량 숏. SMA 재확인", "🔄변곡 숏"),
    }
    sl_tp, cmd, sig_name = sig_map[code]
    text = f"[{display}] {time_str}\n{pfmt} | {ma_txt}\n{sig_name}\n{sl_tp}\n▶ {cmd}"
    send_sms(text)
    _last_sent[key] = now

@app.route("/")
@app.route("/ping")
def ping():
    return "OK", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.get_data(as_text=True) or "{}")
        raw_ticker = (data.get("ticker") or data.get("symbol") or "").upper()
        ticker = "XAUUSD" if "XAU" in raw_ticker or "GOLD" in raw_ticker else \
                 "NAS100" if any(x in raw_ticker for x in ["NAS","NDX","QQQ","US100","USTEC"]) else raw_ticker
        print("수신:", ticker)
        if ticker: analyze_and_sms(ticker)
        return "OK", 200
    except Exception as e:
        print("오류:", e); return "ERROR", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",8080)))
