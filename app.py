“””
Unified Trading Bot v1.1.0
UPRO Trend Bot (70%) + Momentum Rotation Bot (30%)
“””

import os
import json
import asyncio
import requests
import pandas as pd
import numpy as np
import yfinance as yf
import pytz
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime as dt, timedelta
import warnings
import time

try:
from telegram import Bot
except ImportError:
Bot = None

try:
import google.generativeai as genai
GEMINI_OK = True
except ImportError:
GEMINI_OK = False

warnings.filterwarnings(‘ignore’)

# ==============================================================

# 1. 설정

# ==============================================================

KST = pytz.timezone(‘Asia/Seoul’)

# 자본 배분 (합계 = 1.0)

UPRO_RATIO     = 0.70
ROTATION_RATIO = 0.30

# UPRO 봇

SIGNAL_TICKER = ‘SPY’
TRADE_TICKER  = ‘UPRO’
STATE_FILE    = ‘trend_state.json’
HISTORY_FILE  = ‘history_trend.csv’

# Rotation 봇

CANDIDATE_POOL        = [‘NVDA’, ‘TSLA’, ‘META’, ‘AAPL’, ‘MSFT’, ‘AMZN’, ‘GOOGL’]
TOP_N                 = 2
VIX_ENTER_MAX         = 25.0
SPY_6M_MIN            = 0.0
VIX_EXIT_SPIKE        = 0.30
SPY_DAILY_EXIT        = -0.03
STOCK_HARD_STOP       = -0.07
ROTATION_STATE_FILE   = ‘rotation_state.json’
ROTATION_HISTORY_FILE = ‘history_rotation.csv’

# ==============================================================

# 2. KIS API (검증 규격 유지)

# ==============================================================

class KIS_Trader:
def **init**(self):
self.base_url     = “https://openapi.koreainvestment.com:9443”
self.app_key      = os.getenv(‘KIS_APPKEY’)
self.app_secret   = os.getenv(‘KIS_SECRET’)
self.cano         = os.getenv(‘KIS_CANO’)
self.acnt_prdt_cd = os.getenv(‘KIS_ACNT_PRDT_CD’, ‘01’)
self.token        = None
self.error_detail = “Initial”
self._set_token()

```
def _set_token(self):
    try:
        url  = f"{self.base_url}/oauth2/tokenP"
        data = {"grant_type": "client_credentials",
                "appkey": self.app_key, "appsecret": self.app_secret}
        res      = requests.post(url, headers={"content-type": "application/json"},
                                 data=json.dumps(data))
        res_data = res.json()
        self.token = res_data.get('access_token')
        if not self.token:
            self.error_detail = f"Auth Fail: {res_data.get('msg1')}"
    except Exception as e:
        self.error_detail = f"Conn: {str(e)}"

def _headers(self, tr_id):
    return {
        "Content-Type":  "application/json",
        "authorization": f"Bearer {self.token}",
        "appkey":        self.app_key,
        "appsecret":     self.app_secret,
        "tr_id":         tr_id,
        "custtype":      "P",
    }

def get_total_balance(self):
    if not self.token:
        return 0.0
    try:
        url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                  "OVRS_EXCG_CD": "AMEX", "OVRS_ORD_UNPR": "1",
                  "ITEM_CD": TRADE_TICKER}
        res = requests.get(url, headers=self._headers("JTTT3007R"),
                           params=params).json()
        return float(res.get('output', {}).get('ord_psbl_frcr_amt', 0))
    except:
        return 0.0

def get_holdings_qty(self, ticker):
    if not self.token:
        return 0
    try:
        url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                  "OVRS_EXCG_CD": "AMEX", "TR_CRCY_CD": "USD",
                  "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        res = requests.get(url, headers=self._headers("JTTT3012R"), params=params)
        for item in res.json().get('output1', []):
            if item.get('pdno') == ticker:
                return int(float(item.get('ccld_qty_smtl', 0)))
        return 0
    except:
        return 0

def get_current_price(self, ticker):
    try:
        df = yf.download(ticker, period='1d', interval='1m', progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if not df.empty:
            return float(df['Close'].iloc[-1])
        return 0.0
    except:
        return 0.0

def send_order(self, ticker, qty, side="BUY"):
    if not self.token:
        return {"rt_cd": "1", "msg1": "No Token"}
    try:
        url       = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        tr_id     = "TTTT1002U" if side == "BUY" else "TTTT1006U"
        clean_qty = str(int(float(qty)))
        data = {
            "CANO":            self.cano,
            "ACNT_PRDT_CD":    self.acnt_prdt_cd,
            "OVRS_EXCG_CD":    "AMEX",
            "PDNO":            ticker,
            "ORD_QTY":         clean_qty,
            "OVRS_ORD_UNPR":   "0",
            "ORD_SVR_DVSN_CD": "0",
            "ORD_DVSN":        "00",
        }
        return requests.post(url, headers=self._headers(tr_id),
                             data=json.dumps(data)).json()
    except Exception as e:
        return {"rt_cd": "1", "msg1": str(e)}
```

# ==============================================================

# 3. 시장 데이터

# ==============================================================

def get_market_data():
try:
spy_raw = yf.download(SIGNAL_TICKER, period=‘2y’, progress=False,
auto_adjust=True, repair=True)
if isinstance(spy_raw.columns, pd.MultiIndex):
spy_raw.columns = spy_raw.columns.get_level_values(0)
spy_raw   = spy_raw[[‘Open’, ‘High’, ‘Low’, ‘Close’, ‘Volume’]].copy()
spy_close = spy_raw[‘Close’].squeeze()
monthly   = spy_close.resample(‘ME’).last().pct_change().dropna()

```
    vix_raw = yf.download('^VIX', period='2y', progress=False,
                          auto_adjust=True, repair=True)
    if isinstance(vix_raw.columns, pd.MultiIndex):
        vix_raw.columns = vix_raw.columns.get_level_values(0)
    vix_close = vix_raw['Close'].squeeze()

    close_all = {}
    for t in CANDIDATE_POOL:
        try:
            tmp = yf.download(t, period='2y', progress=False,
                              auto_adjust=True, repair=True)
            if isinstance(tmp.columns, pd.MultiIndex):
                tmp.columns = tmp.columns.get_level_values(0)
            close_all[t] = tmp['Close'].squeeze()
        except:
            pass

    return spy_raw, monthly, vix_close, close_all, "Success"
except Exception as e:
    return pd.DataFrame(), pd.Series(), pd.Series(), {}, str(e)
```

# ==============================================================

# 4. UPRO 봇 신호 (v3.6.9 원본 그대로)

# ==============================================================

def get_upro_signal(spy_close, monthly, vix_close):
if os.path.exists(STATE_FILE):
with open(STATE_FILE, ‘r’) as f:
state = json.load(f)
else:
state = {“in_market”: True, “last_exit_price”: 0}

```
if spy_close.empty or len(spy_close) < 20:
    return "WAIT", "Loading", 0.0, state

curr_p        = float(spy_close.iloc[-1])
spy_daily_ret = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
vix_daily_ret = float((vix_close.iloc[-1] / vix_close.iloc[-2]) - 1)
spy_3day_ret  = float((spy_close.iloc[-1] / spy_close.iloc[-4]) - 1) \
                if len(spy_close) >= 4 else 0.0

if vix_daily_ret >= 0.3 or spy_daily_ret <= -0.03 or spy_3day_ret <= -0.05:
    return "EXIT", "Shock Trigger", curr_p, state

if state.get('in_market', True):
    recent = monthly.tail(2).values
    if len(recent) == 2 and recent[0] < 0 and recent[1] < 0:
        return "EXIT", "2m Down", curr_p, state
    return "KEEP", "Holding", curr_p, state
else:
    rebound = (curr_p - state['last_exit_price']) / state['last_exit_price'] \
              if state['last_exit_price'] > 0 else 0
    vix_now  = float(vix_close.iloc[-1])
    vix_prev = float(vix_close.iloc[-2])
    vix_20d  = vix_close.tail(20)
    vix_mean = float(vix_20d.mean())
    vix_std  = float(vix_20d.std())
    vix_rev  = (
        (vix_now > (vix_mean + 2 * vix_std) or
         vix_prev > (vix_mean + 2 * vix_std)) and
        vix_now < vix_prev * 0.95 and
        spy_daily_ret > 0
    )
    if vix_rev:
        return "RE-ENTER", "VIX Reversal", curr_p, state
    if rebound >= 0.02:
        return "RE-ENTER", "2% Rebound", curr_p, state
    return "WAIT", f"Waiting({rebound*100:.1f}%)", curr_p, state
```

# ==============================================================

# 5. Rotation 봇 신호

# ==============================================================

def load_rotation_state():
default = {“in_market”: False, “holdings”: [],
“entry_date”: None, “consecutive_loss”: 0}
if os.path.exists(ROTATION_STATE_FILE):
try:
with open(ROTATION_STATE_FILE) as f:
saved = json.load(f)
for k, v in default.items():
if k not in saved:
saved[k] = v
return saved
except:
pass
return default

def save_rotation_state(state):
with open(ROTATION_STATE_FILE, ‘w’) as f:
json.dump(state, f, indent=2, ensure_ascii=False)

def calc_momentum(series):
try:
s = series.dropna()
if len(s) < 130:
return -999.0
m1 = float(s.iloc[-1] / s.iloc[-21]  - 1) * 100
m3 = float(s.iloc[-1] / s.iloc[-63]  - 1) * 100
m6 = float(s.iloc[-1] / s.iloc[-126] - 1) * 100
return round(m1 * 0.2 + m3 * 0.3 + m6 * 0.5, 2)
except:
return -999.0

def get_rotation_signal(spy_close, vix_close, close_all, rot_state):
try:
spy_daily = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
spy_6m    = float((spy_close.iloc[-1] / spy_close.iloc[-126]) - 1)   
if len(spy_close) >= 126 else 0.0
vix_now   = float(vix_close.iloc[-1])
vix_prev  = float(vix_close.iloc[-2])
vix_daily = float((vix_now - vix_prev) / vix_prev)

```
    scores = {}
    for t in CANDIDATE_POOL:
        if t in close_all:
            scores[t] = calc_momentum(close_all[t])
    top2 = [t for t, _ in sorted(scores.items(),
                                 key=lambda x: x[1], reverse=True)[:TOP_N]]

    hard_stop = False
    if rot_state['in_market']:
        for h in rot_state['holdings']:
            t = h['ticker']
            if t in close_all and len(close_all[t]) >= 2:
                daily = float((close_all[t].iloc[-1] / close_all[t].iloc[-2]) - 1)
                if daily <= STOCK_HARD_STOP:
                    hard_stop = True

    vix_reversal = False
    if len(vix_close) >= 3:
        vix_2prev = float(vix_close.iloc[-3])
        if (vix_prev > vix_2prev * 1.20 and
                vix_now < vix_prev * 0.92 and
                spy_daily > 0):
            vix_reversal = True

    regime_ok = (spy_6m > SPY_6M_MIN and vix_now < VIX_ENTER_MAX) or vix_reversal
    exit_cond = (vix_daily >= VIX_EXIT_SPIKE or spy_daily <= SPY_DAILY_EXIT
                 or hard_stop or rot_state['consecutive_loss'] >= 2 or not regime_ok)

    current = [h['ticker'] for h in rot_state.get('holdings', [])]
    if rot_state['in_market']:
        if exit_cond:
            action = "EXIT"
        elif set(top2) != set(current):
            action = "ROTATE"
        else:
            action = "KEEP"
    else:
        action = "ENTER" if regime_ok else "WAIT"

    return {"action": action, "top2": top2, "scores": scores,
            "spy_daily": round(spy_daily * 100, 2),
            "spy_6m": round(spy_6m * 100, 2),
            "vix_now": round(vix_now, 2),
            "vix_daily": round(vix_daily * 100, 2),
            "regime_ok": regime_ok, "hard_stop": hard_stop,
            "vix_reversal": vix_reversal}
except Exception as e:
    return {"action": "WAIT", "top2": [], "scores": {}, "error": str(e)}
```

# ==============================================================

# 6. 성과 분석

# ==============================================================

def calc_upro_performance(df):
empty = {“total_return”: 0.0, “win_rate”: 0.0, “mdd”: 0.0, “sharpe”: 0.0,
“total_trades”: 0, “win_trades”: 0, “loss_trades”: 0,
“avg_profit”: 0.0, “avg_loss”: 0.0,
“best_trade”: 0.0, “worst_trade”: 0.0,
“equity_curve”: [], “monthly_ret”: []}
if df is None or df.empty:
return empty

```
df = df.copy()
df['Date']  = pd.to_datetime(df['Date'])
df['Price'] = pd.to_numeric(df['Price'], errors='coerce').fillna(0)
df['Qty']   = pd.to_numeric(df['Qty'],   errors='coerce').fillna(0)

buys  = df[df['Action'] == 'BUY'].reset_index(drop=True)
sells = df[df['Action'] == 'SELL'].reset_index(drop=True)

trades = []
equity = 100.0
equity_curve = []
if not buys.empty:
    equity_curve = [{"date": str(buys.loc[0, 'Date'].date()), "equity": 100.0}]

for i in range(min(len(buys), len(sells))):
    buy_p  = float(buys.loc[i, 'Price'])
    sell_p = float(sells.loc[i, 'Price'])
    if buy_p <= 0:
        continue
    ret = (sell_p - buy_p) / buy_p * 100
    trades.append({"buy_date": str(buys.loc[i, 'Date'].date()),
                    "sell_date": str(sells.loc[i, 'Date'].date()),
                    "return_pct": round(ret, 2), "win": ret > 0})
    equity *= (1 + ret / 100)
    equity_curve.append({"date": str(sells.loc[i, 'Date'].date()),
                          "equity": round(equity, 2)})

if not trades:
    return empty

rets   = [t['return_pct'] for t in trades]
wins   = [r for r in rets if r > 0]
losses = [r for r in rets if r <= 0]

eq_vals = [e['equity'] for e in equity_curve]
peak = eq_vals[0] if eq_vals else 100.0
mdd  = 0.0
for v in eq_vals:
    if v > peak:
        peak = v
    dd = (peak - v) / peak * 100
    if dd > mdd:
        mdd = dd

sharpe = 0.0
if len(rets) > 1:
    arr    = np.array(rets)
    sharpe = round(arr.mean() / (arr.std() + 1e-9) * np.sqrt(12), 2)

# 월별 수익률
sell_df = sells.copy().set_index('Date')
if not sell_df.empty:
    monthly_g = []
    for i2 in range(min(len(buys), len(sells))):
        buy_p2  = float(buys.loc[i2, 'Price'])
        sell_p2 = float(sells.loc[i2, 'Price'])
        if buy_p2 > 0:
            monthly_g.append({"date": sells.loc[i2, 'Date'],
                               "ret": round((sell_p2 - buy_p2) / buy_p2 * 100, 2)})
    monthly_df = pd.DataFrame(monthly_g)
    if not monthly_df.empty:
        monthly_df = monthly_df.set_index('date').resample('ME')['ret'].sum().reset_index()
        monthly_ret = [{"month": str(r['date'])[:7], "ret": round(r['ret'], 2)}
                       for _, r in monthly_df.iterrows()]
    else:
        monthly_ret = []
else:
    monthly_ret = []

return {"total_return":  round(equity - 100, 2),
        "win_rate":      round(len(wins) / len(trades) * 100, 1),
        "mdd":           round(mdd, 2),
        "sharpe":        sharpe,
        "total_trades":  len(trades),
        "win_trades":    len(wins),
        "loss_trades":   len(losses),
        "avg_profit":    round(np.mean(wins),   2) if wins   else 0.0,
        "avg_loss":      round(np.mean(losses), 2) if losses else 0.0,
        "best_trade":    round(max(rets), 2) if rets else 0.0,
        "worst_trade":   round(min(rets), 2) if rets else 0.0,
        "equity_curve":  equity_curve,
        "monthly_ret":   monthly_ret}
```

def calc_rotation_performance(df):
empty = {“total_return”: 0.0, “win_rate”: 0.0, “mdd”: 0.0, “sharpe”: 0.0,
“total_trades”: 0, “win_trades”: 0, “loss_trades”: 0,
“avg_profit”: 0.0, “avg_loss”: 0.0,
“best_trade”: 0.0, “worst_trade”: 0.0,
“equity_curve”: [], “monthly_ret”: []}
if df is None or df.empty:
return empty

```
df = df.copy()
sells = df[df['Action'] == 'SELL'].copy()
if 'RetPct' not in sells.columns or sells.empty:
    return empty

sells['RetPct'] = pd.to_numeric(sells['RetPct'], errors='coerce').fillna(0)
sells['Date']   = pd.to_datetime(sells['Date'])
sells = sells.sort_values('Date').reset_index(drop=True)

rets   = sells['RetPct'].tolist()
wins   = [r for r in rets if r > 0]
losses = [r for r in rets if r <= 0]

equity = 100.0
equity_curve = [{"date": str(sells.loc[0, 'Date'].date()), "equity": 100.0}]
for _, row in sells.iterrows():
    equity *= (1 + row['RetPct'] / 100)
    equity_curve.append({"date": str(row['Date'].date()),
                          "equity": round(equity, 2)})

eq_vals = [e['equity'] for e in equity_curve]
peak = eq_vals[0] if eq_vals else 100.0
mdd  = 0.0
for v in eq_vals:
    if v > peak:
        peak = v
    dd = (peak - v) / peak * 100
    if dd > mdd:
        mdd = dd

sharpe = 0.0
if len(rets) > 1:
    arr    = np.array(rets)
    sharpe = round(arr.mean() / (arr.std() + 1e-9) * np.sqrt(12), 2)

sells_idx  = sells.set_index('Date')
monthly_g  = sells_idx['RetPct'].resample('ME').sum().reset_index()
monthly_ret = [{"month": str(r['Date'])[:7], "ret": round(r['RetPct'], 2)}
               for _, r in monthly_g.iterrows()]

return {"total_return":  round(equity - 100, 2),
        "win_rate":      round(len(wins) / len(rets) * 100, 1) if rets else 0.0,
        "mdd":           round(mdd, 2),
        "sharpe":        sharpe,
        "total_trades":  len(rets),
        "win_trades":    len(wins),
        "loss_trades":   len(losses),
        "avg_profit":    round(np.mean(wins),   2) if wins   else 0.0,
        "avg_loss":      round(np.mean(losses), 2) if losses else 0.0,
        "best_trade":    round(max(rets), 2) if rets else 0.0,
        "worst_trade":   round(min(rets), 2) if rets else 0.0,
        "equity_curve":  equity_curve,
        "monthly_ret":   monthly_ret}
```

# ==============================================================

# 7. Gemini

# ==============================================================

def ask_gemini(upro_signal, rot_signal):
if not GEMINI_OK:
return “google-generativeai not installed”
api_key = os.getenv(“GEMINI_API_KEY”, “”)
if not api_key:
return “GEMINI_API_KEY not set”
try:
genai.configure(api_key=api_key)
model  = genai.GenerativeModel(“gemini-1.5-flash”)
prompt = (
“You are a quant trading expert. Analyze these two strategies in Korean within 200 chars.\n”
f”UPRO Bot (70%): {upro_signal}\n”
f”Rotation Bot (30%): signal={rot_signal.get(‘action’)}, “
f”top2={rot_signal.get(‘top2’)}, “
f”SPY6M={rot_signal.get(‘spy_6m’)}%, VIX={rot_signal.get(‘vix_now’)}\n”
“Answer: 1.Market summary 2.Signal validity 3.Key risk”
)
return model.generate_content(prompt).text.strip()
except Exception as e:
return f”Gemini error: {str(e)[:80]}”

# ==============================================================

# 8. Telegram

# ==============================================================

async def send_telegram(msg):
token   = os.getenv(‘TELEGRAM_TOKEN’, ‘’)
chat_id = os.getenv(‘CHAT_ID’, ‘’)
if not token or not chat_id:
return
try:
if Bot:
bot = Bot(token=token)
await bot.send_message(chat_id=chat_id, text=msg, parse_mode=“HTML”)
else:
requests.post(
f”https://api.telegram.org/bot{token}/sendMessage”,
json={“chat_id”: chat_id, “text”: msg, “parse_mode”: “HTML”},
timeout=10,
)
except Exception as e:
print(f”Telegram error: {e}”)

# ==============================================================

# 9. 자동매매 실행

# ==============================================================

async def run_trading():
now_kst      = dt.now(KST)
current_hour = now_kst.hour
today        = now_kst.strftime(’%Y-%m-%d’)

```
trader    = KIS_Trader()
rot_state = load_rotation_state()

total_bal       = trader.get_total_balance()
upro_budget     = total_bal * UPRO_RATIO
rotation_budget = total_bal * ROTATION_RATIO

spy_ohlc, monthly, vix_close, close_all, data_msg = get_market_data()
if spy_ohlc.empty:
    await send_telegram(f"Data load failed: {data_msg}")
    return

spy_close = spy_ohlc['Close']

# KST 20:00 정규 매매
if current_hour == 20:

    # [A] UPRO 봇
    u_signal, u_reason, u_price, u_state = get_upro_signal(spy_close, monthly, vix_close)
    upro_qty   = trader.get_holdings_qty(TRADE_TICKER)
    upro_price = trader.get_current_price(TRADE_TICKER)
    upro_msg   = ""

    if u_signal in ["KEEP", "RE-ENTER"] and upro_qty == 0 and upro_price > 0:
        buy_qty = int((upro_budget * 0.95) / upro_price)
        if buy_qty >= 1:
            res = trader.send_order(TRADE_TICKER, buy_qty, "BUY")
            if res.get('rt_cd') == '0':
                upro_msg = f"BUY {buy_qty}shares @${upro_price:.2f}"
                u_state['in_market']       = True
                u_state['last_exit_price'] = 0
                with open(STATE_FILE, 'w') as f:
                    json.dump(u_state, f)
                pd.DataFrame([{"Date": today, "Action": "BUY",
                               "Qty": buy_qty, "Price": upro_price,
                               "Strategy": "UPRO"}
                              ]).to_csv(HISTORY_FILE, mode='a',
                                        header=not os.path.exists(HISTORY_FILE),
                                        index=False)
            else:
                upro_msg = f"BUY FAIL: {str(res)[:80]}"
        else:
            upro_msg = f"SKIP (budget ${upro_budget:.0f} too small)"

    elif u_signal == "EXIT" and upro_qty > 0:
        res = trader.send_order(TRADE_TICKER, upro_qty, "SELL")
        if res.get('rt_cd') == '0':
            upro_msg = f"SELL {upro_qty}shares @${upro_price:.2f}"
            u_state['in_market']       = False
            u_state['last_exit_price'] = u_price
            with open(STATE_FILE, 'w') as f:
                json.dump(u_state, f)
            pd.DataFrame([{"Date": today, "Action": "SELL",
                           "Qty": upro_qty, "Price": upro_price,
                           "Strategy": "UPRO"}
                          ]).to_csv(HISTORY_FILE, mode='a',
                                    header=not os.path.exists(HISTORY_FILE),
                                    index=False)
        else:
            upro_msg = f"SELL FAIL: {str(res)[:80]}"
    else:
        upro_msg = f"{u_signal} ({u_reason})"

    # [B] Rotation 봇
    r_signal = get_rotation_signal(spy_close, vix_close, close_all, rot_state)
    r_action = r_signal['action']
    top2     = r_signal['top2']
    rot_msg  = ""

    if r_action == "ENTER" and not rot_state['in_market'] and len(top2) > 0:
        per_stock    = (rotation_budget * 0.95) / len(top2)
        new_holdings = []
        bought       = []
        for ticker in top2:
            price = trader.get_current_price(ticker)
            if price <= 0:
                continue
            qty = int(per_stock / price)
            if qty < 1:
                continue
            res = trader.send_order(ticker, qty, "BUY")
            if res.get('rt_cd') == '0':
                new_holdings.append({"ticker": ticker,
                                     "buy_price": price, "shares": qty})
                bought.append(f"{ticker} {qty}shares@${price:.1f}")
                pd.DataFrame([{"Date": today, "Action": "BUY",
                               "Ticker": ticker, "Qty": qty,
                               "Price": price, "Strategy": "ROTATION",
                               "RetPct": 0}
                              ]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                        header=not os.path.exists(ROTATION_HISTORY_FILE),
                                        index=False)
        if new_holdings:
            rot_state['in_market']  = True
            rot_state['holdings']   = new_holdings
            rot_state['entry_date'] = today
            save_rotation_state(rot_state)
            rot_msg = "ENTER: " + ", ".join(bought)
        else:
            rot_msg = f"ENTER SKIP (budget ${rotation_budget:.0f} insufficient)"

    elif r_action == "EXIT" and rot_state['in_market']:
        sold         = []
        monthly_rets = []
        for h in rot_state['holdings']:
            t   = h['ticker']
            qty = trader.get_holdings_qty(t)
            if qty <= 0:
                continue
            price = trader.get_current_price(t)
            res   = trader.send_order(t, qty, "SELL")
            if res.get('rt_cd') == '0':
                ret = (price - h.get('buy_price', price)) / h.get('buy_price', price) * 100
                monthly_rets.append(ret)
                sold.append(f"{t} {qty}shares({ret:+.1f}%)")
                pd.DataFrame([{"Date": today, "Action": "SELL",
                               "Ticker": t, "Qty": qty,
                               "Price": price, "Strategy": "ROTATION",
                               "RetPct": round(ret, 2)}
                              ]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                        header=not os.path.exists(ROTATION_HISTORY_FILE),
                                        index=False)
        avg_ret = np.mean(monthly_rets) if monthly_rets else 0
        rot_state['consecutive_loss'] = rot_state['consecutive_loss'] + 1 \
                                        if avg_ret < 0 else 0
        rot_state['in_market'] = False
        rot_state['holdings']  = []
        save_rotation_state(rot_state)
        rot_msg = "EXIT: " + ", ".join(sold)

    elif r_action == "ROTATE" and rot_state['in_market']:
        current  = [h['ticker'] for h in rot_state['holdings']]
        to_sell  = [t for t in current if t not in top2]
        to_buy   = [t for t in top2    if t not in current]
        rot_log  = []
        for t in to_sell:
            qty = trader.get_holdings_qty(t)
            if qty > 0:
                price = trader.get_current_price(t)
                res   = trader.send_order(t, qty, "SELL")
                if res.get('rt_cd') == '0':
                    rot_log.append(f"OUT:{t}")
                    pd.DataFrame([{"Date": today, "Action": "SELL",
                                   "Ticker": t, "Qty": qty,
                                   "Price": price, "Strategy": "ROTATION",
                                   "RetPct": 0}
                                  ]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                            header=not os.path.exists(ROTATION_HISTORY_FILE),
                                            index=False)
        time.sleep(2)
        freed        = (rotation_budget * 0.95) / max(len(to_buy), 1)
        new_holdings = [h for h in rot_state['holdings']
                        if h['ticker'] not in to_sell]
        for t in to_buy:
            price = trader.get_current_price(t)
            if price <= 0:
                continue
            qty = int(freed / price)
            if qty < 1:
                continue
            res = trader.send_order(t, qty, "BUY")
            if res.get('rt_cd') == '0':
                new_holdings.append({"ticker": t, "buy_price": price, "shares": qty})
                rot_log.append(f"IN:{t}")
                pd.DataFrame([{"Date": today, "Action": "BUY",
                               "Ticker": t, "Qty": qty,
                               "Price": price, "Strategy": "ROTATION",
                               "RetPct": 0}
                              ]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                        header=not os.path.exists(ROTATION_HISTORY_FILE),
                                        index=False)
        rot_state['holdings'] = new_holdings
        save_rotation_state(rot_state)
        rot_msg = "ROTATE: " + " > ".join(rot_log)
    else:
        rot_msg = f"{r_action} (TOP2: {','.join(top2)})"

    gemini_txt = ask_gemini(f"{u_signal}:{u_reason}", r_signal)

    msg = (
        f"<b>Unified Bot - Trade Report</b> {now_kst.strftime('%m/%d %H:%M')}\n"
        f"Balance: ${total_bal:,.2f} | UPRO: ${upro_budget:,.2f} | ROT: ${rotation_budget:,.2f}\n"
        f"SPY: {r_signal.get('spy_daily', 0):+.2f}% | VIX: {r_signal.get('vix_now', 0):.1f}\n"
        f"UPRO: {upro_msg}\n"
        f"ROT:  {rot_msg}\n"
        f"Gemini: {gemini_txt[:200]}"
    )
    await send_telegram(msg)

# KST 01:00 긴급 탈출
elif current_hour == 1:
    u_signal, u_reason, u_price, u_state = get_upro_signal(spy_close, monthly, vix_close)
    r_signal  = get_rotation_signal(spy_close, vix_close, close_all, rot_state)
    emergency = []

    if u_signal == "EXIT":
        upro_qty = trader.get_holdings_qty(TRADE_TICKER)
        if upro_qty > 0:
            price = trader.get_current_price(TRADE_TICKER)
            res   = trader.send_order(TRADE_TICKER, upro_qty, "SELL")
            if res.get('rt_cd') == '0':
                emergency.append(f"UPRO EMERGENCY SELL {upro_qty}shares@${price:.2f}")
                u_state['in_market']       = False
                u_state['last_exit_price'] = u_price
                with open(STATE_FILE, 'w') as f:
                    json.dump(u_state, f)

    if r_signal['action'] == "EXIT" and rot_state['in_market']:
        for h in rot_state['holdings']:
            t   = h['ticker']
            qty = trader.get_holdings_qty(t)
            if qty > 0:
                price = trader.get_current_price(t)
                res   = trader.send_order(t, qty, "SELL")
                if res.get('rt_cd') == '0':
                    emergency.append(f"ROT EMERGENCY SELL {t} {qty}shares@${price:.2f}")
        rot_state['in_market'] = False
        rot_state['holdings']  = []
        save_rotation_state(rot_state)

    if emergency:
        await send_telegram(
            f"<b>EMERGENCY EXIT</b> {now_kst.strftime('%H:%M')}\n" +
            "\n".join(emergency)
        )

# KST 07:00 아침 리포트
elif current_hour == 7:
    r_signal   = get_rotation_signal(spy_close, vix_close, close_all, rot_state)
    upro_qty   = trader.get_holdings_qty(TRADE_TICKER)
    rot_hold   = ", ".join([h['ticker'] for h in rot_state.get('holdings', [])]) or "CASH"
    gemini_txt = ask_gemini("morning_check", r_signal)
    msg = (
        f"<b>Morning Report</b> {now_kst.strftime('%m/%d %H:%M')}\n"
        f"Balance: ${total_bal:,.2f}\n"
        f"UPRO: {str(upro_qty) + 'shares' if upro_qty > 0 else 'CASH'}\n"
        f"ROT: {rot_hold}\n"
        f"SPY 6M: {r_signal.get('spy_6m', 0):+.1f}% | VIX: {r_signal.get('vix_now', 0):.1f}\n"
        f"TOP2: {', '.join(r_signal.get('top2', []))}\n"
        f"Gemini: {gemini_txt[:200]}"
    )
    await send_telegram(msg)
```

# ==============================================================

# 10. Streamlit 대시보드

# ==============================================================

def run_dashboard():
now_kst = dt.now(KST)
st.set_page_config(page_title=“Unified Bot v1.1”, layout=“wide”, page_icon=“🤖”)
st.markdown(”””
<style>
.main { background: #0a0f1e; }
h1, h2, h3 { color: #e0f0ff !important; }
div[data-testid=“metric-container”] {
background: rgba(0,20,50,0.6);
border: 1px solid #1a3050;
border-radius: 10px;
padding: 8px 12px;
}
</style>
“””, unsafe_allow_html=True)

```
with st.sidebar:
    st.markdown("### Unified Bot v1.1")
    st.caption(f"{now_kst.strftime('%m/%d %H:%M')} KST")
    st.divider()
    st.markdown(f"**자본 배분**\n- UPRO: {int(UPRO_RATIO*100)}%\n- ROT: {int(ROTATION_RATIO*100)}%")
    st.divider()
    st.markdown("**UPRO EXIT**")
    st.caption("VIX+30% / SPY-3% / SPY 3일-5% / 연속2개월하락")
    st.markdown("**ROT EXIT**")
    st.caption("VIX+30% / SPY-3% / 종목-7% 하드스탑 / 레짐이탈")
    st.divider()
    st.markdown("**후보 풀**")
    for t in CANDIDATE_POOL:
        st.caption(f"- {t}")

# 데이터 로드
@st.cache_data(ttl=300)
def load_all():
    return get_market_data()

spy_ohlc, monthly, vix_close, close_all, data_msg = load_all()
rot_state = load_rotation_state()

if spy_ohlc.empty:
    st.error(f"데이터 로드 실패: {data_msg}")
    return

spy_close                             = spy_ohlc['Close']
u_signal, u_reason, u_price, u_state = get_upro_signal(spy_close, monthly, vix_close)
r_signal                              = get_rotation_signal(spy_close, vix_close,
                                                            close_all, rot_state)

df_upro = pd.read_csv(HISTORY_FILE) \
          if os.path.exists(HISTORY_FILE) else pd.DataFrame()
df_rot  = pd.read_csv(ROTATION_HISTORY_FILE) \
          if os.path.exists(ROTATION_HISTORY_FILE) else pd.DataFrame()

upro_perf = calc_upro_performance(df_upro)
rot_perf  = calc_rotation_performance(df_rot)

tab1, tab2, tab3 = st.tabs(["실시간 현황", "성과 분석", "거래 로그"])

# ----------------------------------------------------------
# TAB 1: 실시간 현황
# ----------------------------------------------------------
with tab1:
    st.title("Unified Trading Bot")
    st.caption(
        f"UPRO {int(UPRO_RATIO*100)}% + Rotation {int(ROTATION_RATIO*100)}%"
        f"  |  {now_kst.strftime('%Y-%m-%d %H:%M')} KST"
    )
    st.divider()

    trader    = KIS_Trader()
    total_bal = trader.get_total_balance()
    ca, cb, cc = st.columns(3)
    ca.metric("총 주문가능금액",    f"${total_bal:,.2f}")
    cb.metric("UPRO 예산 (70%)", f"${total_bal * UPRO_RATIO:,.2f}")
    cc.metric("ROT 예산 (30%)",  f"${total_bal * ROTATION_RATIO:,.2f}")
    st.divider()

    col_u, col_r = st.columns(2)

    with col_u:
        st.subheader("UPRO 트렌드 봇")
        m1, m2, m3 = st.columns(3)
        m1.metric("포지션", "IN" if u_state.get('in_market') else "OUT")
        m2.metric("신호",   u_signal)
        m3.metric("SPY",    f"${u_price:.2f}")
        if u_signal == "KEEP":
            st.success(f"KEEP - {u_reason}")
        elif u_signal == "EXIT":
            st.error(f"EXIT - {u_reason}")
        elif u_signal == "RE-ENTER":
            st.success(f"RE-ENTER - {u_reason}")
        else:
            st.info(f"WAIT - {u_reason}")

        fig_spy = go.Figure()
        fig_spy.add_trace(go.Scatter(
            x=spy_ohlc['Close'].tail(63).index,
            y=spy_ohlc['Close'].tail(63).values,
            line=dict(color="#38bdf8", width=2),
            fill='tozeroy', fillcolor='rgba(56,189,248,0.05)',
        ))
        fig_spy.update_layout(template='plotly_dark', height=180,
                              margin=dict(l=5, r=5, t=5, b=5),
                              paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
                              showlegend=False)
        st.plotly_chart(fig_spy, use_container_width=True)

    with col_r:
        st.subheader("모멘텀 로테이션 봇")
        r1, r2, r3 = st.columns(3)
        r1.metric("포지션", "IN" if rot_state['in_market'] else "OUT")
        r2.metric("신호",   r_signal['action'])
        r3.metric("VIX",    f"{r_signal.get('vix_now', 0):.1f}")

        r_action = r_signal['action']
        if r_action == "KEEP":
            st.success(f"KEEP - {', '.join([h['ticker'] for h in rot_state['holdings']])}")
        elif r_action == "EXIT":
            st.error("EXIT - 탈출 신호")
        elif r_action == "ENTER":
            st.success(f"ENTER - {', '.join(r_signal.get('top2', []))}")
        elif r_action == "ROTATE":
            st.warning(f"ROTATE - {', '.join(r_signal.get('top2', []))}")
        else:
            st.info(f"WAIT - SPY 6M: {r_signal.get('spy_6m', 0):+.1f}%")

        scores = r_signal.get('scores', {})
        if scores:
            top2_set = set(r_signal.get('top2', []))
            df_sc    = pd.DataFrame(
                sorted(scores.items(), key=lambda x: x[1], reverse=True),
                columns=['ticker', 'score']
            )
            fig_sc = go.Figure(go.Bar(
                x=df_sc['ticker'], y=df_sc['score'],
                marker_color=["#fbbf24" if t in top2_set else "#2a4060"
                              for t in df_sc['ticker']],
                text=[f"{v:.1f}" for v in df_sc['score']],
                textposition='outside',
            ))
            fig_sc.update_layout(template='plotly_dark', height=180,
                                 margin=dict(l=5, r=5, t=5, b=5),
                                 paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
                                 showlegend=False)
            st.plotly_chart(fig_sc, use_container_width=True)

    st.divider()

    fig2 = make_subplots(rows=2, cols=1, row_heights=[0.6, 0.4],
                         shared_xaxes=True, vertical_spacing=0.05)
    fig2.add_trace(go.Candlestick(
        x=spy_ohlc.tail(126).index,
        open=spy_ohlc.tail(126)['Open'], high=spy_ohlc.tail(126)['High'],
        low=spy_ohlc.tail(126)['Low'],   close=spy_ohlc.tail(126)['Close'],
        name='SPY',
    ), row=1, col=1)
    fig2.add_trace(go.Bar(
        x=vix_close.tail(126).index, y=vix_close.tail(126).values,
        name='VIX',
        marker_color=['#f87171' if v > 25 else '#f59e0b' if v > 20 else '#34d399'
                      for v in vix_close.tail(126).values],
    ), row=2, col=1)
    fig2.update_layout(template='plotly_dark', height=450,
                       margin=dict(l=10, r=10, t=10, b=10),
                       paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
                       xaxis_rangeslider_visible=False)
    st.plotly_chart(fig2, use_container_width=True)

    st.divider()
    st.subheader("Gemini AI 분석")
    if st.button("분석 요청", type="secondary"):
        with st.spinner("Gemini 분석 중..."):
            st.info(ask_gemini(f"{u_signal}:{u_reason}", r_signal))

    with st.expander("Debug - 데이터 엔진 상태", expanded=False):
        st.write(f"데이터 상태: {data_msg}")
        st.write(f"SPY Shape: {spy_ohlc.shape} | Columns: {spy_ohlc.columns.tolist()}")
        st.write(f"Rotation State: {rot_state}")
        if not spy_ohlc.empty:
            st.dataframe(spy_ohlc.tail(3))

# ----------------------------------------------------------
# TAB 2: 성과 분석
# ----------------------------------------------------------
with tab2:
    st.subheader("실제 성과 분석")

    has_upro = upro_perf['total_trades'] > 0
    has_rot  = rot_perf['total_trades'] > 0

    if not has_upro and not has_rot:
        st.info("아직 거래 기록이 없습니다. 첫 거래 후 성과가 표시됩니다.")
        st.caption("history_trend.csv / history_rotation.csv 생성 후 자동 집계됩니다.")
    else:
        # KPI 카드
        st.markdown("#### 전략별 핵심 지표")
        for label, perf in [("UPRO 트렌드 봇", upro_perf),
                             ("모멘텀 로테이션 봇", rot_perf)]:
            st.markdown(f"**{label}**")
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("총 수익률",  f"{perf['total_return']:+.1f}%")
            k2.metric("MDD",       f"-{perf['mdd']:.1f}%")
            k3.metric("승률",      f"{perf['win_rate']:.0f}%")
            k4.metric("샤프비율",   f"{perf['sharpe']:.2f}")
            k5.metric("총 거래",    f"{perf['total_trades']}회")

            k6, k7, k8, k9, k10 = st.columns(5)
            k6.metric("승/패",     f"{perf['win_trades']}/{perf['loss_trades']}")
            k7.metric("평균 수익", f"{perf['avg_profit']:+.1f}%")
            k8.metric("평균 손실", f"{perf['avg_loss']:+.1f}%")
            k9.metric("최고 거래", f"{perf['best_trade']:+.1f}%")
            k10.metric("최악 거래",f"{perf['worst_trade']:+.1f}%")
            st.divider()

        # 누적 수익률 곡선
        st.markdown("#### 누적 수익률 곡선")
        fig_eq = go.Figure()

        if has_upro and upro_perf['equity_curve']:
            dates_u = [e['date']   for e in upro_perf['equity_curve']]
            vals_u  = [e['equity'] for e in upro_perf['equity_curve']]
            fig_eq.add_trace(go.Scatter(x=dates_u, y=vals_u,
                                        name="UPRO 봇",
                                        line=dict(color="#38bdf8", width=2.5)))

        if has_rot and rot_perf['equity_curve']:
            dates_r = [e['date']   for e in rot_perf['equity_curve']]
            vals_r  = [e['equity'] for e in rot_perf['equity_curve']]
            fig_eq.add_trace(go.Scatter(x=dates_r, y=vals_r,
                                        name="Rotation 봇",
                                        line=dict(color="#fbbf24", width=2.5)))

        # SPY B&H 비교
        if not df_upro.empty:
            try:
                start_d = pd.to_datetime(df_upro['Date'].min()).strftime('%Y-%m-%d')
                spy_bh  = yf.download(SIGNAL_TICKER, start=start_d,
                                      progress=False, auto_adjust=True)
                if isinstance(spy_bh.columns, pd.MultiIndex):
                    spy_bh.columns = spy_bh.columns.get_level_values(0)
                if not spy_bh.empty:
                    spy_norm = spy_bh['Close'] / spy_bh['Close'].iloc[0] * 100
                    fig_eq.add_trace(go.Scatter(
                        x=spy_norm.index.astype(str), y=spy_norm.values,
                        name="SPY B&H",
                        line=dict(color="#64748b", width=1.5, dash='dash')))
            except:
                pass

        fig_eq.update_layout(template='plotly_dark', height=350,
                              margin=dict(l=10, r=10, t=10, b=10),
                              paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
                              yaxis_title="누적 수익 (시작=100)",
                              legend=dict(orientation='h', y=1.05))
        st.plotly_chart(fig_eq, use_container_width=True)

        # 월별 수익률 히트맵
        st.markdown("#### 월별 수익률 히트맵")
        for label, perf in [("UPRO 봇", upro_perf), ("Rotation 봇", rot_perf)]:
            if not perf['monthly_ret']:
                continue
            st.caption(label)
            mdf = pd.DataFrame(perf['monthly_ret'])
            mdf['year']  = mdf['month'].str[:4]
            mdf['mon']   = mdf['month'].str[5:7]
            pivot = mdf.pivot(index='year', columns='mon', values='ret').fillna(0)
            mon_labels = ['Jan','Feb','Mar','Apr','May','Jun',
                          'Jul','Aug','Sep','Oct','Nov','Dec']
            col_map = {str(i+1).zfill(2): mon_labels[i] for i in range(12)}
            pivot.columns = [col_map.get(c, c) for c in pivot.columns]

            fig_hm = go.Figure(go.Heatmap(
                z=pivot.values,
                x=pivot.columns.tolist(),
                y=pivot.index.tolist(),
                colorscale=[
                    [0.0, '#7f1d1d'], [0.4, '#f87171'],
                    [0.5, '#1e293b'],
                    [0.6, '#34d399'], [1.0, '#064e3b']
                ],
                zmid=0,
                text=[[f"{v:+.1f}%" for v in row] for row in pivot.values],
                texttemplate="%{text}",
                showscale=True,
            ))
            fig_hm.update_layout(template='plotly_dark', height=180,
                                 margin=dict(l=10, r=10, t=10, b=10),
                                 paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e')
            st.plotly_chart(fig_hm, use_container_width=True)

# ----------------------------------------------------------
# TAB 3: 거래 로그
# ----------------------------------------------------------
with tab3:
    st.subheader("거래 로그")
    log_col1, log_col2 = st.columns(2)

    with log_col1:
        st.caption("UPRO 봇 (history_trend.csv)")
        if not df_upro.empty:
            df_show = df_upro.tail(20).iloc[::-1].copy()
            st.dataframe(df_show, use_container_width=True, hide_index=True)
            csv_u = df_upro.to_csv(index=False).encode('utf-8')
            st.download_button("CSV 다운로드 (UPRO)", csv_u,
                               file_name="history_trend.csv",
                               mime="text/csv")
        else:
            st.info("거래 없음")

    with log_col2:
        st.caption("Rotation 봇 (history_rotation.csv)")
        if not df_rot.empty:
            df_show_r = df_rot.tail(20).iloc[::-1].copy()
            st.dataframe(df_show_r, use_container_width=True, hide_index=True)
            csv_r = df_rot.to_csv(index=False).encode('utf-8')
            st.download_button("CSV 다운로드 (Rotation)", csv_r,
                               file_name="history_rotation.csv",
                               mime="text/csv")
        else:
            st.info("거래 없음")
```

# ==============================================================

# 11. 진입점

# ==============================================================

if os.getenv(‘GITHUB_ACTIONS’) == ‘true’:
asyncio.run(run_trading())
else:
run_dashboard()
