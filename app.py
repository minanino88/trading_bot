"""
Unified Trading Bot v1.2.0
UPRO Trend Bot (70%) + Momentum Rotation Bot (30%)
원본 매매 로직(지정가 주문, Bot 텔레그램, 긴급매도) 완전 복원
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

try:
    from telegram import Bot
except ImportError:
    Bot = None

try:
    from google import genai
    GEMINI_OK = True
except ImportError:
    GEMINI_OK = False


warnings.filterwarnings('ignore')

# ==============================================================
# 1. 설정
# ==============================================================

KST = pytz.timezone('Asia/Seoul')

UPRO_RATIO     = 0.70
ROTATION_RATIO = 0.30

SIGNAL_TICKER = 'SPY'
TRADE_TICKER  = 'UPRO'
STATE_FILE    = 'trend_state.json'
HISTORY_FILE  = 'history_trend.csv'

CANDIDATE_POOL  = ['NVDA', 'TSLA', 'META', 'AAPL', 'MSFT', 'AMZN', 'GOOGL']
TOP_N           = 2
VIX_ENTER_MAX   = 25.0
SPY_6M_MIN      = 0.0
ROTATION_STATE_FILE   = 'rotation_state.json'
ROTATION_HISTORY_FILE = 'history_rotation.csv'

# ==============================================================
# 2. KIS API — 원본 지정가 주문 + price 인자 완전 복원
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

    def get_balance(self):
        if not self.token: return 0.0
        try:
            url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
            params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                      "OVRS_EXCG_CD": "AMEX", "OVRS_ORD_UNPR": "1",
                      "ITEM_CD": TRADE_TICKER}
            res = requests.get(url, headers=self._headers("JTTT3007R"), params=params).json()
            return float(res.get('output', {}).get('ord_psbl_frcr_amt', 0))
        except: return 0.0

    def get_holdings(self, ticker=TRADE_TICKER):
        if not self.token: return 0
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
        except: return 0

    def get_current_price(self, ticker=TRADE_TICKER):
        try:
            df = yf.download(ticker, period='1d', interval='1m', progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if not df.empty: return float(df['Close'].iloc[-1])
            return 0.0
        except: return 0.0

    def send_order(self, ticker, qty, price, side="BUY"):
        if not self.token: return {"rt_cd": "1", "msg1": "No Token"}
        try:
            url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
            tr_id = "TTTT1002U" if side == "BUY" else "TTTT1006U"
            clean_qty = str(int(float(qty)))
            target_price = price * 1.01 if side == "BUY" else price * 0.99
            clean_price  = f"{target_price:.2f}"
            data = {
                "CANO":            self.cano,
                "ACNT_PRDT_CD":    self.acnt_prdt_cd,
                "OVRS_EXCG_CD":    "AMEX",
                "PDNO":            ticker,
                "ORD_QTY":         clean_qty,
                "OVRS_ORD_UNPR":   clean_price,
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN":        "00",
            }
            return requests.post(url, headers=self._headers(tr_id),
                                 data=json.dumps(data)).json()
        except Exception as e: return {"rt_cd": "1", "msg1": str(e)}

# ==============================================================
# 3. 시장 데이터
# ==============================================================

def get_market_data():
    try:
        spy_raw = yf.download(SIGNAL_TICKER, period='2y', progress=False, auto_adjust=True, repair=True)
        if isinstance(spy_raw.columns, pd.MultiIndex):
            spy_raw.columns = spy_raw.columns.get_level_values(0)
        spy_raw   = spy_raw[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        spy_close = spy_raw['Close'].squeeze()
        monthly   = spy_close.resample('ME').last().pct_change().dropna()

        vix_raw = yf.download('^VIX', period='2y', progress=False, auto_adjust=True, repair=True)
        if isinstance(vix_raw.columns, pd.MultiIndex):
            vix_raw.columns = vix_raw.columns.get_level_values(0)
        vix_close = vix_raw['Close'].squeeze()

        close_all = {}
        for t in CANDIDATE_POOL:
            try:
                tmp = yf.download(t, period='2y', progress=False, auto_adjust=True, repair=True)
                if isinstance(tmp.columns, pd.MultiIndex):
                    tmp.columns = tmp.columns.get_level_values(0)
                close_all[t] = tmp['Close'].squeeze()
            except: pass

        return spy_raw, monthly, vix_close, close_all, "Success"
    except Exception as e:
        return pd.DataFrame(), pd.Series(), pd.Series(), {}, str(e)

# ==============================================================
# 4. 신호 로직
# ==============================================================


def get_upro_signal(spy_close, monthly, vix_close):
    state = {"in_market": True, "last_exit_price": 0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                content = f.read().strip()
            if content:
                state = json.loads(content)
        except:
            pass
        
    if spy_close.empty or len(spy_close) < 20:
        return "WAIT", "Loading", 0.0, state

    curr_p    = float(spy_close.iloc[-1])
    spy_daily = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
    vix_daily = float((vix_close.iloc[-1] / vix_close.iloc[-2]) - 1)
    spy_3day  = float((spy_close.iloc[-1] / spy_close.iloc[-4]) - 1) if len(spy_close) >= 4 else 0.0

    if vix_daily >= 0.3 or spy_daily <= -0.03 or spy_3day <= -0.05:
        return "EXIT", "Shock Trigger", curr_p, state

    if state.get('in_market', True):
        recent = monthly.tail(2).values
        if len(recent) == 2 and recent[0] < 0 and recent[1] < 0:
            return "EXIT", "2m Down", curr_p, state
        return "KEEP", "Holding", curr_p, state

    rebound  = (curr_p - state['last_exit_price']) / state['last_exit_price'] if state['last_exit_price'] > 0 else 0
    vix_now  = float(vix_close.iloc[-1])
    vix_prev = float(vix_close.iloc[-2])
    vix_20d  = vix_close.tail(20)
    vix_mean = float(vix_20d.mean())
    vix_std  = float(vix_20d.std())
    vix_rev  = (
        (vix_now > (vix_mean + 2 * vix_std) or vix_prev > (vix_mean + 2 * vix_std))
        and vix_now < vix_prev * 0.95
        and spy_daily > 0
    )
    if vix_rev:        return "RE-ENTER", "VIX Reversal", curr_p, state
    if rebound >= 0.02: return "RE-ENTER", "2% Rebound",  curr_p, state
    return "WAIT", f"Waiting({rebound*100:.1f}%)", curr_p, state

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
        spy_6m = float(spy_close.iloc[-1] / spy_close.iloc[-126] - 1) if len(spy_close) >= 126 else 0.0
        vix_now   = float(vix_close.iloc[-1])
        vix_prev  = float(vix_close.iloc[-2])

        scores = {t: calc_momentum(close_all[t]) for t in CANDIDATE_POOL if t in close_all}
        top2   = [t for t, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:TOP_N]]

        regime_ok = (spy_6m > SPY_6M_MIN and vix_now < VIX_ENTER_MAX)

        if rot_state['in_market']:
            current_holdings = set(h['ticker'] for h in rot_state['holdings'])
            if not regime_ok:              action = "EXIT"
            elif set(top2) != current_holdings: action = "ROTATE"
            else:                          action = "KEEP"
        else:
            action = "ENTER" if regime_ok else "WAIT"

        return {"action": action, "top2": top2, "scores": scores,
                "vix_now": vix_now, "spy_6m": spy_6m,
                "regime_ok": regime_ok, "spy_daily": spy_daily}
    except:
        return {"action": "WAIT", "top2": [], "vix_now": 0, "spy_6m": 0,
                "regime_ok": False, "spy_daily": 0, "scores": {}}

# ==============================================================
# 5. Gemini AI 분석
# ==============================================================

def ask_gemini(u_sig, r_sig):
    if not GEMINI_OK or not os.getenv("GEMINI_API_KEY"): return "AI분석 스킵"
    try:
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        p = f"UPRO {u_sig}, ROT {r_sig['action']}, TOP2 {r_sig.get('top2')}. Korean 150 chars."
        res = client.models.generate_content(model="gemini-1.5-flash", contents=p)
        return res.text.strip()
    except: return "AI분석 실패"


# ==============================================================
# 6. 성과 분석
# ==============================================================

def calc_upro_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0,
             "total_trades": 0, "win_trades": 0, "loss_trades": 0,
             "avg_profit": 0.0, "avg_loss": 0.0,
             "best_trade": 0.0, "worst_trade": 0.0, "equity_curve": []}
    if df is None or df.empty: return empty
    df = df.copy()
    df['Date']  = pd.to_datetime(df['Date'])
    df['Price'] = pd.to_numeric(df['Price'], errors='coerce').fillna(0)
    buys  = df[df['Action'] == 'BUY'].reset_index(drop=True)
    sells = df[df['Action'] == 'SELL'].reset_index(drop=True)
    trades, equity, equity_curve = [], 100.0, []
    if not buys.empty:
        equity_curve = [{"date": str(buys.loc[0, 'Date'].date()), "equity": 100.0}]
    for i in range(min(len(buys), len(sells))):
        buy_p, sell_p = float(buys.loc[i, 'Price']), float(sells.loc[i, 'Price'])
        if buy_p <= 0: continue
        ret = (sell_p - buy_p) / buy_p * 100
        trades.append(ret)
        equity *= (1 + ret / 100)
        equity_curve.append({"date": str(sells.loc[i, 'Date'].date()), "equity": round(equity, 2)})
    if not trades: return empty
    wins   = [r for r in trades if r > 0]
    losses = [r for r in trades if r <= 0]
    eq_vals = [e['equity'] for e in equity_curve]
    peak, mdd = eq_vals[0], 0.0
    for v in eq_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd: mdd = dd
    sharpe = round(np.mean(trades) / (np.std(trades) + 1e-9) * np.sqrt(12), 2) if len(trades) > 1 else 0.0
    return {"total_return": round(equity - 100, 2),
            "win_rate":    round(len(wins)/len(trades)*100, 1),
            "mdd": round(mdd, 2), "sharpe": sharpe,
            "total_trades": len(trades), "win_trades": len(wins), "loss_trades": len(losses),
            "avg_profit": round(np.mean(wins), 2) if wins else 0.0,
            "avg_loss":   round(np.mean(losses), 2) if losses else 0.0,
            "best_trade": round(max(trades), 2) if trades else 0.0, 
            "worst_trade": round(min(trades), 2) if trades else 0.0,
            "equity_curve": equity_curve}

def calc_rotation_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0,
             "total_trades": 0, "win_trades": 0, "loss_trades": 0,
             "avg_profit": 0.0, "avg_loss": 0.0,
             "best_trade": 0.0, "worst_trade": 0.0, "equity_curve": []}
    if df is None or df.empty or 'RetPct' not in df.columns: return empty
    sells = df[df['Action'] == 'SELL'].copy()
    if sells.empty: return empty
    sells['RetPct'] = pd.to_numeric(sells['RetPct'], errors='coerce').fillna(0)
    sells['Date']   = pd.to_datetime(sells['Date'])
    rets, equity = sells['RetPct'].tolist(), 100.0
    equity_curve = [{"date": str(sells.iloc[0]['Date'].date()), "equity": 100.0}]
    for _, row in sells.iterrows():
        equity *= (1 + row['RetPct'] / 100)
        equity_curve.append({"date": str(row['Date'].date()), "equity": round(equity, 2)})
    wins   = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    eq_vals = [e['equity'] for e in equity_curve]
    peak, mdd = eq_vals[0], 0.0
    for v in eq_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd: mdd = dd
    sharpe = round(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(12), 2) if len(rets) > 1 else 0.0
    return {"total_return": round(equity - 100, 2),
            "win_rate":    round(len(wins)/len(rets)*100, 1) if rets else 0.0,
            "mdd": round(mdd, 2), "sharpe": sharpe,
            "total_trades": len(rets), "win_trades": len(wins), "loss_trades": len(losses),
            "avg_profit": round(np.mean(wins), 2) if wins else 0.0,
            "avg_loss":   round(np.mean(losses), 2) if losses else 0.0,
            "best_trade": round(max(rets), 2) if rets else 0.0,
            "worst_trade": round(min(rets), 2) if rets else 0.0,
            "equity_curve": equity_curve}

# ==============================================================
# 7. 매매 실행 — 원본 로직 완전 복원
# ==============================================================

async def run_trading():
    now_kst      = dt.now(KST)
    current_hour = now_kst.hour
    trader  = KIS_Trader()
    token_v = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('CHAT_ID')
    bot = Bot(token=token_v) if (Bot and token_v) else None
    print(f"DEBUG: Bot={bot}, token_v={bool(token_v)}, chat_id={bool(chat_id)}")
    print(f"DEBUG: GITHUB_ACTIONS={os.getenv('GITHUB_ACTIONS')}")
    print(f"DEBUG: bal={trader.get_balance()}")


    spy_ohlc, monthly, vix_close, close_all, d_msg = get_market_data()
    if spy_ohlc.empty: 
        if bot:
            try:
                await bot.send_message(chat_id=chat_id, text="\n".join(msgs), parse_mode="HTML")
                print("텔레그램 전송 성공")
            except Exception as e:
                print(f"텔레그램 전송 실패: {e}")
        else:
            print("bot이 None - TELEGRAM_TOKEN 또는 CHAT_ID 확인 필요")
                return

    
    rot_state = load_rotation_state()
    u_sig, u_re, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_sig = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state)

    if current_hour == 20 or os.getenv('GITHUB_ACTIONS') == 'true':
        bal  = trader.get_balance()
        msgs = [f"🤖 <b>통합봇 [{now_kst.strftime('%H:%M')} KST]</b>",
                f"잔고: ${bal:,.2f}"]

        upro_budget  = bal * UPRO_RATIO
        cur_p_upro   = trader.get_current_price(TRADE_TICKER)
        qty_upro     = trader.get_holdings(TRADE_TICKER)

        if u_sig in ["KEEP", "RE-ENTER"] and qty_upro == 0 and cur_p_upro > 0:
            buy_qty = int((upro_budget * 0.95) / cur_p_upro)
            if buy_qty >= 1:
                res = trader.send_order(TRADE_TICKER, buy_qty, cur_p_upro, "BUY")
                if res.get('rt_cd') == '0':
                    msgs.append(f"✅ UPRO 매수: {buy_qty}주 @ ${cur_p_upro:.2f}")
                    with open(STATE_FILE, 'w') as f:
                        json.dump({"in_market": True, "last_exit_price": 0}, f)
                    pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                                   "Action": "BUY", "Qty": buy_qty, "Price": cur_p_upro}]
                                ).to_csv(HISTORY_FILE, mode='a',
                                         header=not os.path.exists(HISTORY_FILE), index=False)
                else:
                    msgs.append(f"❌ UPRO 매수실패: {str(res)[:150]}")

        elif u_sig == "EXIT" and qty_upro > 0:
            res = trader.send_order(TRADE_TICKER, qty_upro, cur_p_upro, "SELL")
            if res.get('rt_cd') == '0':
                msgs.append(f"✅ UPRO 매도: {qty_upro}주 @ ${cur_p_upro:.2f}")
                with open(STATE_FILE, 'w') as f:
                    json.dump({"in_market": False, "last_exit_price": u_p}, f)
                pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                               "Action": "SELL", "Qty": qty_upro, "Price": cur_p_upro}]
                            ).to_csv(HISTORY_FILE, mode='a',
                                     header=not os.path.exists(HISTORY_FILE), index=False)
            else:
                msgs.append(f"❌ UPRO 매도실패: {str(res)[:150]}")
        else:
            msgs.append(f"UPRO: {u_sig} ({u_re})")

        rot_budget = bal * ROTATION_RATIO
        action     = r_sig['action']
        top2       = r_sig['top2']

        if action in ["ENTER", "ROTATE"]:
            if rot_state['in_market']:
                for h in rot_state['holdings']:
                    cur_p_h = trader.get_current_price(h['ticker'])
                    qty_h   = trader.get_holdings(h['ticker'])
                    if qty_h > 0:
                        res     = trader.send_order(h['ticker'], qty_h, cur_p_h, "SELL")
                        ret_pct = (cur_p_h - h.get('entry_price', cur_p_h)) / \
                                  h.get('entry_price', cur_p_h) * 100
                        status  = "✅" if res.get('rt_cd') == '0' else "❌"
                        msgs.append(f"{status} ROT 매도 {h['ticker']}: {qty_h}주 ({ret_pct:+.1f}%)")
                        pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                                       "Action": "SELL", "Ticker": h['ticker'],
                                       "Qty": qty_h, "Price": cur_p_h,
                                       "RetPct": round(ret_pct, 2)}]
                                    ).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                             header=not os.path.exists(ROTATION_HISTORY_FILE),
                                             index=False)

            new_holdings = []
            per_stock = rot_budget / len(top2) if top2 else 0
            for ticker in top2:
                cur_p_t = trader.get_current_price(ticker)
                if cur_p_t <= 0: continue
                buy_qty = int((per_stock * 0.95) / cur_p_t)
                if buy_qty >= 1:
                    res    = trader.send_order(ticker, buy_qty, cur_p_t, "BUY")
                    status = "✅" if res.get('rt_cd') == '0' else "❌"
                    msgs.append(f"{status} ROT 매수 {ticker}: {buy_qty}주 @ ${cur_p_t:.2f}")
                    new_holdings.append({"ticker": ticker, "qty": buy_qty, "entry_price": cur_p_t})
                    pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                                   "Action": "BUY", "Ticker": ticker,
                                   "Qty": buy_qty, "Price": cur_p_t, "RetPct": 0}]
                                ).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                         header=not os.path.exists(ROTATION_HISTORY_FILE),
                                         index=False)

            rot_state['in_market'] = True
            rot_state['holdings']  = new_holdings
            rot_state['entry_date'] = now_kst.strftime("%Y-%m-%d")
            save_rotation_state(rot_state)

        elif action == "EXIT" and rot_state['in_market']:
            for h in rot_state['holdings']:
                cur_p_h = trader.get_current_price(h['ticker'])
                qty_h   = trader.get_holdings(h['ticker'])
                if qty_h > 0:
                    res     = trader.send_order(h['ticker'], qty_h, cur_p_h, "SELL")
                    ret_pct = (cur_p_h - h.get('entry_price', cur_p_h)) / \
                              h.get('entry_price', cur_p_h) * 100
                    status  = "✅" if res.get('rt_cd') == '0' else "❌"
                    msgs.append(f"{status} ROT 청산 {h['ticker']}: {qty_h}주 ({ret_pct:+.1f}%)")
                    pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"),
                                   "Action": "SELL", "Ticker": h['ticker'],
                                   "Qty": qty_h, "Price": cur_p_h,
                                   "RetPct": round(ret_pct, 2)}]
                                ).to_csv(ROTATION_HISTORY_FILE, mode='a',
                                         header=not os.path.exists(ROTATION_HISTORY_FILE),
                                         index=False)
            rot_state['in_market'] = False
            rot_state['holdings']  = []
            save_rotation_state(rot_state)
        else:
            msgs.append(f"ROT: {action} | TOP2: {top2}")

        msgs.append(f"🧠 AI: {ask_gemini(u_sig, r_sig)}")
        if bot: await bot.send_message(chat_id=chat_id, text="\n".join(msgs), parse_mode="HTML")

    elif current_hour == 1:
        spy_int = yf.download(SIGNAL_TICKER, period='1d', interval='5m', progress=False)
        if not spy_int.empty:
            if isinstance(spy_int.columns, pd.MultiIndex):
                spy_int.columns = spy_int.columns.get_level_values(0)
            day_ret = (float(spy_int['Close'].iloc[-1]) / float(spy_int['Open'].iloc[0])) - 1
            if day_ret <= -0.03:
                qty_upro   = trader.get_holdings(TRADE_TICKER)
                cur_p_upro = trader.get_current_price(TRADE_TICKER)
                if qty_upro > 0:
                    res = trader.send_order(TRADE_TICKER, qty_upro, cur_p_upro, "SELL")
                    with open(STATE_FILE, 'w') as f:
                        json.dump({"in_market": False, "last_exit_price": float(spy_int['Close'].iloc[-1])}, f)
                    if bot: await bot.send_message(chat_id=chat_id, text=f"🚨 [01:00 긴급] UPRO 전량 매도\n결과: {str(res)[:200]}")

                rot_state = load_rotation_state()
                if rot_state['in_market']:
                    for h in rot_state['holdings']:
                        cur_p_h = trader.get_current_price(h['ticker'])
                        qty_h   = trader.get_holdings(h['ticker'])
                        if qty_h > 0: trader.send_order(h['ticker'], qty_h, cur_p_h, "SELL")
                    rot_state['in_market'] = False
                    rot_state['holdings']  = []
                    save_rotation_state(rot_state)
                    if bot: await bot.send_message(chat_id=chat_id, text="🚨 [01:00 긴급] ROT 종목 전량 청산 완료")

# ==============================================================
# 8. Streamlit 대시보드
# ==============================================================

def run_dashboard():
    now_kst = dt.now(KST)
    st.set_page_config(page_title="Unified Bot v1.2", layout="wide", page_icon="🤖")
    spy_ohlc, monthly, vix_close, close_all, data_msg = get_market_data()
    rot_state = load_rotation_state()
    if spy_ohlc.empty:
        st.error(f"데이터 로드 실패: {data_msg}")
        return

    u_signal, u_reason, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_signal = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state)

    df_upro   = pd.read_csv(HISTORY_FILE)           if os.path.exists(HISTORY_FILE)           else pd.DataFrame()
    df_rot    = pd.read_csv(ROTATION_HISTORY_FILE)  if os.path.exists(ROTATION_HISTORY_FILE)  else pd.DataFrame()
    upro_perf = calc_upro_performance(df_upro)
    rot_perf  = calc_rotation_performance(df_rot)

    st.sidebar.title("Unified Bot v1.2")
    st.sidebar.caption(f"Update: {now_kst.strftime('%H:%M:%S')} KST")
    st.sidebar.divider()
    st.sidebar.write("**UPRO EXIT:** VIX+30%, SPY-3%, 3d-5%, 2m연속하락")
    st.sidebar.write("**UPRO ENTER:** VIX Reversal, +2% Rebound")
    st.sidebar.write("**ROT 조건:** SPY 6m>0 & VIX<25")

    st.title("🤖 Unified Trading Bot")
    tab1, tab2, tab3 = st.tabs(["📡 실시간 현황", "📊 성과 분석", "📋 거래 로그"])

    with tab1:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("UPRO Position", "IN" if u_st.get('in_market') else "OUT", u_signal)
        c2.metric("ROT Action",    r_signal['action'], f"TOP2: {', '.join(r_signal['top2'])}" if r_signal['top2'] else "-")
        c3.metric("SPY Price",     f"${spy_ohlc['Close'].iloc[-1]:.2f}")
        c4.metric("VIX",           f"{r_signal['vix_now']:.1f}")
        c5.metric("SPY 6M",        f"{r_signal['spy_6m']*100:+.1f}%")

        if u_signal == "KEEP":   st.success(f"[UPRO] {u_reason}")
        elif u_signal == "EXIT": st.error(f"[UPRO 긴급] {u_reason}")
        else:                    st.info(f"[UPRO] {u_reason}")

        spy126 = spy_ohlc.tail(126)
        vix126 = vix_close.tail(126)
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.5, 0.25, 0.25], vertical_spacing=0.04)
        fig.add_trace(go.Candlestick(x=spy126.index, open=spy126['Open'], high=spy126['High'], low=spy126['Low'], close=spy126['Close'], name="SPY"), row=1, col=1)
        fig.add_trace(go.Bar(x=vix126.index, y=vix126.values, name="VIX", marker_color="orange"), row=2, col=1)
        fig.add_trace(go.Bar(x=spy126.index, y=spy126['Volume'], name="Volume", marker_color="steelblue"), row=3, col=1)
        fig.update_layout(xaxis_rangeslider_visible=False, height=650, template="plotly_dark", margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)

        if r_signal.get('scores'):
            st.subheader("📈 모멘텀 스코어")
            sc_df = pd.DataFrame([{"Ticker": t, "Score": s, "TOP2": "⭐" if t in r_signal['top2'] else ""} for t, s in sorted(r_signal['scores'].items(), key=lambda x: x[1], reverse=True)])
            st.dataframe(sc_df, use_container_width=True, hide_index=True)

    with tab2:
        for label, perf in [("UPRO 트렌드 (70%)", upro_perf), ("모멘텀 로테이션 (30%)", rot_perf)]:
            st.subheader(label)
            k1, k2, k3, k4, k5, k6 = st.columns(6)
            k1.metric("총 수익률",      f"{perf['total_return']:+.1f}%")
            k2.metric("MDD",            f"-{perf['mdd']:.1f}%")
            k3.metric("승률",           f"{perf['win_rate']:.0f}%")
            k4.metric("샤프",           f"{perf['sharpe']:.2f}")
            k5.metric("거래 횟수",      f"{perf['total_trades']}회")
            k6.metric("최대수익/손실",  f"{perf['best_trade']:+.1f}% / {perf['worst_trade']:+.1f}%")
            if perf['equity_curve']:
                eq_df  = pd.DataFrame(perf['equity_curve'])
                fig_eq = go.Figure(go.Scatter(x=eq_df['date'], y=eq_df['equity'], fill='tozeroy', line=dict(color='#3fb950')))
                fig_eq.update_layout(template='plotly_dark', height=250, margin=dict(t=10, b=10), yaxis_title="Equity")
                st.plotly_chart(fig_eq, use_container_width=True)
            st.divider()

    with tab3:
        st.subheader("UPRO 거래 로그")
        if not df_upro.empty: st.dataframe(df_upro.tail(20), use_container_width=True, hide_index=True)
        else: st.info("거래 기록 없음")
        st.subheader("로테이션 거래 로그")
        if not df_rot.empty: st.dataframe(df_rot.tail(20), use_container_width=True, hide_index=True)
        else: st.info("거래 기록 없음")

# ==============================================================
# 9. 진입점
# ==============================================================

if __name__ == "__main__":
    if os.getenv('GITHUB_ACTIONS') == 'true':
        asyncio.run(run_trading())
    else:
        run_dashboard()
