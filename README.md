# WEEX NAS100 15m ORB Trading Bot ($1 Micro-Risk)

Standalone production trading bot designed to trade the **15-Minute Opening Range Breakout (ORB)** strategy on WEEX `NAS100USDT` with strict **$1.00 USD risk per trade** and live **Telegram alerts**, hosted on a $4/month DigitalOcean droplet.

---

## 1. Mathematical Micro-Sizing Architecture

On WEEX, `NAS100USDT` contract value is denominated in USDT per index point:
- Contract Specifications: `contractVal = 0.0001`, `quantityPrecision = 4` decimals.
- 1 contract point move = $1.00 USDT.
- For a **25.0 point Stop Loss** with an exact **$1.00 USD risk ceiling**:
  $$\text{Contract Quantity} = \frac{\text{Risk (\$1.00)}}{\text{Stop Points (25.0)}} = \mathbf{0.0400 \text{ contracts}}.$$

### Asymmetric Payoff Structure:
- **Stop Loss:** 25.0 points ($\Delta = -\$1.00$).
- **Take Profit (2.0R):** 50.0 points ($\Delta = +\$2.00$).
- **EOD Time Stop:** Closed at 15:55 PM ET if neither SL nor TP triggered.

---

## 2. Quick Deploy to DigitalOcean (5 Minutes)

### Step 1: Create a DigitalOcean Droplet
1. Create a Basic Droplet on [DigitalOcean](https://www.digitalocean.com/):
   - **OS:** Ubuntu 24.04 LTS (x64)
   - **Size:** Regular Basic ($4.00/mo or $6.00/mo, 1GB RAM)
   - **Region:** NYC1 / NYC3 (New York data center for low latency to US equity market sessions).

### Step 2: Upload Files to Droplet
From your local terminal, transfer the bot folder:
```bash
scp -r ./weex-nas100-orb-bot root@YOUR_DROPLET_IP:/opt/weex-nas100-orb
```

### Step 3: Run Setup Script
SSH into your droplet:
```bash
ssh root@YOUR_DROPLET_IP
cd /opt/weex-nas100-orb
chmod +x setup_droplet.sh
./setup_droplet.sh
```

### Step 4: Configure API & Telegram Tokens
Create `config.json`:
```bash
cp config.example.json config.json
nano config.json
```
Fill in:
```json
{
  "api_key": "YOUR_WEEX_API_KEY",
  "secret_key": "YOUR_WEEX_SECRET_KEY",
  "passphrase": "YOUR_WEEX_PASSPHRASE",
  "dry_run": false,
  "symbol": "NAS100USDT",
  "risk_usd": 1.0,
  "stop_loss_points": 25.0,
  "take_profit_points": 50.0,
  "telegram_bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
  "telegram_chat_id": "YOUR_TELEGRAM_CHAT_ID"
}
```

### Step 5: Start & Enable Background Service
```bash
systemctl enable --now weex-nas100-orb
```

Check live execution logs:
```bash
journalctl -u weex-nas100-orb -f
```

---

## 3. Telegram Alert Notifications

You will receive real-time updates directly on your phone:
1. **09:45 AM ET:** Opening Range established, OR High/Low boundaries marked, and sizing calculated.
2. **Breakout Entry:** Immediate notification upon breakout trigger with exact entry, stop loss, and 2R take profit.
3. **Exit:** Instant confirmation of Take Profit (+2.0R / +$2.00), Stop Loss (-1.0R / -$1.00), or EOD Close.
