"""
Unified Trading Bot v1.5.1
[Fix] 긴급탈출 후 STATE_FILE 저장 추가 (in_market: False)
[Fix] 긴급탈출 SPY 당일 비교 로직 수정 (period='5d' + 날짜 필터)
[Fix] ROT EXIT 시 실패 종목 있어도 state 초기화되던 고스트 포지션 버그 수정
[Fix] send_order 매도 시 가격 0원 방어 추가
[Fix] yfinance 재시도 로직 추가 (3회, 5초 간격)
[Fix] FALLBACK_POOL 확장 (7개 -> 20개)
[Fix] 성과 분석 거래 건수 부족 시 샤프지수 표기 보호
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
# 1. 설정 (황금 레시피 유지: 50대 50)
# ==============================================================
KST = pytz.timezone('Asia/Seoul')
UPRO_RATIO     = 0.50
ROTATION_RATIO = 0.50
SIGNAL_TICKER = 'SPY'
TRADE_TICKER  = 'UPRO'
STATE_FILE    = 'trend_state.json'
HISTORY_FILE  = 'history_trend.csv'
FALLBACK_POOL         = [
    'NVDA', 'TSLA', 'META', 'AAPL', 'MSFT', 'AMZN', 'GOOGL',
    'AVGO', 'COST', 'NFLX', 'AMD', 'ADBE', 'QCOM', 'INTC',
    'INTU', 'AMAT', 'MU', 'LRCX', 'PANW', 'MRVL'
]
TOP_N                 = 2
VIX_ENTER_MAX         = 25.0
SPY_6M_MIN            = 0.0
ROTATION_STATE_FILE   = 'rotation_state.json'
ROTATION_HISTORY_FILE = 'history_rotation.csv'

# ==============================================================
# 2. KIS API
# ==============================================================
class KIS_Trader:
    def __init__(self):
        self.base_url     = "https://openapi.koreainvestment.com:9443"
        self.app_key      = os.getenv('KIS_APPKEY')
        self.app_secret   = os.getenv('KIS_SECRET')
        self.cano         = os.getenv('KIS_CANO')
        self.acnt_prdt_cd = os.getenv('KIS_ACNT_PRDT_CD', '01')
        self.token        = None
        self._set_token()

    def _get_exch_info(self, ticker):
        """티커에 따른 거래소 코드(주문용)와 시장 코드(조회용) 반환"""
        # AMEX/ARCA 상장 ETF 리스트
        amex_list = ["UPRO", "SPY", "TQQQ", "SQQQ", "VOO", "IVV"]
        # NYSE 상장 종목 (나스닥 100 외 종목 확장 대비)
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
        except: pass

    def _headers(self, tr_id):
        return {"Content-Type": "application/json", "authorization": f"Bearer {self.token}", "appkey": self.app_key, "appsecret": self.app_secret, "tr_id": tr_id, "custtype": "P"}

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
            print(f"📊 잔고 RAW 전체: {json.dumps(res, ensure_ascii=False)}")  
            usd_cash = float(res.get('output', {}).get('ord_psbl_frcr_amt', 0))
            print(f"💰 달러 가용 잔고: ${usd_cash}")
            return usd_cash
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
                    qty = int(float(item.get('ovrs_cblc_qty', item.get('ccld_qty_smtl', 0))))
                    print(f"📦 {ticker} 보유 확인: {qty}주")
                    return qty
            return 0
        except Exception as e: 
            print(f"🚨 KIS 보유종목 조회 에러: {e}")
            return 0

    def get_current_price(self, ticker):
        try:
            df = yf.download(ticker, period='5d', progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            price = float(df['Close'].dropna().iloc[-1])
            if price > 0: return price
        except Exception as e:
            print(f"🚨 [가격로그] yfinance 에러 ({ticker}): {e}")

        try:
            url = f"{self.base_url}/uapi/overseas-stock/v1/quotations/price"
            _, excd = self._get_exch_info(ticker) 
            params = {"AUTH": "", "EXCD": excd, "SYMB": ticker}
            res = requests.get(url, headers=self._headers("HHDFS76410100"), params=params).json()
            price = float(res.get('output', {}).get('last', 0))
            if price > 0: return price
            else: print(f"🚨 [가격로그] KIS 0원 응답 ({ticker}): {res}")
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
            exch_cd, _ = self._get_exch_info(ticker) # 💡 하드코딩 대신 함수 호출
            data = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd, "OVRS_EXCG_CD": exch_cd, "PDNO": ticker, "ORD_QTY": str(int(qty)), "OVRS_ORD_UNPR": f"{order_p:.2f}", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00"}
            return requests.post(url, headers=self._headers(tr_id), data=json.dumps(data)).json()
        except Exception as e: return {"rt_cd": "1", "msg1": str(e)}

# ==============================================================
# 3. 데이터 및 상태 로직
# ==============================================================
def get_top_30_tickers():
    try:
        import io
        url = "https://en.wikipedia.org/wiki/Nasdaq-100"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers)
        tables = pd.read_html(io.StringIO(res.text))
        for table in tables:
            # 'Ticker' 또는 'Symbol' 컬럼이 있는 테이블을 찾음
            target_col = None
            if 'Ticker' in table.columns: target_col = 'Ticker'
            elif 'Symbol' in table.columns: target_col = 'Symbol'
            
            if target_col:
                # head(30)을 제거하여 100개 전체 종목을 가져옴
                full_list = [t.replace('.', '-') for t in table[target_col].tolist()]
                print(f"✅ 나스닥 100 종목 확보: {len(full_list)}개")
                return full_list
        return FALLBACK_POOL
    except Exception as e:
        print(f"⚠️ 위키 파싱 실패, Fallback 사용: {e}")
        return FALLBACK_POOL




def _yf_download_with_retry(ticker_or_list, period='2y', interval='1d', max_retry=3):
    for attempt in range(max_retry):
        try:
            df = yf.download(ticker_or_list, period=period, interval=interval, progress=False)
            if df is not None and not df.empty: return df
            print(f"⚠️ yfinance 빈 응답 ({ticker_or_list}, 시도 {attempt+1}/{max_retry})")
        except Exception as e:
            print(f"⚠️ yfinance 에러 ({ticker_or_list}, 시도 {attempt+1}/{max_retry}): {e}")
        if attempt < max_retry - 1: time.sleep(5)
    return pd.DataFrame()

def get_market_data():
    try:
        tickers = get_top_30_tickers()
        spy_ohlc = _yf_download_with_retry(SIGNAL_TICKER, period='2y')
        vix_data = _yf_download_with_retry('^VIX', period='2y')
        close_prices = _yf_download_with_retry(tickers, period='2y')

        if spy_ohlc.empty: raise ValueError("SPY 데이터 수신 실패")
        if vix_data.empty: raise ValueError("VIX 데이터 수신 실패")

        if isinstance(spy_ohlc.columns, pd.MultiIndex): spy_ohlc.columns = spy_ohlc.columns.get_level_values(0)
        if isinstance(vix_data.columns, pd.MultiIndex): vix_data.columns = vix_data.columns.get_level_values(0)

        if isinstance(close_prices.columns, pd.MultiIndex): close_df = close_prices['Close']
        else: close_df = close_prices[['Close']].rename(columns={'Close': tickers[0]}) if len(tickers) == 1 else close_prices['Close']

        vix_close = vix_data['Close']
        spy_close_series = spy_ohlc['Close'].squeeze()
        monthly = spy_close_series.resample('ME').last().pct_change().dropna()
        close_all = {t: close_df[t].dropna() for t in tickers if t in close_df.columns}
        
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
                        if k not in saved: saved[k] = v
                    return saved
        except: pass
    return default

def save_rotation_state(state):
    with open(ROTATION_STATE_FILE, 'w') as f: json.dump(state, f, indent=2, ensure_ascii=False)

def get_upro_signal(spy_close, monthly, vix_close):
    state = {"in_market": True, "last_exit_price": 0}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                content = f.read().strip()
                if content: state = json.loads(content)
        except: pass
    if spy_close.empty or len(spy_close) < 20: return "WAIT", "Loading", 0.0, state
    curr_p = float(spy_close.iloc[-1]); spy_daily = float((spy_close.iloc[-1] / spy_close.iloc[-2]) - 1)
    vix_daily = float((vix_close.iloc[-1] / vix_close.iloc[-2]) - 1); spy_3day = float((spy_close.iloc[-1] / spy_close.iloc[-4]) - 1) if len(spy_close) >= 4 else 0.0
    if vix_daily >= 0.3 or spy_daily <= -0.03 or spy_3day <= -0.05: return "EXIT", "Shock Trigger", curr_p, state
    if state.get('in_market', True):
        recent = monthly.tail(2).values
        if len(recent) == 2 and recent[0] < 0 and recent[1] < 0: return "EXIT", "2m Down", curr_p, state
        return "KEEP", "Holding", curr_p, state
    vix_now, vix_prev = float(vix_close.iloc[-1]), float(vix_close.iloc[-2])
    vix_20d = vix_close.tail(20); vix_mean, vix_std = float(vix_20d.mean()), float(vix_20d.std())
    vix_rev = ((vix_now > (vix_mean + 2*vix_std) or vix_prev > (vix_mean + 2*vix_std)) and vix_now < vix_prev * 0.95 and spy_daily > 0)
    if vix_rev: return "RE-ENTER", "VIX Reversal", curr_p, state
    rebound = (curr_p - state['last_exit_price']) / state['last_exit_price'] if state['last_exit_price'] > 0 else 0
    if rebound >= 0.02: return "RE-ENTER", "2% Rebound", curr_p, state
    return "WAIT", f"Waiting({rebound*100:.1f}%)", curr_p, state

def get_rotation_signal(spy_close, vix_close, close_all, rot_state, per_stock_budget):
    try:
        spy_6m, vix_now = float(spy_close.iloc[-1]/spy_close.iloc[-126]-1), float(vix_close.iloc[-1])
        def calc_mom(s):
            if len(s) < 126: return -999.0
            m1, m3, m6 = (s.iloc[-1]/s.iloc[-21]-1)*100, (s.iloc[-1]/s.iloc[-63]-1)*100, (s.iloc[-1]/s.iloc[-126]-1)*100
            return round(m1*0.4 + m3*0.4 + m6*0.2, 2)
        scores = {t: calc_mom(series) for t, series in close_all.items()}
        eligible = {t: sc for t, sc in scores.items() if close_all[t].iloc[-1] <= per_stock_budget}
        top2 = [t for t, _ in sorted(eligible.items(), key=lambda x: x[1], reverse=True)[:TOP_N]]
        regime_ok = (spy_6m > 0 and vix_now < 25)
        if rot_state['in_market']:
            action = "EXIT" if not regime_ok else ("ROTATE" if set(top2) != set([h['ticker'] for h in rot_state['holdings']]) else "KEEP")
        else: action = "ENTER" if regime_ok else "WAIT"
        return {"action": action, "top2": top2, "scores": scores, "vix_now": vix_now, "spy_6m": spy_6m}
    except: return {"action": "WAIT", "top2": [], "scores": {}, "vix_now": 0, "spy_6m": 0}

# ==============================================================
# 4. 성과 분석 함수
# ==============================================================
def calc_upro_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0, "total_trades": 0, "equity_curve": []}
    if df is None or df.empty: return empty
    df = df.copy(); df['Date'] = pd.to_datetime(df['Date']); df['Price'] = pd.to_numeric(df['Price'], errors='coerce').fillna(0)
    buys = df[df['Action'] == 'BUY'].reset_index(drop=True); sells = df[df['Action'] == 'SELL'].reset_index(drop=True)
    trades, equity = [], 100.0; equity_curve = [{"date": str(buys.loc[0, 'Date'].date()), "equity": 100.0}] if not buys.empty else []
    for i in range(min(len(buys), len(sells))):
        ret = (float(sells.loc[i, 'Price']) - float(buys.loc[i, 'Price'])) / float(buys.loc[i, 'Price']) * 100
        trades.append(ret); equity *= (1 + ret / 100); equity_curve.append({"date": str(sells.loc[i, 'Date'].date()), "equity": round(equity, 2)})
    if not trades: return empty
    wins = [r for r in trades if r > 0]; eq_vals = [e['equity'] for e in equity_curve]; peak, mdd = (eq_vals[0] if eq_vals else 100.0), 0.0
    for v in eq_vals:
        if v > peak: peak = v
        mdd = max(mdd, (peak - v) / peak * 100)
    sharpe = round(np.mean(trades)/(np.std(trades)+1e-9)*np.sqrt(12), 2) if len(trades) >= 3 else 0.0
    return {"total_return": round(equity - 100, 2), "win_rate": round(len(wins)/len(trades)*100, 1), "mdd": round(mdd, 2), "sharpe": sharpe, "total_trades": len(trades), "equity_curve": equity_curve}

def calc_rotation_performance(df):
    empty = {"total_return": 0.0, "win_rate": 0.0, "mdd": 0.0, "sharpe": 0.0, "total_trades": 0, "equity_curve": []}
    if df is None or df.empty or 'RetPct' not in df.columns: return empty
    sells = df[df['Action'] == 'SELL'].copy(); sells['RetPct'] = pd.to_numeric(sells['RetPct'], errors='coerce').fillna(0); sells = sells.sort_values('Date').reset_index(drop=True)
    equity = 100.0; equity_curve = [{"date": str(pd.to_datetime(sells.iloc[0]['Date']).date()), "equity": 100.0}] if not sells.empty else []
    for _, row in sells.iterrows(): equity *= (1 + row['RetPct'] / 100); equity_curve.append({"date": str(pd.to_datetime(row['Date']).date()), "equity": round(equity, 2)})
    rets = sells['RetPct'].tolist(); wins = [r for r in rets if r > 0]
    eq_vals = [e['equity'] for e in equity_curve]; peak, mdd = (eq_vals[0] if eq_vals else 100.0), 0.0
    for v in eq_vals:
        if v > peak: peak = v
        mdd = max(mdd, (peak - v) / peak * 100)
    return {"total_return": round(equity - 100, 2), "win_rate": round(len(wins)/len(rets)*100, 1) if rets else 0.0, "mdd": round(mdd, 2), "sharpe": round(np.mean(rets)/(np.std(rets)+1e-9)*np.sqrt(12), 2) if len(rets)>1 else 0.0, "total_trades": len(rets), "equity_curve": equity_curve}

# ==============================================================
# 5. 통신/AI & Caching
# ==============================================================
def ask_gemini(u_sig, r_sig):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key: return "API 키 없음"
    
    prompt = f"퀀트 전문가로서 분석해줘. UPRO={u_sig}, ROT={r_sig.get('action') if isinstance(r_sig, dict) else r_sig}. 한국어 150자."
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {'Content-Type': 'application/json'}

    # 💡 핵심: 상위 모델부터 순차적으로 시도 (429 에러 발생 시 다음 모델로)
    # 현재 구글 API에서 작동하는 최신 모델 명칭으로 구성했습니다.
    models = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]
    
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            if res.status_code == 200:
                return res.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            elif res.status_code == 429:
                print(f"⚠️ {model} 할당량 초과(429), 다음 모델로 폴백 시도...")
                continue # 다음 모델로 루프 진행
            else:
                print(f"🚨 {model} 에러 ({res.status_code}): {res.text[:100]}")
                continue
        except Exception as e:
            print(f"🚨 {model} 통신 예외 발생: {e}")
            continue
            
    return "AI 연결 실패 (모든 모델 Quota 초과)"

async def tg_send(token_v, chat_id, text):
    if not token_v or not chat_id: return False
    try:
        async with Bot(token=token_v) as bot:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        return True
    except Exception as e:
        try:
            async with Bot(token=token_v) as bot:
                await bot.send_message(chat_id=chat_id, text=text)
            return True
        except: return False

def get_cached_portfolio_equity():
    trader = KIS_Trader()
    bal = trader.get_balance()
    rot_state = load_rotation_state()
    upro_qty = trader.get_holdings(TRADE_TICKER)
    cur_p_upro = trader.get_current_price(TRADE_TICKER)
    upro_value = upro_qty * cur_p_upro
    rot_value = sum(trader.get_holdings(h['ticker']) * trader.get_current_price(h['ticker']) for h in rot_state.get('holdings', []))
    total_equity = bal + upro_value + rot_value
    return total_equity, bal, upro_qty, upro_value, rot_value

# ==============================================================
# 9. 자동매매
# ==============================================================
async def run_trading():
    now_kst = dt.now(KST); current_hour = now_kst.hour
    trader = KIS_Trader(); token_v, chat_id = os.getenv('TELEGRAM_TOKEN'), os.getenv('CHAT_ID')
    spy_ohlc, monthly, vix_close, close_all, d_msg = get_market_data()
    if spy_ohlc.empty: 
        await tg_send(token_v, chat_id, f"⚠️ 데이터 수신 실패 ({d_msg})") # 👈 알람 추가
        return
    rot_state = load_rotation_state()
    total_equity, bal, upro_qty, upro_value, rot_value = get_cached_portfolio_equity()
    per_stock_budget = (total_equity * ROTATION_RATIO * 0.95) / TOP_N
    u_sig, u_re, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_sig = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state, per_stock_budget)

    if current_hour in [20, 2]:
        upro_target, rot_target = total_equity * UPRO_RATIO, total_equity * ROTATION_RATIO
        msgs = [f"🤖 <b>통합봇 v1.5.1 [{now_kst.strftime('%m/%d %H:%M')}]</b>", f"총자산: ${total_equity:,.2f}"]
        upro_gap = max(0, upro_target - upro_value)
        cur_p_upro = trader.get_current_price(TRADE_TICKER)
        if u_sig in ["KEEP", "RE-ENTER"] and upro_gap > (upro_target * 0.1):
            if cur_p_upro > 0:
                qty = int((upro_gap * 0.95) / cur_p_upro)
                if qty >= 1 and trader.send_order(TRADE_TICKER, qty, "BUY").get('rt_cd') == '0':
                    msgs.append(f"✅ UPRO 매수: {qty}주")
                    with open(STATE_FILE, 'w') as f: json.dump({"in_market": True, "last_exit_price": 0}, f)
                    pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "BUY", "Qty": qty, "Price": cur_p_upro}]).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
        elif u_sig == "EXIT" and upro_qty > 0:
            if trader.send_order(TRADE_TICKER, upro_qty, "SELL").get('rt_cd') == '0':
                msgs.append(f"✅ UPRO 매도: {upro_qty}주")
                with open(STATE_FILE, 'w') as f: json.dump({"in_market": False, "last_exit_price": u_p}, f)
                pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Qty": upro_qty, "Price": cur_p_upro}]).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
        
        action, top2 = r_sig['action'], r_sig['top2']
        if action in ["ENTER", "ROTATE"] and top2:
            for h in rot_state.get('holdings', []):
                q = trader.get_holdings(h['ticker'])
                if q > 0 and trader.send_order(h['ticker'], q, "SELL").get('rt_cd') == '0':
                    cp = trader.get_current_price(h['ticker']); ret = (cp - h.get('entry_price', cp)) / max(h.get('entry_price', cp), 1) * 100
                    pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Ticker": h['ticker'], "Qty": q, "Price": cp, "RetPct": round(ret, 2)}]).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
            time.sleep(2); new_h = []
            for t in top2:
                p = trader.get_current_price(t)
                qty = int(((rot_target * 0.95) / len(top2)) / p) if p > 0 else 0
                if qty >= 1:
                    if trader.send_order(t, qty, "BUY").get('rt_cd') == '0':
                        new_h.append({"ticker": t, "qty": qty, "entry_price": p})
                        pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "BUY", "Ticker": t, "Qty": qty, "Price": p, "RetPct": 0}]).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                        msgs.append(f"✅ ROT 매수 성공: {t} {qty}주")
            rot_state.update({"in_market": len(new_h) > 0, "holdings": new_h}); save_rotation_state(rot_state)
        elif action == "EXIT" and rot_state.get('in_market'):
            remaining = []
            for h in rot_state.get('holdings', []):
                q = trader.get_holdings(h['ticker'])
                if q > 0:
                    if trader.send_order(h['ticker'], q, "SELL").get('rt_cd') == '0':
                        cp = trader.get_current_price(h['ticker']); ret = (cp - h.get('entry_price', cp)) / max(h.get('entry_price', cp), 1) * 100
                        pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Ticker": h['ticker'], "Qty": q, "Price": cp, "RetPct": round(ret, 2)}]).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                    else: remaining.append(h)
            rot_state.update({"in_market": len(remaining) > 0, "holdings": remaining}); save_rotation_state(rot_state); msgs.append("🚨 ROT 하락장 청산")
        msgs.append(f"🧠 AI: {ask_gemini(u_sig, r_sig)}"); await tg_send(token_v, chat_id, "\n".join(msgs))

    elif current_hour in [1, 2]:
        spy_int = _yf_download_with_retry(SIGNAL_TICKER, period='5d', interval='5m')
        if not spy_int.empty:
            if isinstance(spy_int.columns, pd.MultiIndex): spy_int.columns = spy_int.columns.get_level_values(0)
            today_us = (now_kst - timedelta(hours=13)).date()
            spy_today = spy_int[spy_int.index.tz_convert(None).date == today_us] if spy_int.index.tz else spy_int[spy_int.index.date == today_us]
            spy_check = spy_today if len(spy_today) >= 5 else spy_int
            day_ret = (float(spy_check['Close'].iloc[-1]) / float(spy_check['Open'].iloc[0])) - 1
            if day_ret <= -0.03:
                q_u = trader.get_holdings(TRADE_TICKER)
                if q_u > 0:
                    if trader.send_order(TRADE_TICKER, q_u, "SELL").get('rt_cd') == '0':
                        exit_p = trader.get_current_price(TRADE_TICKER)
                        pd.DataFrame([{"Date": dt.now().strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Qty": q_u, "Price": exit_p}]).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
                        with open(STATE_FILE, 'w') as f: json.dump({"in_market": False, "last_exit_price": exit_p}, f)
                if rot_state.get('in_market'):
                    rem = []
                    for h in rot_state['holdings']:
                        q_h = trader.get_holdings(h['ticker'])
                        if q_h > 0:
                            if trader.send_order(h['ticker'], q_h, "SELL").get('rt_cd') == '0':
                                cp_h = trader.get_current_price(h['ticker']); ret_h = (cp_h - h.get('entry_price', cp_h)) / max(h.get('entry_price', cp_h), 1) * 100
                                pd.DataFrame([{"Date": dt.now().strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Ticker": h['ticker'], "Qty": q_h, "Price": cp_h, "RetPct": round(ret_h, 2)}]).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                            else: rem.append(h)
                    rot_state.update({"in_market": len(rem) > 0, "holdings": rem}); save_rotation_state(rot_state)
                await tg_send(token_v, chat_id, "🚨 긴급 탈출 실행 완료")

    elif current_hour in [7, 8]:
        bal_7 = trader.get_balance()
        msg = f"📋 <b>아침 리포트</b>\n잔고: ${bal_7:,.2f} | SPY 6M: {r_sig['spy_6m']*100:+.1f}%\n🧠 {ask_gemini('morning', r_sig)}"
        await tg_send(token_v, chat_id, msg)

# ==============================================================
# 10. Dashboard
# ==============================================================
def plot_perf_chart(perf_data, name, color, spy_series):
    fig = go.Figure()
    if not perf_data['equity_curve']: return fig
    dates = [e['date'] for e in perf_data['equity_curve']]
    eqs = [e['equity'] for e in perf_data['equity_curve']]
    fig.add_trace(go.Scatter(x=dates, y=eqs, fill='tozeroy', name=f'{name} 실제', line=dict(color=color)))
    try:
        start_dt = pd.to_datetime(dates[0])
        if spy_series.index.tz is not None: spy_series = spy_series.tz_convert(None)
        spy_sub = spy_series[spy_series.index >= start_dt]
        if not spy_sub.empty:
            spy_norm = (spy_sub / spy_sub.iloc[0]) * 100
            fig.add_trace(go.Scatter(x=spy_sub.index, y=spy_norm.values, name='SPY (존버)', line=dict(color='gray', dash='dot')))
    except: pass
    fig.update_layout(margin=dict(l=0, r=0, t=20, b=0), template="plotly_dark", height=250)
    return fig

def run_dashboard():
    st.set_page_config(page_title="Unified Bot v1.5.1", layout="wide")
    spy_ohlc, monthly, vix_close, close_all, data_msg = get_market_data()
    if spy_ohlc.empty: return
    total_equity, bal, upro_qty, upro_value, rot_value = get_cached_portfolio_equity()
    actual_per_stock_budget = (total_equity * ROTATION_RATIO * 0.95) / TOP_N if total_equity > 10 else 99999
    rot_state = load_rotation_state()
    u_sig, u_re, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_sig = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state, actual_per_stock_budget)
    df_upro = pd.read_csv(HISTORY_FILE) if os.path.exists(HISTORY_FILE) else pd.DataFrame()
    df_rot = pd.read_csv(ROTATION_HISTORY_FILE) if os.path.exists(ROTATION_HISTORY_FILE) else pd.DataFrame()
    upro_perf = calc_upro_performance(df_upro); rot_perf = calc_rotation_performance(df_rot)

    tab1, tab2, tab3 = st.tabs(["📡 실시간 현황", "📊 성과 분석", "📋 거래 로그"])
    with tab1:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("UPRO", "IN" if u_st.get('in_market') else "OUT", u_sig)
        c2.metric("ROT TOP2", ", ".join(r_sig['top2']), r_sig['action'])
        c3.metric("SPY", f"${spy_ohlc['Close'].iloc[-1]:.2f}")
        c4.metric("VIX", f"{r_sig['vix_now']:.1f}"); c5.metric("SPY 6M", f"{r_sig['spy_6m']*100:+.1f}%")
        spy126 = spy_ohlc.tail(126); vix126 = vix_close.tail(126)
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.7, 0.3])
        fig.add_trace(go.Candlestick(x=spy126.index, open=spy126['Open'], high=spy126['High'], low=spy126['Low'], close=spy126['Close'], name="SPY"), row=1, col=1)
        fig.add_trace(go.Scatter(x=vix126.index, y=vix126.values, name="VIX", line=dict(color='orange')), row=2, col=1)
        fig.update_layout(xaxis_rangeslider_visible=False, height=650, template="plotly_dark")
        st.plotly_chart(fig, use_container_width=True)

    with tab2:
        st.subheader("🚀 UPRO 상세 성과"); k1, k2, k3, k4 = st.columns(4)
        k1.metric("수익률", f"{upro_perf['total_return']:+.1f}%"); k2.metric("MDD", f"-{upro_perf['mdd']:.1f}%")
        k3.metric("승률", f"{upro_perf['win_rate']}%"); k4.metric("샤프지수", upro_perf['sharpe'])
        if upro_perf['equity_curve']: st.plotly_chart(plot_perf_chart(upro_perf, 'UPRO', '#58a6ff', spy_ohlc['Close']), use_container_width=True)
        st.divider(); st.subheader("🔄 ROT 상세 성과"); r1, r2, r3, r4 = st.columns(4)
        r1.metric("수익률", f"{rot_perf['total_return']:+.1f}%"); r2.metric("MDD", f"-{rot_perf['mdd']:.1f}%")
        r3.metric("승률", f"{rot_perf['win_rate']}%"); r4.metric("거래횟수", rot_perf['total_trades'])
        if rot_perf['equity_curve']: st.plotly_chart(plot_perf_chart(rot_perf, 'ROT', '#fbbf24', spy_ohlc['Close']), use_container_width=True)

    with tab3: st.subheader("거래 이력"); st.dataframe(df_upro.tail(10)); st.dataframe(df_rot.tail(10))

if __name__ == "__main__":
    if os.getenv('GITHUB_ACTIONS') == 'true': asyncio.run(run_trading())
    else: run_dashboard()
