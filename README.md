# BTC Trading Bot

Automated BTC trading bot with trdr.io signal integration and Bybit exchange connectivity.

## Features

- **Signal Integration**: Receives webhooks from trdr.io
  - BTC Double Diamond (highest priority)
  - BTC Diamond
  - Diamond Top Levels
  - Aggregated Liquidation

- **Smart Entry Logic**:
  - Local range detection (1-4h after alert)
  - Liquidity sweep detection
  - Multi-factor confirmation
  - Risk/Reward validation

- **Risk Management**:
  - Position size based on risk percentage
  - Maximum positions limit
  - No averaging, no duplicate positions

- **Position Management**:
  - TP1: 1:2 RR (close 50%)
  - TP2: 1:3 RR (close 30%)
  - Trailing stop on remaining 20%

- **Web Dashboard**:
  - Real-time price updates
  - Active alerts monitoring
  - Position tracking
  - Trade history
  - Settings configuration

## Installation

```bash
# Clone repository
cd btc_trading_bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Copy environment file
cp .env.example .env

# Edit .env with your Bybit API keys
```

## Configuration

Edit `.env` file:

```env
# Bybit API (get from https://testnet.bybit.com for testnet)
BYBIT_API_KEY=your_api_key
BYBIT_API_SECRET=your_api_secret
BYBIT_TESTNET=true  # Set to false for mainnet

# Risk settings
MAX_POSITIONS=3
RISK_PER_TRADE=1.0  # % of balance
MIN_RR=2.0

# Take profit settings
TP1_RR=2.0
TP1_CLOSE_PERCENT=50
TP2_RR=3.0
TP2_CLOSE_PERCENT=30
```

## Running

```bash
# Start the bot
python run.py

# Or with uvicorn directly
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

Access:
- Dashboard: http://localhost:8000
- API Docs: http://localhost:8000/docs

## Webhook Setup

Configure trdr.io to send webhooks to:

```
POST http://your-server:8000/webhook/trdr
```

Expected payload format:
```json
{
    "type": "BTC Diamond",
    "symbol": "BTCUSDT",
    "price": 65000.00,
    "levels": [64500, 65500],
    "message": "Alert message",
    "timestamp": "2024-01-15T12:00:00Z"
}
```

## Trading Logic

### Entry Conditions (LONG)
1. Valid alert received
2. Local range formed
3. Price sweeps below local low
4. Price returns to range
5. 2+ confirmation conditions met
6. RR >= 2.0

### Entry Conditions (SHORT)
1. Valid alert received
2. Local range formed
3. Price sweeps above local high
4. Price returns to range
5. 2+ confirmation conditions met
6. RR >= 2.0

### Confirmation Factors
- Price back in range
- Impulse reversal candle
- Liquidation spike
- Volume confirmation

## API Endpoints

### Webhooks
- `POST /webhook/trdr` - Receive trdr.io alerts
- `GET /webhook/test` - Test webhook endpoint
- `POST /webhook/test-alert` - Send test alert

### Trading
- `GET /api/positions` - Active positions
- `GET /api/trades` - Trade history
- `POST /api/positions/{id}/close` - Close position

### Alerts
- `GET /api/alerts` - Alert history
- `GET /api/alerts/active` - Active alerts

### Settings
- `GET /api/settings` - Get settings
- `PUT /api/settings` - Update settings
- `POST /api/settings/start` - Start bot
- `POST /api/settings/stop` - Stop bot

### WebSocket
- `WS /ws` - Real-time updates

## Project Structure

```
btc_trading_bot/
├── backend/
│   ├── main.py              # FastAPI application
│   ├── config.py            # Configuration
│   ├── core/
│   │   ├── alert_processor.py
│   │   ├── range_detector.py
│   │   ├── liquidity_tracker.py
│   │   ├── confirmation.py
│   │   └── trading_engine.py
│   ├── trading/
│   │   ├── bybit_client.py
│   │   ├── trade_executor.py
│   │   ├── position_manager.py
│   │   └── risk_manager.py
│   ├── api/
│   │   ├── webhooks.py
│   │   ├── trades.py
│   │   └── settings.py
│   ├── services/
│   │   ├── market_data.py
│   │   └── websocket_manager.py
│   └── database/
│       ├── db.py
│       └── repositories.py
├── frontend/
│   └── index.html           # Web dashboard
├── requirements.txt
├── .env.example
├── run.py
└── README.md
```

## Testing

### Test Webhook
```bash
curl -X POST http://localhost:8000/webhook/test-alert \
  -H "Content-Type: application/json" \
  -d '{"alert_type": "BTC Diamond", "price": 65000}'
```

### Run Tests
```bash
pytest tests/
```

## Safety Notes

- Always start on TESTNET first
- Use small risk percentage initially
- Monitor the bot closely
- Never risk more than you can afford to lose
- The bot does not guarantee profits

## License

MIT
