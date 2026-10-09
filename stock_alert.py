"""Telegram market assistant. Run --serve for instant commands, --once for GitHub Actions.

Market prices come from Yahoo Finance via yfinance and can be delayed/unavailable.
Only the authorised TELEGRAM_CHAT_ID may issue commands.
"""
import argparse
import html
import json
import logging
import os
import re
import time
from datetime import datetime, time as clock_time
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

LOG = logging.getLogger('market-bot')
LOCAL_TZ = ZoneInfo('Europe/Vilnius')
TOK = os.environ.get('TELEGRAM_TOKEN', '').strip()
CHAT = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
BASE = f'https://api.telegram.org/bot{TOK}'
STATE_FILE = Path(os.environ.get('BOT_STATE_PATH', 'bot_state.json'))

# ONLY these six instruments are enabled by default; VIX is available via /vix.
DEFAULT = ['IWDA.AS', 'MSTR', 'IS3N.DE', 'ASPI', 'BTC-EUR', 'ETH-EUR']
LABELS = {
    'IWDA.AS': 'iShares Core MSCI World USD (Acc)',
    'MSTR': 'Strategy Class A',
    'IS3N.DE': 'iShares Core MSCI EM IMI USD (Acc)',
    'ASPI': 'ASP Isotopes',
    'BTC-EUR': 'Bitcoin',
    'ETH-EUR': 'Ethereum',
    '^VIX': 'CBOE VIX',
}
# Known quote currencies for initial tickers. Others are read from Yahoo metadata.
KNOWN_CCY = {
    'IWDA.AS': 'EUR', 'MSTR': 'USD', 'IS3N.DE': 'EUR',
    'ASPI': 'USD', 'BTC-EUR': 'EUR', 'ETH-EUR': 'EUR', '^VIX': 'POINTS',
}
ALIASES = {
    'core msci world usd acc': 'IWDA.AS', 'core msci world': 'IWDA.AS',
    'msci world': 'IWDA.AS', 'iwda': 'IWDA.AS',
    'strategy a': 'MSTR', 'strategy': 'MSTR', 'microstrategy': 'MSTR',
    'core msci em imi usd acc': 'IS3N.DE', 'core msci em imi': 'IS3N.DE',
    'msci em imi': 'IS3N.DE', 'is3n': 'IS3N.DE',
    'asp isotopes': 'ASPI', 'asp isotpes': 'ASPI', 'asp isotopes inc': 'ASPI',
    'bitcoin eur': 'BTC-EUR', 'bitcoin': 'BTC-EUR', 'btc': 'BTC-EUR',
    'btc eur': 'BTC-EUR', 'btc-eur': 'BTC-EUR',
    'eth eur': 'ETH-EUR', 'ethereum eur': 'ETH-EUR',
    'ethereum': 'ETH-EUR', 'eth': 'ETH-EUR',
    'vix': '^VIX', 'volatility': '^VIX',
    # Retain familiar aliases for manually adding other assets later.
    'nvidia': 'NVDA', 'tesla': 'TSLA', 'apple': 'AAPL',
    'microsoft': 'MSFT', 'palantir': 'PLTR',
}
# First scheduled report: 08:30 Lithuania. Other updates are after EU and US close.
SLOTS = {'morning': (8, 30), 'europe_close': (18, 45), 'us_close': (0, 15)}
SLOT_TITLES = {
    'morning': '☀️ MORNING MARKET REPORT',
    'europe_close': '🇪🇺 EUROPEAN CLOSE',
    'us_close': '🇺🇸 US MARKET CLOSE',
}


def new_state():
    return {'version': 2, 'active': DEFAULT[:], 'disabled': [],
            'offset': 0, 'reports_sent': {}, 'alerts_sent': {},
            'commands_registered': False}


def load_state():
    if not STATE_FILE.exists():
        return new_state()
    old = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    if old.get('version') != 2:
        # Upgrade the previous ZIP's watchlist to exactly the six requested assets.
        # Keep Telegram offset to avoid replaying previously processed commands.
        return {**new_state(), 'offset': int(old.get('offset', 0))}
    for key, value in new_state().items():
        old.setdefault(key, value)
    return old


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(STATE_FILE.suffix + '.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, STATE_FILE)


def tg(method, data, timeout=45):
    r = requests.post(f'{BASE}/{method}', json=data, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if not payload.get('ok'):
        raise RuntimeError(f'Telegram {method}: {payload.get("description")}')
    return payload['result']


def register_menu(state):
    if state.get('commands_registered'):
        return
    commands = [
        ('new', 'Latest completed daily moves'),
        ('now', 'Latest available quotes'),
        ('lifetime', 'Historical return since first available data'),
        ('vix', 'Market volatility index'),
        ('check', 'Check one asset, e.g. /check MSTR'),
        ('list', 'View active and disabled assets'),
        ('add', 'Add asset, e.g. /add PLTR'),
        ('disable', 'Pause asset, e.g. /disable MSTR'),
        ('enable', 'Resume asset, e.g. /enable MSTR'),
        ('remove', 'Delete asset, e.g. /remove MSTR'),
        ('test', 'Test Telegram connection'),
        ('help', 'Show all commands'),
    ]
    tg('setMyCommands', {'commands': [
        {'command': name, 'description': description}
        for name, description in commands
    ]})
    state['commands_registered'] = True
    save_state(state)


def send(text):
    """Send Telegram HTML; callers send individual complete blocks (< 4,096 chars)."""
    tg('sendMessage', {'chat_id': CHAT, 'text': text, 'parse_mode': 'HTML',
                       'link_preview_options': {'is_disabled': True}})


def send_blocks(title, blocks):
    """Split only between cards, never in the middle of HTML tags."""
    current = title
    for block in blocks:
        if len(current) + len(block) + 3 > 3500 and current != title:
            send(current)
            current = title + '\n\n' + block
        else:
            current += '\n\n' + block
    send(current)


def clean_name(s):
    return re.sub(r'[^\w]+', ' ', s.lower()).strip()


def resolve(query):
    normalized = clean_name(query)
    if normalized in ALIASES:
        return ALIASES[normalized]
    ticker = query.strip().upper()
    if re.fullmatch(r'\^?[A-Z0-9][A-Z0-9.\-=]{0,24}', ticker):
        return ticker
    return None


def currency(symbol):
    if symbol in KNOWN_CCY:
        return KNOWN_CCY[symbol]
    # Never silently mark an unknown USD ticker as EUR.
    t = yf.Ticker(symbol)
    try:
        ccy = t.fast_info.currency
    except Exception as exc:
        raise ValueError(f'Cannot verify currency for {symbol}: {exc}') from exc
    if not ccy:
        raise ValueError(f'Unknown trading currency for {symbol}')
    return ccy.upper()


def raw_closes(symbol, period='3mo'):
    df = yf.Ticker(symbol).history(period=period, interval='1d', auto_adjust=True)
    if df.empty or 'Close' not in df:
        raise ValueError(f'No prices returned for {symbol}')
    closes = df['Close'].dropna()
    if len(closes) < 2:
        raise ValueError(f'Not enough daily closes for {symbol}')
    # Keep one price per local exchange date, where dates correspond to daily candles.
    return {timestamp.date(): float(price) for timestamp, price in closes.items()}


def finish_time(symbol):
    if symbol.endswith('-EUR') or symbol.endswith('-USD'):
        return ZoneInfo('UTC'), clock_time(0, 10), True
    if symbol.endswith('.AS') or symbol.endswith('.DE') or symbol.endswith('.MI'):
        return ZoneInfo('Europe/Amsterdam'), clock_time(17, 45), False
    return ZoneInfo('America/New_York'), clock_time(16, 25), False


def complete_dates(symbol, prices, now=None):
    """Remove current daily candle until after its market's close (+ buffer)."""
    tz, finishing, crypto = finish_time(symbol)
    market_now = (now or datetime.now(tz)).astimezone(tz)
    return {d: p for d, p in prices.items()
            if (d < market_now.date() or
                (not crypto and d == market_now.date() and market_now.time() >= finishing))}


def fx_eurusd(period='3mo'):
    # Yahoo EURUSD=X quotes US dollars per euro.
    result = raw_closes('EURUSD=X', period)
    if not result:
        raise ValueError('EUR/USD FX prices unavailable')
    return result


def eur_prices(symbol, period='3mo', completed=True, now=None):
    base = raw_closes(symbol, period)
    if completed:
        base = complete_dates(symbol, base, now)
    ccy = currency(symbol)
    if ccy in ('EUR', 'POINTS'):
        return base, ccy
    if ccy not in ('USD',):
        raise ValueError(f'{symbol} uses {ccy}; EUR conversion is not set up for this currency')
    fx = fx_eurusd(period)
    keys = sorted(fx)
    from bisect import bisect_right
    result = {}
    for d, price in base.items():
        loc = bisect_right(keys, d) - 1
        if loc < 0:
            continue  # Can't compare EUR prices without FX history.
        rate = fx[keys[loc]]
        if rate > 0:
            result[d] = price / rate
    if not result:
        raise ValueError(f'Could not convert {symbol} to EUR')
    return result, 'EUR'


def evaluate(prices):
    """Use EUR-denominated close-to-close moves; streak compounds over all red sessions."""
    ordered = sorted(prices.items())
    if len(ordered) < 2:
        raise ValueError('Insufficient completed daily sessions')
    (prev_date, previous), (date, latest) = ordered[-2:]
    change = (latest / previous - 1.0) * 100
    streak = 0
    # Count strictly negative returns, starting with the newest session.
    for i in range(len(ordered) - 1, 0, -1):
        if ordered[i][1] < ordered[i-1][1]:
            streak += 1
        else:
            break
    since = (latest / ordered[-streak-1][1] - 1) * 100 if streak else 0.0
    red_dates = [(str(d), (p / ordered[i-1][1] - 1) * 100)
                 for i, (d, p) in enumerate(ordered)
                 if i > 0 and i >= len(ordered) - min(streak, 7)] if streak else []
    return {'date': str(date), 'price': latest, 'change': change,
            'streak': streak, 'since': since, 'days': red_dates,
            'first': str(ordered[0][0]), 'base': ordered[0][1],
            'lifetime': (latest / ordered[0][1] - 1) * 100}


def graph(symbol):
    url = 'https://finance.yahoo.com/quote/' + quote(symbol, safe='') + '/chart/'
    return f'<a href="{html.escape(url, quote=True)}">📈 View chart</a>'


def nice_name(symbol):
    return html.escape(LABELS.get(symbol, symbol))


def money(value, ccy):
    return f'{value:,.2f} €' if ccy == 'EUR' else f'{value:,.2f} pts'


def streak_lines(stats):
    if stats['streak'] < 2:
        return ''
    result = (f'\n🔥 <b>{stats["streak"]} consecutive red sessions</b>'
              f'\n📉 Combined streak: <b>{stats["since"]:+.2f}%</b>')
    for day, change in stats['days']:
        result += f'\n   🔴 {day}: {change:+.2f}%'
    if stats['streak'] > 7:
        result += '\n   (Showing latest 7 red sessions)'
    return result


def card(symbol, mode='closed'):
    prices, ccy = eur_prices(symbol, 'max' if mode == 'lifetime' else '3mo')
    stats = evaluate(prices)
    icon = '🟢' if stats['change'] > 0 else ('🔴' if stats['change'] < 0 else '⚪')
    title = f'{icon} <b>{nice_name(symbol)}</b> <code>{html.escape(symbol)}</code>'
    if mode == 'lifetime':
        return (title + f'\n💶 Latest close: <b>{money(stats["price"], ccy)}</b>'
                f'\n🕰️ Since {stats["first"]}: <b>{stats["lifetime"]:+.2f}%</b>'
                f'\n📅 Last completed: {stats["date"]}\n{graph(symbol)}'), stats
    if mode == 'now':
        # yfinance intraday data may be delayed; on closed markets it can be stale.
        try:
            intra = yf.Ticker(symbol).history(period='2d', interval='5m', auto_adjust=True)
            if not intra.empty:
                row = intra['Close'].dropna()
                if not row.empty:
                    original = float(row.iloc[-1])
                    stamp = row.index[-1]
                    if ccy == 'EUR' and currency(symbol) == 'USD':
                        fx = yf.Ticker('EURUSD=X').history(period='2d', interval='5m')
                        fx_close = fx['Close'].dropna() if not fx.empty else None
                        if fx_close is None or fx_close.empty:
                            raise ValueError('Intraday FX conversion unavailable')
                        original /= float(fx_close.iloc[-1])
                    elif ccy == 'EUR' and currency(symbol) != 'EUR':
                        raise ValueError('Unsupported intraday currency')
                    # Compare with latest *completed* close, not an unfinished daily candle.
                    diff = (original / stats['price'] - 1) * 100
                    return (title + f'\n⚡ Latest quote: <b>{money(original, ccy)}</b> '
                            f'({diff:+.2f}% vs last complete close)'
                            f'\n🕒 Quote: {stamp.strftime("%Y-%m-%d %H:%M %Z")}'
                            f'\n🔒 Last completed: {stats["date"]}, {money(stats["price"], ccy)}'
                            + streak_lines(stats) + f'\n{graph(symbol)}'), stats
        except Exception as exc:
            LOG.warning('Intraday quote unavailable for %s: %s', symbol, exc)
        # Still return a useful latest completed quote when intraday fails.
    return (title + f'\n💶 Close: <b>{money(stats["price"], ccy)}</b>'
            f'\n📅 Session: {stats["date"]} | Daily: <b>{stats["change"]:+.2f}%</b>'
            + streak_lines(stats) + f'\n{graph(symbol)}'), stats


def build_report(state, title, mode='closed'):
    result = []
    alerts = []
    for symbol in state['active']:
        try:
            text, stats = card(symbol, mode)
            result.append(text)
            if mode == 'closed' and stats['streak'] >= 3:
                marker = f'{symbol}:{stats["date"]}'
                if not state['alerts_sent'].get(marker):
                    alerts.append((marker, symbol, text))
        except Exception as exc:
            LOG.warning('Price retrieval problem for %s: %s', symbol, exc)
            result.append(f'⚠️ <b>{nice_name(symbol)}</b>: {html.escape(str(exc))}')
    return result, alerts


def report(state, title, mode='closed', alerts=False):
    intro = title + '\n<em>Prices and daily moves shown in EUR. Data may be delayed.</em>'
    cards, candidate_alerts = build_report(state, intro, mode)
    if not cards:
        send(intro + '\n\nNo active instruments. Use /add TICKER.')
    else:
        send_blocks(intro, cards)
    if alerts:
        for marker, symbol, text in candidate_alerts:
            send('🚨 <b>RED STREAK ALERT</b>\n\n' + text)
            state['alerts_sent'][marker] = True
            save_state(state)


def single(symbol, mode='closed'):
    text, _ = card(symbol, mode)
    send(text)


HELP = (
    '🤖 <b>MARKET BOT</b>\n\n'
    '/new — newest <b>completed</b> daily closes\n'
    '/now — latest available quotes (may be delayed)\n'
    '/lifetime — return since first available EUR-adjusted history\n'
    '/lifetime MSTR — one asset since earliest available history\n'
    '/vix — VIX volatility index (points, not euros)\n'
    '/check bitcoin — latest available BTC-EUR quote\n'
    '/list — enabled/disabled instruments\n'
    '/add PLTR — add a ticker\n'
    '/disable MSTR — stop tracking\n'
    '/enable MSTR — resume tracking\n'
    '/remove MSTR — delete from watchlist\n'
    '/test — test the bot\n'
    '/help — available commands\n\n'
    'You can also type NEW, NOW, LIFETIME or VIX without a slash. '
    'Use exact tickers for companies not in the alias list.'
)


def command(message, state):
    parts = message.strip().split(maxsplit=1)
    cmd = parts[0].split('@')[0].lower().lstrip('/')
    arg = parts[1].strip() if len(parts) > 1 else ''
    if cmd in ('start', 'help'):
        send(HELP)
    elif cmd == 'test':
        send('✅ Telegram bot connected. Instant commands require the --serve host.')
    elif cmd in ('now', 'status'):
        report(state, '⚡ <b>LATEST AVAILABLE QUOTES</b>', mode='now')
    elif cmd == 'new':
        report(state, '🆕 <b>LAST COMPLETED DAILY CLOSES</b>')
    elif cmd == 'lifetime':
        if arg:
            symbol = resolve(arg)
            if not symbol:
                send('Enter a ticker, e.g. /lifetime MSTR')
            else:
                single(symbol, 'lifetime')
        else:
            report(state, '🕰️ <b>AVAILABLE HISTORICAL PERFORMANCE</b>', mode='lifetime')
    elif cmd == 'vix':
        single('^VIX', mode='now')
    elif cmd == 'list':
        active = '\n'.join('• ' + nice_name(s) + f' (<code>{html.escape(s)}</code>)'
                           for s in state['active']) or '(empty)'
        disabled = '\n'.join('• ' + nice_name(s) + f' (<code>{html.escape(s)}</code>)'
                             for s in state['disabled']) or '(empty)'
        send('✅ <b>ACTIVE</b>\n' + active + '\n\n⏸️ <b>DISABLED</b>\n' + disabled)
    elif cmd in ('add', 'enable', 'disable', 'remove', 'check'):
        symbol = resolve(arg)
        if not symbol:
            send('⚠️ Provide the ticker or known name. Example: /add ASPI')
            return
        if cmd == 'check':
            single(symbol, 'now')
            return
        if cmd in ('add', 'enable'):
            if symbol not in state['active']:
                # Reject misspelled tickers rather than silently tracking nonexistent assets.
                _, _ = eur_prices(symbol)
                state['active'].append(symbol)
            if symbol in state['disabled']:
                state['disabled'].remove(symbol)
            send(f'✅ <code>{html.escape(symbol)}</code> enabled.')
        else:
            if symbol in state['active']:
                state['active'].remove(symbol)
            if symbol in state['disabled']:
                state['disabled'].remove(symbol)
            if cmd == 'disable' and symbol not in state['disabled']:
                state['disabled'].append(symbol)
            send(f'✅ <code>{html.escape(symbol)}</code> '
                 + ('disabled.' if cmd == 'disable' else 'removed.'))
    else:
        send('Unknown command. Send /help to see available commands.')


def handle_updates(state, updates):
    for event in updates:
        state['offset'] = max(int(state.get('offset', 0)), event['update_id'] + 1)
        msg = event.get('message', {})
        if str(msg.get('chat', {}).get('id', '')) != CHAT:
            save_state(state)  # Do not allow strangers to use the bot.
            continue
        message = msg.get('text', '').strip()
        if message:
            try:
                command(message, state)
            except Exception as exc:
                LOG.exception('Command error')
                try:
                    send(f'⚠️ Command could not finish: {html.escape(str(exc))}')
                except Exception:
                    LOG.exception('Could not send command error')
        save_state(state)


def schedule_reports(state, now=None):
    local_now = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    for slot, (hour, minute) in SLOTS.items():
        start = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        minutes_since = (local_now - start).total_seconds() / 60
        # Grace window prevents all missed reports being sent in a burst after downtime.
        if not 0 <= minutes_since <= 90:
            continue
        marker = f'{slot}:{local_now.date().isoformat()}'
        if state['reports_sent'].get(slot) == marker:
            continue
        title = (SLOT_TITLES[slot] + '\n'
                 + f'📅 Lithuania: {local_now:%Y-%m-%d %H:%M}')
        report(state, title, alerts=True)
        state['reports_sent'][slot] = marker
        save_state(state)
        LOG.info('Sent scheduled %s report', slot)


def run_once(state):
    updates = tg('getUpdates', {'offset': state['offset'], 'timeout': 0,
                                'allowed_updates': ['message']})
    handle_updates(state, updates)
    schedule_reports(state)
    save_state(state)


def serve(state):
    LOG.info('Starting continuous Telegram long polling; authorised chat %s', CHAT)
    # Only ONE Telegram getUpdates consumer can run for this bot token.
    while True:
        try:
            schedule_reports(state)
            updates = tg('getUpdates', {'offset': state['offset'], 'timeout': 25,
                                        'allowed_updates': ['message']}, timeout=40)
            handle_updates(state, updates)
        except KeyboardInterrupt:
            break
        except Exception:
            LOG.exception('Polling error: retrying in 10 seconds')
            time.sleep(10)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not TOK or not CHAT:
        raise SystemExit('Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID environment variables')
    parser = argparse.ArgumentParser()
    parser.add_argument('--serve', action='store_true', help='Always-on server, instant commands')
    parser.add_argument('--once', action='store_true', help='One pass for GitHub Actions')
    args = parser.parse_args()
    if args.serve == args.once:
        parser.error('Choose exactly one: --serve or --once')
    state = load_state()
    # Persist schema migration before starting polling.
    save_state(state)
    try:
        register_menu(state)
    except Exception as exc:
        LOG.warning('Could not register Telegram command menu: %s', exc)
    if args.serve:
        serve(state)
    else:
        run_once(state)


if __name__ == '__main__':
    main()
