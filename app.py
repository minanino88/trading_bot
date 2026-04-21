"""
Unified Trading Bot v1.1.0
UPRO Trend Bot (70%) + Momentum Rotation Bot (30%)
"""

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

warnings.filterwarnings('ignore')

# ==============================================================
# 1. 설정
# ==============================================================

KST = pytz.timezone('Asia/Seoul')

# 자본 배분 (합계 = 1.0)
UPRO_RATIO     = 0.70
ROTATION_RATIO = 0.30

# UPRO 봇
SIGNAL_TICKER = 'SPY'
TRADE_TICKER  = 'UPRO'
STATE_FILE    = 'trend_state.json'
HISTORY_FILE  = 'history_trend.csv'

# Rotation 봇
CANDIDATE_POOL        = ['NVDA', 'TSLA', 'META', 'AAPL', 'MSFT', 'AMZN', 'GOOGL']
TOP_N                 = 2
VIX_ENTER_MAX         = 25.0
SPY_6M_MIN            = 0.0
VIX_EXIT_SPIKE        = 0.30
SPY_DAILY_EXIT        = -0.03
STOCK_HARD_STOP       = -0.07
ROTATION_STATE_FILE   = 'rotation_state.json'
ROTATION_HISTORY_FILE = 'history_rotation.csv'

# ==============================================================
# 2. KIS API (검증 규격 유지)
# ==============================================================

class KIS_Trader:
    def __init__(self):
        self.base_url     = "https://openapi.koreainvestment.com:9443"
        self.app_key      = os.getenv('KIS_APPKEY')
        self.app_secret   = os.getenv('KIS_SECRET')
        self.cano         = os.getenv('KIS_CANO')
        self.acnt_prdt_cd = os.getenv('KIS_ACNT_PRDT_CD', '01')
        self.token        = None
        self.error_detail = "Initial"
        self._set_token()

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

# ==============================================================
# 3. 시장 데이터
# ==============================================================

def get_market_data():
    try:
        spy_raw = yf.download(SIGNAL_TICKER, period='2y', progress=False,
                              auto_adjust=True, repair=True)
        if isinstance(spy_raw.columns, pd.MultiIndex):
            spy_raw.columns = spy_raw.columns.get_level_values(0)
        spy_raw   = spy_raw[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        spy_close = spy_raw['Close'].squeeze()
        monthly   = spy_close.resample('ME').last().pct_change().dropna()

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

# ==============================================================
# 4. UPRO 봇 신호 (v3.6.9 원본 그대로)
# ==============================================================

def get_upro_signal(spy_close, monthly, vix_close):
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
    else:
        state = {"in_market": True, "last_exit_price": 0}

    if spy_close.empty or len(spy_close) < 20:
        return "WAIT", "Loading", 0.0, state

    curr_p        = float(spy_close.iloc[-1])
    spy_daily_ret = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
    vix_daily_ret = float((vix_close.iloc[-1] / vix_close.iloc[-2]) - 1)
    spy_3day_ret  = float((spy_close.iloc[-1] / spy_close.iloc[-4]) - 1) if len(spy_close) >= 4 else 0.0

    if vix_daily_ret >= 0.3 or spy_daily_ret <= -0.03 or spy_3day_ret <= -0.05:
        return "EXIT", "Shock Trigger", curr_p, state

    if state.get('in_market', True):
        recent = monthly.tail(2).values
        if len(recent) == 2 and recent[0] < 0 and recent[1] < 0:
            return "EXIT", "2m Down", curr_p, state
        return "KEEP", "Holding", curr_p, state
    else:
        rebound = (curr_p - state['last_exit_price']) / state['last_exit_price'] if state['last_exit_price'] > 0 else 0
        vix_now  = float(vix_close.iloc[-1])
        vix_prev = float(vix_close.iloc[-2])
        vix_20d  = vix_close.tail(20)
        vix_mean = float(vix_20d.mean())
        vix_std  = float(vix_20d.std())
        vix_rev  = ((vix_now > (vix_mean + 2 * vix_std) or vix_prev > (vix_mean + 2 * vix_std)) and vix_now < vix_prev * 0.95 and spy_daily_ret > 0)
        if vix_rev:
            return "RE-ENTER", "VIX Reversal", curr_p, state
        if rebound >= 0.02:
            return "RE-ENTER", "2% Rebound", curr_p, state
        return "WAIT", f"Waiting({rebound*100:.1f}%)", curr_p, state

# ==============================================================
# 5. Rotation 봇 신호
# ==============================================================

def load_rotation_state():
    default = {"in_market": False, "holdings": [], "entry_date": None, "consecutive_loss": 0}
    if os.path.exists(ROTATION_STATE_FILE):
        try:
            with open(ROTATION_STATE_FILE) as f:
                saved = json.load(f)
            for k, v in default.items():
                if k not in saved: saved[k] = v
            return saved
        except: pass
    return default

def save_rotation_state(state):
    with open(ROTATION_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def calc_momentum(series):
    try:
        s = series.dropna()
        if len(s) < 130: return -999.0
        m1 = float(s.iloc[-1] / s.iloc[-21]  - 1) * 100
        m3 = float(s.iloc[-1] / s.iloc[-63]  - 1) * 100
        m6 = float(s.iloc[-1] / s.iloc[-126] - 1) * 100
        return round(m1 * 0.2 + m3 * 0.3 + m6 * 0.5, 2)
    except: return -999.0

def get_rotation_signal(spy_close, vix_close, close_all, rot_state):
    try:
        spy_daily = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
        spy_6m    = float((spy_close.iloc[-1] / spy_close.iloc[-126]) - 1) if len(spy_close) >= 126 else 0.0
        vix_now   = float(vix_close.iloc[-1])
        vix_prev  = float(vix_close.iloc[-2])
        vix_daily = float((vix_now - vix_prev) / vix_prev)

        scores = {t: calc_momentum(close_all[t]) for t in CANDIDATE_POOL if t in close_all}
        top2 = [t for t, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:TOP_N]]

        hard_stop = False
        if rot_state['in_market']:
            for h in rot_state['holdings']:
                t = h['ticker']
                if t in close_all and len(close_all[t]) >= 2:
                    daily = float((close_all[t].iloc[-1] / close_all[t].iloc[-2]) - 1)
                    if daily <= STOCK_HARD_STOP: hard_stop = True

        vix_reversal = False
        if len(vix_close) >= 3:
            vix_2prev = float(vix_close.iloc[-3])
            if (vix_prev > vix_2prev * 1.20 and vix_now < vix_prev * 0.92 and spy_daily > 0):
                vix_reversal = True

        regime_ok = (spy_6m > SPY_6M_MIN and vix_now < VIX_ENTER_MAX) or vix_reversal
        exit_cond = (vix_daily >= VIX_EXIT_SPIKE or spy_daily <= SPY_DAILY_EXIT or hard_stop or rot_state['consecutive_loss'] >= 2 or not regime_ok)

        current = [h['ticker'] for h in rot_state.get('holdings', [])]
        if rot_state['in_market']:
            if exit_cond: action = "EXIT"
            elif set(top2) != set(current): action = "ROTATE"
            else: action = "KEEP"
        else: action = "ENTER" if regime_ok else "WAIT"

        return {"action": action, "top2": top2, "scores": scores, "spy_daily": round(spy_daily * 100, 2), "spy_6m": round(spy_6m * 100, 2), "vix_now": round(vix_now, 2), "vix_daily": round(vix_daily * 100, 2), "regime_ok": regime_ok, "hard_stop": hard_stop, "vix_reversal": vix_reversal}
    except Exception as e: return {"action": "WAIT", "top2": [], "scores": {}, "error": str(e)}

# ==============================================================
# 6. 성과 분석
# ==============================================================

def calc_upro_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0, "total_trades": 0, "win_trades": 0, "loss_trades": 0, "avg_profit": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0, "equity_curve": [], "monthly_ret": []}
    if df is None or df.empty: return empty
    df = df.copy()
    df['Date']  = pd.to_datetime(df['Date'])
    df['Price'] = pd.to_numeric(df['Price'], errors='coerce').fillna(0)
    buys  = df[df['Action'] == 'BUY'].reset_index(drop=True)
    sells = df[df['Action'] == 'SELL'].reset_index(drop=True)
    trades, equity, equity_curve = [], 100.0, []
    if not buys.empty: equity_curve = [{"date": str(buys.loc[0, 'Date'].date()), "equity": 100.0}]
    for i in range(min(len(buys), len(sells))):
        buy_p, sell_p = float(buys.loc[i, 'Price']), float(sells.loc[i, 'Price'])
        if buy_p <= 0: continue
        ret = (sell_p - buy_p) / buy_p * 100
        trades.append({"return_pct": round(ret, 2)})
        equity *= (1 + ret / 100)
        equity_curve.append({"date": str(sells.loc[i, 'Date'].date()), "equity": round(equity, 2)})
    if not trades: return empty
    rets = [t['return_pct'] for t in trades]
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    eq_vals = [e['equity'] for e in equity_curve]
    peak, mdd = eq_vals[0] if eq_vals else 100.0, 0.0
    for v in eq_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd: mdd = dd
    sharpe = round(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(12), 2) if len(rets) > 1 else 0.0
    return {"total_return": round(equity - 100, 2), "win_rate": round(len(wins) / len(trades) * 100, 1), "mdd": round(mdd, 2), "sharpe": sharpe, "total_trades": len(trades), "win_trades": len(wins), "loss_trades": len(losses), "avg_profit": round(np.mean(wins), 2) if wins else 0.0, "avg_loss": round(np.mean(losses), 2) if losses else 0.0, "best_trade": round(max(rets), 2) if rets else 0.0, "worst_trade": round(min(rets), 2) if rets else 0.0, "equity_curve": equity_curve, "monthly_ret": []}

def calc_rotation_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0, "total_trades": 0, "win_trades": 0, "loss_trades": 0, "avg_profit": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0, "equity_curve": [], "monthly_ret": []}
    if df is None or df.empty or 'RetPct' not in df.columns: return empty
    sells = df[df['Action'] == 'SELL'].copy()
    if sells.empty: return empty
    sells['RetPct'] = pd.to_numeric(sells['RetPct'], errors='coerce').fillna(0)
    sells['Date'] = pd.to_datetime(sells['Date'])
    rets, equity, equity_curve = sells['RetPct'].tolist(), 100.0, [{"date": str(sells.iloc[0]['Date'].date()), "equity": 100.0}]
    for _, row in sells.iterrows():
        equity *= (1 + row['RetPct'] / 100)
        equity_curve.append({"date": str(row['Date'].date()), "equity": round(equity, 2)})
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    eq_vals = [e['equity'] for e in equity_curve]
    peak, mdd = eq_vals[0] if eq_vals else 100.0, 0.0
    for v in eq_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd: mdd = dd
    sharpe = round(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(12), 2) if len(rets) > 1 else 0.0
    return {"total_return": round(equity - 100, 2), "win_rate": round(len(wins) / len(rets) * 100, 1) if rets else 0.0, "mdd": round(mdd, 2), "sharpe": sharpe, "total_trades": len(rets), "win_trades": len(wins), "loss_trades": len(losses), "avg_profit": round(np.mean(wins), 2) if wins else 0.0, "avg_loss": round(np.mean(losses), 2) if losses else 0.0, "best_trade": round(max(rets), 2) if rets else 0.0, "worst_trade": round(min(rets), 2) if rets else 0.0, "equity_curve": equity_curve, "monthly_ret": []}

# ==============================================================
# 7. Gemini & Telegram
# ==============================================================

def ask_gemini(upro_signal, rot_signal):
    if not GEMINI_OK: return "google-generativeai not installed"
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key: return "GEMINI_API_KEY not set"
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (f"You are a quant trading expert. Analyze in Korean within 200 chars.\nUPRO: {upro_signal}\nRotation: {rot_signal.get('action')}, top2={rot_signal.get('top2')}, SPY6M={rot_signal.get('spy_6m')}, VIX={rot_signal.get('vix_now')}")
        return model.generate_content(prompt).text.strip()
    except Exception as e: return f"Gemini error: {str(e)[:50]}"

async def send_telegram(msg):
    token, chat_id = os.getenv('TELEGRAM_TOKEN', ''), os.getenv('CHAT_ID', '')
    if not token or not chat_id: return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass

# ==============================================================
# 9. 자동매매 실행
# ==============================================================

async def run_trading():
    now_kst = dt.now(KST)
    trader, rot_state = KIS_Trader(), load_rotation_state()
    total_bal = trader.get_total_balance()
    upro_budget, rotation_budget = total_bal * UPRO_RATIO, total_bal * ROTATION_RATIO
    spy_ohlc, monthly, vix_close, close_all, data_msg = get_market_data()
    if spy_ohlc.empty: return
    spy_close, today = spy_ohlc['Close'], dt.now(KST).strftime('%Y-%m-%d')

    if now_kst.hour == 20:
        # [A] UPRO
        u_sig, u_re, u_p, u_st = get_upro_signal(spy_close, monthly, vix_close)
        u_qty, u_curr_p, u_msg = trader.get_holdings_qty(TRADE_TICKER), trader.get_current_price(TRADE_TICKER), ""
        if u_sig in ["KEEP", "RE-ENTER"] and u_qty == 0 and u_curr_p > 0:
            buy_qty = int((upro_budget * 0.95) / u_curr_p)
            if buy_qty >= 1 and trader.send_order(TRADE_TICKER, buy_qty, "BUY").get('rt_cd') == '0':
                u_msg, u_st['in_market'], u_st['last_exit_price'] = f"BUY {buy_qty}@{u_curr_p}", True, 0
                with open(STATE_FILE, 'w') as f: json.dump(u_st, f)
                pd.DataFrame([{"Date": today, "Action": "BUY", "Qty": buy_qty, "Price": u_curr_p, "Strategy": "UPRO"}]).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
        elif u_sig == "EXIT" and u_qty > 0:
            if trader.send_order(TRADE_TICKER, u_qty, "SELL").get('rt_cd') == '0':
                u_msg, u_st['in_market'], u_st['last_exit_price'] = f"SELL {u_qty}@{u_curr_p}", False, u_p
                with open(STATE_FILE, 'w') as f: json.dump(u_st, f)
                pd.DataFrame([{"Date": today, "Action": "SELL", "Qty": u_qty, "Price": u_curr_p, "Strategy": "UPRO"}]).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
        else: u_msg = f"{u_sig} ({u_re})"

        # [B] Rotation
        r_sig = get_rotation_signal(spy_close, vix_close, close_all, rot_state)
        r_act, top2, r_msg = r_sig['action'], r_sig['top2'], ""
        if r_act == "ENTER" and not rot_state['in_market'] and top2:
            per, new_h = (rotation_budget * 0.95) / len(top2), []
            for t in top2:
                p = trader.get_current_price(t)
                qty = int(per / p) if p > 0 else 0
                if qty >= 1 and trader.send_order(t, qty, "BUY").get('rt_cd') == '0':
                    new_h.append({"ticker": t, "buy_price": p, "shares": qty})
                    pd.DataFrame([{"Date": today, "Action": "BUY", "Ticker": t, "Qty": qty, "Price": p, "Strategy": "ROTATION", "RetPct": 0}]).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
            if new_h: rot_state['in_market'], rot_state['holdings'], rot_state['entry_date'], r_msg = True, new_h, today, f"ENTER: {len(new_h)} stocks"
            save_rotation_state(rot_state)
        elif r_act == "EXIT" and rot_state['in_market']:
            rets = []
            for h in rot_state['holdings']:
                q, p = trader.get_holdings_qty(h['ticker']), trader.get_current_price(h['ticker'])
                if q > 0 and trader.send_order(h['ticker'], q, "SELL").get('rt_cd') == '0':
                    ret = (p - h['buy_price']) / h['buy_price'] * 100
                    rets.append(ret)
                    pd.DataFrame([{"Date": today, "Action": "SELL", "Ticker": h['ticker'], "Qty": q, "Price": p, "Strategy": "ROTATION", "RetPct": round(ret, 2)}]).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
            rot_state['in_market'], rot_state['holdings'], r_msg = False, [], "EXIT COMPLETE"
            save_rotation_state(rot_state)
        else: r_msg = f"{r_act}"

        gem_txt = ask_gemini(f"{u_sig}", r_sig)
        await send_telegram(f"<b>Trade Report</b>\nUPRO: {u_msg}\nROT: {r_msg}\nGemini: {gem_txt}")

    elif now_kst.hour == 1: # 긴급탈출 로직 (생략 없이 유지)
        u_sig, _, u_p, u_st = get_upro_signal(spy_close, monthly, vix_close)
        if u_sig == "EXIT":
            q = trader.get_holdings_qty(TRADE_TICKER)
            if q > 0 and trader.send_order(TRADE_TICKER, q, "SELL").get('rt_cd') == '0':
                u_st['in_market'], u_st['last_exit_price'] = False, u_p
                with open(STATE_FILE, 'w') as f: json.dump(u_st, f)
                await send_telegram("🚨 UPRO EMERGENCY EXIT")

# ==============================================================
# 10. Streamlit 대시보드
# ==============================================================

def run_dashboard():
    st.set_page_config(page_title="Unified Bot", layout="wide")
    spy_ohlc, monthly, vix_close, close_all, _ = get_market_data()
    if spy_ohlc.empty: return
    rot_state = load_rotation_state()
    u_sig, u_re, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_sig = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state)
    
    st.title("🤖 Unified Bot v1.1")
    c1, c2 = st.columns(2)
    c1.metric("UPRO Signal", u_sig, u_re)
    c2.metric("ROT Signal", r_sig['action'], f"VIX: {r_sig['vix_now']}")
    
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True)
    fig.add_trace(go.Candlestick(x=spy_ohlc.index, open=spy_ohlc['Open'], high=spy_ohlc['High'], low=spy_ohlc['Low'], close=spy_ohlc['Close']), row=1, col=1)
    fig.add_trace(go.Bar(x=vix_close.index, y=vix_close.values), row=2, col=1)
    fig.update_layout(xaxis_rangeslider_visible=False, height=600, template="plotly_dark")
    st.plotly_chart(fig, use_container_width=True)

if __name__ == "__main__":
    if os.getenv('GITHUB_ACTIONS') == 'true': asyncio.run(run_trading())
    else: run_dashboard()
