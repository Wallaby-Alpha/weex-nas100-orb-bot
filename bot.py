"""
weex-nas100-orb-bot / bot.py
----------------------------
Production 15-Minute Opening Range Breakout (ORB) Trading Bot for WEEX NAS100USDT.
Designed for deployment on a DigitalOcean Droplet ($1 micro-risk live forward testing).

Core Strategy & Execution Architecture:
1. Exact Micro-Risk Sizing ($1.00 USD risk):
   - Computes dynamic or fixed contract quantity to risk EXACTLY $1.00 on a 25-point stop loss.
   - Sizing formula: Qty = Risk ($1.00) / (Stop Distance * ContractVal).
   - On WEEX NAS100USDT (ContractVal = 0.0001, 1 pt change on 1.0 contract = $1.00 USDT PnL):
     Qty = 1.00 / 25.0 = 0.0400 contracts.
2. Opening Range (OR) Calculation:
   - Evaluates closed 1m or 15m candles during 09:30 AM – 09:45 AM America/New_York.
   - Determines OR High and OR Low.
3. Execution Trigger:
   - At 09:45 AM ET, monitors breakout of OR High (Long) or breakdown of OR Low (Short).
   - Only ONE trade taken per calendar day (strictly preventing overtrading).
4. Dual Invalidation & Profit Target:
   - Stop Loss: Fixed 25 points ($1.00 loss).
   - Take Profit: Fixed 50 points (2.0R = +$2.00 gain).
   - EOD Time Stop: At 15:55 PM ET, closes open market position if neither SL nor TP triggered.
5. Exchange-Native Orders & Telegram Broadcasts:
   - Directly places market entry with SL & TP brackets to WEEX matching engine.
   - Dispatches formatted Telegram alerts at: Range Set, Entry Filled, Exit Triggered, and Daily Summary.
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

# Ensure UTF-8 output on all operating systems
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
logger = logging.getLogger("weex_nas100_orb")

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
        "symbol": "NAS100USDT",
        "risk_usd": 1.0,
        "stop_loss_points": 25.0,
        "take_profit_points": 50.0,
        "opening_range_minutes": 15,
        "market_open_hour_ny": 9,
        "market_open_minute_ny": 30,
        "eod_close_hour_ny": 15,
        "eod_close_minute_ny": 55,
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
        logger.error(f"Telegram alert delivery failed: {e}")
        return False


class WeexClient:
    """Production client for WEEX V3 Contract API."""
    def __init__(self, api_key: str, secret_key: str, passphrase: str, dry_run: bool = False):
        self.base_url = "https://api-contract.weex.com"
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.dry_run = dry_run
        self.exchange_info: Dict[str, Dict[str, Any]] = {}
        self.refresh_exchange_info()

    def _generate_signature(self, timestamp: str, method: str, path: str, query: str = "", body: str = "") -> str:
        message = f"{timestamp}{method.upper()}{path}{query}{body}"
        mac = hmac.new(self.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _get_headers(self, method: str, path: str, query: str = "", body: str = "") -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        sign = self._generate_signature(ts, method, path, query, body)
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }

    def refresh_exchange_info(self):
        url = f"{self.base_url}/capi/v3/market/exchangeInfo"
        try:
            r = requests.get(url, timeout=12)
            if r.status_code == 200:
                symbols = r.json().get("symbols", [])
                meta = {}
                for s in symbols:
                    sym = s.get("symbol", "")
                    meta[sym] = {
                        "pricePrecision": int(s.get("pricePrecision", 2)),
                        "quantityPrecision": int(s.get("quantityPrecision", 4)),
                        "contractVal": float(s.get("contractVal", 0.0001)),
                        "minOrderSize": float(s.get("minOrderSize", 0.0001)),
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
        """
        Calculates position quantity to achieve strict micro-risk (e.g. $1.00 USD).
        On WEEX NAS100USDT: 1 unit change in price = 1.0 USDT per 1.0 contract.
        Quantity = Risk / StopPoints.
        """
        info = self.exchange_info.get(symbol, {
            "pricePrecision": 2,
            "quantityPrecision": 4,
            "contractVal": 0.0001,
            "minOrderSize": 0.0001
        })
        min_size = info.get("minOrderSize", 0.0001)
        prec = info.get("quantityPrecision", 4)

        raw_qty = risk_usd / stop_points
        qty = max(min_size, raw_qty)
        qty_formatted = f"{qty:.{prec}f}"
        return float(qty_formatted), qty_formatted

    def place_order_with_brackets(
        self,
        symbol: str,
        side: str,           # "BUY" or "SELL"
        position_side: str,  # "LONG" or "SHORT"
        quantity: float,
        stop_loss: float,
        take_profit: float
    ) -> Dict[str, Any]:
        info = self.exchange_info.get(symbol, {
            "pricePrecision": 2,
            "quantityPrecision": 4
        })
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
                logger.info(f"✅ Live WEEX Order Placed Successfully: {res}")
                return {
                    "status": "success",
                    "orderId": res.get("data", {}).get("orderId", "N/A"),
                    "data": res.get("data"),
                    "quantity": qty_str,
                    "stop_loss": sl_str,
                    "take_profit": tp_str
                }
            else:
                logger.error(f"❌ WEEX Order Failed: {r.status_code} | {res}")
                return {"status": "error", "error": res}
        except Exception as e:
            logger.error(f"Exception during order placement: {e}")
            return {"status": "error", "error": str(e)}

    def close_position_market(self, symbol: str, position_side: str, quantity: float) -> Dict[str, Any]:
        """Closes open long or short position via counter market order."""
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
                logger.info(f"✅ Position closed successfully: {res}")
                return {"status": "success", "data": res.get("data")}
            else:
                logger.error(f"❌ Failed to close position: {res}")
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
        self.symbol = config.get("symbol", "NAS100USDT")
        self.risk_usd = float(config.get("risk_usd", 1.0))
        self.stop_pts = float(config.get("stop_loss_points", 25.0))
        self.target_pts = float(config.get("take_profit_points", 50.0))
        self.prop_tracker = PropFirmTracker(
            account_id="MFF_Flex_25K_Sim",
            starting_balance=25000.0,
            eval_target=1500.0,
            max_trailing_dd=1000.0,
            contracts=2,
            pt_value=2.0,
            commission_per_tr=4.50
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
            "last_trade_date": "",
            "opening_range": None
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

    def calculate_opening_range(self) -> Optional[Dict[str, float]]:
        """
        Fetches 1m klines from 09:30 to 09:45 AM ET to calculate High & Low.
        """
        df = self.client.get_klines(self.symbol, interval="1m", limit=35)
        if df is None or len(df) < 15:
            logger.warning("Insufficient 1m klines fetched for ORB calculation.")
            return None

        # Convert open_time to NY timezone
        df["time_ny"] = df["open_time"].dt.tz_convert(NY_TZ)
        today_date = datetime.now(NY_TZ).date()

        # Filter strictly for today between 09:30:00 and 09:44:59
        mask = (
            (df["time_ny"].dt.date == today_date) &
            (df["time_ny"].dt.time >= dtime(9, 30)) &
            (df["time_ny"].dt.time < dtime(9, 45))
        )
        or_df = df[mask]
        if len(or_df) < 10:
            logger.warning(f"Expected >=10 bars in 15m window, found {len(or_df)} bars.")
            return None

        or_high = float(or_df["high"].max())
        or_low = float(or_df["low"].min())
        return {
            "high": or_high,
            "low": or_low,
            "range": round(or_high - or_low, 2)
        }

    def run_cycle(self):
        now_ny = datetime.now(NY_TZ)
        today_str = now_ny.strftime("%Y-%m-%d")
        curr_time = now_ny.time()

        # 1. Reset daily state at 09:00 AM NY
        if curr_time < dtime(9, 29):
            if self.state.get("last_trade_date") != today_str and self.state.get("opening_range") is not None:
                self.state["opening_range"] = None
                self._save_state()
            return

        # 2. At 09:45 AM NY, establish the 15m Opening Range
        if dtime(9, 45) <= curr_time < dtime(9, 46) and self.state.get("opening_range") is None:
            orb = self.calculate_opening_range()
            if orb:
                self.state["opening_range"] = orb
                self._save_state()
                qty, qty_str = self.client.calculate_micro_qty(self.symbol, self.risk_usd, self.stop_pts)
                msg = (
                    f"🎯 <b>WEEX {self.symbol} 15m Opening Range Set</b>\n"
                    f"• <b>Date:</b> {today_str}\n"
                    f"• <b>OR High:</b> <code>{orb['high']:.2f}</code>\n"
                    f"• <b>OR Low:</b> <code>{orb['low']:.2f}</code>\n"
                    f"• <b>Span:</b> <code>{orb['range']:.2f} pts</code>\n"
                    f"• <b>Target Risk:</b> <code>${self.risk_usd:.2f}</code> (Qty: <code>{qty_str}</code> contracts)\n"
                    f"• <b>SL / TP:</b> <code>25 pts / 50 pts (2.0R)</code>\n"
                    f"<i>Armed and scanning for breakout...</i>"
                )
                self.notify(msg)

        # 3. Monitor active breakout triggers if no trade taken today
        orb = self.state.get("opening_range")
        if orb and self.state.get("last_trade_date") != today_str and self.state.get("active_trade") is None:
            # Trade entry allowed between 09:45 AM and 15:00 PM NY
            if dtime(9, 45) <= curr_time <= dtime(15, 0):
                current_price = self.client.get_latest_price(self.symbol)
                if current_price is None:
                    return

                qty, qty_str = self.client.calculate_micro_qty(self.symbol, self.risk_usd, self.stop_pts)

                # Bullish Breakout of OR High
                if current_price >= orb["high"]:
                    entry_p = current_price
                    sl = entry_p - self.stop_pts
                    tp = entry_p + self.target_pts
                    order_res = self.client.place_order_with_brackets(
                        symbol=self.symbol,
                        side="BUY",
                        position_side="LONG",
                        quantity=qty,
                        stop_loss=sl,
                        take_profit=tp
                    )
                    if order_res.get("status") in ["success", "dry_run_success"]:
                        self.state["active_trade"] = {
                            "symbol": self.symbol,
                            "side": "LONG",
                            "entry_price": entry_p,
                            "stop_loss": sl,
                            "take_profit": tp,
                            "quantity": qty,
                            "entry_time": now_ny.strftime("%H:%M:%S")
                        }
                        self.state["last_trade_date"] = today_str
                        self._save_state()
                        msg = (
                            f"🚀 <b>WEEX {self.symbol} LONG Fill</b>\n"
                            f"• <b>Entry:</b> <code>{entry_p:.2f}</code>\n"
                            f"• <b>Stop Loss:</b> <code>{sl:.2f}</code> (-${self.risk_usd:.2f})\n"
                            f"• <b>Take Profit:</b> <code>{tp:.2f}</code> (+${self.risk_usd * 2:.2f} | 2R)\n"
                            f"• <b>Position Size:</b> <code>{qty_str}</code> contracts\n"
                            f"• <b>Time:</b> {now_ny.strftime('%H:%M:%S')} ET"
                        )
                        self.notify(msg)

                # Bearish Breakdown of OR Low
                elif current_price <= orb["low"]:
                    entry_p = current_price
                    sl = entry_p + self.stop_pts
                    tp = entry_p - self.target_pts
                    order_res = self.client.place_order_with_brackets(
                        symbol=self.symbol,
                        side="SELL",
                        position_side="SHORT",
                        quantity=qty,
                        stop_loss=sl,
                        take_profit=tp
                    )
                    if order_res.get("status") in ["success", "dry_run_success"]:
                        self.state["active_trade"] = {
                            "symbol": self.symbol,
                            "side": "SHORT",
                            "entry_price": entry_p,
                            "stop_loss": sl,
                            "take_profit": tp,
                            "quantity": qty,
                            "entry_time": now_ny.strftime("%H:%M:%S")
                        }
                        self.state["last_trade_date"] = today_str
                        self._save_state()
                        msg = (
                            f"🔻 <b>WEEX {self.symbol} SHORT Fill</b>\n"
                            f"• <b>Entry:</b> <code>{entry_p:.2f}</code>\n"
                            f"• <b>Stop Loss:</b> <code>{sl:.2f}</code> (-${self.risk_usd:.2f})\n"
                            f"• <b>Take Profit:</b> <code>{tp:.2f}</code> (+${self.risk_usd * 2:.2f} | 2R)\n"
                            f"• <b>Position Size:</b> <code>{qty_str}</code> contracts\n"
                            f"• <b>Time:</b> {now_ny.strftime('%H:%M:%S')} ET"
                        )
                        self.notify(msg)

        # 4. Check active position status & EOD Close at 15:55 PM NY
        active = self.state.get("active_trade")
        if active:
            current_price = self.client.get_latest_price(self.symbol)
            if current_price:
                # Target Hit Check
                target_hit = (active["side"] == "LONG" and current_price >= active["take_profit"]) or \
                             (active["side"] == "SHORT" and current_price <= active["take_profit"])
                # Stop Hit Check
                stop_hit = (active["side"] == "LONG" and current_price <= active["stop_loss"]) or \
                           (active["side"] == "SHORT" and current_price >= active["stop_loss"])

                if target_hit:
                    pnl_usd = self.risk_usd * 2.0
                    pts = self.target_pts
                    prop_res = self.prop_tracker.record_trade(
                        date_str=today_str,
                        side=active["side"],
                        entry_price=active["entry_price"],
                        exit_price=active["take_profit"],
                        pts=pts,
                        r_multiple=2.0,
                        outcome="TARGET"
                    )
                    prop_box = self.prop_tracker.format_telegram_summary(prop_res)
                    msg = (
                        f"💰 <b>WEEX {self.symbol} TAKE PROFIT HIT (+2.0R)</b>\n"
                        f"• <b>Exit Price:</b> <code>{current_price:.2f}</code>\n"
                        f"• <b>Live PnL ($1 Risk):</b> <code>+${pnl_usd:.2f}</code>\n"
                        f"• <b>Outcome:</b> WIN"
                        f"{prop_box}"
                    )
                    self.notify(msg)
                    self.state["active_trade"] = None
                    self._save_state()

                elif stop_hit:
                    pnl_usd = -self.risk_usd
                    pts = -self.stop_pts
                    prop_res = self.prop_tracker.record_trade(
                        date_str=today_str,
                        side=active["side"],
                        entry_price=active["entry_price"],
                        exit_price=active["stop_loss"],
                        pts=pts,
                        r_multiple=-1.0,
                        outcome="STOP"
                    )
                    prop_box = self.prop_tracker.format_telegram_summary(prop_res)
                    msg = (
                        f"🛑 <b>WEEX {self.symbol} STOP LOSS HIT (-1.0R)</b>\n"
                        f"• <b>Exit Price:</b> <code>{current_price:.2f}</code>\n"
                        f"• <b>Live PnL ($1 Risk):</b> <code>-${self.risk_usd:.2f}</code>\n"
                        f"• <b>Outcome:</b> LOSS"
                        f"{prop_box}"
                    )
                    self.notify(msg)
                    self.state["active_trade"] = None
                    self._save_state()

                # EOD Close at 15:55 PM NY
                elif curr_time >= dtime(self.cfg.get("eod_close_hour_ny", 15), self.cfg.get("eod_close_minute_ny", 55)):
                    self.client.close_position_market(self.symbol, active["side"], active["quantity"])
                    diff = (current_price - active["entry_price"]) if active["side"] == "LONG" else (active["entry_price"] - current_price)
                    pnl_usd = round((diff / self.stop_pts) * self.risk_usd, 2)
                    r_multiple = round(diff / self.stop_pts, 2)
                    prop_res = self.prop_tracker.record_trade(
                        date_str=today_str,
                        side=active["side"],
                        entry_price=active["entry_price"],
                        exit_price=current_price,
                        pts=diff,
                        r_multiple=r_multiple,
                        outcome="EOD"
                    )
                    prop_box = self.prop_tracker.format_telegram_summary(prop_res)
                    msg = (
                        f"⏱ <b>WEEX {self.symbol} EOD TIME STOP (15:55 ET)</b>\n"
                        f"• <b>Exit Price:</b> <code>{current_price:.2f}</code>\n"
                        f"• <b>Live PnL ($1 Risk):</b> <code>{'+' if pnl_usd >= 0 else ''}${pnl_usd:.2f}</code> ({r_multiple:+.2f}R)\n"
                        f"• <b>Outcome:</b> EOD SCRATCH/PARTIAL"
                        f"{prop_box}"
                    )
                    self.notify(msg)
                    self.state["active_trade"] = None
                    self._save_state()


def main():
    cfg = load_config()
    bot = OrbTradingBot(cfg)
    logger.info(f"Initialized WEEX NAS100 15m ORB Bot | Risk: ${cfg.get('risk_usd', 1.0)} | Dry-Run: {cfg.get('dry_run')}")
    bot.notify("🤖 <b>WEEX NAS100 15m ORB Bot Online on DigitalOcean</b> ($1 Risk Micro Mode)")

    poll_sec = int(cfg.get("poll_interval_seconds", 5))
    while True:
        try:
            bot.run_cycle()
        except Exception as e:
            logger.error(f"Error in main loop cycle: {e}")
        time.sleep(poll_sec)


if __name__ == "__main__":
    main()
