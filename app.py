“””
Unified Trading Bot v1.2.1
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

# [FIX] Gemini import 수정 (google.generativeai 가 올바른 방법)

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

UPRO_RATIO     = 0.70
ROTATION_RATIO = 0.30

SIGNAL_TICKER = ‘SPY’
TRADE_TICKER  = ‘UPRO’
STATE_FILE    = ‘trend_state.json’
HISTORY_FILE  = ‘history_trend.csv’

CANDIDATE_POOL        = [‘NVDA’, ‘TSLA’, ‘META’, ‘AAPL’, ‘MSFT’, ‘AMZN’, ‘GOOGL’]
TOP_N                 = 2
VIX_ENTER_MAX         = 25.0
SPY_6M_MIN            = 0.0
ROTATION_STATE_FILE   = ‘rotation_state.json’
ROTATION_HISTORY_FILE = ‘history_rotation.csv’

# ==============================================================

# 2. KIS API

# [FIX] send_order: 검증된 시장가 규격 복원 (OVRS_ORD_UNPR=“0”)

# price 인자 제거 - 한투 해외주식 시장가는 “0” 고정

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

def get_balance(self):
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

def get_holdings(self, ticker=TRADE_TICKER):
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

def get_current_price(self, ticker=TRADE_TICKER):
    try:
        df = yf.download(ticker, period='1d', interval='1m', progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if not df.empty:
            return float(df['Close'].iloc[-1])
        return 0.0
    except:
        return 0.0

# [FIX] price 인자 제거, 시장가 규격 복원
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

# 4. UPRO 봇 신호

# ==============================================================

def get_upro_signal(spy_close, monthly, vix_close):
state = {“in_market”: True, “last_exit_price”: 0}
if os.path.exists(STATE_FILE):
try:
with open(STATE_FILE, ‘r’) as f:
content = f.read().strip()
if content:
state = json.loads(content)
except:
pass

```
if spy_close.empty or len(spy_close) < 20:
    return "WAIT", "Loading", 0.0, state

curr_p    = float(spy_close.iloc[-1])
spy_daily = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
vix_daily = float((vix_close.iloc[-1] / vix_close.iloc[-2]) - 1)
spy_3day  = float((spy_close.iloc[-1] / spy_close.iloc[-4]) - 1) \
            if len(spy_close) >= 4 else 0.0

if vix_daily >= 0.3 or spy_daily <= -0.03 or spy_3day <= -0.05:
    return "EXIT", "Shock Trigger", curr_p, state

if state.get('in_market', True):
    recent = monthly.tail(2).values
    if len(recent) == 2 and recent[0] < 0 and recent[1] < 0:
        return "EXIT", "2m Down", curr_p, state
    return "KEEP", "Holding", curr_p, state

rebound  = (curr_p - state['last_exit_price']) / state['last_exit_price'] \
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
    spy_daily > 0
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
spy_6m    = float(spy_close.iloc[-1] / spy_close.iloc[-126] - 1)   
if len(spy_close) >= 126 else 0.0
vix_now   = float(vix_close.iloc[-1])
vix_prev  = float(vix_close.iloc[-2])

```
    scores = {t: calc_momentum(close_all[t])
              for t in CANDIDATE_POOL if t in close_all}
    top2   = [t for t, _ in sorted(scores.items(),
                                   key=lambda x: x[1], reverse=True)[:TOP_N]]

    regime_ok = (spy_6m > SPY_6M_MIN and vix_now < VIX_ENTER_MAX)

    if rot_state['in_market']:
        current = set(h['ticker'] for h in rot_state['holdings'])
        if not regime_ok:
            action = "EXIT"
        elif set(top2) != current:
            action = "ROTATE"
        else:
            action = "KEEP"
    else:
        action = "ENTER" if regime_ok else "WAIT"

    return {"action": action, "top2": top2, "scores": scores,
            "vix_now": vix_now, "spy_6m": spy_6m,
            "regime_ok": regime_ok, "spy_daily": spy_daily}
except:
    return {"action": "WAIT", "top2": [], "vix_now": 0,
            "spy_6m": 0, "regime_ok": False, "spy_daily": 0, "scores": {}}
```

# ==============================================================

# 6. 성과 분석

# ==============================================================

def calc_upro_performance(df):
empty = {“total_return”: 0.0, “win_rate”: 0.0, “mdd”: 0.0, “sharpe”: 0.0,
“total_trades”: 0, “win_trades”: 0, “loss_trades”: 0,
“avg_profit”: 0.0, “avg_loss”: 0.0,
“best_trade”: 0.0, “worst_trade”: 0.0, “equity_curve”: []}
if df is None or df.empty:
return empty
df = df.copy()
df[‘Date’]  = pd.to_datetime(df[‘Date’])
df[‘Price’] = pd.to_numeric(df[‘Price’], errors=‘coerce’).fillna(0)
buys  = df[df[‘Action’] == ‘BUY’].reset_index(drop=True)
sells = df[df[‘Action’] == ‘SELL’].reset_index(drop=True)
trades, equity = [], 100.0
equity_curve = ([{“date”: str(buys.loc[0, ‘Date’].date()), “equity”: 100.0}]
if not buys.empty else [])
for i in range(min(len(buys), len(sells))):
buy_p  = float(buys.loc[i, ‘Price’])
sell_p = float(sells.loc[i, ‘Price’])
if buy_p <= 0:
continue
ret = (sell_p - buy_p) / buy_p * 100
trades.append(ret)
equity *= (1 + ret / 100)
equity_curve.append({“date”: str(sells.loc[i, ‘Date’].date()),
“equity”: round(equity, 2)})
if not trades:
return empty
wins   = [r for r in trades if r > 0]
losses = [r for r in trades if r <= 0]
eq_vals = [e[‘equity’] for e in equity_curve]
peak, mdd = eq_vals[0], 0.0
for v in eq_vals:
if v > peak:
peak = v
dd = (peak - v) / peak * 100
if dd > mdd:
mdd = dd
sharpe = round(np.mean(trades) / (np.std(trades) + 1e-9) * np.sqrt(12), 2)   
if len(trades) > 1 else 0.0
return {“total_return”:  round(equity - 100, 2),
“win_rate”:      round(len(wins) / len(trades) * 100, 1),
“mdd”:           round(mdd, 2), “sharpe”: sharpe,
“total_trades”:  len(trades), “win_trades”: len(wins),
“loss_trades”:   len(losses),
“avg_profit”:    round(np.mean(wins),   2) if wins   else 0.0,
“avg_loss”:      round(np.mean(losses), 2) if losses else 0.0,
“best_trade”:    round(max(trades), 2) if trades else 0.0,
“worst_trade”:   round(min(trades), 2) if trades else 0.0,
“equity_curve”:  equity_curve}

def calc_rotation_performance(df):
empty = {“total_return”: 0.0, “win_rate”: 0.0, “mdd”: 0.0, “sharpe”: 0.0,
“total_trades”: 0, “win_trades”: 0, “loss_trades”: 0,
“avg_profit”: 0.0, “avg_loss”: 0.0,
“best_trade”: 0.0, “worst_trade”: 0.0, “equity_curve”: []}
if df is None or df.empty or ‘RetPct’ not in df.columns:
return empty
sells = df[df[‘Action’] == ‘SELL’].copy()
if sells.empty:
return empty
sells[‘RetPct’] = pd.to_numeric(sells[‘RetPct’], errors=‘coerce’).fillna(0)
sells[‘Date’]   = pd.to_datetime(sells[‘Date’])
sells = sells.sort_values(‘Date’).reset_index(drop=True)
rets, equity = sells[‘RetPct’].tolist(), 100.0
equity_curve = [{“date”: str(sells.loc[0, ‘Date’].date()), “equity”: 100.0}]
for _, row in sells.iterrows():
equity *= (1 + row[‘RetPct’] / 100)
equity_curve.append({“date”: str(row[‘Date’].date()),
“equity”: round(equity, 2)})
wins   = [r for r in rets if r > 0]
losses = [r for r in rets if r <= 0]
eq_vals = [e[‘equity’] for e in equity_curve]
peak, mdd = eq_vals[0], 0.0
for v in eq_vals:
if v > peak:
peak = v
dd = (peak - v) / peak * 100
if dd > mdd:
mdd = dd
sharpe = round(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(12), 2)   
if len(rets) > 1 else 0.0
return {“total_return”:  round(equity - 100, 2),
“win_rate”:      round(len(wins) / len(rets) * 100, 1) if rets else 0.0,
“mdd”:           round(mdd, 2), “sharpe”: sharpe,
“total_trades”:  len(rets), “win_trades”: len(wins),
“loss_trades”:   len(losses),
“avg_profit”:    round(np.mean(wins),   2) if wins   else 0.0,
“avg_loss”:      round(np.mean(losses), 2) if losses else 0.0,
“best_trade”:    round(max(rets), 2) if rets else 0.0,
“worst_trade”:   round(min(rets), 2) if rets else 0.0,
“equity_curve”:  equity_curve}

# ==============================================================

# 7. Gemini

# [FIX] google.generativeai 표준 방식

# ==============================================================

def ask_gemini(u_sig, r_sig):
if not GEMINI_OK:
return “Gemini SDK 미설치”
api_key = os.getenv(“GEMINI_API_KEY”, “”)
if not api_key:
return “GEMINI_API_KEY 미설정”
try:
genai.configure(api_key=api_key)
model  = genai.GenerativeModel(“gemini-1.5-flash”)
prompt = (
f”UPRO {u_sig}, ROT {r_sig.get(‘action’)}, “
f”TOP2 {r_sig.get(‘top2’)}, VIX {r_sig.get(‘vix_now’)}. “
“Korean 150 chars: 1.시장평가 2.리스크”
)
return model.generate_content(prompt).text.strip()
except Exception as e:
return f”Gemini 오류: {str(e)[:60]}”

# ==============================================================

# 8. 자동매매

# [FIX] return 들여쓰기 수정 / send_order price 인자 제거 /

# 텔레그램 전송 위치 및 try-except 정상화

# ==============================================================

async def run_trading():
now_kst      = dt.now(KST)
current_hour = now_kst.hour

```
trader  = KIS_Trader()
token_v = os.getenv('TELEGRAM_TOKEN')
chat_id = os.getenv('CHAT_ID')
bot     = Bot(token=token_v) if (Bot and token_v) else None

print(f"[DEBUG] hour={current_hour} | token={bool(token_v)} | chat_id={bool(chat_id)}")
print(f"[DEBUG] KIS token={bool(trader.token)} | bal={trader.get_balance()}")

# [FIX] 데이터 로드 실패 처리 - return 위치 수정
spy_ohlc, monthly, vix_close, close_all, d_msg = get_market_data()
if spy_ohlc.empty:
    err = f"[ERROR] 데이터 로드 실패: {d_msg}"
    print(err)
    if bot:
        await bot.send_message(chat_id=chat_id, text=err)
    return  # 올바른 위치

rot_state = load_rotation_state()
spy_close = spy_ohlc['Close']
u_sig, u_re, u_p, u_st = get_upro_signal(spy_close, monthly, vix_close)
r_sig = get_rotation_signal(spy_close, vix_close, close_all, rot_state)

# KST 20:00 정규 매매
if current_hour == 20:
    bal         = trader.get_balance()
    upro_budget = bal * UPRO_RATIO
    rot_budget  = bal * ROTATION_RATIO
    msgs = [
        f"<b>통합봇 정규매매 [{now_kst.strftime('%m/%d %H:%M')} KST]</b>",
        f"잔고: ${bal:,.2f} | UPRO: ${upro_budget:,.2f} | ROT: ${rot_budget:,.2f}",
    ]

    # [A] UPRO 봇
    cur_p_upro = trader.get_current_price(TRADE_TICKER)
    qty_upro   = trader.get_holdings(TRADE_TICKER)

    if u_sig in ["KEEP", "RE-ENTER"] and qty_upro == 0 and cur_p_upro > 0:
        buy_qty = int((upro_budget * 0.95) / cur_p_upro)
        if buy_qty >= 1:
            res = trader.send_order(TRADE_TICKER, buy_qty, "BUY")
            if res.get('rt_cd') == '0':
                msgs.append(f"UPRO 매수: {buy_qty}주 @ ${cur_p_upro:.2f}")
                with open(STATE_FILE, 'w') as f:
                    json.dump({"in_market": True, "last_exit_price": 0}, f)
                pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                               "Action": "BUY", "Qty": buy_qty,
                               "Price": cur_p_upro}
                              ]).to_csv(HISTORY_FILE, mode='a',
                                        header=not os.path.exists(HISTORY_FILE),
                                        index=False)
            else:
                msgs.append(f"UPRO 매수실패: {str(res)[:120]}")
        else:
            msgs.append(f"UPRO SKIP (잔고 ${upro_budget:.0f} 부족)")
    elif u_sig == "EXIT" and qty_upro > 0:
        res = trader.send_order(TRADE_TICKER, qty_upro, "SELL")
        if res.get('rt_cd') == '0':
            msgs.append(f"UPRO 매도: {qty_upro}주 @ ${cur_p_upro:.2f}")
            with open(STATE_FILE, 'w') as f:
                json.dump({"in_market": False, "last_exit_price": u_p}, f)
            pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                           "Action": "SELL", "Qty": qty_upro,
                           "Price": cur_p_upro}
                          ]).to_csv(HISTORY_FILE, mode='a',
                                    header=not os.path.exists(HISTORY_FILE),
                                    index=False)
        else:
            msgs.append(f"UPRO 매도실패: {str(res)[:120]}")
    else:
        msgs.append(f"UPRO: {u_sig} ({u_re})")

    # [B] Rotation 봇
    action = r_sig['action']
    top2   = r_sig['top2']

    if action in ["ENTER", "ROTATE"]:
        if rot_state['in_market']:
            for h in rot_state['holdings']:
                qty_h   = trader.get_holdings(h['ticker'])
                cur_p_h = trader.get_current_price(h['ticker'])
                if qty_h > 0:
                    res     = trader.send_order(h['ticker'], qty_h, "SELL")
                    ret_pct = (cur_p_h - h.get('entry_price', cur_p_h)) / \
                              max(h.get('entry_price', cur_p_h), 1) * 100
                    ok = "OK" if res.get('rt_cd') == '0' else "FAIL"
                    msgs.append(f"ROT 매도 {h['ticker']}: {qty_h}주 ({ret_pct:+.1f}%) [{ok}]")
                    pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                                   "Action": "SELL", "Ticker": h['ticker'],
                                   "Qty": qty_h, "Price": cur_p_h,
                                   "RetPct": round(ret_pct, 2)}
                                  ]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                            header=not os.path.exists(ROTATION_HISTORY_FILE),
                                            index=False)
            time.sleep(2)

        new_holdings = []
        per_stock    = (rot_budget * 0.95) / len(top2) if top2 else 0
        for ticker in top2:
            cur_p_t = trader.get_current_price(ticker)
            if cur_p_t <= 0:
                continue
            buy_qty = int(per_stock / cur_p_t)
            if buy_qty < 1:
                msgs.append(f"ROT {ticker} SKIP (잔고 부족)")
                continue
            res = trader.send_order(ticker, buy_qty, "BUY")
            ok  = "OK" if res.get('rt_cd') == '0' else "FAIL"
            msgs.append(f"ROT 매수 {ticker}: {buy_qty}주 @ ${cur_p_t:.2f} [{ok}]")
            new_holdings.append({"ticker": ticker, "qty": buy_qty,
                                 "entry_price": cur_p_t})
            pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                           "Action": "BUY", "Ticker": ticker,
                           "Qty": buy_qty, "Price": cur_p_t, "RetPct": 0}
                          ]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                    header=not os.path.exists(ROTATION_HISTORY_FILE),
                                    index=False)

        rot_state['in_market']  = True
        rot_state['holdings']   = new_holdings
        rot_state['entry_date'] = now_kst.strftime("%Y-%m-%d")
        save_rotation_state(rot_state)

    elif action == "EXIT" and rot_state['in_market']:
        for h in rot_state['holdings']:
            qty_h   = trader.get_holdings(h['ticker'])
            cur_p_h = trader.get_current_price(h['ticker'])
            if qty_h > 0:
                res     = trader.send_order(h['ticker'], qty_h, "SELL")
                ret_pct = (cur_p_h - h.get('entry_price', cur_p_h)) / \
                          max(h.get('entry_price', cur_p_h), 1) * 100
                ok = "OK" if res.get('rt_cd') == '0' else "FAIL"
                msgs.append(f"ROT 청산 {h['ticker']}: {qty_h}주 ({ret_pct:+.1f}%) [{ok}]")
                pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                               "Action": "SELL", "Ticker": h['ticker'],
                               "Qty": qty_h, "Price": cur_p_h,
                               "RetPct": round(ret_pct, 2)}
                              ]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                        header=not os.path.exists(ROTATION_HISTORY_FILE),
                                        index=False)
        rot_state['in_market'] = False
        rot_state['holdings']  = []
        save_rotation_state(rot_state)
    else:
        msgs.append(f"ROT: {action} | TOP2: {', '.join(top2)}")

    msgs.append(f"AI: {ask_gemini(u_sig, r_sig)}")

    # [FIX] 텔레그램 전송 정상화
    full_msg = "\n".join(msgs)
    print(full_msg)
    if bot:
        try:
            await bot.send_message(chat_id=chat_id, text=full_msg,
                                   parse_mode="HTML")
            print("[OK] 텔레그램 전송 성공")
        except Exception as e:
            print(f"[ERROR] 텔레그램 전송 실패: {e}")
    else:
        print("[WARN] bot=None - TELEGRAM_TOKEN 또는 python-telegram-bot 확인")

# KST 01:00 긴급 탈출
elif current_hour == 1:
    spy_int = yf.download(SIGNAL_TICKER, period='1d', interval='5m', progress=False)
    if spy_int.empty:
        return
    if isinstance(spy_int.columns, pd.MultiIndex):
        spy_int.columns = spy_int.columns.get_level_values(0)
    day_ret = (float(spy_int['Close'].iloc[-1]) /
               float(spy_int['Open'].iloc[0])) - 1

    if day_ret <= -0.03:
        e_msgs = [f"<b>긴급탈출 [{now_kst.strftime('%H:%M')}]</b>",
                  f"SPY 당일 {day_ret*100:.1f}% 하락"]

        qty_upro   = trader.get_holdings(TRADE_TICKER)
        cur_p_upro = trader.get_current_price(TRADE_TICKER)
        if qty_upro > 0:
            res = trader.send_order(TRADE_TICKER, qty_upro, "SELL")
            ok  = "OK" if res.get('rt_cd') == '0' else "FAIL"
            e_msgs.append(f"UPRO 긴급매도 {qty_upro}주 [{ok}]")
            if res.get('rt_cd') == '0':
                with open(STATE_FILE, 'w') as f:
                    json.dump({"in_market": False,
                               "last_exit_price": float(spy_int['Close'].iloc[-1])}, f)

        rot_state = load_rotation_state()
        if rot_state['in_market']:
            for h in rot_state['holdings']:
                qty_h   = trader.get_holdings(h['ticker'])
                cur_p_h = trader.get_current_price(h['ticker'])
                if qty_h > 0:
                    res = trader.send_order(h['ticker'], qty_h, "SELL")
                    ok  = "OK" if res.get('rt_cd') == '0' else "FAIL"
                    e_msgs.append(f"ROT 긴급매도 {h['ticker']} {qty_h}주 [{ok}]")
            rot_state['in_market'] = False
            rot_state['holdings']  = []
            save_rotation_state(rot_state)

        full_msg = "\n".join(e_msgs)
        print(full_msg)
        if bot:
            try:
                await bot.send_message(chat_id=chat_id, text=full_msg,
                                       parse_mode="HTML")
            except Exception as e:
                print(f"[ERROR] 텔레그램 전송 실패: {e}")
    else:
        print(f"[01:00] SPY {day_ret*100:.1f}% - 긴급탈출 조건 미해당")

# KST 07:00 아침 리포트
elif current_hour == 7:
    rot_state  = load_rotation_state()
    r_sig      = get_rotation_signal(spy_close, vix_close, close_all, rot_state)
    qty_upro   = trader.get_holdings(TRADE_TICKER)
    rot_hold   = ", ".join([h['ticker'] for h in rot_state.get('holdings', [])]) \
                 or "CASH"
    bal        = trader.get_balance()
    report     = (
        f"<b>아침 리포트 [{now_kst.strftime('%m/%d %H:%M')}]</b>\n"
        f"잔고: ${bal:,.2f}\n"
        f"UPRO: {str(qty_upro) + '주' if qty_upro > 0 else 'CASH'}\n"
        f"ROT: {rot_hold}\n"
        f"SPY 6M: {r_sig.get('spy_6m', 0)*100:+.1f}% | "
        f"VIX: {r_sig.get('vix_now', 0):.1f}\n"
        f"TOP2: {', '.join(r_sig.get('top2', []))}\n"
        f"AI: {ask_gemini('morning', r_sig)}"
    )
    print(report)
    if bot:
        try:
            await bot.send_message(chat_id=chat_id, text=report, parse_mode="HTML")
        except Exception as e:
            print(f"[ERROR] 텔레그램 전송 실패: {e}")
else:
    print(f"[{current_hour}:00] 스케줄 외 시간 - 패스")
```

# ==============================================================

# 9. Streamlit 대시보드

# ==============================================================

def run_dashboard():
now_kst = dt.now(KST)
st.set_page_config(page_title=“Unified Bot v1.2”, layout=“wide”, page_icon=“🤖”)
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
    st.title("Unified Bot v1.2")
    st.caption(f"Update: {now_kst.strftime('%H:%M:%S')} KST")
    st.divider()
    st.markdown(f"**자본 배분**\n- UPRO: {int(UPRO_RATIO*100)}%\n"
                f"- ROT: {int(ROTATION_RATIO*100)}%")
    st.divider()
    st.markdown("**UPRO EXIT**")
    st.caption("VIX+30% / SPY-3% / 3일-5% / 연속2개월하락")
    st.markdown("**UPRO ENTER**")
    st.caption("VIX Reversal / +2% Rebound")
    st.markdown("**ROT 조건**")
    st.caption("SPY 6M>0 & VIX<25")
    st.divider()
    st.markdown("**후보 풀**")
    for t in CANDIDATE_POOL:
        st.caption(f"- {t}")

@st.cache_data(ttl=300)
def load_all():
    return get_market_data()

spy_ohlc, monthly, vix_close, close_all, data_msg = load_all()
rot_state = load_rotation_state()

if spy_ohlc.empty:
    st.error(f"데이터 로드 실패: {data_msg}")
    return

spy_close                          = spy_ohlc['Close']
u_signal, u_reason, u_p, u_st     = get_upro_signal(spy_close, monthly, vix_close)
r_signal                           = get_rotation_signal(spy_close, vix_close,
                                                          close_all, rot_state)

df_upro   = pd.read_csv(HISTORY_FILE) \
            if os.path.exists(HISTORY_FILE) else pd.DataFrame()
df_rot    = pd.read_csv(ROTATION_HISTORY_FILE) \
            if os.path.exists(ROTATION_HISTORY_FILE) else pd.DataFrame()
upro_perf = calc_upro_performance(df_upro)
rot_perf  = calc_rotation_performance(df_rot)

st.title("Unified Trading Bot")
tab1, tab2, tab3 = st.tabs(["실시간 현황", "성과 분석", "거래 로그"])

# TAB 1 ------------------------------------------------
with tab1:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("UPRO 포지션", "IN" if u_st.get('in_market') else "OUT")
    c2.metric("UPRO 신호",   u_signal)
    c3.metric("ROT 신호",    r_signal['action'])
    c4.metric("VIX",         f"{r_signal['vix_now']:.1f}")
    c5.metric("SPY 6M",      f"{r_signal['spy_6m']*100:+.1f}%")

    if u_signal == "KEEP":
        st.success(f"[UPRO] {u_reason}")
    elif u_signal == "EXIT":
        st.error(f"[UPRO 긴급] {u_reason}")
    elif u_signal == "RE-ENTER":
        st.success(f"[UPRO RE-ENTER] {u_reason}")
    else:
        st.info(f"[UPRO] {u_reason}")

    r_action = r_signal['action']
    if r_action == "KEEP":
        held = ', '.join([h['ticker'] for h in rot_state.get('holdings', [])])
        st.success(f"[ROT] KEEP - {held}")
    elif r_action == "EXIT":
        st.error("[ROT] EXIT - 탈출 신호")
    elif r_action in ["ENTER", "ROTATE"]:
        st.warning(f"[ROT] {r_action} - {', '.join(r_signal.get('top2', []))}")
    else:
        st.info("[ROT] WAIT")

    spy126 = spy_ohlc.tail(126)
    vix126 = vix_close.tail(126)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.5, 0.25, 0.25], vertical_spacing=0.04)
    fig.add_trace(go.Candlestick(
        x=spy126.index, open=spy126['Open'], high=spy126['High'],
        low=spy126['Low'], close=spy126['Close'], name="SPY"
    ), row=1, col=1)
    fig.add_trace(go.Bar(x=vix126.index, y=vix126.values,
                         name="VIX", marker_color="orange"), row=2, col=1)
    fig.add_trace(go.Bar(x=spy126.index, y=spy126['Volume'],
                         name="Volume", marker_color="steelblue"), row=3, col=1)
    fig.update_layout(xaxis_rangeslider_visible=False, height=650,
                      template="plotly_dark", margin=dict(t=10, b=10),
                      paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e')
    st.plotly_chart(fig, use_container_width=True)

    if r_signal.get('scores'):
        st.subheader("모멘텀 스코어")
        sc_df = pd.DataFrame([
            {"Ticker": t, "Score": s,
             "TOP2": "★" if t in r_signal['top2'] else ""}
            for t, s in sorted(r_signal['scores'].items(),
                               key=lambda x: x[1], reverse=True)
        ])
        st.dataframe(sc_df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Gemini AI 분석")
    if st.button("분석 요청", type="secondary"):
        with st.spinner("분석 중..."):
            st.info(ask_gemini(u_signal, r_signal))

    with st.expander("Debug", expanded=False):
        st.write(f"데이터: {data_msg} | Shape: {spy_ohlc.shape}")
        st.write(f"Rotation State: {rot_state}")
        st.dataframe(spy_ohlc.tail(3))

# TAB 2 ------------------------------------------------
with tab2:
    st.subheader("실제 성과 분석")
    if upro_perf['total_trades'] == 0 and rot_perf['total_trades'] == 0:
        st.info("아직 거래 기록이 없습니다. 첫 거래 후 성과가 표시됩니다.")
    else:
        for label, perf in [("UPRO 트렌드 봇 (70%)", upro_perf),
                             ("모멘텀 로테이션 봇 (30%)", rot_perf)]:
            st.markdown(f"**{label}**")
            k1, k2, k3, k4, k5, k6 = st.columns(6)
            k1.metric("총 수익률",    f"{perf['total_return']:+.1f}%")
            k2.metric("MDD",          f"-{perf['mdd']:.1f}%")
            k3.metric("승률",         f"{perf['win_rate']:.0f}%")
            k4.metric("샤프",         f"{perf['sharpe']:.2f}")
            k5.metric("거래 횟수",    f"{perf['total_trades']}회")
            k6.metric("승/패",        f"{perf['win_trades']}/{perf['loss_trades']}")
            k7, k8, k9, k10 = st.columns(4)
            k7.metric("평균 수익",  f"{perf['avg_profit']:+.1f}%")
            k8.metric("평균 손실",  f"{perf['avg_loss']:+.1f}%")
            k9.metric("최고 거래",  f"{perf['best_trade']:+.1f}%")
            k10.metric("최악 거래", f"{perf['worst_trade']:+.1f}%")
            if perf['equity_curve']:
                eq_df = pd.DataFrame(perf['equity_curve'])
                fig_eq = go.Figure(go.Scatter(
                    x=eq_df['date'], y=eq_df['equity'],
                    fill='tozeroy', line=dict(color='#3fb950', width=2)
                ))
                fig_eq.update_layout(template='plotly_dark', height=260,
                                     margin=dict(t=10, b=10),
                                     paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
                                     yaxis_title="누적 수익 (시작=100)")
                st.plotly_chart(fig_eq, use_container_width=True)
            st.divider()

        # 비교 곡선
        st.markdown("**전략 비교 곡선**")
        fig_cmp = go.Figure()
        if upro_perf['equity_curve']:
            eq_u = pd.DataFrame(upro_perf['equity_curve'])
            fig_cmp.add_trace(go.Scatter(x=eq_u['date'], y=eq_u['equity'],
                                         name="UPRO 봇",
                                         line=dict(color="#38bdf8", width=2)))
        if rot_perf['equity_curve']:
            eq_r = pd.DataFrame(rot_perf['equity_curve'])
            fig_cmp.add_trace(go.Scatter(x=eq_r['date'], y=eq_r['equity'],
                                         name="Rotation 봇",
                                         line=dict(color="#fbbf24", width=2)))
        if not df_upro.empty:
            try:
                start_d = pd.to_datetime(df_upro['Date'].min()).strftime('%Y-%m-%d')
                spy_bh  = yf.download(SIGNAL_TICKER, start=start_d,
                                      progress=False, auto_adjust=True)
                if isinstance(spy_bh.columns, pd.MultiIndex):
                    spy_bh.columns = spy_bh.columns.get_level_values(0)
                if not spy_bh.empty:
                    spy_norm = spy_bh['Close'] / spy_bh['Close'].iloc[0] * 100
                    fig_cmp.add_trace(go.Scatter(
                        x=spy_norm.index.astype(str), y=spy_norm.values,
                        name="SPY B&H",
                        line=dict(color="#64748b", width=1.5, dash='dash')))
            except:
                pass
        fig_cmp.update_layout(template='plotly_dark', height=320,
                               margin=dict(t=10, b=10),
                               paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
                               yaxis_title="누적 수익 (시작=100)",
                               legend=dict(orientation='h', y=1.05))
        st.plotly_chart(fig_cmp, use_container_width=True)

# TAB 3 ------------------------------------------------
with tab3:
    st.subheader("거래 로그")
    col1, col2 = st.columns(2)
    with col1:
        st.caption("UPRO 봇")
        if not df_upro.empty:
            st.dataframe(df_upro.tail(20).iloc[::-1],
                         use_container_width=True, hide_index=True)
            st.download_button("CSV 다운로드 (UPRO)",
                               df_upro.to_csv(index=False).encode('utf-8'),
                               file_name="history_trend.csv", mime="text/csv")
        else:
            st.info("거래 기록 없음")
    with col2:
        st.caption("Rotation 봇")
        if not df_rot.empty:
            st.dataframe(df_rot.tail(20).iloc[::-1],
                         use_container_width=True, hide_index=True)
            st.download_button("CSV 다운로드 (Rotation)",
                               df_rot.to_csv(index=False).encode('utf-8'),
                               file_name="history_rotation.csv", mime="text/csv")
        else:
            st.info("거래 기록 없음")
```

# ==============================================================

# 10. 진입점

# ==============================================================

if os.getenv(‘GITHUB_ACTIONS’) == ‘true’:
asyncio.run(run_trading())
else:
run_dashboard()
