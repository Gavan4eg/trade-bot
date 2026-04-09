# BTC Trading Bot

Automated BTC trading bot with trdr.io signal integration and Bybit exchange connectivity.

## How It Works

The bot receives signals from trdr.io and executes trades only after full pipeline confirmation:

```
trdr.io webhook → Range Detection → Liquidity Sweep → Confirmation → Trade
```

1. **Signal received** — BTC Diamond, Double Diamond, Diamond Top Levels, or Aggregated Liquidation
2. **Range detection** — bot maps the local High/Low/Mid over last 24×1h candles
3. **Sweep detection** — waits for price to break above high (SHORT setup) or below low (LONG setup)
4. **Confirmation** — requires 2+ of: candle closed back in range, impulse reversal candle, liquidation spike, volume confirmation
5. **Trade execution** — entry at range boundary, SL behind sweep low/high (or behind liquidation cluster if available), TP1=1:2, TP2=1:3, trailing on remainder

## Alert Priority

| Priority | Signal | Role |
|----------|--------|------|
| 1 | BTC Double Diamond | Strongest entry signal |
| 2 | BTC Diamond | Standard entry signal |
| 3 | Diamond Top Levels | Weaker entry signal |
| 4 | Aggregated Liquidation | Confirming factor only — never opens trades standalone |

## Position Management

- **TP1** — 1:2 RR, close 50% of position
- **TP2** — 1:3 RR, close 30% of remaining
- **Trailing stop** — activates after TP2, trails remaining 20%
- **Breakeven** — stop moved to entry after TP1
- **Liquidation cluster stop** — if Aggregated Liquidation data is available, stop is placed behind the cluster zone instead of the local extremum

## Risk Rules

- Max 1 long + 1 short at a time
- No averaging, no duplicate entries
- Position size = 1% account risk per trade (configurable)
- Minimum RR = 2.0 (configurable)

---

## Installation

```bash
cd btc_trading_bot
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
```

## Configuration

Edit `.env`:

```env
# Bybit API
BYBIT_API_KEY=your_key
BYBIT_API_SECRET=your_secret
BYBIT_TESTNET=true          # false for mainnet

# Trading
PAPER_TRADING=false
RISK_PER_TRADE=1.0          # % of balance per trade
MAX_POSITIONS=3
MIN_RR=2.0

# Range detection
RANGE_MAX_WIDTH_PERCENT=5   # max allowed range width

# Liquidation zone stop buffer
LIQUIDATION_BUFFER_PERCENT=0.2

# Webhook security
WEBHOOK_TOKEN=yourSecretToken
```

## Running

```bash
python run.py
```

Dashboard: `http://localhost:8000`

---

## Webhook Setup (trdr.io)

Send alerts to:
```
POST https://your-server/webhook/trdr?token=yourSecretToken
```

trdr.io payload format (handled automatically):
```json
{
  "name": "BTC Diamond",
  "time": "2024-01-15T12:00:00Z",
  "price": 84000,
  "side": "long",
  "ticker": "BTCUSDT",
  "base": "BTC",
  "message": "...",
  "cooldown": 300
}
```

---

## Testing Webhooks Locally

> Replace `mySecretToken123` with your `WEBHOOK_TOKEN` from `.env`

### BTC Diamond (LONG setup)
```bash
curl -X POST "http://localhost:8000/webhook/trdr?token=mySecretToken123" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "BTC Diamond",
    "time": "2024-01-15T12:00:00Z",
    "price": 84000,
    "side": "long",
    "ticker": "BTCUSDT",
    "base": "BTC",
    "cooldown": 300
  }'
```

### BTC Double Diamond (LONG setup — highest priority)
```bash
curl -X POST "http://localhost:8000/webhook/trdr?token=mySecretToken123" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "BTC Double Diamond",
    "time": "2024-01-15T12:00:00Z",
    "price": 84000,
    "side": "long",
    "ticker": "BTCUSDT",
    "base": "BTC",
    "cooldown": 300
  }'
```

### Diamond Top Levels (SHORT setup)
```bash
curl -X POST "http://localhost:8000/webhook/trdr?token=mySecretToken123" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Diamond Top Levels",
    "time": "2024-01-15T12:00:00Z",
    "price": 84000,
    "side": "short",
    "ticker": "BTCUSDT",
    "base": "BTC",
    "cooldown": 300
  }'
```

### Aggregated Liquidation (confirming factor for active Diamond)
```bash
curl -X POST "http://localhost:8000/webhook/trdr?token=mySecretToken123" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Aggregated Liquidation",
    "time": "2024-01-15T12:01:00Z",
    "price": 83500,
    "side": "long",
    "ticker": "BTCUSDT",
    "base": "BTC",
    "exchange": "Binance USDⓈ-M",
    "message": "Alert: Aggregated Liquidation\nSide: Long\nBTC/USDT\nLongs: 5000000 > 2000000",
    "cooldown": 300
  }'
```

### Force sweep (skip waiting, for testing pipeline)
```bash
curl -X POST "http://localhost:8000/webhook/test-force-sweep"
```

### Check pipeline state
```bash
curl http://localhost:8000/webhook/test-engine-state
```

### Check open positions (in-memory)
```bash
curl http://localhost:8000/webhook/test-positions
```

### Check balance
```bash
curl http://localhost:8000/api/settings/balance
```

---

## API Endpoints

### Webhooks
| Method | Path | Description |
|--------|------|-------------|
| POST | `/webhook/trdr` | Receive trdr.io alert |
| GET  | `/webhook/test` | Health check |
| GET  | `/webhook/test-engine-state` | Live pipeline state |
| GET  | `/webhook/test-positions` | In-memory positions |
| POST | `/webhook/test-force-sweep` | Force sweep for testing |

### Trading
| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/positions` | Active positions |
| GET  | `/api/trades` | Trade history |
| POST | `/api/positions/{id}/close` | Close position manually |

### Alerts
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/alerts` | Alert history with full pipeline state |
| GET | `/api/alerts/active` | Active alerts with live pipeline |

### Settings
| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/settings` | Get settings |
| PUT  | `/api/settings` | Update settings |
| POST | `/api/settings/start` | Start bot |
| POST | `/api/settings/stop` | Stop bot |
| GET  | `/api/settings/balance` | Exchange balance |
| GET  | `/api/settings/status` | Bot status |

### WebSocket
`WS /ws` — real-time price, alert, position updates

---

## Deploying to VPS

### Requirements
- Ubuntu 22.04 VPS (2GB RAM minimum)
- Python 3.11+
- Domain or static IP (for trdr.io webhook)

### Setup

```bash
# Install dependencies
sudo apt update && sudo apt install python3-pip python3-venv nginx -y

# Clone and install
git clone <repo> /opt/btc_bot
cd /opt/btc_bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Create systemd service
sudo nano /etc/systemd/system/btcbot.service
```

Service file:
```ini
[Unit]
Description=BTC Trading Bot
After=network.target

[Service]
WorkingDirectory=/opt/btc_bot
ExecStart=/opt/btc_bot/venv/bin/python run.py
Restart=always
RestartSec=5
EnvironmentFile=/opt/btc_bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable btcbot
sudo systemctl start btcbot
```

### Nginx reverse proxy (with SSL)

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
    }
}
```

```bash
sudo certbot --nginx -d yourdomain.com
```

---

## Safety Notes

- Always test on TESTNET first
- Use small `RISK_PER_TRADE` initially (0.5–1%)
- Monitor logs: `tail -f logs/bot.log`
- The bot does not guarantee profits
- Never risk more than you can afford to lose
