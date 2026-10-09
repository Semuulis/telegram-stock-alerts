import os
from datetime import datetime, timezone, timedelta

import requests
import yfinance as yf

# Stocks, ETFs and cryptocurrencies to monitor
WATCHLIST = [
    "NVDA",
    "TSLA",
    "AAPL",
    "MSFT",
    "VOO",
    "QQQ",
    "BTC-USD",
    "ETH-USD"
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
    # Use UTC dates to exclude the unfinished current day
    today = datetime.now(timezone.utc).date()

    data = yf.Ticker(symbol).history(
        period="1mo",
        interval="1d",
        end=today.isoformat(),
        auto_adjust=True
    )

    closes = data["Close"].dropna()

    if len(closes) < 5:
        return

    # Last 3 returns, plus one earlier return
    recent = closes.tail(5)
    changes = recent.pct_change().dropna() * 100

    last_three = changes.iloc[-3:]
    previous = changes.iloc[-4]

    # Alert on day 3, not day 4 or 5
    if not (last_three < 0).all():
        return

    if previous < 0:
        return

    # Only act on yesterday's completed session
    latest_date = closes.index[-1].date()
    if latest_date != today - timedelta(days=1):
        return

    total = (
        recent.iloc[-1] / recent.iloc[-4] - 1
    ) * 100

    message = f"🚨 3 RED DAYS: {symbol}\n\n"

    for date, change in last_three.items():
        message += f"🔴 {date:%Y-%m-%d}: {change:.2f}%\n"

    message += f"\n📉 Total: {total:.2f}%"
    message += f"\n💰 Closing price: ${recent.iloc[-1]:.2f}"

    send_alert(message)
    print(f"Alert sent: {symbol}")


for symbol in WATCHLIST:
    try:
        check_asset(symbol)
    except Exception as error:
        print(f"Error with {symbol}: {error}")
