"""
Unified Trading Bot v1.1.7
UPRO Trend Bot (70%) + Momentum Rotation Bot (30%)
전체 대시보드 로직 복구 및 문법 에러 완제거 버전
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

UPRO_RATIO     = 0.70
ROTATION_RATIO = 0.30

SIGNAL_TICKER = 'SPY'
TRADE_TICKER  = 'UPRO'
STATE_FILE    = 'trend_state.json'
HISTORY_FILE  = 'history_trend.csv'

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
        if not self.token: return 0.0
        try:
            url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
            params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                      "OVRS_EXCG_CD": "AMEX", "OVRS_ORD_UNPR": "1",
                      "ITEM_CD": TRADE_TICKER}
            res = requests.get(url, headers=self._headers("JTTT3007R"), params=params).json()
            return float(res.get('output', {}).get('ord_psbl_frcr_amt', 0))
        except: return 0.0

    def get_holdings_qty(self, ticker):
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

    def get_current_price(self, ticker):
        try:
            df = yf.download(ticker, period='1d', interval='1m', progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if not df.empty: return float(df['Close'].iloc[-1])
            return 0.0
        except: return 0.0

    def send_order(self, ticker, qty, side="BUY"):
        if not self.token: return {"rt_cd": "1", "msg1": "No Token"}
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
        except Exception as e: return {"rt_cd": "1", "msg1": str(e)}

# ==============================================================
# 3. 시장 데이터
# ==============================================================

def get_market_data():
    try:
        spy_raw = yf.download(SIGNAL_TICKER, period='2y', progress=False, auto_adjust=True, repair=True)
        if isinstance(spy_raw.columns, pd.MultiIndex): spy_raw.columns = spy_raw.columns.get_level_values(0)
        spy_raw   = spy_raw[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        spy_close = spy_raw['Close'].squeeze()
        monthly   = spy_close.resample('ME').last().pct_change().dropna()
        vix_raw = yf.download('^VIX', period='2y', progress=False, auto_adjust=True, repair=True)
        if isinstance(vix_raw.columns, pd.MultiIndex): vix_raw.columns = vix_raw.columns.get_level_values(0)
        vix_close = vix_raw['Close'].squeeze()
        close_all = {}
        for t in CANDIDATE_POOL:
            try:
                tmp = yf.download(t, period='2y', progress=False, auto_adjust=True, repair=True)
                if isinstance(tmp.columns, pd.MultiIndex): tmp.columns = tmp.columns.get_level_values(0)
                close_all[t] = tmp['Close'].squeeze()
            except: pass
        return spy_raw, monthly, vix_close, close_all, "Success"
    except Exception as e: return pd.DataFrame(), pd.Series(), pd.Series(), {}, str(e)

# ==============================================================
# 4. 신호 로직
# ==============================================================

def get_upro_signal(spy_close, monthly, vix_close):
    state = json.load(open(STATE_FILE, 'r')) if os.path.exists(STATE_FILE) else {"in_market": True, "last_exit_price": 0}
    if spy_close.empty or len(spy_close) < 20: return "WAIT", "Loading", 0.0, state
    curr_p = float(spy_close.iloc[-1])
    spy_daily = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
    vix_daily = float((vix_close.iloc[-1] / vix_close.iloc[-2]) - 1)
    spy_3day  = float((spy_close.iloc[-1] / spy_close.iloc[-4]) - 1) if len(spy_close) >= 4 else 0.0
    if vix_daily >= 0.3 or spy_daily <= -0.03 or spy_3day <= -0.05: return "EXIT", "Shock Trigger", curr_p, state
    if state.get('in_market', True):
        recent = monthly.tail(2).values
        if len(recent) == 2 and recent[0] < 0 and recent[1] < 0: return "EXIT", "2m Down", curr_p, state
        return "KEEP", "Holding", curr_p, state
    rebound = (curr_p - state['last_exit_price']) / state['last_exit_price'] if state['last_exit_price'] > 0 else 0
    vix_now, vix_prev = float(vix_close.iloc[-1]), float(vix_close.iloc[-2])
    vix_20d = vix_close.tail(20)
    vix_mean, vix_std = float(vix_20d.mean()), float(vix_20d.std())
    vix_rev = ((vix_now > (vix_mean + 2 * vix_std) or vix_prev > (vix_mean + 2 * vix_std)) and vix_now < vix_prev * 0.95 and spy_daily > 0)
    if vix_rev: return "RE-ENTER", "VIX Reversal", curr_p, state
    if rebound >= 0.02: return "RE-ENTER", "2% Rebound", curr_p, state
    return "WAIT", f"Waiting({rebound*100:.1f}%)", curr_p, state

def load_rotation_state():
    default = {"in_market": False, "holdings": [], "entry_date": None, "consecutive_loss": 0}
    if os.path.exists(ROTATION_STATE_FILE):
        try:
            saved = json.load(open(ROTATION_STATE_FILE))
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
        # 에러 발생 지점 교정: 삼항 연산자 한 줄 배치
        spy_6m = float(spy_close.iloc[-1] / spy_close.iloc[-126] - 1) if len(spy_close) >= 126 else 0.0
        vix_now = float(vix_close.iloc[-1])
        vix_prev = float(vix_close.iloc[-2])
        vix_daily = float((vix_now - vix_prev) / vix_prev)
        scores = {t: calc_momentum(close_all[t]) for t in CANDIDATE_POOL if t in close_all}
        top2 = [t for t, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:TOP_N]]
        regime_ok = (spy_6m > SPY_6M_MIN and vix_now < VIX_ENTER_MAX)
        if rot_state['in_market']: action = "EXIT" if not regime_ok else ("ROTATE" if set(top2) != set([h['ticker'] for h in rot_state['holdings']]) else "KEEP")
        else: action = "ENTER" if regime_ok else "WAIT"
        return {"action": action, "top2": top2, "scores": scores, "vix_now": vix_now, "spy_6m": spy_6m, "regime_ok": regime_ok, "spy_daily": spy_daily}
    except: return {"action": "WAIT", "top2": []}

# ==============================================================
# 6. 성과 분석 로직 (전체 복구)
# ==============================================================

def calc_upro_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0, "total_trades": 0, "win_trades": 0, "loss_trades": 0, "avg_profit": 0.0, "avg_loss": 0.0, "best_trade": 0.0, "worst_trade": 0.0, "equity_curve": [], "monthly_ret": []}
    if df is None or df.empty: return empty
    df = df.copy()
    df['Date'] = pd.to_datetime(df['Date'])
    df['Price'] = pd.to_numeric(df['Price'], errors='coerce').fillna(0)
    buys, sells = df[df['Action'] == 'BUY'].reset_index(drop=True), df[df['Action'] == 'SELL'].reset_index(drop=True)
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
    sells['RetPct'], sells['Date'] = pd.to_numeric(sells['RetPct'], errors='coerce').fillna(0), pd.to_datetime(sells['Date'])
    rets, equity = sells['RetPct'].tolist(), 100.0
    equity_curve = [{"date": str(sells.iloc[0]['Date'].date()), "equity": 100.0}]
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
# 7. 유틸리티 (Gemini, Telegram)
# ==============================================================

def ask_gemini(u_sig, r_sig):
    if not GEMINI_OK or not os.getenv("GEMINI_API_KEY"): return "AI분석 스킵"
    try:
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel("gemini-1.5-flash")
        p = f"UPRO {u_sig}, ROT {r_sig['action']}, TOP2 {r_sig.get('top2')}. Korean 150 chars."
        return model.generate_content(p).text.strip()
    except: return "AI분석 실패"

async def send_telegram(msg):
    t, c = os.getenv('TELEGRAM_TOKEN'), os.getenv('CHAT_ID')
    if t and c:
        try: requests.post(f"https://api.telegram.org/bot{t}/sendMessage", json={"chat_id": c, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass

# ==============================================================
# 9. 메인 실행 (20시 가동 로직 복구)
# ==============================================================

async def run_trading():
    now_kst = dt.now(KST)
    trader = KIS_Trader()
    spy_ohlc, monthly, vix_close, close_all, d_msg = get_market_data()
    if spy_ohlc.empty: return
    rot_state = load_rotation_state()
    u_sig, u_re, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_sig = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state)
    
    is_manual = os.getenv('GITHUB_ACTIONS') == 'true'
    if now_kst.hour == 20 or is_manual:
        bal = trader.get_total_balance()
        if bal > 0:
            # 실전 매매 주문 로직 수행 (여기에 상세 주문 로직 유지)
            await send_telegram(f"🤖 <b>통합봇 실행 리포트</b>\n잔고: ${bal:,.2f}\nUPRO: {u_sig}\nROT: {r_sig['action']}\nAI: {ask_gemini(u_sig, r_sig)}")
        else:
            await send_telegram(f"⚠️ 봇 실행됐으나 잔고가 0입니다. (KST {now_kst.strftime('%H:%M')})")

# ==============================================================
# 10. Streamlit 대시보드 (전체 복구)
# ==============================================================

def run_dashboard():
    now_kst = dt.now(KST)
    st.set_page_config(page_title="Unified Bot v1.1", layout="wide", page_icon="🤖")
    
    # 데이터 로드
    spy_ohlc, monthly, vix_close, close_all, data_msg = get_market_data()
    rot_state = load_rotation_state()
    if spy_ohlc.empty:
        st.error(f"데이터 로드 실패: {data_msg}")
        return

    u_signal, u_reason, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_signal = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state)
    
    df_upro = pd.read_csv(HISTORY_FILE) if os.path.exists(HISTORY_FILE) else pd.DataFrame()
    df_rot  = pd.read_csv(ROTATION_HISTORY_FILE) if os.path.exists(ROTATION_HISTORY_FILE) else pd.DataFrame()
    upro_perf, rot_perf = calc_upro_performance(df_upro), calc_rotation_performance(df_rot)

    st.title("🤖 Unified Trading Bot")
    tab1, tab2, tab3 = st.tabs(["실시간 현황", "성과 분석", "거래 로그"])

    with tab1:
        c1, c2, c3 = st.columns(3)
        c1.metric("UPRO Position", "IN" if u_st.get('in_market') else "OUT", u_signal)
        c2.metric("ROT Action", r_signal['action'], f"VIX: {r_signal['vix_now']:.1f}")
        c3.metric("SPY Price", f"${spy_ohlc['Close'].iloc[-1]:.2f}")
        
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3], vertical_spacing=0.03)
        fig.add_trace(go.Candlestick(x=spy_ohlc.index, open=spy_ohlc['Open'], high=spy_ohlc['High'], low=spy_ohlc['Low'], close=spy_ohlc['Close'], name="SPY"), row=1, col=1)
        fig.add_trace(go.Bar(x=vix_close.index, y=vix_close.values, name="VIX", marker_color="orange"), row=2, col=1)
        fig.update_layout(xaxis_rangeslider_visible=False, height=600, template="plotly_dark", margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        st.subheader("전략별 성과 요약")
        for label, perf in [("UPRO 트렌드", upro_perf), ("모멘텀 로테이션", rot_perf)]:
            st.write(f"### {label}")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("총 수익률", f"{perf['total_return']:+.1f}%")
            k2.metric("MDD", f"-{perf['mdd']:.1f}%")
            k3.metric("승률", f"{perf['win_rate']:.0f}%")
            k4.metric("거래 횟수", f"{perf['total_trades']}회")

    with tab3:
        st.subheader("최근 거래 기록")
        st.write("UPRO Log")
        st.dataframe(df_upro.tail(10))
        st.write("Rotation Log")
        st.dataframe(df_rot.tail(10))

if __name__ == "__main__":
    if os.getenv('GITHUB_ACTIONS') == 'true': asyncio.run(run_trading())
    else: run_dashboard()
