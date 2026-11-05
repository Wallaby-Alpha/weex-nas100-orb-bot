"""
diagnose_weex_order.py
----------------------
Diagnoses WEEX Error -1191: "Business precondition not satisfied."
Checks:
1. Futures Account Balance (Available Margin in USDT)
2. API Key Permissions (Contract Trading enabled?)
3. Account Position Mode (One-Way / "BOTH" vs Hedge / "LONG"/"SHORT")
4. QQQUSDT Leverage & Margin Mode
5. Order Payload validation (testing pure order without brackets vs with brackets)
"""

import sys
import json
import time
import hmac
import hashlib
import base64
from pathlib import Path
import requests

CONFIG_PATH = Path("config.json")
if not CONFIG_PATH.exists():
    print("Error: config.json not found in current directory.")
    sys.exit(1)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

API_KEY = cfg.get("api_key", "")
SECRET_KEY = cfg.get("secret_key", "")
PASSPHRASE = cfg.get("passphrase", "")
BASE_URL = "https://api-contract.weex.com"

def get_headers(method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time() * 1000))
    sign_str = ts + method.upper() + path
    if body:
        sign_str += body
    sig = hmac.new(SECRET_KEY.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256).digest()
    return {
        "ACCESS-KEY": API_KEY,
        "ACCESS-SIGN": base64.b64encode(sig).decode("utf-8"),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
        "locale": "en-US"
    }

def check_account_balance():
    print("\n--- 1. Checking Futures Account Balance ---")
    path = "/capi/v3/account/balance"
    headers = get_headers("GET", path)
    r = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)
    print("Status:", r.status_code)
    try:
        data = r.json()
        print("Response:", json.dumps(data, indent=2))
        return data
    except Exception as e:
        print("Raw text:", r.text)
        return None

def check_positions():
    print("\n--- 2. Checking Current Positions / Mode ---")
    for ep in ["/capi/v3/position/currentPosition", "/capi/v3/account/positions", "/capi/v3/position/all-position"]:
        headers = get_headers("GET", ep)
        r = requests.get(f"{BASE_URL}{ep}", headers=headers, timeout=5)
        if r.status_code != 404:
            print(f"Endpoint {ep} Status {r.status_code}:", r.text[:200])

def check_leverage(symbol="QQQUSDT"):
    print(f"\n--- 3. Checking Leverage for {symbol} ---")
    path = f"/capi/v3/account/leverage?symbol={symbol}"
    headers = get_headers("GET", path)
    r = requests.get(f"{BASE_URL}{path}", headers=headers, timeout=10)
    print("Leverage check:", r.status_code, r.text)

def main():
    print("=" * 60)
    print("WEEX V3 ACCOUNT & ORDER DIAGNOSTIC TOOL")
    print("=" * 60)
    print("API Key:", API_KEY[:6] + "..." if len(API_KEY) > 6 else "EMPTY")
    check_account_balance()
    check_positions()
    check_leverage("QQQUSDT")

if __name__ == "__main__":
    main()
