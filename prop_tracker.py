"""
prop_tracker.py
---------------
Real-Time Prop Firm Shadow Simulation Engine.
Supports:
1. MFF Rapid 50K (Code JJ: $80 eval, $0 activation, $3,000 target, $2,000 trailing DD, 90% payout split)
2. MFF Flex 25K ($20 promo, $1,500 target, $1,000 trailing DD, 80% payout split)

Automatically scales live $1.00 micro-trades on WEEX QQQUSDT into realistic
futures prop firm trades (8 MNQ in Eval -> 4 MNQ in Funded).
"""

import os
import json
from datetime import datetime
from typing import Dict, Any, Optional

STATE_FILE = "prop_account_state.json"

class PropFirmTracker:
    def __init__(
        self,
        account_id: str = "MFF_Rapid_50K_Sim",
        account_type: str = "MFF_RAPID_50K", # "MFF_RAPID_50K" or "MFF_FLEX_25K"
        starting_balance: float = 50000.0,
        eval_target: float = 3000.0,
        max_trailing_dd: float = 2000.0,
        eval_contracts: int = 8,       # 8 MNQ in Eval ($400 risk on 25 pt stop)
        funded_contracts: int = 4,     # 4 MNQ in Funded ($200 risk on 25 pt stop)
        pt_value: float = 2.0,         # $2/pt per MNQ
        commission_per_tr: float = 4.50,
        payout_split: float = 0.90     # 90% trader split on Rapid
    ):
        self.account_id = account_id
        self.account_type = account_type
        self.starting_balance = starting_balance
        self.eval_target = eval_target
        self.max_trailing_dd = max_trailing_dd
        self.eval_contracts = eval_contracts
        self.funded_contracts = funded_contracts
        self.pt_value = pt_value
        self.commission_per_tr = commission_per_tr
        self.payout_split = payout_split
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
            "account_type": self.account_type,
            "stage": "EVALUATION",  # EVALUATION -> SIM_FUNDED -> PAYOUT_READY -> BREACHED
            "current_balance": self.starting_balance,
            "peak_balance": self.starting_balance,
            "dd_floor": self.starting_balance - self.max_trailing_dd,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "winning_days_qualifier": 0,
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
        # Determine contracts based on current stage
        is_eval = (self.state["stage"] == "EVALUATION")
        contracts = self.eval_contracts if is_eval else self.funded_contracts
        
        # Calculate simulated MNQ PnL
        # Normalize QQQ pts to NQ pts (ratio ~40:1, so 1 pt on QQQ = ~40 pts on NQ)
        # Or standard R multiple: risk is $400 on eval (8 MNQ), $200 on funded (4 MNQ)
        base_risk = 400.0 if is_eval else 200.0
        gross_pnl = r_multiple * base_risk
        net_pnl = gross_pnl - (self.commission_per_tr * (contracts / 2.0))

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

        # Update Trailing DD Floor
        if new_balance > self.state["peak_balance"]:
            self.state["peak_balance"] = round(new_balance, 2)
            new_floor = new_balance - self.max_trailing_dd
            # In funded stage, floor locks at starting balance + $100 buffer
            if self.state["stage"] == "SIM_FUNDED" and new_floor > (self.starting_balance + 100.0):
                new_floor = self.starting_balance + 100.0
            self.state["dd_floor"] = round(new_floor, 2)

        # Drawdown breach check
        if new_balance <= self.state["dd_floor"]:
            self.state["stage"] = "BREACHED"

        # Check Evaluation Target
        profit_from_start = new_balance - self.starting_balance
        if self.state["stage"] == "EVALUATION" and profit_from_start >= self.eval_target:
            # 50% consistency rule
            max_day = max(self.state["daily_history"].values()) if self.state["daily_history"] else 0.0
            consistency_ratio = (max_day / profit_from_start) if profit_from_start > 0 else 1.0
            if consistency_ratio <= 0.50:
                self.state["stage"] = "SIM_FUNDED"
                self.state["peak_balance"] = new_balance
                self.state["dd_floor"] = new_balance - self.max_trailing_dd

        # Check Funded Winning Days ($150+ threshold on 50k, $100 on 25k)
        min_win_day = 150.0 if self.account_type == "MFF_RAPID_50K" else 100.0
        min_payout_bal = 2000.0 if self.account_type == "MFF_RAPID_50K" else 1000.0
        
        if self.state["stage"] == "SIM_FUNDED":
            winning_days = sum(1 for p in self.state["daily_history"].values() if p >= min_win_day)
            self.state["winning_days_qualifier"] = winning_days
            if winning_days >= 5 and profit_from_start >= min_payout_bal:
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
            "outcome": outcome,
            "stage": self.state["stage"]
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
        
        if stage == "EVALUATION":
            pct = min(100.0, max(0.0, (profit / self.eval_target) * 100.0))
            filled = int(pct / 10)
            bar = "█" * filled + "░" * (10 - filled)
            stage_str = f"🎯 <b>EVALUATION PROGRESS:</b> <code>[{bar}] {pct:.1f}%</code>\n• <b>Target:</b> <code>${profit:+,.2f} / ${self.eval_target:,.2f}</code>"
        elif stage == "SIM_FUNDED":
            w_days = self.state.get("winning_days_qualifier", 0)
            stage_str = f"💼 <b>SIM_FUNDED ACTIVE</b>\n• <b>Payout Qualifier:</b> <code>{w_days} / 5 Winning Days</code>"
        elif stage == "PAYOUT_READY":
            avail = (balance - self.starting_balance) * 0.50 * self.payout_split
            stage_str = f"🏆 <b>PAYOUT READY! (Est. Cash: ~${avail:,.2f} at {int(self.payout_split*100)}% Split)</b>"
        else:
            stage_str = f"❌ <b>ACCOUNT BREACHED (MAX DD EXCEEDED)</b>"

        box = (
            f"\n───────────────────────────\n"
            f"🏛 <b>SHADOW PROP FIRM ({self.account_id})</b>\n"
            f"• <b>Sim PnL:</b> <code>{'+' if net_pnl >= 0 else ''}${net_pnl:,.2f}</code>\n"
            f"• <b>Sim Balance:</b> <code>${balance:,.2f}</code>\n"
            f"• <b>Trailing Floor:</b> <code>${self.state['dd_floor']:,.2f}</code> (Buffer: <code>${buffer_val:,.2f}</code>)\n"
            f"{stage_str}"
        )
        return box
