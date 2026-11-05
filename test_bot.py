import pytest
import os
import json
from bot import WeexClient, OrbTradingBot

def test_micro_sizing_calculation():
    client = WeexClient(api_key="test", secret_key="test", passphrase="test", dry_run=True)
    client.exchange_info["QQQUSDT"] = {
        "pricePrecision": 2,
        "quantityPrecision": 4,
        "contractVal": 0.0001,
        "minOrderSize": 0.0001
    }
    
    # Exactly $1.00 risk on a 1.0 point stop loss on QQQ
    qty, qty_str = client.calculate_micro_qty("QQQUSDT", risk_usd=1.0, stop_points=1.0)
    assert qty == 1.0
    assert qty_str == "1.0000"
    
    # 1.0 pt stop loss * 1.0 contract = $1.00 USD risk exactly
    loss_amount = 1.0 * qty
    assert loss_amount == pytest.approx(1.00, 1e-4)

def test_dry_run_bracket_order():
    client = WeexClient(api_key="test", secret_key="test", passphrase="test", dry_run=True)
    client.exchange_info["QQQUSDT"] = {
        "pricePrecision": 2,
        "quantityPrecision": 4
    }
    res = client.place_order_with_brackets(
        symbol="QQQUSDT",
        side="BUY",
        position_side="LONG",
        quantity=1.0,
        stop_loss=680.00,
        take_profit=682.00
    )
    assert res["status"] == "dry_run_success"
    assert res["quantity"] == "1.0000"
    assert res["stop_loss"] == "680.00"
    assert res["take_profit"] == "682.00"

def test_config_loader():
    test_cfg = {
        "symbol": "QQQUSDT",
        "strategy_mode": "HYBRID",
        "risk_usd": 1.0,
        "continuation_stop_pts": 1.0,
        "continuation_target_pts": 2.0,
        "reversion_r": 1.5
    }
    bot = OrbTradingBot(test_cfg)
    assert bot.symbol == "QQQUSDT"
    assert bot.strategy_mode == "HYBRID"
    assert bot.risk_usd == 1.0
    assert bot.cont_stop_pts == 1.0
    assert bot.cont_target_pts == 2.0
    assert bot.reversion_r == 1.5
    assert bot.prop_tracker.account_id == "MFF_Rapid_50K_Sim"
    assert bot.prop_tracker.starting_balance == 50000.0
