"""
Unified Trading Bot v1.3.2
UPRO Trend Bot (70%) + Momentum Rotation Bot (30%)
[수정] JSONDecodeError 완벽 방지 + v1.3.0 상세 로직 100% 복구
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
                print(f"[KIS] 토큰 발급 실패: {self.error_detail}")
            else:
                print(f"[KIS] 토큰 발급 성공")
        except Exception as e:
            self.error_detail = f"Conn: {str(e)}"
            print(f"[KIS] 토큰 연결 오류: {e}")

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
            amt = float(res.get('output', {}).get('ord_psbl_frcr_amt', 0))
            print(f"[KIS] 잔고 조회: ${amt:.2f}")
            return amt
        except Exception as e:
            print(f"[KIS] 잔고 조회 실패: {e}")
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
        except Exception as e:
            print(f"[KIS] 보유조회 실패: {e}")
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

    def send_order(self, ticker, qty, side="BUY"):
        if not self.token: return {"rt_cd": "1", "msg1": "No Token"}
        try:
            url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
            tr_id = "TTTT1002U" if side == "BUY" else "TTTT1006U"
            
            curr_p = self.get_current_price(ticker)
            if curr_p <= 0: return {"rt_cd": "1", "msg1": "Price Fetch Fail"}
            order_p = curr_p * 1.01 if side == "BUY" else curr_p * 0.99
            
            # [진짜 핵심 수정] 종목별 거래소 구분
            # UPRO, SPY는 AMEX / 나머지는 NASD (나스닥)
            exch_cd = "AMEX" if ticker in ["UPRO", "SPY"] else "NASD"
            
            data = {
                "CANO":            self.cano,
                "ACNT_PRDT_CD":    self.acnt_prdt_cd,
                "OVRS_EXCG_CD":    exch_cd,  # 고정된 "AMEX"를 exch_cd로 변경!
                "PDNO":            ticker,
                "ORD_QTY":         str(int(float(qty))),
                "OVRS_ORD_UNPR":   f"{order_p:.2f}",
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN":        "00",
            }
            res = requests.post(url, headers=self._headers(tr_id), data=json.dumps(data)).json()
            # 실패 원인을 로그로 남겨야 나중에 또 안될 때 바로 알 수 있습니다.
            print(f"[KIS] {side} {ticker} {qty}주 결과: {res.get('rt_cd')}, {res.get('msg1')}")
            return res
        except Exception as e: return {"rt_cd": "1", "msg1": str(e)}




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
# 4. UPRO 봇 신호
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

# ==============================================================
# 5. Rotation 봇 신호
# ==============================================================

def load_rotation_state():
    default = {"in_market": False, "holdings": [], "entry_date": None, "consecutive_loss": 0}
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
        spy_6m    = float(spy_close.iloc[-1] / spy_close.iloc[-126] - 1) if len(spy_close) >= 126 else 0.0
        vix_now   = float(vix_close.iloc[-1])

        scores = {t: calc_momentum(close_all[t]) for t in CANDIDATE_POOL if t in close_all}
        top2   = [t for t, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:TOP_N]]

        regime_ok = (spy_6m > SPY_6M_MIN and vix_now < VIX_ENTER_MAX)

        if rot_state['in_market']:
            current = set(h['ticker'] for h in rot_state['holdings'])
            if not regime_ok:         action = "EXIT"
            elif set(top2) != current: action = "ROTATE"
            else:                      action = "KEEP"
        else:
            action = "ENTER" if regime_ok else "WAIT"

        return {"action": action, "top2": top2, "scores": scores,
                "vix_now": vix_now, "spy_6m": spy_6m,
                "regime_ok": regime_ok, "spy_daily": spy_daily}
    except:
        return {"action": "WAIT", "top2": [], "vix_now": 0, "spy_6m": 0, "regime_ok": False, "spy_daily": 0, "scores": {}}

# ==============================================================
# 6. 성과 분석 (v1.3.0 전체 복구)
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
    trades, equity = [], 100.0
    equity_curve = ([{"date": str(buys.loc[0, 'Date'].date()), "equity": 100.0}] if not buys.empty else [])
    for i in range(min(len(buys), len(sells))):
        buy_p  = float(buys.loc[i, 'Price'])
        sell_p = float(sells.loc[i, 'Price'])
        if buy_p <= 0: continue
        ret = (sell_p - buy_p) / buy_p * 100
        trades.append(ret)
        equity *= (1 + ret / 100)
        equity_curve.append({"date": str(sells.loc[i, 'Date'].date()), "equity": round(equity, 2)})
    if not trades: return empty
    wins, losses = [r for r in trades if r > 0], [r for r in trades if r <= 0]
    eq_vals = [e['equity'] for e in equity_curve]
    peak, mdd = (eq_vals[0] if eq_vals else 100.0), 0.0
    for v in eq_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd: mdd = dd
    sharpe = round(np.mean(trades) / (np.std(trades) + 1e-9) * np.sqrt(12), 2) if len(trades) > 1 else 0.0
    return {"total_return": round(equity - 100, 2), "win_rate": round(len(wins)/len(trades)*100, 1),
            "mdd": round(mdd, 2), "sharpe": sharpe, "total_trades": len(trades), "win_trades": len(wins),
            "loss_trades": len(losses), "avg_profit": round(np.mean(wins), 2) if wins else 0.0,
            "avg_loss": round(np.mean(losses), 2) if losses else 0.0, "best_trade": round(max(trades), 2),
            "worst_trade": round(min(trades), 2), "equity_curve": equity_curve}

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
    sells = sells.sort_values('Date').reset_index(drop=True)
    rets, equity = sells['RetPct'].tolist(), 100.0
    equity_curve = [{"date": str(sells.iloc[0]['Date'].date()), "equity": 100.0}]
    for _, row in sells.iterrows():
        equity *= (1 + row['RetPct'] / 100)
        equity_curve.append({"date": str(row['Date'].date()), "equity": round(equity, 2)})
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r <= 0]
    eq_vals = [e['equity'] for e in equity_curve]
    peak, mdd = eq_vals[0], 0.0
    for v in eq_vals:
        if v > peak: peak = v
        dd = (peak - v) / peak * 100
        if dd > mdd: mdd = dd
    sharpe = round(np.mean(rets) / (np.std(rets) + 1e-9) * np.sqrt(12), 2) if len(rets) > 1 else 0.0
    return {"total_return": round(equity - 100, 2), "win_rate": round(len(wins)/len(rets)*100, 1) if rets else 0.0,
            "mdd": round(mdd, 2), "sharpe": sharpe, "total_trades": len(rets), "win_trades": len(wins),
            "loss_trades": len(losses), "avg_profit": round(np.mean(wins), 2) if wins else 0.0,
            "avg_loss": round(np.mean(losses), 2) if losses else 0.0, "best_trade": round(max(rets), 2) if rets else 0.0,
            "worst_trade": round(min(rets), 2) if rets else 0.0, "equity_curve": equity_curve}

# ==============================================================
# 7. Gemini & Telegram 헬퍼
# ==============================================================

def ask_gemini(u_sig, r_sig):
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key: return "API 키 누락"
    
    # [2026년 표준] v1beta 대신 v1 정식 엔드포인트를 사용합니다.
    # 모델명에서 -latest를 제거한 가장 안정적인 식별자를 씁니다.
    url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={api_key}"
    headers = {'Content-Type': 'application/json'}
    
    # 시장 상황에 따른 프롬프트 구성
    prompt = (f"UPRO 신호: {u_sig}, ROT 액션: {r_sig.get('action')}. "
              "한국어 150자 내외로 전문적인 투자 리스크와 대응 전략을 요약해줘.")
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0.7}
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        res_json = res.json()
        
        # 성공 시 텍스트 추출
        if res.status_code == 200 and 'candidates' in res_json:
            return res_json['candidates'][0]['content']['parts'][0]['text'].strip()
        
        # 404 등 에러 발생 시 원인을 정확히 로그에 찍어 다음 조치를 가능하게 합니다.
        err_msg = res_json.get('error', {}).get('message', 'Unknown Error')
        return f"AI 지연 (Error {res.status_code}: {err_msg[:40]})"
        
    except Exception as e:
        return f"AI 연결 실패: {str(e)[:20]}"



async def tg_send(bot, chat_id, text):
    if not bot or not chat_id:
        print(f"[TG] 전송 불가 - 봇 또는 ID 누락")
        return False
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        print(f"[TG] 전송 성공")
        return True
    except Exception as e:
        print(f"[TG] 전송 실패: {e}")
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        except: return False

# ==============================================================
# 9. 자동매매 (v1.3.0 스케줄 및 디버그 로직 완전 복구)
# ==============================================================

async def run_trading():
    now_kst      = dt.now(KST)
    current_hour = now_kst.hour
    print(f"\n{'='*60}\n[START] {now_kst.strftime('%Y-%m-%d %H:%M:%S')} (hour={current_hour})\n{'='*60}")

    token_v, chat_id = os.getenv('TELEGRAM_TOKEN'), os.getenv('CHAT_ID')
    bot = Bot(token=token_v) if (Bot and token_v) else None
    trader = KIS_Trader()

    # [MISSING-1] 환경변수 체크 로그 (실행 시 설정 오류 확인용)
    print(f"[ENV] TG_TOKEN={'OK' if token_v else 'MISSING'} | KIS_KEY={'OK' if os.getenv('KIS_APPKEY') else 'MISSING'}")
    print(f"[ENV] GEMINI_KEY={'OK' if os.getenv('GEMINI_API_KEY') else 'MISSING'}")

    spy_ohlc, monthly, vix_close, close_all, d_msg = get_market_data()
    if spy_ohlc.empty:
        await tg_send(bot, chat_id, f"⚠️ 데이터 로드 실패: {d_msg}")
        return

    rot_state = load_rotation_state()
    u_sig, u_re, u_p, u_st = get_upro_signal(spy_ohlc['Close'], monthly, vix_close)
    r_sig = get_rotation_signal(spy_ohlc['Close'], vix_close, close_all, rot_state)

    # [MISSING-1] 현재 신호 상태 로그
    print(f"[SIG] UPRO={u_sig}({u_re}) | ROT={r_sig['action']} | TOP={r_sig['top2']}")
    print(f"[SIG] VIX={r_sig['vix_now']:.1f} | SPY 6M={r_sig['spy_6m']*100:+.1f}%")

    # ------------------------------------------------------
    # 1. 정규 매매 섹션 (사용자 설정 시간에 실행)
    # ------------------------------------------------------
    if current_hour == 9: # 민환님 설정에 따라 0 또는 7로 조정
        bal = trader.get_balance()
        msgs = [f"🤖 <b>통합봇 정규매매 [{now_kst.strftime('%m/%d %H:%M')} KST]</b>", f"잔고: ${bal:,.2f}"]
        
        # [A] UPRO 섹션
        upro_budget, cur_p_upro = bal * UPRO_RATIO, trader.get_current_price(TRADE_TICKER)
        qty_upro = trader.get_holdings(TRADE_TICKER)
        
        if u_sig in ["KEEP", "RE-ENTER"] and qty_upro == 0 and cur_p_upro > 0:
            buy_qty = int((upro_budget * 0.95) / cur_p_upro)
            if buy_qty >= 1:
                res = trader.send_order(TRADE_TICKER, buy_qty, "BUY")
                if res.get('rt_cd') == '0':
                    msgs.append(f"✅ UPRO 매수: {buy_qty}주")
                    with open(STATE_FILE, 'w') as f: json.dump({"in_market": True, "last_exit_price": 0}, f)
                    pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "BUY", "Qty": buy_qty, "Price": cur_p_upro}]).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
                else: msgs.append(f"❌ UPRO 매수실패: {res.get('msg1')}")
        elif u_sig == "EXIT" and qty_upro > 0:
            res = trader.send_order(TRADE_TICKER, qty_upro, "SELL")
            if res.get('rt_cd') == '0':
                msgs.append(f"✅ UPRO 매도: {qty_upro}주")
                with open(STATE_FILE, 'w') as f: json.dump({"in_market": False, "last_exit_price": u_p}, f)
                pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Qty": qty_upro, "Price": cur_p_upro}]).to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
        else: msgs.append(f"📈 UPRO 유지 ({u_sig})")

        # [B] Rotation 섹션
        action, top2, rot_budget = r_sig['action'], r_sig['top2'], bal * ROTATION_RATIO
        if action in ["ENTER", "ROTATE"]:
            if rot_state.get('in_market'):
                for h in rot_state.get('holdings', []):
                    qty_h = trader.get_holdings(h['ticker'])
                    if qty_h > 0:
                        res = trader.send_order(h['ticker'], qty_h, "SELL")
                        if res.get('rt_cd') == '0':
                            msgs.append(f"✅ ROT 매도(교체): {h['ticker']}")
                            # 로그 기록
                            cur_p_h = trader.get_current_price(h['ticker'])
                            ret = (cur_p_h - h.get('entry_price', cur_p_h)) / max(h.get('entry_price', cur_p_h), 1) * 100
                            pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "SELL", "Ticker": h['ticker'], "Qty": qty_h, "Price": cur_p_h, "RetPct": round(ret, 2)}]).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                time.sleep(2)

            new_h, per_stock = [], (rot_budget * 0.95) / len(top2) if top2 else 0
            for t in top2:
                p = trader.get_current_price(t)
                qty = int(per_stock / p) if p > 0 else 0
                if qty >= 1:
                    res = trader.send_order(t, qty, "BUY")
                    if res.get('rt_cd') == '0':
                        msgs.append(f"✅ ROT 매수: {t} ({qty}주)")
                        new_h.append({"ticker": t, "qty": qty, "entry_price": p})
                        pd.DataFrame([{"Date": now_kst.strftime("%Y-%m-%d %H:%M"), "Action": "BUY", "Ticker": t, "Qty": qty, "Price": p, "RetPct": 0}]).to_csv(ROTATION_HISTORY_FILE, mode='a', header=not os.path.exists(ROTATION_HISTORY_FILE), index=False)
                    else: msgs.append(f"❌ ROT 실패({t}): {res.get('msg1')}")
                else:
                    # [돈 부족 알림]
                    msgs.append(f"⚠️ ROT 매수불가({t}): 주가(${p})가 예산(${per_stock:.1f})보다 비쌈")

            if new_h:
                rot_state.update({"in_market": True, "holdings": new_h, "entry_date": now_kst.strftime("%Y-%m-%d")})
                save_rotation_state(rot_state)
        elif action == "KEEP":
            hold_list = [h['ticker'] for h in rot_state.get('holdings', [])]
            msgs.append(f"💎 ROT 유지: {', '.join(hold_list) if hold_list else '보유 종목 없음'}")
        elif action == "WAIT":
            msgs.append(f"⏳ ROT 관망 (VIX: {r_sig.get('vix_now', 0):.1f})")
        elif action == "EXIT":
            if rot_state.get('in_market'):
                for h in rot_state.get('holdings', []):
                    qty_h = trader.get_holdings(h['ticker'])
                    if qty_h > 0: trader.send_order(h['ticker'], qty_h, "SELL")
                rot_state.update({"in_market": False, "holdings": []})
                save_rotation_state(rot_state)
            msgs.append("🚨 ROT 하락장 대응 전량 청산")

        msgs.append(f"🧠 AI: {ask_gemini(u_sig, r_sig)}")
        await tg_send(bot, chat_id, "\n".join(msgs))

    # ------------------------------------------------------
    # 2. 긴급 탈출 섹션 (01시) - [BUG-6 수정 반영]
    # ------------------------------------------------------
    elif current_hour == 1:
        spy_int = yf.download(SIGNAL_TICKER, period='1d', interval='5m', progress=False)
        if not spy_int.empty:
            if isinstance(spy_int.columns, pd.MultiIndex): spy_int.columns = spy_int.columns.get_level_values(0)
            day_ret = (float(spy_int['Close'].iloc[-1]) / float(spy_int['Open'].iloc[0])) - 1
            if day_ret <= -0.03:
                # UPRO 청산
                qty_upro = trader.get_holdings(TRADE_TICKER)
                if qty_upro > 0: trader.send_order(TRADE_TICKER, qty_upro, "SELL")
                # [BUG-6] ROT 청산 로직 추가
                if rot_state.get('in_market'):
                    for h in rot_state.get('holdings', []):
                        qty_h = trader.get_holdings(h['ticker'])
                        if qty_h > 0: trader.send_order(h['ticker'], qty_h, "SELL")
                    rot_state.update({"in_market": False, "holdings": []})
                    save_rotation_state(rot_state)
                await tg_send(bot, chat_id, f"🚨 <b>긴급 탈출 실행</b> (SPY {day_ret*100:.1f}%) - 전 포지션 정리")

    # ------------------------------------------------------
    # 3. 아침 리포트 섹션 (07시) - [BUG-5 수정 반영]
    # ------------------------------------------------------
    elif current_hour == 7:
        bal = trader.get_balance()
        qty_upro = trader.get_holdings(TRADE_TICKER)
        rot_hold = ", ".join([h['ticker'] for h in rot_state.get('holdings', [])]) or "CASH"
        
        msg = (
            f"📋 <b>아침 리포트 [{now_kst.strftime('%m/%d %H:%M')}]</b>\n"
            f"잔고: ${bal:,.2f}\n"
            f"UPRO: {str(qty_upro)+'주' if qty_upro > 0 else 'CASH'}\n"
            f"ROT: {rot_hold}\n"
            f"SPY 6M: {r_sig['spy_6m']*100:+.1f}% | VIX: {r_sig['vix_now']:.1f}\n"
            f"TOP{TOP_N}: {', '.join(r_sig['top2'])}\n"
            f"🧠 {ask_gemini('morning', r_sig)}"
        )
        await tg_send(bot, chat_id, msg)

    # ------------------------------------------------------
    # 4. 수동 테스트 섹션 (그 외 시간) - [BUG-7 수정 반영]
    # ------------------------------------------------------
    else:
        test_msg = (
            f"🧪 <b>수동 실행 테스트 [{now_kst.strftime('%m/%d %H:%M')}]</b>\n"
            f"잔고: ${trader.get_balance():,.2f}\n"
            f"UPRO 신호: {u_sig} ({u_re})\n"
            f"ROT 신호: {r_sig['action']} | TOP{TOP_N}: {', '.join(r_sig['top2'])}\n"
            f"VIX: {r_sig['vix_now']:.1f} | SPY 6M: {r_sig['spy_6m']*100:+.1f}%\n"
            f"ℹ️ 현재 스케줄 외 시간 — 매매 스킵"
        )
        await tg_send(bot, chat_id, test_msg)

# ==============================================================
# 10. Streamlit 대시보드 (v1.3.0 완전 복구)
# ==============================================================

def run_dashboard():
    now_kst = dt.now(KST)
    st.set_page_config(page_title="Unified Bot v1.3", layout="wide", page_icon="🤖")
    st.markdown("""
    <style>
    .main { background: #0a0f1e; }
    h1, h2, h3 { color: #e0f0ff !important; }
    div[data-testid="metric-container"] { background: rgba(0,20,50,0.6); border: 1px solid #1a3050; border-radius: 10px; padding: 8px 12px; }
    </style>
    """, unsafe_allow_html=True)
    with st.sidebar:
        st.title("Unified Bot v1.3")
        st.caption(f"Update: {now_kst.strftime('%H:%M:%S')} KST")
        st.divider()
        st.markdown(f"**자본 배분**\n- UPRO: {int(UPRO_RATIO*100)}%\n- ROT: {int(ROTATION_RATIO*100)}%")
        st.divider()
        st.markdown("**UPRO EXIT**\n- VIX+30%, SPY-3%, 3d-5%, 2m하락")
        st.markdown("**ROT 조건**\n- SPY 6M>0 & VIX<25")
        st.divider()
        st.markdown("**후보 풀**")
        for t in CANDIDATE_POOL: st.caption(f"- {t}")

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
    tab1, tab2, tab3 = st.tabs(["📡 실시간 현황", "📊 성과 분석", "📋 거래 로그"])

    with tab1:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("UPRO 포지션", "IN" if u_st.get('in_market') else "OUT", u_signal)
        c2.metric("ROT Action", r_signal['action'], f"TOP2: {', '.join(r_signal['top2'])}")
        c3.metric("SPY Price", f"${spy_ohlc['Close'].iloc[-1]:.2f}")
        c4.metric("VIX", f"{r_signal['vix_now']:.1f}")
        c5.metric("SPY 6M", f"{r_signal['spy_6m']*100:+.1f}%")
        
        spy126, vix126 = spy_ohlc.tail(126), vix_close.tail(126)
        fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.5, 0.25, 0.25], vertical_spacing=0.04)
        fig.add_trace(go.Candlestick(x=spy126.index, open=spy126['Open'], high=spy126['High'], low=spy126['Low'], close=spy126['Close'], name="SPY"), row=1, col=1)
        fig.add_trace(go.Bar(x=vix126.index, y=vix126.values, name="VIX", marker_color="orange"), row=2, col=1)
        fig.add_trace(go.Bar(x=spy126.index, y=spy126['Volume'], name="Volume", marker_color="steelblue"), row=3, col=1)
        fig.update_layout(xaxis_rangeslider_visible=False, height=650, template="plotly_dark", margin=dict(t=10, b=10), paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e')
        st.plotly_chart(fig, use_container_width=True)
        if r_signal.get('scores'):
            st.subheader("모멘텀 스코어 (Momentum Scores)")
            sc_df = pd.DataFrame([
                {"Ticker": t, "Score": s, "TOP1": "★" if t in r_signal['top2'] else ""}
                for t, s in sorted(r_signal['scores'].items(), key=lambda x: x[1], reverse=True)
            ])
            st.dataframe(sc_df, use_container_width=True, hide_index=True)


    with tab2:
        for label, perf in [("UPRO 트렌드 봇 (70%)", upro_perf), ("모멘텀 로테이션 봇 (30%)", rot_perf)]:
            st.markdown(f"**{label}**")
            k1, k2, k3, k4, k5, k6 = st.columns(6)
            k1.metric("총 수익률", f"{perf['total_return']:+.1f}%")
            k2.metric("MDD", f"-{perf['mdd']:.1f}%")
            k3.metric("승률", f"{perf['win_rate']:.0f}%")
            k4.metric("샤프", f"{perf['sharpe']:.2f}")
            k5.metric("거래 횟수", f"{perf['total_trades']}회")
            k6.metric("최대 수익/손실", f"{perf['best_trade']:+.1f}% / {perf['worst_trade']:+.1f}%")
            if perf['equity_curve']:
                eq_df = pd.DataFrame(perf['equity_curve'])
                fig_eq = go.Figure(go.Scatter(x=eq_df['date'], y=eq_df['equity'], fill='tozeroy', line=dict(color='#3fb950', width=2)))
                fig_eq.update_layout(template='plotly_dark', height=260, margin=dict(t=10, b=10), paper_bgcolor='#0a0f1e', plot_bgcolor='#0a0f1e', yaxis_title="누적 수익 (시작=100)")
                st.plotly_chart(fig_eq, use_container_width=True)
            st.divider()

    with tab3:
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("UPRO Log")
            if not df_upro.empty: st.dataframe(df_upro.tail(20).iloc[::-1], use_container_width=True, hide_index=True)
            else: st.info("기록 없음")
        with col2:
            st.subheader("Rotation Log")
            if not df_rot.empty: st.dataframe(df_rot.tail(20).iloc[::-1], use_container_width=True, hide_index=True)
            else: st.info("기록 없음")

# ==============================================================
# 11. 진입점
# ==============================================================

if __name__ == "__main__":
    if os.getenv('GITHUB_ACTIONS') == 'true':
        asyncio.run(run_trading())
    else:
        run_dashboard()
