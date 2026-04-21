# “””

# Unified Trading Bot v1.0.0
UPRO 트렌드 봇 (70%) + 모멘텀 로테이션 봇 (30%) 통합

자본 배분:
UPRO_RATIO     = 0.70  ← 검증된 주력 (기존 app.py 로직 그대로)
ROTATION_RATIO = 0.30  ← 실전 검증 시작 (TOP2 모멘텀 로테이션)

스케줄 (GitHub Actions):
KST 20:00 → 정규 매매
KST 01:00 → 긴급 탈출
KST 07:00 → 아침 리포트

# 상태 파일:
trend_state.json      ← 기존 UPRO 봇 상태 (호환 유지)
rotation_state.json   ← Rotation 봇 상태 (신규)
history_trend.csv     ← 기존 UPRO 거래 로그 (호환 유지)
history_rotation.csv  ← Rotation 거래 로그 (신규)

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

# ══════════════════════════════════════════════════════════════

# 1. 설정

# ══════════════════════════════════════════════════════════════

KST = pytz.timezone(‘Asia/Seoul’)

# ── 자본 배분 (핵심: 두 비율 합계 = 1.0)

UPRO_RATIO     = 0.70
ROTATION_RATIO = 0.30

# ── UPRO 봇 (기존 그대로)

SIGNAL_TICKER = ‘SPY’
TRADE_TICKER  = ‘UPRO’
STATE_FILE    = ‘trend_state.json’
HISTORY_FILE  = ‘history_trend.csv’

# ── Rotation 봇 (신규)

CANDIDATE_POOL   = [‘NVDA’, ‘TSLA’, ‘META’, ‘AAPL’, ‘MSFT’, ‘AMZN’, ‘GOOGL’]
TOP_N            = 2
VIX_ENTER_MAX    = 25.0
SPY_6M_MIN       = 0.0
VIX_EXIT_SPIKE   = 0.30
SPY_DAILY_EXIT   = -0.03
STOCK_HARD_STOP  = -0.07
ROTATION_STATE_FILE   = ‘rotation_state.json’
ROTATION_HISTORY_FILE = ‘history_rotation.csv’

# ══════════════════════════════════════════════════════════════

# 2. KIS API (기존 검증 규격 그대로 유지)

# ══════════════════════════════════════════════════════════════

class KIS_Trader:
def **init**(self):
self.base_url    = “https://openapi.koreainvestment.com:9443”
self.app_key     = os.getenv(‘KIS_APPKEY’)
self.app_secret  = os.getenv(‘KIS_SECRET’)
self.cano        = os.getenv(‘KIS_CANO’)
self.acnt_prdt_cd= os.getenv(‘KIS_ACNT_PRDT_CD’, ‘01’)
self.token       = None
self.error_detail= “Initial”
self._set_token()

```
def _set_token(self):
    try:
        url  = f"{self.base_url}/oauth2/tokenP"
        data = {"grant_type": "client_credentials",
                "appkey": self.app_key, "appsecret": self.app_secret}
        res  = requests.post(url, headers={"content-type": "application/json"},
                             data=json.dumps(data))
        res_data    = res.json()
        self.token  = res_data.get('access_token')
        if not self.token:
            self.error_detail = f"Auth Fail: {res_data.get('msg1')}"
    except Exception as e:
        self.error_detail = f"Conn: {str(e)}"

def _headers(self, tr_id: str) -> dict:
    return {
        "Content-Type":  "application/json",
        "authorization": f"Bearer {self.token}",
        "appkey":        self.app_key,
        "appsecret":     self.app_secret,
        "tr_id":         tr_id,
        "custtype":      "P",
    }

def get_total_balance(self) -> float:
    """전체 주문가능금액(USD) — UPRO+Rotation 공유 원천"""
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

def get_holdings_qty(self, ticker: str) -> int:
    """특정 종목 보유 수량"""
    if not self.token:
        return 0
    try:
        url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                  "OVRS_EXCG_CD": "AMEX", "TR_CRCY_CD": "USD",
                  "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        res = requests.get(url, headers=self._headers("JTTT3012R"),
                           params=params)
        for item in res.json().get('output1', []):
            if item.get('pdno') == ticker:
                return int(float(item.get('ccld_qty_smtl', 0)))
        return 0
    except:
        return 0

def get_all_holdings(self) -> list:
    """전체 보유 종목 리스트"""
    if not self.token:
        return []
    try:
        url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                  "OVRS_EXCG_CD": "AMEX", "TR_CRCY_CD": "USD",
                  "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        res = requests.get(url, headers=self._headers("JTTT3012R"),
                           params=params)
        result = []
        for item in res.json().get('output1', []):
            qty = float(item.get('ccld_qty_smtl', 0) or 0)
            if qty > 0:
                result.append({
                    'ticker':    item.get('pdno', ''),
                    'shares':    int(qty),
                    'avg_price': float(item.get('pchs_avg_pric', 0) or 0),
                })
        return result
    except:
        return []

def get_current_price(self, ticker: str) -> float:
    try:
        df = yf.download(ticker, period='1d', interval='1m', progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if not df.empty:
            return float(df['Close'].iloc[-1])
        return 0.0
    except:
        return 0.0

def send_order(self, ticker: str, qty, side: str = "BUY") -> dict:
    if not self.token:
        return {"rt_cd": "1", "msg1": "No Token"}
    try:
        url       = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        tr_id     = "TTTT1002U" if side == "BUY" else "TTTT1006U"
        clean_qty = str(int(float(qty)))
        data = {
            "CANO":           self.cano,
            "ACNT_PRDT_CD":   self.acnt_prdt_cd,
            "OVRS_EXCG_CD":   "AMEX",
            "PDNO":           ticker,
            "ORD_QTY":        clean_qty,
            "OVRS_ORD_UNPR":  "0",
            "ORD_SVR_DVSN_CD":"0",
            "ORD_DVSN":       "00",
        }
        return requests.post(url, headers=self._headers(tr_id),
                             data=json.dumps(data)).json()
    except Exception as e:
        return {"rt_cd": "1", "msg1": str(e)}
```

# ══════════════════════════════════════════════════════════════

# 3. 시장 데이터 (기존 2중 Flattening 그대로)

# ══════════════════════════════════════════════════════════════

def get_market_data():
“”“SPY + VIX + 후보 종목 전체 다운로드”””
try:
tickers  = [SIGNAL_TICKER, ‘^VIX’, TRADE_TICKER] + CANDIDATE_POOL
raw      = yf.download(tickers, period=‘2y’, progress=False,
auto_adjust=True, repair=True)
if isinstance(raw.columns, pd.MultiIndex):
raw.columns = raw.columns.get_level_values(0)

```
    # SPY OHLCV 분리 (기존 방식 호환)
    spy_raw  = yf.download(SIGNAL_TICKER, period='2y', progress=False,
                           auto_adjust=True, repair=True)
    if isinstance(spy_raw.columns, pd.MultiIndex):
        spy_raw.columns = spy_raw.columns.get_level_values(0)
    spy_raw  = spy_raw[['Open', 'High', 'Low', 'Close', 'Volume']].copy()

    vix_raw  = yf.download('^VIX', period='2y', progress=False,
                           auto_adjust=True, repair=True)
    if isinstance(vix_raw.columns, pd.MultiIndex):
        vix_raw.columns = vix_raw.columns.get_level_values(0)

    spy_close = spy_raw['Close'].squeeze()
    vix_close = vix_raw['Close'].squeeze()
    monthly   = spy_close.resample('ME').last().pct_change().dropna()

    # 후보 종목 종가 (Rotation용)
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

# ══════════════════════════════════════════════════════════════

# 4. UPRO 봇 신호 (기존 get_signal 그대로)

# ══════════════════════════════════════════════════════════════

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
    vix_now, vix_prev = float(vix_close.iloc[-1]), float(vix_close.iloc[-2])
    vix_20d  = vix_close.tail(20)
    vix_mean = float(vix_20d.mean())
    vix_std  = float(vix_20d.std())
    vix_rev  = (
        (vix_now > (vix_mean + 2 * vix_std) or
         vix_prev > (vix_mean + 2 * vix_std)) and
        vix_now < vix_prev * 0.95 and
        spy_daily_ret > 0
    )
    if vix_rev:     return "RE-ENTER", "VIX Reversal", curr_p, state
    if rebound >= 0.02: return "RE-ENTER", "2% Rebound", curr_p, state
    return "WAIT", f"Waiting({rebound*100:.1f}%)", curr_p, state
```

# ══════════════════════════════════════════════════════════════

# 5. Rotation 봇 신호 (신규)

# ══════════════════════════════════════════════════════════════

def load_rotation_state() -> dict:
default = {
“in_market”: False,
“holdings”: [],   # [{“ticker”:“NVDA”,“buy_price”:120.0,“shares”:3}]
“entry_date”: None,
“consecutive_loss”: 0,
}
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

def save_rotation_state(state: dict):
with open(ROTATION_STATE_FILE, ‘w’) as f:
json.dump(state, f, indent=2, ensure_ascii=False)

def calc_momentum(series: pd.Series) -> float:
“”“1M×0.2 + 3M×0.3 + 6M×0.5 가중 모멘텀”””
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

def get_rotation_signal(spy_close, vix_close, close_all: dict, rot_state: dict) -> dict:
try:
spy_daily = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
spy_6m    = float((spy_close.iloc[-1] / spy_close.iloc[-126]) - 1)   
if len(spy_close) >= 126 else 0.0
vix_now   = float(vix_close.iloc[-1])
vix_prev  = float(vix_close.iloc[-2])
vix_daily = float((vix_now - vix_prev) / vix_prev)

```
    # 모멘텀 스코어 계산
    scores = {}
    for t in CANDIDATE_POOL:
        if t in close_all:
            scores[t] = calc_momentum(close_all[t])
    top2 = [t for t, _ in sorted(scores.items(),
                                 key=lambda x: x[1], reverse=True)[:TOP_N]]

    # 하드스탑 체크 (보유 종목 당일 -7%)
    hard_stop = False
    if rot_state['in_market']:
        for h in rot_state['holdings']:
            t = h['ticker']
            if t in close_all and len(close_all[t]) >= 2:
                daily = float((close_all[t].iloc[-1] / close_all[t].iloc[-2]) - 1)
                if daily <= STOCK_HARD_STOP:
                    hard_stop = True

    # VIX 역발상
    vix_reversal = False
    if len(vix_close) >= 3:
        vix_2prev = float(vix_close.iloc[-3])
        if (vix_prev > vix_2prev * 1.20 and
                vix_now < vix_prev * 0.92 and
                spy_daily > 0):
            vix_reversal = True

    # 레짐 판단
    regime_ok = (spy_6m > SPY_6M_MIN and vix_now < VIX_ENTER_MAX) or vix_reversal

    # EXIT 조건
    exit_cond = (
        vix_daily  >= VIX_EXIT_SPIKE or
        spy_daily  <= SPY_DAILY_EXIT or
        hard_stop  or
        rot_state['consecutive_loss'] >= 2 or
        not regime_ok
    )

    # 현재 보유
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

    return {
        "action":       action,
        "top2":         top2,
        "scores":       scores,
        "spy_daily":    round(spy_daily * 100, 2),
        "spy_6m":       round(spy_6m * 100, 2),
        "vix_now":      round(vix_now, 2),
        "vix_daily":    round(vix_daily * 100, 2),
        "regime_ok":    regime_ok,
        "hard_stop":    hard_stop,
        "vix_reversal": vix_reversal,
    }
except Exception as e:
    return {"action": "WAIT", "top2": [], "scores": {}, "error": str(e)}
```

# ══════════════════════════════════════════════════════════════

# 6. Gemini 분석 (선택)

# ══════════════════════════════════════════════════════════════

def ask_gemini(upro_signal: str, rot_signal: dict) -> str:
if not GEMINI_OK:
return “google-generativeai 미설치”
api_key = os.getenv(“GEMINI_API_KEY”, “”)
if not api_key:
return “GEMINI_API_KEY 미설정”
try:
genai.configure(api_key=api_key)
model = genai.GenerativeModel(“gemini-1.5-flash”)
prompt = f”””
퀀트 트레이딩 전문가로서 아래 두 전략의 현황을 분석해 주세요.

[UPRO 트렌드 봇 (70% 자본)]
신호: {upro_signal}

[모멘텀 로테이션 봇 (30% 자본)]
신호: {rot_signal.get(‘action’)}
TOP2 종목: {rot_signal.get(‘top2’)}
SPY 6M: {rot_signal.get(‘spy_6m’)}% | VIX: {rot_signal.get(‘vix_now’)}
레짐 OK: {rot_signal.get(‘regime_ok’)}

200자 이내로 간결하게:

1. 전체 시장 상황 한줄 평가
1. 두 전략 신호 적절성 의견
1. 주의할 리스크 1가지
   “””
   return model.generate_content(prompt).text.strip()
   except Exception as e:
   return f”Gemini 오류: {str(e)[:80]}”

# ══════════════════════════════════════════════════════════════

# 7. Telegram

# ══════════════════════════════════════════════════════════════

async def send_telegram(msg: str):
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
print(f”Telegram 전송 실패: {e}”)

# ══════════════════════════════════════════════════════════════

# 8. 자동매매 실행

# ══════════════════════════════════════════════════════════════

async def run_trading():
now_kst      = dt.now(KST)
current_hour = now_kst.hour
today        = now_kst.strftime(’%Y-%m-%d’)

```
trader    = KIS_Trader()
rot_state = load_rotation_state()

# 전체 잔고 확인
total_bal = trader.get_total_balance()

# ── 배분 금액 계산
upro_budget     = total_bal * UPRO_RATIO        # 70%
rotation_budget = total_bal * ROTATION_RATIO    # 30%

print(f"[{now_kst.strftime('%H:%M')}] 총잔고: ${total_bal:.2f} | "
      f"UPRO예산: ${upro_budget:.2f} | Rotation예산: ${rotation_budget:.2f}")

# 데이터 로드
spy_ohlc, monthly, vix_close, close_all, data_msg = get_market_data()
if spy_ohlc.empty:
    await send_telegram(f"⚠️ 데이터 로드 실패: {data_msg}")
    return

spy_close = spy_ohlc['Close']

# ── KST 20:00 정규 매매
if current_hour == 20:

    # ────────────────────────────────
    # [A] UPRO 봇 (기존 로직 그대로)
    # ────────────────────────────────
    u_signal, u_reason, u_price, u_state = get_upro_signal(spy_close, monthly, vix_close)
    upro_qty  = trader.get_holdings_qty(TRADE_TICKER)
    upro_price= trader.get_current_price(TRADE_TICKER)
    upro_msg  = ""

    if u_signal in ["KEEP", "RE-ENTER"] and upro_qty == 0 and upro_price > 0:
        buy_qty = int((upro_budget * 0.95) / upro_price)
        if buy_qty >= 1:
            res = trader.send_order(TRADE_TICKER, buy_qty, "BUY")
            if res.get('rt_cd') == '0':
                upro_msg = f"✅ UPRO 매수 {buy_qty}주 @${upro_price:.2f}"
                u_state['in_market']       = True
                u_state['last_exit_price'] = 0
                with open(STATE_FILE, 'w') as f:
                    json.dump(u_state, f)
                pd.DataFrame([{
                    "Date": today, "Action": "BUY",
                    "Qty": buy_qty, "Price": upro_price,
                    "Strategy": "UPRO", "Budget": upro_budget,
                }]).to_csv(HISTORY_FILE, mode='a',
                           header=not os.path.exists(HISTORY_FILE), index=False)
            else:
                upro_msg = f"❌ UPRO 매수실패: {str(res)[:100]}"

    elif u_signal == "EXIT" and upro_qty > 0:
        res = trader.send_order(TRADE_TICKER, upro_qty, "SELL")
        if res.get('rt_cd') == '0':
            upro_msg = f"✅ UPRO 매도 {upro_qty}주 @${upro_price:.2f}"
            u_state['in_market']       = False
            u_state['last_exit_price'] = u_price
            with open(STATE_FILE, 'w') as f:
                json.dump(u_state, f)
            pd.DataFrame([{
                "Date": today, "Action": "SELL",
                "Qty": upro_qty, "Price": upro_price,
                "Strategy": "UPRO", "Budget": upro_budget,
            }]).to_csv(HISTORY_FILE, mode='a',
                       header=not os.path.exists(HISTORY_FILE), index=False)
        else:
            upro_msg = f"❌ UPRO 매도실패: {str(res)[:100]}"
    else:
        upro_msg = f"— UPRO {u_signal} ({u_reason})"

    # ────────────────────────────────
    # [B] Rotation 봇
    # ────────────────────────────────
    r_signal  = get_rotation_signal(spy_close, vix_close, close_all, rot_state)
    r_action  = r_signal['action']
    top2      = r_signal['top2']
    rot_msg   = ""

    if r_action == "ENTER" and not rot_state['in_market'] and len(top2) > 0:
        per_stock = (rotation_budget * 0.95) / len(top2)
        new_holdings = []
        bought = []
        for ticker in top2:
            price = trader.get_current_price(ticker)
            if price <= 0:
                continue
            qty = int(per_stock / price)
            if qty < 1:
                continue
            res = trader.send_order(ticker, qty, "BUY")
            if res.get('rt_cd') == '0':
                new_holdings.append({"ticker": ticker, "buy_price": price, "shares": qty})
                bought.append(f"{ticker} {qty}주@${price:.1f}")
                pd.DataFrame([{
                    "Date": today, "Action": "BUY", "Ticker": ticker,
                    "Qty": qty, "Price": price, "Strategy": "ROTATION",
                }]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                           header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
        rot_state['in_market'] = True
        rot_state['holdings']  = new_holdings
        rot_state['entry_date']= today
        save_rotation_state(rot_state)
        rot_msg = "✅ ROT 진입: " + ", ".join(bought)

    elif r_action == "EXIT" and rot_state['in_market']:
        sold = []
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
                sold.append(f"{t} {qty}주@${price:.1f}({ret:+.1f}%)")
                pd.DataFrame([{
                    "Date": today, "Action": "SELL", "Ticker": t,
                    "Qty": qty, "Price": price,
                    "Strategy": "ROTATION", "RetPct": round(ret, 2),
                }]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                           header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
        avg_ret = np.mean(monthly_rets) if monthly_rets else 0
        if avg_ret < 0:
            rot_state['consecutive_loss'] += 1
        else:
            rot_state['consecutive_loss']  = 0
        rot_state['in_market'] = False
        rot_state['holdings']  = []
        save_rotation_state(rot_state)
        rot_msg = "🔴 ROT 탈출: " + ", ".join(sold)

    elif r_action == "ROTATE" and rot_state['in_market']:
        current  = [h['ticker'] for h in rot_state['holdings']]
        to_sell  = [t for t in current if t not in top2]
        to_buy   = [t for t in top2    if t not in current]
        rot_log  = []
        # 매도
        for t in to_sell:
            qty = trader.get_holdings_qty(t)
            if qty > 0:
                price = trader.get_current_price(t)
                res   = trader.send_order(t, qty, "SELL")
                if res.get('rt_cd') == '0':
                    rot_log.append(f"OUT {t}")
                    pd.DataFrame([{
                        "Date": today, "Action": "SELL", "Ticker": t,
                        "Qty": qty, "Price": price, "Strategy": "ROTATION",
                    }]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                               header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
        import time; time.sleep(2)
        # 매수
        freed_budget = (rotation_budget * 0.95) / max(len(to_buy), 1)
        new_holdings = [h for h in rot_state['holdings'] if h['ticker'] not in to_sell]
        for t in to_buy:
            price = trader.get_current_price(t)
            if price <= 0:
                continue
            qty = int(freed_budget / price)
            if qty < 1:
                continue
            res = trader.send_order(t, qty, "BUY")
            if res.get('rt_cd') == '0':
                new_holdings.append({"ticker": t, "buy_price": price, "shares": qty})
                rot_log.append(f"IN {t}")
                pd.DataFrame([{
                    "Date": today, "Action": "BUY", "Ticker": t,
                    "Qty": qty, "Price": price, "Strategy": "ROTATION",
                }]).to_csv(ROTATION_HISTORY_FILE, mode='a',
                           header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
        rot_state['holdings'] = new_holdings
        save_rotation_state(rot_state)
        rot_msg = "🔄 ROT 교체: " + " → ".join(rot_log)
    else:
        rot_msg = f"— ROT {r_action} (TOP2: {','.join(top2)})"

    # Gemini 분석
    gemini_txt = ask_gemini(f"{u_signal}:{u_reason}", r_signal)

    # 통합 Telegram 알림
    msg = f"""🤖 <b>통합봇 정규매매</b> {now_kst.strftime('%m/%d %H:%M')}
```

━━━━━━━━━━━━━━━━━━━━
💼 총잔고: ${total_bal:,.2f}
├ UPRO예산: ${upro_budget:,.2f} (70%)
└ ROT예산:  ${rotation_budget:,.2f} (30%)
━━━━━━━━━━━━━━━━━━━━
📊 SPY: {r_signal.get(‘spy_daily’,0):+.2f}% | VIX: {r_signal.get(‘vix_now’,0):.1f}
━━━━━━━━━━━━━━━━━━━━
🔵 UPRO봇: {upro_msg}
🟡 ROT봇:  {rot_msg}
━━━━━━━━━━━━━━━━━━━━
🤖 Gemini: {gemini_txt[:250]}”””
await send_telegram(msg)

```
# ── KST 01:00 긴급 탈출
elif current_hour == 1:
    u_signal, u_reason, u_price, u_state = get_upro_signal(spy_close, monthly, vix_close)
    r_signal  = get_rotation_signal(spy_close, vix_close, close_all, rot_state)
    emergency = []

    # UPRO 긴급 탈출
    if u_signal == "EXIT":
        upro_qty = trader.get_holdings_qty(TRADE_TICKER)
        if upro_qty > 0:
            price = trader.get_current_price(TRADE_TICKER)
            res   = trader.send_order(TRADE_TICKER, upro_qty, "SELL")
            if res.get('rt_cd') == '0':
                emergency.append(f"🔴 UPRO 긴급매도 {upro_qty}주@${price:.2f}")
                u_state['in_market']       = False
                u_state['last_exit_price'] = u_price
                with open(STATE_FILE, 'w') as f:
                    json.dump(u_state, f)

    # Rotation 긴급 탈출
    if r_signal['action'] == "EXIT" and rot_state['in_market']:
        for h in rot_state['holdings']:
            t   = h['ticker']
            qty = trader.get_holdings_qty(t)
            if qty > 0:
                price = trader.get_current_price(t)
                res   = trader.send_order(t, qty, "SELL")
                if res.get('rt_cd') == '0':
                    emergency.append(f"🔴 ROT {t} 긴급매도 {qty}주@${price:.2f}")
        rot_state['in_market'] = False
        rot_state['holdings']  = []
        save_rotation_state(rot_state)

    if emergency:
        await send_telegram(
            f"🚨 <b>긴급탈출</b> {now_kst.strftime('%H:%M')}\n" +
            "\n".join(emergency)
        )

# ── KST 07:00 아침 리포트
elif current_hour == 7:
    r_signal  = get_rotation_signal(spy_close, vix_close, close_all, rot_state)
    upro_qty  = trader.get_holdings_qty(TRADE_TICKER)
    rot_hold  = ", ".join([h['ticker'] for h in rot_state.get('holdings', [])]) or "없음"
    gemini_txt= ask_gemini("morning_check", r_signal)

    msg = f"""📋 <b>아침 리포트</b> {now_kst.strftime('%m/%d %H:%M')}
```

━━━━━━━━━━━━━━━━━━━━
💵 총잔고: ${total_bal:,.2f}
🔵 UPRO: {‘보유 ’ + str(upro_qty) + ‘주’ if upro_qty > 0 else ‘현금’}
🟡 ROT:  {rot_hold}
━━━━━━━━━━━━━━━━━━━━
📊 SPY 6M: {r_signal.get(‘spy_6m’,0):+.1f}% | VIX: {r_signal.get(‘vix_now’,0):.1f}
🎯 모멘텀 TOP2: {’, ’.join(r_signal.get(‘top2’,[]))}
━━━━━━━━━━━━━━━━━━━━
🤖 {gemini_txt[:200]}”””
await send_telegram(msg)

# ══════════════════════════════════════════════════════════════

# 9. Streamlit 대시보드

# ══════════════════════════════════════════════════════════════

def run_dashboard():
now_kst = dt.now(KST)
st.set_page_config(page_title=“Unified Bot v1.0”, layout=“wide”, page_icon=“🤖”)

```
st.markdown("""
<style>
.main { background:#0a0f1e; }
h1,h2,h3 { color:#e0f0ff !important; }
</style>
""", unsafe_allow_html=True)

# 사이드바
with st.sidebar:
    st.markdown("### 🤖 Unified Bot v1.0")
    st.caption(f"{now_kst.strftime('%m/%d %H:%M')} KST")
    st.divider()
    st.markdown(f"**자본 배분**\n- 🔵 UPRO: {int(UPRO_RATIO*100)}%\n- 🟡 ROT: {int(ROTATION_RATIO*100)}%")
    st.divider()
    st.markdown("**UPRO EXIT**")
    st.caption("VIX+30% / SPY-3% / SPY 3일-5% / 연속2개월하락")
    st.markdown("**ROT EXIT**")
    st.caption("VIX+30% / SPY-3% / 종목-7% 하드스탑 / 레짐이탈")
    st.divider()
    st.markdown("**후보 풀**")
    for t in CANDIDATE_POOL:
        st.caption(f"• {t}")

# 데이터 로드
@st.cache_data(ttl=300)
def load_all():
    return get_market_data()

spy_ohlc, monthly, vix_close, close_all, data_msg = load_all()
rot_state = load_rotation_state()

if spy_ohlc.empty:
    st.error(f"데이터 로드 실패: {data_msg}")
    return

spy_close                            = spy_ohlc['Close']
u_signal, u_reason, u_price, u_state = get_upro_signal(spy_close, monthly, vix_close)
r_signal                             = get_rotation_signal(spy_close, vix_close, close_all, rot_state)

# ── 헤더
st.title("🤖 Unified Trading Bot")
st.caption(f"UPRO 트렌드 {int(UPRO_RATIO*100)}% + 모멘텀 로테이션 {int(ROTATION_RATIO*100)}%  |  {now_kst.strftime('%Y-%m-%d %H:%M')} KST")
st.divider()

# ── 자본 배분 현황 (상단 바)
trader = KIS_Trader()
total_bal       = trader.get_total_balance()
upro_budget     = total_bal * UPRO_RATIO
rotation_budget = total_bal * ROTATION_RATIO

ca, cb, cc = st.columns(3)
ca.metric("💵 총 주문가능금액", f"${total_bal:,.2f}")
cb.metric("🔵 UPRO 예산 (70%)", f"${upro_budget:,.2f}")
cc.metric("🟡 ROT 예산 (30%)", f"${rotation_budget:,.2f}")

st.divider()

# ── 두 봇 나란히
col_upro, col_rot = st.columns(2)

# ── UPRO 봇 패널
with col_upro:
    st.subheader("🔵 UPRO 트렌드 봇")
    u1, u2, u3 = st.columns(3)
    u1.metric("포지션", "IN" if u_state.get('in_market') else "OUT")
    u2.metric("신호", u_signal)
    u3.metric("SPY", f"${u_price:.2f}")

    if u_signal == "KEEP":
        st.success(f"✅ {u_reason}")
    elif u_signal == "EXIT":
        st.error(f"🔴 {u_reason}")
    elif u_signal == "RE-ENTER":
        st.success(f"🟢 {u_reason}")
    else:
        st.info(f"⚪ {u_reason}")

    # UPRO 차트
    upro_data = spy_ohlc['Close'].tail(63)
    fig_upro  = go.Figure()
    fig_upro.add_trace(go.Scatter(
        x=upro_data.index, y=upro_data.values,
        line=dict(color="#38bdf8", width=2), name="SPY",
        fill='tozeroy', fillcolor='rgba(56,189,248,0.05)'
    ))
    fig_upro.update_layout(
        template='plotly_dark', height=200,
        margin=dict(l=5, r=5, t=5, b=5),
        paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
        showlegend=False,
    )
    st.plotly_chart(fig_upro, use_container_width=True)

# ── Rotation 봇 패널
with col_rot:
    st.subheader("🟡 모멘텀 로테이션 봇")
    r1, r2, r3 = st.columns(3)
    r1.metric("포지션", "IN" if rot_state['in_market'] else "OUT")
    r2.metric("신호", r_signal['action'])
    r3.metric("VIX", f"{r_signal.get('vix_now', 0):.1f}")

    r_action = r_signal['action']
    if r_action == "KEEP":
        st.success(f"✅ 보유유지: {', '.join([h['ticker'] for h in rot_state['holdings']])}")
    elif r_action == "EXIT":
        st.error("🔴 탈출 신호")
    elif r_action == "ENTER":
        st.success(f"🟢 진입: {', '.join(r_signal.get('top2', []))}")
    elif r_action == "ROTATE":
        st.warning(f"🔄 교체: {', '.join(r_signal.get('top2', []))}")
    else:
        st.info(f"⚪ 대기 | SPY 6M: {r_signal.get('spy_6m', 0):+.1f}%")

    # 모멘텀 스코어 바
    scores = r_signal.get('scores', {})
    if scores:
        top2_set = set(r_signal.get('top2', []))
        df_sc    = pd.DataFrame(
            sorted(scores.items(), key=lambda x: x[1], reverse=True),
            columns=['종목', '스코어']
        )
        fig_sc = go.Figure(go.Bar(
            x=df_sc['종목'], y=df_sc['스코어'],
            marker_color=["#fbbf24" if t in top2_set else "#2a4060"
                          for t in df_sc['종목']],
            text=[f"{v:.1f}" for v in df_sc['스코어']],
            textposition='outside',
        ))
        fig_sc.update_layout(
            template='plotly_dark', height=200,
            margin=dict(l=5, r=5, t=5, b=25),
            paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
            showlegend=False,
        )
        st.plotly_chart(fig_sc, use_container_width=True)

st.divider()

# ── 통합 VIX 차트
st.subheader("📊 SPY / VIX 차트")
fig2 = make_subplots(rows=2, cols=1, row_heights=[0.6, 0.4],
                     shared_xaxes=True, vertical_spacing=0.05)
fig2.add_trace(go.Candlestick(
    x=spy_ohlc.tail(126).index,
    open=spy_ohlc.tail(126)['Open'],
    high=spy_ohlc.tail(126)['High'],
    low=spy_ohlc.tail(126)['Low'],
    close=spy_ohlc.tail(126)['Close'],
    name='SPY',
), row=1, col=1)
fig2.add_trace(go.Bar(
    x=vix_close.tail(126).index,
    y=vix_close.tail(126).values,
    name='VIX',
    marker_color=['#f87171' if v > 25 else '#f59e0b' if v > 20 else '#34d399'
                  for v in vix_close.tail(126).values],
), row=2, col=1)
fig2.update_layout(
    template='plotly_dark', height=480,
    margin=dict(l=10, r=10, t=10, b=10),
    paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e',
    xaxis_rangeslider_visible=False,
)
st.plotly_chart(fig2, use_container_width=True)

# ── Gemini 분석
st.divider()
st.subheader("🤖 Gemini AI 분석")
if st.button("분석 요청", type="secondary"):
    with st.spinner("Gemini 분석 중..."):
        result = ask_gemini(f"{u_signal}:{u_reason}", r_signal)
        st.info(result)

# ── 거래 로그
st.divider()
st.subheader("📋 거래 로그")
log_col1, log_col2 = st.columns(2)

with log_col1:
    st.caption("🔵 UPRO 봇")
    if os.path.exists(HISTORY_FILE):
        df_u = pd.read_csv(HISTORY_FILE)
        st.dataframe(df_u.tail(10).iloc[::-1], use_container_width=True, hide_index=True)
    else:
        st.info("거래 없음")

with log_col2:
    st.caption("🟡 Rotation 봇")
    if os.path.exists(ROTATION_HISTORY_FILE):
        df_r = pd.read_csv(ROTATION_HISTORY_FILE)
        st.dataframe(df_r.tail(10).iloc[::-1], use_container_width=True, hide_index=True)
    else:
        st.info("거래 없음")

# ── 디버그 (기존 유지)
with st.expander("🛠️ 데이터 엔진 상태 점검 (Debug)", expanded=False):
    st.write(f"데이터 상태: {data_msg}")
    st.write(f"SPY Shape: {spy_ohlc.shape}")
    st.write(f"컬럼: {spy_ohlc.columns.tolist()}")
    st.write(f"Rotation 상태: {rot_state}")
    if not spy_ohlc.empty:
        st.write("SPY 최근 3일:", spy_ohlc.tail(3))
```

# ══════════════════════════════════════════════════════════════

# 10. 진입점

# ══════════════════════════════════════════════════════════════

if os.getenv(‘GITHUB_ACTIONS’) == ‘true’:
asyncio.run(run_trading())
else:
run_dashboard()
