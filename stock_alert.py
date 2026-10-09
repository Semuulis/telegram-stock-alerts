"""Private Telegram market bot, run periodically with GitHub Actions."""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

TOKEN = os.environ['TELEGRAM_TOKEN']
CHAT_ID = str(os.environ['TELEGRAM_CHAT_ID']).strip()
API = f'https://api.telegram.org/bot{TOKEN}'
STATE_PATH = Path('bot_state.json')
LITHUANIA = ZoneInfo('Europe/Vilnius')

DEFAULT_WATCHLIST = [
    'NVDA', 'TSLA', 'AAPL', 'MSFT', 'MSTR', 'BTC-USD', 'ETH-USD',
    'VOO', 'QQQ', 'IWDA.AS', 'VALL.AS', 'VWCE.DE', '^VIX',
]

# Names are case-insensitive; new instruments can always be added by ticker.
ALIASES = {
    'nvidia': 'NVDA', 'tesla': 'TSLA', 'apple': 'AAPL',
    'microsoft': 'MSFT', 'microstrategy': 'MSTR', 'strategy': 'MSTR',
    'bitcoin': 'BTC-USD', 'btc': 'BTC-USD',
    'ethereum': 'ETH-USD', 'eth': 'ETH-USD',
    'vanguard sp500': 'VOO', 'sp500': 'VOO',
    'nasdaq': 'QQQ', 'nasdaq 100': 'QQQ',
    'msci world': 'IWDA.AS', 'ishares world': 'IWDA.AS',
    'ishares msci world': 'IWDA.AS',
    'vanguard global all cap': 'VALL.AS', 'global all cap': 'VALL.AS',
    'vanguard all world': 'VWCE.DE', 'all world': 'VWCE.DE',
    'vix': '^VIX',
}


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    return {'active': DEFAULT_WATCHLIST[:], 'disabled': [],
            'offset': 0, 'daily_date': '', 'last_alerts': {}}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + '\n',
                          encoding='utf-8')


def tg(method, payload):
    response = requests.post(f'{API}/{method}', json=payload, timeout=35)
    response.raise_for_status()
    result = response.json()
    if not result.get('ok'):
        raise RuntimeError(f'Telegram {method}: {result.get("description")}')
    return result['result']


def send(message):
    # Telegram limits a text message to 4096 characters.
    lines = message.split('\n')
    chunk = ''
    for line in lines:
        if len(chunk) + len(line) + 1 > 3700 and chunk:
            tg('sendMessage', {'chat_id': CHAT_ID, 'text': chunk})
            chunk = ''
        chunk += line + '\n'
    if chunk.strip():
        tg('sendMessage', {'chat_id': CHAT_ID, 'text': chunk})


def resolve(query):
    value = ' '.join(query.casefold().strip().split())
    if value in ALIASES:
        return ALIASES[value]
    candidate = query.strip().upper()
    if re.fullmatch(r'\^?[A-Z0-9][A-Z0-9.\-=]{0,19}', candidate):
        return candidate
    return None


def closed_prices(symbol):
    history = yf.Ticker(symbol).history(period='2mo', interval='1d',
                                       auto_adjust=True)
    if history.empty or 'Close' not in history:
        raise ValueError(f'No daily price data for {symbol}')
    closes = history['Close'].dropna()
    if closes.empty:
        raise ValueError(f'No closing prices for {symbol}')
    # yfinance timestamps are normally exchange-local. Crypto uses UTC.
    exchange_tz = closes.index.tz
    if exchange_tz is None:
        raise ValueError(f'Cannot determine market timezone for {symbol}')
    current_market_date = datetime.now(exchange_tz).date()
    closes = closes[closes.index.date < current_market_date]
    if len(closes) < 4:
        raise ValueError(f'Insufficient completed sessions for {symbol}')
    return closes


def analyze(symbol):
    closes = closed_prices(symbol)
    pct = closes.pct_change().dropna() * 100
    last_change = float(pct.iloc[-1])
    streak = 0
    for move in reversed(pct.tolist()):
        if move < 0:
            streak += 1
        else:
            break
    last_3 = pct.iloc[-3:]
    latest_session = closes.index[-1].strftime('%Y-%m-%d')
    return {'symbol': symbol, 'date': latest_session,
            'price': float(closes.iloc[-1]), 'change': last_change,
            'streak': streak, 'days': [(d.strftime('%m-%d'), float(v))
                                       for d, v in last_3.items()],
            'total_3': (float(closes.iloc[-1]) /
                        float(closes.iloc[-4]) - 1) * 100}


def row(data, today=False):
    sign = '🟢' if data['change'] > 0 else ('🔴' if data['change'] < 0 else '⚪')
    s = (f"{sign} {data['symbol']}: {data['change']:+.2f}% "
         f"| {data['price']:,.2f} | close {data['date']}")
    if data['streak'] >= 3:
        s += f"\n   🚨 {data['streak']} RED CLOSES IN A ROW"
        s += ''.join(f"\n   🔴 {d}: {v:+.2f}%" for d, v in data['days'])
        s += f"\n   Last 3 combined: {data['total_3']:+.2f}%"
    return s


def report(state, title, alert_on_new_streak=False):
    lines = [title, 'Daily close-to-close changes; last finished session.', '']
    for symbol in state['active']:
        try:
            data = analyze(symbol)
            lines.append(row(data))
            if alert_on_new_streak and data['streak'] == 3:
                # Remember which completed candle triggered this alert.
                if state['last_alerts'].get(symbol) != data['date']:
                    state['last_alerts'][symbol] = data['date']
                    send('🚨 THREE RED DAYS\n' + row(data))
        except Exception as exc:
            lines.append(f'⚠️ {symbol}: data unavailable ({exc})')
    return '\n'.join(lines)


HELP = ("📊 MARKET BOT COMMANDS\n"
        "/now — all monitored assets right now (latest completed closes)\n"
        "/check NVDA — one asset\n"
        "/list — active and disabled symbols\n"
        "/add bitcoin — add using a known name or ticker\n"
        "/disable TSLA — temporarily turn off\n"
        "/enable TSLA — turn back on\n"
        "/remove TSLA — delete from the watchlist\n"
        "/test — test Telegram notifications\n"
        "/help — show commands\n\n"
        "Unknown company names? Use an exchange ticker, e.g. /add PLTR.")


def command(text, state):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split('@')[0].lower()
    query = parts[1].strip() if len(parts) == 2 else ''
    if cmd in ('/start', '/help'):
        send(HELP)
    elif cmd == '/test':
        send('✅ Telegram market bot is connected and responding!')
    elif cmd in ('/now', '/status'):
        send(report(state, '📊 WATCHLIST UPDATE'))
    elif cmd == '/list':
        send('✅ ACTIVE:\n' + ('\n'.join(state['active']) or '(none)') +
             '\n\n⏸️ DISABLED:\n' +
             ('\n'.join(state['disabled']) or '(none)'))
    elif cmd in ('/add', '/enable', '/disable', '/remove', '/check'):
        symbol = resolve(query)
        if not symbol:
            send('Please provide a known name or ticker, e.g. /add bitcoin or /add PLTR')
            return
        if cmd in ('/add', '/enable'):
            if symbol not in state['active']:
                # Verify the ticker before storing it.
                try:
                    closed_prices(symbol)
                except Exception as exc:
                    send(f'⚠️ Could not verify {symbol}: {exc}')
                    return
                state['active'].append(symbol)
            if symbol in state['disabled']:
                state['disabled'].remove(symbol)
            send(f'✅ {symbol} enabled. Active assets: {len(state["active"])}')
        elif cmd == '/check':
            try:
                send(row(analyze(symbol)))
            except Exception as exc:
                send(f'⚠️ {symbol}: {exc}')
        else:
            if symbol in state['active']:
                state['active'].remove(symbol)
            if cmd == '/disable' and symbol not in state['disabled']:
                state['disabled'].append(symbol)
            if cmd == '/remove' and symbol in state['disabled']:
                state['disabled'].remove(symbol)
            state['last_alerts'].pop(symbol, None)
            send(f"✅ {symbol} {'disabled' if cmd == '/disable' else 'removed'}")
    else:
        send('Unknown command. Send /help for available commands.')


def main():
    state = load_state()
    # Single short poll per GitHub Actions run; replies arrive on next scheduled run.
    updates = tg('getUpdates', {'offset': state.get('offset', 0), 'timeout': 0,
                                'allowed_updates': ['message']})
    for item in updates:
        update_id = item['update_id']
        state['offset'] = max(state.get('offset', 0), update_id + 1)
        msg = item.get('message', {})
        if str(msg.get('chat', {}).get('id')) != CHAT_ID:
            continue  # Never allow strangers to control your watchlist.
        text = msg.get('text', '').strip()
        if text:
            try:
                command(text, state)
            except Exception as exc:
                print(f'Command failed: {exc}')
                try:
                    send(f'⚠️ Command failed: {exc}')
                except Exception:
                    pass

    local_now = datetime.now(LITHUANIA)
    local_day = local_now.date().isoformat()
    # Once per day, after 09:15 Vilnius time (US session has closed).
    if (local_now.hour, local_now.minute) >= (9, 15) and state.get('daily_date') != local_day:
        summary = report(state, f'🌍 DAILY MARKET REPORT — {local_day}',
                         alert_on_new_streak=True)
        send(summary)
        state['daily_date'] = local_day

    save_state(state)


if __name__ == '__main__':
    main()
