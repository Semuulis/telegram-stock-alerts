import os
import requests
import yfinance as yf

# Stocks, ETFs and cryptocurrencies to monitor
WATCHLIST = [
    # STOCKS
    "NVDA",       # NVIDIA
    "TSLA",       # Tesla
    "AAPL",       # Apple
    "MSFT",       # Microsoft
    "MSTR",       # MicroStrategy

    # CRYPTOCURRENCIES
    "BTC-USD",    # Bitcoin
    "ETH-USD",    # Ethereum

    # US ETFS
    "VOO",        # Vanguard S&P 500
    "QQQ",        # Nasdaq-100

    # EUROPEAN ACCUMULATING ETFS
    "IWDA.AS",    # iShares Core MSCI World (Acc)
    "VALL.AS",    # Vanguard Global All-Cap (0.07% Acc)
    "VWCE.DE",    # Vanguard FTSE All-World (Acc)

    # MARKET VOLATILITY
    "^VIX"        # CBOE Volatility Index
]

BOT_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def send_alert(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )
    response.raise_for_status()


def check_asset(symbol):
    data = yf.Ticker(symbol).history(
        period="1mo",
        interval="1d",
        auto_adjust=True
    )

    closes = data["Close"].dropna()

    if len(closes) < 5:
        return

    # Calculate last four daily changes
    changes = closes.pct_change().dropna() * 100

    last_three = changes.iloc[-3:]
    previous_day = changes.iloc[-4]

    # Detect exactly three consecutive red days
    if not (last_three < 0).all():
        return

    if previous_day < 0:
        return

    total_drop = (
        closes.iloc[-1] / closes.iloc[-4] - 1
    ) * 100

    message = f"🚨 3 RED DAYS: {symbol}\n\n"

    for date, change in last_three.items():
        message += f"🔴 {date:%Y-%m-%d}: {change:.2f}%\n"

    message += f"\n📉 Total decline: {total_drop:.2f}%"
    message += f"\n💰 Closing price: ${closes.iloc[-1]:.2f}"

    send_alert(message)
    print(f"Alert sent for {symbol}")


# Test notification
send_alert("✅ Stock Monitoring Bot connected successfully!")

for symbol in WATCHLIST:
    try:
        check_asset(symbol)
    except Exception as e:
        print(f"Error checking {symbol}: {e}")
