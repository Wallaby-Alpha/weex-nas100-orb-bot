import pytest
import os
import json
from bot import WeexClient, OrbTradingBot

def test_micro_sizing_calculation():
    client = WeexClient(api_key="test", secret_key="test", passphrase="test", dry_run=True)
    # Mock NAS100USDT exchange info
    client.exchange_info["NAS100USDT"] = {
        "pricePrecision": 2,
        "quantityPrecision": 4,
        "contractVal": 0.0001,
        "minOrderSize": 0.0001
    }
    
    # Exactly $1.00 risk on a 25.0 point stop loss
    # Formula: Qty = 1.00 / 25.0 = 0.0400 contracts
    qty, qty_str = client.calculate_micro_qty("NAS100USDT", risk_usd=1.0, stop_points=25.0)
    assert qty == 0.04
    assert qty_str == "0.0400"
    
    # 25 pt stop loss * 0.04 contracts = $1.00 USD risk exactly
    loss_amount = 25.0 * qty
    assert loss_amount == pytest.approx(1.00, 1e-4)

def test_dry_run_bracket_order():
    client = WeexClient(api_key="test", secret_key="test", passphrase="test", dry_run=True)
    client.exchange_info["NAS100USDT"] = {
        "pricePrecision": 2,
        "quantityPrecision": 4
    }
    res = client.place_order_with_brackets(
        symbol="NAS100USDT",
        side="BUY",
        position_side="LONG",
        quantity=0.04,
        stop_loss=21000.00,
        take_profit=21075.00
    )
    assert res["status"] == "dry_run_success"
    assert res["quantity"] == "0.0400"
    assert res["stop_loss"] == "21000.00"
    assert res["take_profit"] == "21075.00"

def test_config_loader():
    test_cfg = {
        "symbol": "NAS100USDT",
        "risk_usd": 1.0,
        "stop_loss_points": 25.0,
        "take_profit_points": 50.0
    }
    bot = OrbTradingBot(test_cfg)
    assert bot.symbol == "NAS100USDT"
    assert bot.risk_usd == 1.0
    assert bot.stop_pts == 25.0
    assert bot.target_pts == 50.0
