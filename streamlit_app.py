
"""
Unified Trading Bot v1.7.1 (Masterpiece + VIX Dynamic Allocation)
[Update] v1.6.5의 VIX 기반 동적 비중 조절 로직 복구 및 완벽 이식
[Base] v1.7.0 structure (Score Buffer, 고스트 포지션 차단, Nasdaq 전체 풀 복구 등 8가지 핵심 방어 완벽 유지)
[Fix] 깃허브 에디터 SyntaxError(unterminated string literal) 방지를 위한 세미콜론 및 줄바꿈 완전 제거/분리
"""

import os
import json
import asyncio
import time
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

warnings.filterwarnings('ignore')

# ==============================================================
# 1. 설정
# ==============================================================
KST = pytz.timezone('Asia/Seoul')
SIGNAL_TICKER = 'SPY'
TRADE_TICKER  = 'UPRO'
STATE_FILE    = 'trend_state.json'
HISTORY_FILE  = 'history_trend.csv'

FALLBACK_POOL = [
    'NVDA', 'TSLA', 'META', 'AAPL', 'MSFT', 'AMZN', 'GOOGL',
    'AVGO', 'COST', 'NFLX', 'AMD', 'ADBE', 'QCOM', 'INTC',
    'INTU', 'AMAT', 'MU', 'LRCX', 'PANW', 'MRVL'
]
TOP_N                 = 2
VIX_ENTER_MAX         = 25.0
SPY_6M_MIN            = 0.0
ROTATION_STATE_FILE   = 'rotation_state.json'
ROTATION_HISTORY_FILE = 'history_rotation.csv'

# 💡 [v1.7.1 복구] VIX 기반 동적 비중 계산 함수
def get_dynamic_upro_ratio(vix_now):
    if vix_now < 15:
        return 0.65
    elif vix_now < 20:
        return 0.55
    elif vix_now < 25:
        return 0.45
    elif vix_now < 30:
        return 0.30
    else:
        return 0.15

# ==============================================================
# 2. KIS API 
# ==============================================================
class KIS_Trader:
    def __init__(self):
        self.base_url     = "https://openapi.koreainvestment.com:9443"
        self.app_key      = os.getenv('KIS_APPKEY', '').strip()
        self.app_secret   = os.getenv('KIS_SECRET', '').strip()
        self.cano         = os.getenv('KIS_CANO', '').strip()
        self.acnt_prdt_cd = os.getenv('KIS_ACNT_PRDT_CD', '01').strip()
        self.token        = None
        self._set_token()

    def _get_exch_info(self, ticker):
        amex_list = ["UPRO", "SPY", "TQQQ", "SQQQ", "VOO", "IVV"]
        nyse_list = ["VRT", "UNH", "JPM", "V", "MA"] 
        if ticker in amex_list:
            return "AMEX", "AMS"
        elif ticker in nyse_list:
            return "NYSE", "NYS"
        else:
            return "NASD", "NAS"

    def _set_token(self):
        try:
            url  = f"{self.base_url}/oauth2/tokenP"
            data = {"grant_type": "client_credentials", "appkey": self.app_key, "appsecret": self.app_secret}
            res = requests.post(url, headers={"content-type": "application/json"}, data=json.dumps(data)).json()
            self.token = res.get('access_token')
            if not self.token:
                print(f"🚨 [토큰 발급 실패 사유] KIS 서버 응답: {json.dumps(res, ensure_ascii=False)}")
        except Exception as e:
            print(f"🚨 [토큰 통신 자체 에러]: {e}")

    def _headers(self, tr_id):
        return {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P"
        }

    def get_balance(self):
        try:
            url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd, 
                "OVRS_EXCG_CD": "AMEX",
                "OVRS_ORD_UNPR": "1",
                "ITEM_CD": "UPRO"
            }
            res = requests.get(url, headers=self._headers("JTTT3007R"), params=params).json()
            return float(res.get('output', {}).get('ord_psbl_frcr_amt', 0))
        except Exception as e:
            print(f"🚨 KIS 시스템 에러: {e}")
            return 0.0

    def get_holdings(self, ticker):
        try:
            url = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
            exch_cd, _ = self._get_exch_info(ticker)
            params = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd, 
                "OVRS_EXCG_CD": exch_cd,
                "TR_CRCY_CD": "USD", 
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": ""
            }
            res = requests.get(url, headers=self._headers("JTTT3012R"), params=params).json()
            for item in res.get('output1', []):
                if item.get('pdno') == ticker or item.get('ovrs_pdno') == ticker:
                    return int(float(item.get('ovrs_cblc_qty', item.get('ccld_qty_smtl', 0))))
            return 0  
        except Exception as e: 
            print(f"🚨 KIS 보유종목 조회 에러: {e}")
            return -1 

    def get_current_price(self, ticker):
        try:
            df = yf.download(ticker, period='5d', progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            price = float(df['Close'].dropna().iloc[-1])
            if price > 0:
                return price
        except Exception:
            pass

        try:
            url = f"{self.base_url}/uapi/overseas-stock/v1/quotations/price"
            _, excd = self._get_exch_info(ticker) 
            params = {"AUTH": "", "EXCD": excd, "SYMB": ticker}
            res = requests.get(url, headers=self._headers("HHDFS76410100"), params=params).json()
            price = float(res.get('output', {}).get('last', 0))
            if price > 0:
                return price
        except Exception as e:
            print(f"🚨 [가격로그] KIS 에러 ({ticker}): {e}")
        
        return 0.0

    def send_order(self, ticker, qty, side="BUY"):
        try:
            url = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
            tr_id = "TTTT1002U" if side == "BUY" else "TTTT1006U"
            curr_p = self.get_current_price(ticker)
            
            if curr_p <= 0:
                print(f"🚨 [주문차단] {ticker} 현재가 0원, {side} 주문 취소")
                return {"rt_cd": "1", "msg1": f"현재가 수신 실패(0원) - {ticker} {side} 취소"}
                
            order_p = curr_p * 1.01 if side == "BUY" else curr_p * 0.99
            exch_cd, _ = self._get_exch_info(ticker)
            data = {
                "CANO": self.cano,
                "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "OVRS_EXCG_CD": exch_cd,
                "PDNO": ticker,
                "ORD_QTY": str(int(qty)),
                "OVRS_ORD_UNPR": f"{order_p:.2f}",
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN": "00"
            }
            return requests.post(url, headers=self._headers(tr_id), data=json.dumps(data)).json()
        except Exception as e:
            return {"rt_cd": "1", "msg1": str(e)}

# ==============================================================
# 3. 데이터 및 상태 로직
# ==============================================================
def get_nasdaq_100_tickers():
    try:
        import io
        url = "https://en.wikipedia.org/wiki/Nasdaq-100"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers)
        tables = pd.read_html(io.StringIO(res.text))
        for table in tables:
            if 'Ticker' in table.columns:
                return [t.replace('.', '-') for t in table['Ticker'].tolist()]
            elif 'Symbol' in table.columns:
                return [t.replace('.', '-') for t in table['Symbol'].tolist()]
        return FALLBACK_POOL
    except Exception:
        return FALLBACK_POOL

def _yf_download_with_retry(ticker_or_list, period='2y', interval='1d', max_retry=3):
    if isinstance(ticker_or_list, str):
        for attempt in range(max_retry):
            try:
                df = yf.download(ticker_or_list, period=period, interval=interval, progress=False)
                if df is not None and not df.empty:
                    return df
            except Exception:
                pass
            if attempt < max_retry - 1:
                time.sleep(5)
        return pd.DataFrame()

    BATCH_SIZE = 20
    all_dfs = []
    for i in range(0, len(ticker_or_list), BATCH_SIZE):
        batch = ticker_or_list[i:i+BATCH_SIZE]
        for attempt in range(max_retry):
            try:
                df = yf.download(batch, period=period, interval=interval, progress=False)
                if df is not None and not df.empty:
                    all_dfs.append(df)
                    break
            except Exception:
                pass
            if attempt < max_retry - 1:
                time.sleep(3)
        time.sleep(1)

    if not all_dfs:
        return pd.DataFrame()
        
    try:
        combined = pd.concat(all_dfs, axis=1)
        combined = combined.loc[:, ~combined.columns.duplicated()]
        return combined
    except Exception:
        if all_dfs:
            return all_dfs[0]
        else:
            return pd.DataFrame()

def get_market_data():
    try:
        tickers = get_nasdaq_100_tickers()
        spy_ohlc = _yf_download_with_retry(SIGNAL_TICKER, period='2y')
        vix_data = _yf_download_with_retry('^VIX', period='2y')
        close_prices = _yf_download_with_retry(tickers, period='2y')

        if spy_ohlc.empty:
            raise ValueError("SPY 데이터 수신 실패")
        if vix_data.empty:
            raise ValueError("VIX 데이터 수신 실패")

        if isinstance(spy_ohlc.columns, pd.MultiIndex):
            spy_ohlc.columns = spy_ohlc.columns.get_level_values(0)
        if isinstance(vix_data.columns, pd.MultiIndex):
            vix_data.columns = vix_data.columns.get_level_values(0)
        
        if 'Close' not in spy_ohlc.columns or 'Close' not in vix_data.columns:
            raise ValueError("야후 데이터에서 Close 컬럼 누락")

        if isinstance(close_prices.columns, pd.MultiIndex):
            close_df = close_prices['Close']
        else:
            if len(tickers) == 1:
                close_df = close_prices[['Close']].rename(columns={'Close': tickers[0]})
            else:
                if 'Close' in close_prices.columns:
                    close_df = close_prices['Close']
                else:
                    close_df = close_prices

        vix_close = vix_data['Close']
        spy_close_col = spy_ohlc['Close']
        
        if isinstance(spy_close_col, pd.DataFrame):
            spy_close_series = spy_close_col.iloc[:, 0]
        else:
            spy_close_series = spy_close_col
            
        monthly = spy_close_series.resample('ME').last().pct_change().dropna()
        
        close_all = {}
        for t in tickers:
            if t in close_df.columns:
                close_all[t] = close_df[t].dropna()
                
        return spy_ohlc, monthly, vix_close, close_all, "Success"
    except Exception as e: 
        return pd.DataFrame(), pd.Series(), pd.Series(), {}, f"Data Error: {str(e)}"

def load_rotation_state():
    default = {"in_market": False, "holdings": [], "entry_date": None}
    if os.path.exists(ROTATION_STATE_FILE):
        try:
            with open(ROTATION_STATE_FILE, 'r') as f:
                content = f.read().strip()
                if content:
                    saved = json.loads(content)
                    for k, v in default.items():
                        if k not in saved:
                            saved[k] = v
                    return saved
        except Exception:
            pass
    return default

def save_rotation_state(state):
    with open(ROTATION_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def get_upro_signal(spy_close, monthly, vix_close):
    state = {"in_market": True, "last_exit_price": 0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                content = f.read().strip()
                if content:
                    state = json.loads(content)
        except Exception:
            pass
            
    if spy_close.empty or len(spy_close) < 20:
        return "WAIT", "Loading", 0.0, state
        
    curr_p = float(spy_close.iloc[-1])
    spy_daily = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
    vix_daily = float((vix_close.iloc[-1] / vix_close.iloc[-2]) - 1)
    
    if len(spy_close) >= 4:
        spy_3day = float((spy_close.iloc[-1] / spy_close.iloc[-4]) - 1)
    else:
        spy_3day = 0.0
        
    if vix_daily >= 0.3 or spy_daily <= -0.03 or spy_3day <= -0.05:
        return "EXIT", "Shock Trigger", curr_p, state
        
    if state.get('in_market', True):
        recent = monthly.tail(2).values
        if len(recent) == 2 and recent[0] < 0 and recent[1] < 0:
            return "EXIT", "2m Down", curr_p, state
        return "KEEP", "Holding", curr_p, state
        
    vix_now = float(vix_close.iloc[-1])
    vix_prev = float(vix_close.iloc[-2])
    vix_20d = vix_close.tail(20)
    vix_mean = float(vix_20d.mean())
    vix_std = float(vix_20d.std())
    
    vix_spike = (vix_now > (vix_mean + 2*vix_std)) or (vix_prev > (vix_mean + 2*vix_std))
    vix_rev = vix_spike and (vix_now < vix_prev * 0.95) and (spy_daily > 0)
    
    if vix_rev:
        return "RE-ENTER", "VIX Reversal", curr_p, state
        
    if state['last_exit_price'] > 0:
        rebound = (curr_p - state['last_exit_price']) / state['last_exit_price']
    else:
        rebound = 0
        
    if rebound >= 0.02:
        return "RE-ENTER", "2% Rebound", curr_p, state
        
    return "WAIT", f"Waiting({rebound*100:.1f}%)", curr_p, state

def get_rotation_signal(spy_close, vix_close, close_all, rot_state, per_stock_budget):
    try:
        spy_6m = float(spy_close.iloc[-1]/spy_close.iloc[-126]-1)
        vix_now = float(vix_close.iloc[-1])
        
        def calc_mom(s):
            if len(s) < 126:
                return -999.0
            m1 = (s.iloc[-1]/s.iloc[-21]-1)*100
            m3 = (s.iloc[-1]/s.iloc[-63]-1)*100
            m6 = (s.iloc[-1]/s.iloc[-126]-1)*100
            return round(m1*0.4 + m3*0.4 + m6*0.2, 2)
            
        scores = {}
        for t, series in close_all.items():
            scores[t] = calc_mom(series)
            
        if rot_state.get('in_market'):
            current_holdings_pre = [h['ticker'] for h in rot_state.get('holdings', [])]
        else:
            current_holdings_pre = []
            
        eligible = {}
        for t, sc in scores.items():
            if close_all[t].iloc[-1] <= per_stock_budget or t in current_holdings_pre:
                eligible[t] = sc
                
        SCORE_MARGIN = 5.0
        
        if rot_state.get('in_market'):
            adjusted_scores = {}
            for t in eligible.keys():
                base_score = scores.get(t, -999)
                if t in current_holdings_pre:
                    adjusted_scores[t] = base_score + SCORE_MARGIN 
                else:
                    adjusted_scores[t] = base_score
                    
            eligible_sorted = sorted(eligible.items(), key=lambda x: adjusted_scores.get(x[0], -999), reverse=True)
            target_portfolio = [t for t, _ in eligible_sorted[:TOP_N]]
        else:
            eligible_sorted = sorted(eligible.items(), key=lambda x: x[1], reverse=True)
            target_portfolio = [t for t, _ in eligible_sorted[:TOP_N]]

        if not target_portfolio and rot_state.get('in_market'):
            target_portfolio = [h['ticker'] for h in rot_state.get('holdings', [])]

        regime_ok = (spy_6m > 0 and vix_now < 25)
        
        if rot_state.get('in_market'):
            current_tickers_set = set([h['ticker'] for h in rot_state.get('holdings', [])])
            if not regime_ok:
                action = "EXIT"
            elif set(target_portfolio) != current_tickers_set:
                action = "ROTATE"
            else:
                action = "KEEP"
        else: 
            if regime_ok:
                action = "ENTER"
            else:
                action = "WAIT"
            
        return {"action": action, "top2": target_portfolio, "scores": scores, "vix_now": vix_now, "spy_6m": spy_6m}
    except Exception as e: 
        print(f"Signal Error: {e}")
        return {"action": "WAIT", "top2": [], "scores": {}, "vix_now": 0, "spy_6m": 0}

# ==============================================================
# 4. 성과 분석 함수
# ==============================================================
def calc_upro_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0, "total_trades": 0, "equity_curve": []}
    if df is None or df.empty:
        return empty
        
    df = df.copy()
    df['Date'] = pd.to_datetime(df['Date'])
    df['Price'] = pd.to_numeric(df['Price'], errors='coerce').fillna(0)
    
    buys = df[df['Action'] == 'BUY'].reset_index(drop=True)
    sells = df[df['Action'] == 'SELL'].reset_index(drop=True)
    
    trades = []
    equity = 100.0
    equity_curve = []
    if not buys.empty:
        equity_curve = [{"date": str(buys.loc[0, 'Date'].date()), "equity": 100.0}]
        
    for i in range(min(len(buys), len(sells))):
        buy_p = float(buys.loc[i, 'Price'])
        sell_p = float(sells.loc[i, 'Price'])
        ret = (sell_p - buy_p) / buy_p * 100
        trades.append(ret)
        equity *= (1 + ret / 100)
        equity_curve.append({"date": str(sells.loc[i, 'Date'].date()), "equity": round(equity, 2)})
        
    if not trades:
        return empty
        
    wins = [r for r in trades if r > 0]
    eq_vals = [e['equity'] for e in equity_curve]
    
    peak = eq_vals[0] if eq_vals else 100.0
    mdd = 0.0
    for v in eq_vals: 
        if v > peak:
            peak = v
        mdd = max(mdd, (peak - v) / peak * 100)
        
    sharpe = 0.0
    if len(trades) >= 3:
        sharpe = round(np.mean(trades)/(np.std(trades)+1e-9)*np.sqrt(12), 2)
        
    return {
        "total_return": round(equity - 100, 2),
        "win_rate": round(len(wins)/len(trades)*100, 1),
        "mdd": round(mdd, 2),
        "sharpe": sharpe,
        "total_trades": len(trades),
        "equity_curve": equity_curve
    }

def calc_rotation_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0, "total_trades": 0, "equity_curve": []}
    if df is None or df.empty or 'RetPct' not in df.columns:
        return empty
        
    sells = df[df['Action'] == 'SELL'].copy()
    sells['RetPct'] = pd.to_numeric(sells['RetPct'], errors='coerce').fillna(0)
    sells = sells.sort_values('Date').reset_index(drop=True)
    
    equity = 100.0
    equity_curve = []
    if not sells.empty:
        equity_curve = [{"date": str(pd.to_datetime(sells.iloc[0]['Date']).date()), "equity": 100.0}]
        
    for _, row in sells.iterrows():
        equity *= (1 + row['RetPct'] / 100)
        equity_curve.append({"date": str(pd.to_datetime(row['Date']).date()), "equity": round(equity, 2)})
        
    rets = sells['RetPct'].tolist()
    wins = [r for r in rets if r > 0]
    eq_vals = [e['equity'] for e in equity_curve]
    
    peak = eq_vals[0] if eq_vals else 100.0
    mdd = 0.0
    for v in eq_vals:
        if v > peak:
            peak = v
        mdd = max(mdd, (peak - v) / peak * 100)
        
    sharpe = 0.0
    if len(rets) >= 3:
        sharpe = round(np.mean(rets)/(np.std(rets)+1e-9)*np.sqrt(12), 2)
        
    return {
        "total_return": round(equity - 100, 2),
        "win_rate": round(len(wins)/len(rets)*100, 1) if rets else 0.0,
        "mdd": round(mdd, 2),
        "sharpe": sharpe,
        "total_trades": len(rets),
        "equity_curve": equity_curve
    }

# ==============================================================
# 5. 통신/AI & Caching
# ==============================================================
def ask_gemini(u_sig, r_sig):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return "API 키 없음"
        
    action_str = r_sig.get('action') if isinstance(r_sig, dict) else r_sig
    prompt = f"퀀트 전문가로서 분석해줘. UPRO={u_sig}, ROT={action_str}. 한국어 150자."
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {'Content-Type': 'application/json'}
    
    models = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash"]
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            if res.status_code == 200:
                return res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            elif res.status_code == 429:
                continue 
            else:
                continue
        except Exception:
            continue
    return "AI 연결 실패 (모든 모델 초과)"

async def tg_send(token_v, chat_id, text):
    if not token_v or not chat_id:
        return False
    try:
        async with Bot(token=token_v) as bot:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return True
    except Exception as e:
        print(f"Telegram HTML Error: {e}")
        try:
            async with Bot(token=token_v) as bot:
                await bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception as e2:
            print(f"Telegram Plain Error: {e2}")
            return False

def get_cached_portfolio_equity():
    trader = KIS_Trader()
    bal = trader.get_balance()
    rot_state = load_rotation_state()
    
    upro_qty = max(trader.get_holdings(TRADE_TICKER), 0)
    cur_p_upro = trader.get_current_price(TRADE_TICKER)
    upro_value = upro_qty * cur_p_upro
    
    rot_value = 0
    for h in rot_state.get('holdings', []):
        qty = max(trader.get_holdings(h['ticker']), 0)
        price = trader.get_current_price(h['ticker'])
        rot_value += qty * price
        
    total_equity = bal + upro_value + rot_value
    return total_equity, bal, upro_qty, upro_value, rot_value

# ==============================================================
# 9. 자동매매
# ==============================================================
async def run_trading():
    now_kst = dt.now(KST)
    current_hour = now_kst.hour
    
    trader = KIS_Trader()
    token_v = os.getenv('TELEGRAM_TOKEN')
    chat_id = os.getenv('CHAT_ID')
    bot = Bot(token=token_v) if (Bot and token_v) else None    
    
    spy_ohlc, monthly, vix_close, close_all, d_msg = get_market_data()

    if spy_ohlc.empty:
        if bot:
            await tg_send(token_v, chat_id, f"⚠️ 데이터 로드 실패: {d_msg}")
        return
        
    rot_state = load_rotation_state()
    bal = trader.get_balance()
    
    upro_raw_qty = trader.get_holdings(TRADE_TICKER)
    upro_qty = max(upro_raw_qty, 0)
    cur_p_upro = trader.get_current_price(TRADE_TICKER)
    
    upro_value = upro_qty * cur_p_upro
    
    rot_value = 0
    for h in rot_state.get('holdings', []):
        qty = max(trader.get_holdings(h['ticker']), 0)
        price = trader.get_current_price(h['ticker'])
        rot_value += qty * price
        
    total_equity = bal + upro_value + rot_value
    
    vix_now = float(vix_close.iloc[-1])
    upro_ratio = get_dynamic_upro_ratio(vix_now)
    rot_ratio = 1.0 - upro_ratio
    
    per_stock_budget = (total_equity * rot_ratio * 0.95) / TOP_N
    
    u_sig, u_re, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_sig = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state, per_stock_budget)

    if current_hour in [20, 21,22,23,0]:
        upro_target = total_equity * upro_ratio
        rot_target = total_equity * rot_ratio
        
        msgs = [
            f"🤖 <b>통합봇 v1.7.1 [{now_kst.strftime('%m/%d %H:%M')}]</b>", 
            f"총자산: ${total_equity:,.2f}",
            f"📊 비중: UPRO {upro_ratio*100:.0f}% | ROT {rot_ratio*100:.0f}% (VIX: {vix_now:.1f})"
        ]
        
        upro_gap = max(0, upro_target - upro_value)
        
        if upro_raw_qty == -1:
            msgs.append("⚠️ UPRO 잔고 조회 통신 에러: 중복 매수 방지를 위해 이번 턴 UPRO 매매 스킵")
        else:
            if u_sig in ["KEEP", "RE-ENTER"] and upro_gap > (upro_target * 0.1):
                if cur_p_upro > 0:  
                    qty = int((upro_gap * 0.95) / cur_p_upro)
                    if qty >= 1:
                        order_res = trader.send_order(TRADE_TICKER, qty, "BUY")
                        if order_res.get('rt_cd') == '0':
                            msgs.append(f"✅ UPRO 매수: {qty}주")
                            with open(STATE_FILE, 'w') as f:
                                json.dump({"in_market": True, "last_exit_price": 0}, f)
                            
                            hist_data = [{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "BUY", "Qty": qty, "Price": cur_p_upro}]
                            pd.DataFrame(hist_data).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
                else:
                    msgs.append("⚠️ UPRO 현재가 수신 실패(0.0)로 매수 보류")
            elif u_sig == "EXIT" and upro_qty > 0:
                order_res = trader.send_order(TRADE_TICKER, upro_qty, "SELL")
                if order_res.get('rt_cd') == '0':
                    msgs.append(f"✅ UPRO 매도: {upro_qty}주")
                    with open(STATE_FILE, 'w') as f:
                        json.dump({"in_market": False, "last_exit_price": u_p}, f)
                    
                    hist_data = [{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Qty": upro_qty, "Price": cur_p_upro}]
                    pd.DataFrame(hist_data).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
        
        action = r_sig['action']
        top2 = r_sig['top2']
        
        if action in ["ENTER", "ROTATE"] and top2:
            new_h = []
            retained_tickers = []
            
            for h in rot_state.get('holdings', []):
                q = trader.get_holdings(h['ticker'])
                if q > 0:
                    if h['ticker'] not in top2:
                        order_res = trader.send_order(h['ticker'], q, "SELL")
                        if order_res.get('rt_cd') == '0':
                            cp = trader.get_current_price(h['ticker'])
                            ret = (cp - h.get('entry_price', cp)) / max(h.get('entry_price', cp), 1) * 100
                            hist_data = [{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Ticker": h['ticker'], "Qty": q, "Price": cp, "RetPct": round(ret, 2)}]
                            pd.DataFrame(hist_data).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                        else:
                            msgs.append(f"⚠️ ROT {h['ticker']} 매도 실패, 상태 유지")
                            new_h.append(h)
                    else:
                        new_h.append(h)
                        retained_tickers.append(h['ticker'])
                elif q == -1:
                    msgs.append(f"⚠️ ROT {h['ticker']} 조회 오류로 상태 유지")
                    new_h.append(h)
                    if h['ticker'] in top2:
                        retained_tickers.append(h['ticker'])
                        
            time.sleep(2)
            
            for t in top2:
                if t in retained_tickers:
                    msgs.append(f"🔄 ROT 유지: {t} (추가 매수 생략)")
                    continue
                    
                p = trader.get_current_price(t)
                if p > 0:
                    qty = int(((rot_target * 0.95) / len(top2)) / p)
                else:
                    qty = 0
                    
                if qty >= 1:
                    order_res = trader.send_order(t, qty, "BUY")
                    if order_res.get('rt_cd') == '0':
                        new_h.append({"ticker": t, "qty": qty, "entry_price": p})
                        hist_data = [{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "BUY", "Ticker": t, "Qty": qty, "Price": p, "RetPct": 0}]
                        pd.DataFrame(hist_data).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                        msgs.append(f"✅ ROT 매수 성공: {t} {qty}주")
                    else:
                        msgs.append(f"❌ ROT {t} 주문 실패: {order_res.get('msg1')}")
                else:
                    msgs.append(f"⚠️ ROT {t} 매수 보류 (현재가 0원 또는 자금 부족)")
            
            rot_state.update({"in_market": True, "holdings": new_h})
            save_rotation_state(rot_state)

        elif action == "EXIT" and rot_state.get('in_market'):
            remaining = []
            for h in rot_state.get('holdings', []):
                q = trader.get_holdings(h['ticker'])
                if q > 0:
                    order_res = trader.send_order(h['ticker'], q, "SELL")
                    if order_res.get('rt_cd') == '0':
                        cp = trader.get_current_price(h['ticker'])
                        ret = (cp - h.get('entry_price', cp)) / max(h.get('entry_price', cp), 1) * 100
                        hist_data = [{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Ticker": h['ticker'], "Qty": q, "Price": cp, "RetPct": round(ret, 2)}]
                        pd.DataFrame(hist_data).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                    else:
                        remaining.append(h)
                elif q == -1: 
                    remaining.append(h)
            
            rot_state.update({"in_market": len(remaining) > 0, "holdings": remaining})
            save_rotation_state(rot_state)
            msgs.append("🚨 ROT 하락장 청산 시도")
            
        msgs.append(f"🧠 AI: {ask_gemini(u_sig, r_sig)}")
        final_msg = "\n".join(msgs)
        await tg_send(token_v, chat_id, final_msg)

    elif current_hour in [1, 2, 3, 4, 5]:
        spy_int = _yf_download_with_retry(SIGNAL_TICKER, period='5d', interval='5m')
        if not spy_int.empty:
            if isinstance(spy_int.columns, pd.MultiIndex):
                spy_int.columns = spy_int.columns.get_level_values(0)            
            
            today_us = now_kst.astimezone(pytz.timezone('US/Eastern')).date()
            if spy_int.index.tz:
                spy_today = spy_int[spy_int.index.tz_convert(None).date == today_us]
            else:
                spy_today = spy_int[spy_int.index.date == today_us]
                
            # 💡 [여기서부터 수정된 부분입니다]
            if len(spy_today) >= 5:
                drop_ratio = (float(spy_today['Close'].iloc[-1]) / float(spy_today['Open'].iloc[0])) - 1
                if drop_ratio <= -0.03:
                    
                    # 1. UPRO 긴급 매도
                    if upro_qty > 0:
                        order_res = trader.send_order(TRADE_TICKER, upro_qty, "SELL")
                        if order_res.get('rt_cd') == '0':
                            exit_p = trader.get_current_price(TRADE_TICKER)
                            hist_data = [{"Date": dt.now().strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Qty": upro_qty, "Price": exit_p}]
                            pd.DataFrame(hist_data).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
                            with open(STATE_FILE, 'w') as f:
                                json.dump({"in_market": False, "last_exit_price": exit_p}, f)
                    
                    # 2. ROT 긴급 매도 (UPRO와 동일한 층위에서 실행)
                    if rot_state.get('in_market'):
                        rem = []
                        for h in rot_state.get('holdings', []):
                            q_h = trader.get_holdings(h['ticker'])
                            if q_h > 0:
                                order_res = trader.send_order(h['ticker'], q_h, "SELL")
                                if order_res.get('rt_cd') == '0':
                                    cp_h = trader.get_current_price(h['ticker'])
                                    ret_h = (cp_h - h.get('entry_price', cp_h)) / max(h.get('entry_price', cp_h), 1) * 100
                                    hist_data = [{"Date": dt.now().strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Ticker": h['ticker'], "Qty": q_h, "Price": cp_h, "RetPct": round(ret_h, 2)}]
                                    pd.DataFrame(hist_data).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                                else:
                                    rem.append(h)
                            elif q_h == -1:
                                rem.append(h)
                                
                        rot_state.update({"in_market": len(rem) > 0, "holdings": rem})
                        save_rotation_state(rot_state)
                        
                    # 3. 텔레그램 알림
                    await tg_send(token_v, chat_id, "🚨 긴급 탈출 실행 완료")
                    
            else:
                # 당일 5분봉 데이터가 5개 미만일 때 5일치 폴백을 막고 스킵합니다.
                print("⚠️ 당일 5분봉 데이터 부족으로 긴급탈출 감지 스킵")

    elif current_hour in [7, 8, 9, 10]:
        bal_7 = trader.get_balance()
        report_msg = [
            "📋 <b>아침 리포트</b>",
            f"잔고: ${bal_7:,.2f} | SPY 6M: {r_sig['spy_6m']*100:+.1f}%",
            f"🧠 {ask_gemini('morning', r_sig)}"
        ]
        await tg_send(token_v, chat_id, "\n".join(report_msg))
    
    else:
        test_msg = [
            "🧪 <b>수동 테스트</b>",
            f"UPRO: {u_sig} | ROT: {r_sig['action']}",
            f"TOP2: {', '.join(r_sig['top2'])}"
        ]
        await tg_send(token_v, chat_id, "\n".join(test_msg))

# ==============================================================
# 10. Dashboard
# ==============================================================
def plot_perf_chart(perf_data, name, color, spy_series):
    fig = go.Figure()
    if not perf_data['equity_curve']:
        return fig
        
    dates = [e['date'] for e in perf_data['equity_curve']]
    eqs = [e['equity'] for e in perf_data['equity_curve']]
    fig.add_trace(go.Scatter(x=dates, y=eqs, fill='tozeroy', name=f'{name} 실제', line=dict(color=color)))
    
    try:
        start_dt = pd.to_datetime(dates[0])
        if spy_series.index.tz is not None:
            spy_series = spy_series.tz_convert(None) 
        spy_sub = spy_series[spy_series.index >= start_dt]
        if not spy_sub.empty:
            spy_norm = (spy_sub / spy_sub.iloc[0]) * 100
            fig.add_trace(go.Scatter(x=spy_sub.index, y=spy_norm.values, name='SPY (존버)', line=dict(color='gray', dash='dot')))
    except Exception:
        pass 
        
    fig.update_layout(margin=dict(l=0, r=0, t=20, b=0), template="plotly_dark", height=250)
    return fig

def run_dashboard():
    now_kst = dt.now(KST)
    st.set_page_config(page_title="Unified Bot v1.7.1", layout="wide")
    
    spy_ohlc, monthly, vix_close, close_all, data_msg = get_market_data()
    if spy_ohlc.empty:
        st.error(f"데이터 실패: {data_msg}")
        return

    total_equity, bal, upro_qty, upro_value, rot_value = get_cached_portfolio_equity()
    
    vix_now = float(vix_close.iloc[-1])
    upro_ratio = get_dynamic_upro_ratio(vix_now)
    rot_ratio = 1.0 - upro_ratio
    
    if total_equity > 10:
        actual_per_stock_budget = (total_equity * rot_ratio * 0.95) / TOP_N
    else:
        actual_per_stock_budget = 99999
    
    rot_state = load_rotation_state()
    u_sig, u_re, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_sig = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state, actual_per_stock_budget)
    
    df_upro = pd.read_csv(HISTORY_FILE) if os.path.exists(HISTORY_FILE) else pd.DataFrame()
    df_rot = pd.read_csv(ROTATION_HISTORY_FILE) if os.path.exists(ROTATION_HISTORY_FILE) else pd.DataFrame()
    
    upro_perf = calc_upro_performance(df_upro)
    rot_perf = calc_rotation_performance(df_rot)

    st.write(f"### 🎯 현재 목표 비중: UPRO {upro_ratio*100:.0f}% : ROT {rot_ratio*100:.0f}%")

    tab1, tab2, tab3 = st.tabs(["📡 실시간 현황", "📊 성과 분석", "📋 거래 로그"])
    with tab1:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("UPRO", "IN" if u_st.get('in_market') else "OUT", u_sig)
        c2.metric("ROT TOP2", ", ".join(r_sig['top2']), r_sig['action'])
        c3.metric("SPY", f"${spy_ohlc['Close'].iloc[-1]:.2f}")
        c4.metric("VIX", f"{r_sig['vix_now']:.1f}")
        c5.metric("SPY 6M", f"{r_sig['spy_6m']*100:+.1f}%")
        
        spy126 = spy_ohlc.tail(126)
        vix126 = vix_close.tail(126)
        
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.7, 0.3])
        fig.add_trace(go.Candlestick(x=spy126.index, open=spy126['Open'], high=spy126['High'], low=spy126['Low'], close=spy126['Close'], name="SPY"), row=1, col=1)
        fig.add_trace(go.Scatter(x=vix126.index, y=vix126.values, name="VIX", line=dict(color='orange')), row=2, col=1)
        fig.update_layout(xaxis_rangeslider_visible=False, height=650, template="plotly_dark")
        st.plotly_chart(fig, use_container_width=True)
        
        if r_sig.get('scores'):
            st.subheader(f"모멘텀 랭킹 (상위 30개 / 필터: ${actual_per_stock_budget:.1f} 이하)")
            sorted_scores = sorted(r_sig['scores'].items(), key=lambda x: x[1], reverse=True)[:30]
            sc_list = [{"Ticker": t, "Score": s, "Rank": "★" if t in r_sig['top2'] else ""} for t, s in sorted_scores]
            sc_df = pd.DataFrame(sc_list)
            st.dataframe(sc_df, use_container_width=True, hide_index=True)

    with tab2:
        st.subheader("🚀 UPRO 상세 성과")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("수익률", f"{upro_perf['total_return']:+.1f}%")
        k2.metric("MDD", f"-{upro_perf['mdd']:.1f}%")
        k3.metric("승률", f"{upro_perf['win_rate']}%")
        k4.metric("샤프지수", upro_perf['sharpe'])
        if upro_perf['equity_curve']:
            st.plotly_chart(plot_perf_chart(upro_perf, 'UPRO', '#58a6ff', spy_ohlc['Close']), use_container_width=True)
            
        st.divider()
        st.subheader("🔄 ROT 상세 성과")
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("수익률", f"{rot_perf['total_return']:+.1f}%")
        r2.metric("MDD", f"-{rot_perf['mdd']:.1f}%")
        r3.metric("승률", f"{rot_perf['win_rate']}%")
        r4.metric("거래횟수", rot_perf['total_trades'])
        if rot_perf['equity_curve']:
            st.plotly_chart(plot_perf_chart(rot_perf, 'ROT', '#fbbf24', spy_ohlc['Close']), use_container_width=True)

    with tab3:
        st.subheader("거래 이력")
        st.dataframe(df_upro.tail(10))
        st.dataframe(df_rot.tail(10))

if __name__ == "__main__":
    if os.getenv('GITHUB_ACTIONS') == 'true':
        asyncio.run(run_trading())
    else:
        run_dashboard()
