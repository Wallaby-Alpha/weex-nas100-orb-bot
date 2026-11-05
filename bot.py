"""
weex-nas100-orb-bot / bot.py
----------------------------
Dual-Engine (Continuation 2.0R + Mean Reversion 1.5R) Trading Bot for WEEX QQQUSDT.
Executes JJ Simon's Syndicate Model on live micro-capital ($1.00 risk):

Two Core Strategy Engines:
1. Channel A: 1-Minute Continuation (2.0R Target)
   - Trigger: 1m candle close breakout outside the 15m Opening Range (09:30-09:45 ET or 20:00-20:15 ET).
   - Stop Loss: 1.0 pt (on QQQ). Take Profit: 2.0 pt (2.0R).
2. Channel B: 5-Minute Mean Reversion (1.5R Target - JJ Simon)
   - Trigger: Price sweeps outside the 15m range, but a 5m candle closes BACK INSIDE (Failed Auction Reclaim).
   - Stop Loss: Extreme sweep wick + 0.15 pt buffer.
   - Take Profit: Stop Distance * 1.5 (1.5R Target).

Modes Supported:
- "HYBRID": Looks for 1m Continuation in Phase A (early session), then scans for 5m Reversion in Phase B.
- "CONTINUATION": Exclusively trades 1m Breakout Continuation @ 2.0R.
- "REVERSION": Exclusively trades 5m Failed Auction Reclaim @ 1.5R.

Core Features:
- Micro-Risk Sizing: Exactly $1.00 USD risk per trade on WEEX contracts.
- Shadow Prop Firm Tracker: Simulates a $50K MFF Rapid account (8 MNQ Eval -> 4 MNQ Funded, 90% split).
- Telegram Broadcasts: Instant order fill alerts with strategy tags (🚀 Continuation vs 🧲 Reversion).
"""

import os
import sys
import json
import time
import hmac
import hashlib
import base64
import logging
from pathlib import Path
from datetime import datetime, time as dtime
from typing import Dict, Any, List, Optional, Tuple
import requests
import pandas as pd
import pytz
from prop_tracker import PropFirmTracker

# Ensure UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("weex_nas100_orb.log", mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger("weex_qqq_syndicate")

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
EXAMPLE_CONFIG_PATH = SCRIPT_DIR / "config.example.json"
STATE_FILE = SCRIPT_DIR / "orb_state.json"

NY_TZ = pytz.timezone("America/New_York")


def load_config() -> Dict[str, Any]:
    default_cfg = {
        "api_key": os.environ.get("WEEX_API_KEY", ""),
        "secret_key": os.environ.get("WEEX_SECRET_KEY", ""),
        "passphrase": os.environ.get("WEEX_PASSPHRASE", ""),
        "dry_run": False,
        "symbol": "QQQUSDT",
        "strategy_mode": "HYBRID", # "HYBRID", "CONTINUATION", "REVERSION"
        "risk_usd": 1.0,
        "continuation_stop_pts": 1.0,
        "continuation_target_pts": 2.0,
        "reversion_r": 1.5,
        "trade_asia": True,
        "trade_ny": True,
        "poll_interval_seconds": 5,
        "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", "")
    }

    candidates = [CONFIG_PATH, EXAMPLE_CONFIG_PATH]
    for c in candidates:
        if c.exists():
            try:
                with open(c, "r", encoding="utf-8") as f:
                    file_cfg = json.load(f)
                    default_cfg.update(file_cfg)
                    logger.info(f"Loaded configuration from {c}")
                    break
            except Exception as e:
                logger.warning(f"Error reading {c}: {e}")

    return default_cfg


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")
        return False


class WeexClient:
    def __init__(self, api_key: str, secret_key: str, passphrase: str, dry_run: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.dry_run = dry_run
        self.base_url = "https://api-contract.weex.com"
        self.exchange_info: Dict[str, Dict[str, Any]] = {}
        self._load_exchange_info()

    def _get_headers(self, method: str, request_path: str, body: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        sign_str = timestamp + method.upper() + request_path
        if body:
            sign_str += body

        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256
        ).digest()
        sign_b64 = base64.b64encode(signature).decode("utf-8")

        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign_b64,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US"
        }

    def _load_exchange_info(self):
        url = f"{self.base_url}/capi/v3/market/exchangeInfo"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json().get("symbols", [])
                meta = {}
                for s in data:
                    sym = s.get("symbol", "")
                    meta[sym] = {
                        "pricePrecision": int(s.get("pricePrecision", 2)),
                        "quantityPrecision": int(s.get("quantityPrecision", 2)),
                        "contractVal": float(s.get("contractVal", 0.01)),
                        "minOrderSize": float(s.get("minOrderSize", 0.01)),
                        "maxOrderSize": float(s.get("maxOrderSize", 100000000.0)),
                    }
                self.exchange_info = meta
                logger.info(f"Loaded exchange specifications for {len(self.exchange_info)} symbols.")
        except Exception as e:
            logger.warning(f"Failed to fetch exchangeInfo: {e}")

    def get_latest_price(self, symbol: str) -> Optional[float]:
        url = f"{self.base_url}/capi/v3/market/ticker/24hr?symbol={symbol}"
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and len(data) > 0:
                    return float(data[0].get("lastPrice", 0.0))
        except Exception as e:
            logger.warning(f"Error fetching ticker price: {e}")
        return None

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 60) -> Optional[pd.DataFrame]:
        url = f"{self.base_url}/capi/v3/market/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        try:
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                raw_data = r.json()
                if not isinstance(raw_data, list) or len(raw_data) == 0:
                    return None
                cols = [
                    "open_time", "open", "high", "low", "close", "volume",
                    "close_time", "quote_volume", "trades", "taker_base_vol", "taker_quote_vol"
                ]
                df = pd.DataFrame(raw_data, columns=cols[:len(raw_data[0])])
                for c in ["open", "high", "low", "close", "volume"]:
                    df[c] = df[c].astype(float)
                df["open_time"] = pd.to_datetime(df["open_time"].astype(int), unit="ms", utc=True)
                df.sort_values("open_time", inplace=True)
                df.reset_index(drop=True, inplace=True)
                return df
        except Exception as e:
            logger.error(f"Error fetching klines for {symbol}: {e}")
        return None

    def calculate_micro_qty(self, symbol: str, risk_usd: float, stop_points: float) -> Tuple[float, str]:
        info = self.exchange_info.get(symbol, {
            "pricePrecision": 2,
            "quantityPrecision": 4,
            "contractVal": 0.0001,
            "minOrderSize": 0.0001
        })
        prec = info.get("quantityPrecision", 4)
        min_size = info.get("minOrderSize", 0.0001)

        raw_qty = risk_usd / stop_points
        qty = max(min_size, raw_qty)
        qty_formatted = f"{qty:.{prec}f}"
        return float(qty_formatted), qty_formatted

    def place_order_with_brackets(
        self,
        symbol: str,
        side: str,
        position_side: str,
        quantity: float,
        stop_loss: float,
        take_profit: float
    ) -> Dict[str, Any]:
        info = self.exchange_info.get(symbol, {"pricePrecision": 2, "quantityPrecision": 4})
        p_prec = info.get("pricePrecision", 2)
        q_prec = info.get("quantityPrecision", 4)

        qty_str = f"{quantity:.{q_prec}f}"
        sl_str = f"{stop_loss:.{p_prec}f}"
        tp_str = f"{take_profit:.{p_prec}f}"

        if self.dry_run:
            logger.info(f"[DRY-RUN] Place Order: {symbol} {side} {position_side} Qty={qty_str} SL={sl_str} TP={tp_str}")
            return {
                "status": "dry_run_success",
                "orderId": f"dry_{int(time.time()*1000)}",
                "quantity": qty_str,
                "stop_loss": sl_str,
                "take_profit": tp_str
            }

        path = "/capi/v3/order"
        payload = {
            "symbol": symbol,
            "side": side.upper(),
            "positionSide": position_side.upper(),
            "type": "MARKET",
            "quantity": qty_str,
            "slTriggerPrice": sl_str,
            "tpTriggerPrice": tp_str,
            "SlWorkingType": "MARK_PRICE",
            "TpWorkingType": "MARK_PRICE",
            "newClientOrderId": f"orb_{int(time.time()*1000)}"
        }
        body_str = json.dumps(payload)
        headers = self._get_headers("POST", path, body=body_str)

        logger.info(f"Submitting WEEX V3 Bracket Order: {payload}")
        try:
            r = requests.post(f"{self.base_url}{path}", headers=headers, data=body_str, timeout=12)
            res = r.json()
            if r.status_code == 200 and res.get("code") == "00000":
                logger.info(f"Live WEEX Order Placed Successfully: {res}")
                return {
                    "status": "success",
                    "orderId": res.get("data", {}).get("orderId", "N/A"),
                    "data": res.get("data"),
                    "quantity": qty_str,
                    "stop_loss": sl_str,
                    "take_profit": tp_str
                }
            else:
                logger.error(f"WEEX Order Failed: {r.status_code} | {res}")
                return {"status": "error", "error": res}
        except Exception as e:
            logger.error(f"Exception during order placement: {e}")
            return {"status": "error", "error": str(e)}

    def close_position_market(self, symbol: str, position_side: str, quantity: float) -> Dict[str, Any]:
        side = "SELL" if position_side.upper() == "LONG" else "BUY"
        info = self.exchange_info.get(symbol, {"quantityPrecision": 4})
        qty_str = f"{quantity:.{info.get('quantityPrecision', 4)}f}"

        if self.dry_run:
            logger.info(f"[DRY-RUN] Close Market Position: {symbol} {side} {position_side} Qty={qty_str}")
            return {"status": "dry_run_success", "orderId": f"dry_close_{int(time.time()*1000)}"}

        path = "/capi/v3/order"
        payload = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side.upper(),
            "type": "MARKET",
            "quantity": qty_str,
            "newClientOrderId": f"orb_close_{int(time.time()*1000)}"
        }
        body_str = json.dumps(payload)
        headers = self._get_headers("POST", path, body=body_str)

        try:
            r = requests.post(f"{self.base_url}{path}", headers=headers, data=body_str, timeout=12)
            res = r.json()
            if r.status_code == 200 and res.get("code") == "00000":
                logger.info(f"Position closed successfully: {res}")
                return {"status": "success", "data": res.get("data")}
            else:
                logger.error(f"Failed to close position: {res}")
                return {"status": "error", "error": res}
        except Exception as e:
            logger.error(f"Exception closing position: {e}")
            return {"status": "error", "error": str(e)}


class OrbTradingBot:
    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        self.client = WeexClient(
            api_key=config.get("api_key", ""),
            secret_key=config.get("secret_key", ""),
            passphrase=config.get("passphrase", ""),
            dry_run=config.get("dry_run", False)
        )
        self.symbol = config.get("symbol", "QQQUSDT")
        self.strategy_mode = config.get("strategy_mode", "HYBRID").upper() # "HYBRID", "CONTINUATION", "REVERSION"
        self.risk_usd = float(config.get("risk_usd", 1.0))
        self.cont_stop_pts = float(config.get("continuation_stop_pts", 1.0))
        self.cont_target_pts = float(config.get("continuation_target_pts", 2.0))
        self.reversion_r = float(config.get("reversion_r", 1.5)) # JJ Simon 1.5R target
        
        # Prop Firm Shadow Tracker (Simulates MFF Rapid 50K)
        self.prop_tracker = PropFirmTracker(
            account_id="MFF_Rapid_50K_Sim",
            account_type="MFF_RAPID_50K",
            starting_balance=50000.0,
            eval_target=3000.0,
            max_trailing_dd=2000.0,
            eval_contracts=8,
            funded_contracts=4,
            pt_value=2.0,
            commission_per_tr=4.50,
            payout_split=0.90
        )
        self.state: Dict[str, Any] = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Error loading state: {e}")
        return {
            "active_trade": None,
            "session_trades": {},
            "current_session_orb": None
        }

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to persist state: {e}")

    def notify(self, message: str):
        logger.info(f"[NOTIFY] {message}")
        send_telegram(
            self.cfg.get("telegram_bot_token", ""),
            self.cfg.get("telegram_chat_id", ""),
            message
        )

    def calculate_opening_range(self, session: str) -> Optional[Dict[str, float]]:
        target_time = dtime(9, 30) if session == "NY" else dtime(20, 0)
        df_15m = self.client.get_klines(self.symbol, interval="15m", limit=20)
        if df_15m is not None and not df_15m.empty:
            df_15m["time_ny"] = df_15m["open_time"].dt.tz_convert(NY_TZ)
            today_date = datetime.now(NY_TZ).date()
            for _, row in df_15m.iterrows():
                row_dt = row["time_ny"]
                if row_dt.date() == today_date and row_dt.time() == target_time:
                    or_h = float(row["high"])
                    or_l = float(row["low"])
                    return {
                        "high": or_h,
                        "low": or_l,
                        "range": round(or_h - or_l, 2)
                    }

        # Fallback to 1m
        start_h, start_m = (9, 30) if session == "NY" else (20, 0)
        end_h, end_m = (9, 45) if session == "NY" else (20, 15)
        df_1m = self.client.get_klines(self.symbol, interval="1m", limit=120)
        if df_1m is not None and not df_1m.empty:
            df_1m["time_ny"] = df_1m["open_time"].dt.tz_convert(NY_TZ)
            today_date = datetime.now(NY_TZ).date()
            t_start = dtime(start_h, start_m)
            t_end = dtime(end_h, end_m)
            mask = (
                (df_1m["time_ny"].dt.date == today_date) &
                (df_1m["time_ny"].dt.time >= t_start) &
                (df_1m["time_ny"].dt.time < t_end)
            )
            or_df = df_1m[mask]
            if len(or_df) >= 10:
                or_h = float(or_df["high"].max())
                or_l = float(or_df["low"].min())
                return {
                    "high": or_h,
                    "low": or_l,
                    "range": round(or_h - or_l, 2)
                }

        return None

    def run_cycle(self):
        now_ny = datetime.now(NY_TZ)
        today_str = now_ny.strftime("%Y-%m-%d")
        curr_time = now_ny.time()

        # 1. ASIA SESSION (20:00 to 02:00 ET)
        if self.cfg.get("trade_asia", True):
            asia_key = f"{today_str}_ASIA"
            if (curr_time >= dtime(20, 15) or curr_time < dtime(2, 0)):
                orb_info = self.state.get("current_session_orb")
                if not orb_info or orb_info.get("session") != "ASIA" or orb_info.get("date") != today_str:
                    orb = self.calculate_opening_range("ASIA")
                    if orb:
                        self.state["current_session_orb"] = {"session": "ASIA", "orb": orb, "date": today_str}
                        self._save_state()
                        self._broadcast_or_established("ASIA", orb)

            orb_info = self.state.get("current_session_orb")
            if orb_info and orb_info.get("session") == "ASIA" and orb_info.get("date") == today_str and not self.state.get("session_trades", {}).get(asia_key) and self.state.get("active_trade") is None:
                if (curr_time >= dtime(20, 15) or curr_time < dtime(2, 0)):
                    self._evaluate_setups("ASIA", asia_key, orb_info["orb"], now_ny)

        # 2. NEW YORK SESSION (09:30 to 15:55 ET)
        if self.cfg.get("trade_ny", True):
            ny_key = f"{today_str}_NY"
            if dtime(9, 45) <= curr_time <= dtime(15, 50):
                orb_info = self.state.get("current_session_orb")
                if not orb_info or orb_info.get("session") != "NY" or orb_info.get("date") != today_str:
                    orb = self.calculate_opening_range("NY")
                    if orb:
                        self.state["current_session_orb"] = {"session": "NY", "orb": orb, "date": today_str}
                        self._save_state()
                        self._broadcast_or_established("NY", orb)

            orb_info = self.state.get("current_session_orb")
            if orb_info and orb_info.get("session") == "NY" and orb_info.get("date") == today_str and not self.state.get("session_trades", {}).get(ny_key) and self.state.get("active_trade") is None:
                if dtime(9, 45) <= curr_time <= dtime(15, 0):
                    self._evaluate_setups("NY", ny_key, orb_info["orb"], now_ny)

        # 3. POSITION MANAGEMENT
        self._manage_active_trade(curr_time, today_str, now_ny)

    def _broadcast_or_established(self, session_tag: str, orb: Dict[str, float]):
        icon = "🌏" if session_tag == "ASIA" else "🗽"
        msg = (
            f"{icon} <b>WEEX {self.symbol} [{session_tag}] 15m Range Established</b>\n"
            f"• <b>Strategy Mode:</b> <code>{self.strategy_mode}</code>\n"
            f"• <b>OR High:</b> <code>{orb['high']:.2f}</code>\n"
            f"• <b>OR Low:</b> <code>{orb['low']:.2f}</code> (Span: <code>{orb['range']:.2f} pts</code>)\n"
            f"• <b>Micro Risk:</b> <code>${self.risk_usd:.2f}</code>\n"
            f"• <b>Continuation Rule:</b> 1m Breakout @ <code>{self.cont_target_pts:.1f} pts (2.0R)</code>\n"
            f"• <b>Reversion Rule:</b> 5m Reclaim @ <code>1.5R (JJ Simon Target)</code>\n"
            f"<i>Scanning session for triggers...</i>"
        )
        self.notify(msg)

    def _evaluate_setups(self, session_tag: str, session_key: str, orb: Dict[str, float], now_ny: datetime):
        curr_time = now_ny.time()
        
        # Determine Phase: Phase A (First 30m of post-OR) vs Phase B (Remainder)
        # NY Phase A: 09:45 to 10:15 ET. Phase B: 10:15 to 15:00 ET.
        # Asia Phase A: 20:15 to 20:45 ET. Phase B: 20:45 to 02:00 ET.
        is_phase_a = False
        if session_tag == "NY" and dtime(9, 45) <= curr_time < dtime(10, 15):
            is_phase_a = True
        elif session_tag == "ASIA" and dtime(20, 15) <= curr_time < dtime(20, 45):
            is_phase_a = True

        # Check 1: CONTINUATION (Allowed in Phase A if HYBRID, or anytime if CONTINUATION mode)
        can_trade_cont = (self.strategy_mode == "CONTINUATION") or (self.strategy_mode == "HYBRID" and is_phase_a)
        if can_trade_cont:
            if self._check_continuation_trigger(session_tag, session_key, orb, now_ny):
                return

        # Check 2: MEAN REVERSION (Allowed in Phase B if HYBRID, or anytime if REVERSION mode)
        can_trade_rev = (self.strategy_mode == "REVERSION") or (self.strategy_mode == "HYBRID" and not is_phase_a)
        if can_trade_rev:
            self._check_reversion_trigger(session_tag, session_key, orb, now_ny)

    def _check_continuation_trigger(self, session_tag: str, session_key: str, orb: Dict[str, float], now_ny: datetime) -> bool:
        """Evaluates 1m candle close breakout outside Opening Range."""
        df_1m = self.client.get_klines(self.symbol, interval="1m", limit=5)
        if df_1m is None or len(df_1m) < 2:
            return False

        last_closed = df_1m.iloc[-2] # Last fully closed 1m candle
        close_p = float(last_closed["close"])

        # 1m Long Breakout
        if close_p > orb["high"]:
            entry_p = self.client.get_latest_price(self.symbol) or close_p
            sl = entry_p - self.cont_stop_pts
            tp = entry_p + self.cont_target_pts
            qty, qty_str = self.client.calculate_micro_qty(self.symbol, self.risk_usd, self.cont_stop_pts)
            res = self.client.place_order_with_brackets(self.symbol, "BUY", "LONG", qty, sl, tp)
            if res.get("status") in ["success", "dry_run_success"]:
                self._record_entry(session_tag, session_key, "LONG", "CONTINUATION", entry_p, sl, tp, qty, qty_str, 2.0, now_ny)
                return True
            else:
                self._handle_order_failure(session_key, res)

        # 1m Short Breakdown
        elif close_p < orb["low"]:
            entry_p = self.client.get_latest_price(self.symbol) or close_p
            sl = entry_p + self.cont_stop_pts
            tp = entry_p - self.cont_target_pts
            qty, qty_str = self.client.calculate_micro_qty(self.symbol, self.risk_usd, self.cont_stop_pts)
            res = self.client.place_order_with_brackets(self.symbol, "SELL", "SHORT", qty, sl, tp)
            if res.get("status") in ["success", "dry_run_success"]:
                self._record_entry(session_tag, session_key, "SHORT", "CONTINUATION", entry_p, sl, tp, qty, qty_str, 2.0, now_ny)
                return True
            else:
                self._handle_order_failure(session_key, res)

        return False

    def _check_reversion_trigger(self, session_tag: str, session_key: str, orb: Dict[str, float], now_ny: datetime) -> bool:
        """Evaluates 5m Failed Auction Reclaim: Pokes outside, closes BACK INSIDE."""
        df_5m = self.client.get_klines(self.symbol, interval="5m", limit=6)
        if df_5m is None or len(df_5m) < 2:
            return False

        last_closed = df_5m.iloc[-2]
        high_5m = float(last_closed["high"])
        low_5m = float(last_closed["low"])
        close_5m = float(last_closed["close"])

        # Sweep High -> Close Back Below OR High -> SHORT REVERSION
        if high_5m > orb["high"] and close_5m <= orb["high"]:
            entry_p = self.client.get_latest_price(self.symbol) or close_5m
            # Extreme wick + 0.15 pt buffer
            stop_dist = max(0.60, min(1.50, (high_5m - entry_p) + 0.15))
            sl = entry_p + stop_dist
            tp = entry_p - (stop_dist * self.reversion_r) # 1.5R Target
            qty, qty_str = self.client.calculate_micro_qty(self.symbol, self.risk_usd, stop_dist)
            res = self.client.place_order_with_brackets(self.symbol, "SELL", "SHORT", qty, sl, tp)
            if res.get("status") in ["success", "dry_run_success"]:
                self._record_entry(session_tag, session_key, "SHORT", "REVERSION", entry_p, sl, tp, qty, qty_str, self.reversion_r, now_ny)
                return True
            else:
                self._handle_order_failure(session_key, res)

        # Sweep Low -> Close Back Above OR Low -> LONG REVERSION
        elif low_5m < orb["low"] and close_5m >= orb["low"]:
            entry_p = self.client.get_latest_price(self.symbol) or close_5m
            stop_dist = max(0.60, min(1.50, (entry_p - low_5m) + 0.15))
            sl = entry_p - stop_dist
            tp = entry_p + (stop_dist * self.reversion_r) # 1.5R Target
            qty, qty_str = self.client.calculate_micro_qty(self.symbol, self.risk_usd, stop_dist)
            res = self.client.place_order_with_brackets(self.symbol, "BUY", "LONG", qty, sl, tp)
            if res.get("status") in ["success", "dry_run_success"]:
                self._record_entry(session_tag, session_key, "LONG", "REVERSION", entry_p, sl, tp, qty, qty_str, self.reversion_r, now_ny)
                return True
            else:
                self._handle_order_failure(session_key, res)

        return False

    def _record_entry(self, session_tag, session_key, side, stype, entry_p, sl, tp, qty, qty_str, target_r, now_ny):
        self.state["active_trade"] = {
            "session": session_tag,
            "symbol": self.symbol,
            "side": side,
            "type": stype,
            "entry_price": entry_p,
            "stop_loss": sl,
            "take_profit": tp,
            "quantity": qty,
            "target_r": target_r,
            "stop_dist": abs(entry_p - sl),
            "entry_time": now_ny.strftime("%H:%M:%S")
        }
        if "session_trades" not in self.state:
            self.state["session_trades"] = {}
        self.state["session_trades"][session_key] = True
        self._save_state()

        icon = "🚀" if stype == "CONTINUATION" else "🧲"
        msg = (
            f"{icon} <b>WEEX {self.symbol} [{session_tag}] {stype} {side} Fill</b>\n"
            f"• <b>Entry:</b> <code>{entry_p:.2f}</code>\n"
            f"• <b>Stop Loss:</b> <code>{sl:.2f}</code> (-${self.risk_usd:.2f})\n"
            f"• <b>Take Profit:</b> <code>{tp:.2f}</code> (+${self.risk_usd * target_r:.2f} | {target_r:.1f}R)\n"
            f"• <b>Position Size:</b> <code>{qty_str}</code> contracts\n"
            f"• <b>Time:</b> {now_ny.strftime('%H:%M:%S')} ET"
        )
        self.notify(msg)

    def _handle_order_failure(self, session_key: str, res: Dict[str, Any]):
        logger.error(f"Order failed: {res}. Marking session locked.")
        if "session_trades" not in self.state:
            self.state["session_trades"] = {}
        self.state["session_trades"][session_key] = True
        self._save_state()
        self.notify(f"⚠️ <b>WEEX Order Rejected for {self.symbol}</b>: <code>{res.get('error', 'Rejected')}</code>")

    def _manage_active_trade(self, curr_time: dtime, today_str: str, now_ny: datetime):
        active = self.state.get("active_trade")
        if not active:
            return

        current_price = self.client.get_latest_price(self.symbol)
        if not current_price:
            return

        session_tag = active.get("session", "NY")
        target_r = active.get("target_r", 2.0)
        stype = active.get("type", "TRADE")

        target_hit = (active["side"] == "LONG" and current_price >= active["take_profit"]) or \
                     (active["side"] == "SHORT" and current_price <= active["take_profit"])
        stop_hit = (active["side"] == "LONG" and current_price <= active["stop_loss"]) or \
                   (active["side"] == "SHORT" and current_price >= active["stop_loss"])

        is_session_end = False
        if session_tag == "NY" and curr_time >= dtime(15, 55):
            is_session_end = True
        elif session_tag == "ASIA" and dtime(2, 0) <= curr_time < dtime(3, 0):
            is_session_end = True

        if target_hit:
            pnl_usd = self.risk_usd * target_r
            prop_res = self.prop_tracker.record_trade(
                date_str=today_str, side=active["side"], entry_price=active["entry_price"],
                exit_price=active["take_profit"], pts=active["stop_dist"] * target_r,
                r_multiple=target_r, outcome="TARGET"
            )
            prop_box = self.prop_tracker.format_telegram_summary(prop_res)
            msg = (
                f"💰 <b>WEEX {self.symbol} [{session_tag}] {stype} TAKE PROFIT HIT (+{target_r:.1f}R)</b>\n"
                f"• <b>Exit Price:</b> <code>{current_price:.2f}</code>\n"
                f"• <b>Live Micro PnL ($1 Risk):</b> <code>+${pnl_usd:.2f}</code>\n"
                f"• <b>Outcome:</b> WIN"
                f"{prop_box}"
            )
            self.notify(msg)
            self.state["active_trade"] = None
            self._save_state()

        elif stop_hit:
            pnl_usd = -self.risk_usd
            prop_res = self.prop_tracker.record_trade(
                date_str=today_str, side=active["side"], entry_price=active["entry_price"],
                exit_price=active["stop_loss"], pts=-active["stop_dist"],
                r_multiple=-1.0, outcome="STOP"
            )
            prop_box = self.prop_tracker.format_telegram_summary(prop_res)
            msg = (
                f"🛑 <b>WEEX {self.symbol} [{session_tag}] {stype} STOP LOSS HIT (-1.0R)</b>\n"
                f"• <b>Exit Price:</b> <code>{current_price:.2f}</code>\n"
                f"• <b>Live Micro PnL ($1 Risk):</b> <code>-${self.risk_usd:.2f}</code>\n"
                f"• <b>Outcome:</b> LOSS"
                f"{prop_box}"
            )
            self.notify(msg)
            self.state["active_trade"] = None
            self._save_state()

        elif is_session_end:
            self.client.close_position_market(self.symbol, active["side"], active["quantity"])
            diff = (current_price - active["entry_price"]) if active["side"] == "LONG" else (active["entry_price"] - current_price)
            pnl_usd = round((diff / active["stop_dist"]) * self.risk_usd, 2)
            r_multiple = round(diff / active["stop_dist"], 2)
            prop_res = self.prop_tracker.record_trade(
                date_str=today_str, side=active["side"], entry_price=active["entry_price"],
                exit_price=current_price, pts=diff, r_multiple=r_multiple, outcome="SESSION_END"
            )
            prop_box = self.prop_tracker.format_telegram_summary(prop_res)
            msg = (
                f"⏱ <b>WEEX {self.symbol} [{session_tag}] {stype} SESSION TIME STOP</b>\n"
                f"• <b>Exit Price:</b> <code>{current_price:.2f}</code>\n"
                f"• <b>Live Micro PnL ($1 Risk):</b> <code>{'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}</code> ({r_multiple:+.2f}R)\n"
                f"• <b>Outcome:</b> SCRATCH/PARTIAL"
                f"{prop_box}"
            )
            self.notify(msg)
            self.state["active_trade"] = None
            self._save_state()


def main():
    cfg = load_config()
    bot = OrbTradingBot(cfg)
    logger.info(f"Initialized WEEX Dual-Engine Syndicate Bot on {cfg.get('symbol', 'QQQUSDT')} | Mode: {cfg.get('strategy_mode', 'HYBRID')}")
    bot.notify(
        f"🤖 <b>WEEX QQQ Dual-Engine Syndicate Bot Online</b>\n"
        f"• <b>Symbol:</b> <code>{cfg.get('symbol', 'QQQUSDT')}</code>\n"
        f"• <b>Mode:</b> <code>{cfg.get('strategy_mode', 'HYBRID')}</code> (Continuation 2.0R + Reversion 1.5R)\n"
        f"• <b>Micro Risk:</b> <code>${cfg.get('risk_usd', 1.0):.2f}</code>\n"
        f"• <b>Prop Shadow:</b> <code>MFF Rapid 50K (Code JJ | 90% Split)</code>"
    )

    poll_sec = int(cfg.get("poll_interval_seconds", 5))
    while True:
        try:
            bot.run_cycle()
        except Exception as e:
            logger.error(f"Error in main loop cycle: {e}")
        time.sleep(poll_sec)


if __name__ == "__main__":
    main()
