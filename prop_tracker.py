"""
prop_tracker.py
---------------
Real-Time Prop Firm Shadow Simulation Engine for MFF Flex 25K (or configurable).

Automatically converts live $1 micro-trades (from WEEX NAS100) into realistic
futures prop firm trades (e.g. 2 MNQ contracts, $100 risk per trade).

Tracks:
1. Balance & Equity progression
2. End-of-Day (EOD) Trailing Drawdown Floor ($1,000 trailing buffer)
3. 50% Consistency Rule compliance (no single day > 50% of total profit)
4. Evaluation Target ($1,500) & Automatic promotion to SIM_FUNDED
5. 5 Winning Days >= $100 requirement for 1st payout extraction
6. Formatted Telegram report box ready for instant dispatch
"""

import os
import json
from datetime import datetime
from typing import Dict, Any, Optional

STATE_FILE = "prop_account_state.json"

class PropFirmTracker:
    def __init__(
        self,
        account_id: str = "MFF_Flex_25K_Sim",
        starting_balance: float = 25000.0,
        eval_target: float = 1500.0,
        max_trailing_dd: float = 1000.0,
        contracts: int = 2,           # 2 MNQ contracts
        pt_value: float = 2.0,        # $2/pt per MNQ contract
        commission_per_tr: float = 4.50
    ):
        self.account_id = account_id
        self.starting_balance = starting_balance
        self.eval_target = eval_target
        self.max_trailing_dd = max_trailing_dd
        self.contracts = contracts
        self.pt_value = pt_value
        self.commission_per_tr = commission_per_tr
        self.state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "account_id": self.account_id,
            "stage": "EVALUATION",  # EVALUATION -> SIM_FUNDED -> PAYOUT_READY -> BREACHED
            "current_balance": self.starting_balance,
            "peak_eod_balance": self.starting_balance,
            "dd_floor": self.starting_balance - self.max_trailing_dd,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "winning_days_100": 0,
            "daily_history": {},     # "YYYY-MM-DD": net_pnl
            "trades": []
        }

    def _save_state(self):
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"[!] Failed to save prop state: {e}")

    def record_trade(
        self,
        date_str: str,
        side: str,
        entry_price: float,
        exit_price: float,
        pts: float,
        r_multiple: float,
        outcome: str
    ) -> Dict[str, Any]:
        """
        Calculates simulated PnL based on 2 MNQ contracts.
        2 MNQ: 1 point = $4.00 gross ($2.00 * 2).
        25 pt stop = -$100 gross. 50 pt target = +$200 gross.
        """
        gross_pnl = pts * (self.pt_value * self.contracts)
        net_pnl = gross_pnl - self.commission_per_tr

        # Update balance
        prev_balance = self.state["current_balance"]
        new_balance = prev_balance + net_pnl
        self.state["current_balance"] = round(new_balance, 2)
        self.state["total_trades"] += 1

        if net_pnl > 0:
            self.state["wins"] += 1
        else:
            self.state["losses"] += 1

        # Track daily history
        day_pnl = self.state["daily_history"].get(date_str, 0.0) + net_pnl
        self.state["daily_history"][date_str] = round(day_pnl, 2)

        # Update EOD Trailing Floor (MFF Flex: trails highest EOD balance)
        if new_balance > self.state["peak_eod_balance"]:
            self.state["peak_eod_balance"] = round(new_balance, 2)
            # In funded stage, MLL often locks at starting balance + $100
            new_floor = new_balance - self.max_trailing_dd
            if self.state["stage"] == "SIM_FUNDED" and new_floor > (self.starting_balance + 100.0):
                new_floor = self.starting_balance + 100.0
            self.state["dd_floor"] = round(new_floor, 2)

        # Drawdown breach check
        if new_balance <= self.state["dd_floor"]:
            self.state["stage"] = "BREACHED"

        # Check Evaluation Target
        profit_from_start = new_balance - self.starting_balance
        if self.state["stage"] == "EVALUATION" and profit_from_start >= self.eval_target:
            # Check 50% consistency rule: max day <= 50% of total profit
            max_day = max(self.state["daily_history"].values()) if self.state["daily_history"] else 0.0
            consistency_ratio = (max_day / profit_from_start) if profit_from_start > 0 else 1.0
            if consistency_ratio <= 0.50:
                self.state["stage"] = "SIM_FUNDED"

        # Check Funded Winning Days (Days with >= +$100 profit)
        if self.state["stage"] == "SIM_FUNDED":
            winning_days = sum(1 for p in self.state["daily_history"].values() if p >= 100.0)
            self.state["winning_days_100"] = winning_days
            if winning_days >= 5 and profit_from_start >= 1000.0:
                self.state["stage"] = "PAYOUT_READY"

        self.state["trades"].append({
            "date": date_str,
            "side": side,
            "entry": entry_price,
            "exit": exit_price,
            "pts": pts,
            "r_multiple": r_multiple,
            "net_pnl": round(net_pnl, 2),
            "balance_after": round(new_balance, 2),
            "outcome": outcome
        })

        self._save_state()

        return {
            "net_pnl": net_pnl,
            "new_balance": new_balance,
            "dd_floor": self.state["dd_floor"],
            "remaining_buffer": new_balance - self.state["dd_floor"],
            "stage": self.state["stage"],
            "profit_from_start": profit_from_start
        }

    def format_telegram_summary(self, trade_summary: Dict[str, Any]) -> str:
        net_pnl = trade_summary["net_pnl"]
        balance = trade_summary["new_balance"]
        buffer_val = trade_summary["remaining_buffer"]
        stage = trade_summary["stage"]
        profit = trade_summary["profit_from_start"]
        
        # Target progress bar
        if stage == "EVALUATION":
            pct = min(100.0, max(0.0, (profit / self.eval_target) * 100.0))
            filled = int(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            stage_str = f"🎯 <b>EVALUATION PROGRESS:</b> <code>[{bar}] {pct:.1f}%</code>\n• <b>Target:</b> <code>${profit:+,.2f} / ${self.eval_target:,.2f}</code>"
        elif stage == "SIM_FUNDED":
            w_days = self.state.get("winning_days_100", 0)
            stage_str = f"💼 <b>SIM_FUNDED ACTIVE</b>\n• <b>Payout Qualifier:</b> <code>{w_days} / 5 Winning Days >= $100</code>"
        elif stage == "PAYOUT_READY":
            stage_str = f"🏆 <b>PAYOUT ELIGIBLE! ($1,000+ Available)</b>"
        else:
            stage_str = f"❌ <b>ACCOUNT BREACHED (MAX DD EXCEEDED)</b>"

        box = (
            f"\n───────────────────────────\n"
            f"🏛 <b>SHADOW PROP FIRM (MFF Flex 25K)</b>\n"
            f"• <b>Sim Trade (2 MNQ):</b> <code>{'+' if net_pnl >= 0 else ''}${net_pnl:,.2f}</code>\n"
            f"• <b>Account Balance:</b> <code>${balance:,.2f}</code>\n"
            f"• <b>Trailing DD Floor:</b> <code>${self.state['dd_floor']:,.2f}</code> (Buffer: <code>${buffer_val:,.2f}</code>)\n"
            f"{stage_str}"
        )
        return box
