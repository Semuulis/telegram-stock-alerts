[README.md](https://github.com/user-attachments/files/33262638/README.md)
# Telegram Market Bot v2

## Default watchlist (exactly six)

- `IWDA.AS` — iShares Core MSCI World UCITS ETF USD (Acc), **EUR Amsterdam listing**
- `MSTR` — Strategy Inc Class A common shares, USD converted to EUR
- `IS3N.DE` — iShares Core MSCI EM IMI UCITS ETF USD (Acc), **EUR Xetra listing**
- `ASPI` — ASP Isotopes Inc, USD converted to EUR
- `BTC-EUR` — Bitcoin priced in euros
- `ETH-EUR` — Ether priced in euros

`^VIX` is available separately via `/vix` and displayed in **index points**, never mislabeled as euros.

Old watchlist saved using the previous script is **reset on first run** to these six. New additions/removals are saved normally after the upgrade.

## Commands (Telegram chat)

`/new` — Latest completed daily close of every watched instrument; daily % in EUR; negative streak length and cumulative change from the close just before the streak.

`/now` — Latest available intraday quote (may be delayed), compared with last completed close. EUR for all priced assets.

`/lifetime` — Available historical EUR-adjusted return since first available date. `/lifetime MSTR` for one instrument. For USD tickers, only dates with a matching earlier or same-day FX quote can be compared. Historical returns are based on provider *adjusted closes*, not a personal portfolio's purchases.

`/vix` — VIX quote and chart link, index points (not euros). VIX is volatility, not a euro investment price.

`/check bitcoin`, `/list`, `/add TSLA`, `/disable ASPI`, `/enable ASPI`, `/remove TSLA`, `/test`, `/help`, `/start`. Also accepts plain `NEW`, `NOW`, `LIFETIME` and `VIX`.

**Every asset card has a chart hyperlink.** Negative session streaks are shown from 2 days upward; an additional one-time alert is sent after each new completed negative session once the streak reaches 3 days (3, 4, 5, etc.).

## Scheduled reports (Europe/Vilnius)

- **08:30** — Morning report: latest completed European/US/crypto day.
- **18:45** — Europe-close report (after a buffer for Euronext/Xetra).
- **00:15** — US-close report (after a buffer; next Lithuanian calendar day).

They send even on weekends, showing the **date of the most recent completed session**, which could be Friday for stocks/ETFs. On GitHub Actions, job delays may shift these times. A 90-minute catch-up grace period prevents late stale messages. Times follow Lithuania's DST automatically on a continuous host.

## Setup A: GitHub Actions (no 24/7 host, commands delayed)

1. Copy `stock_alert.py`, `requirements.txt`, and `.github/workflows/monitor.yml` into the **root** of your existing GitHub repository (preserve folder paths).
2. Your existing GitHub Actions secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` are reused.
3. Settings → Actions → General → Workflow permissions should permit write to contents, unless managed by an organization. This workflow declares `permissions: contents: write`. GitHub state `bot_state.json` is explicitly tracked with `git add -f` even though ignored locally.
4. Actions → **Telegram Market Monitor (GitHub fallback)** → **Run workflow** to test. `/test` + manually run workflow.
5. Commands are checked approximately every 15 minutes, not instant. GitHub may delay or skip cron runs. On public repos inactive for 60 days, cron may be disabled automatically.

## Setup B: Railway persistent worker (instant, preferred)

1. Upload these files to your existing GitHub repository (the `Procfile` is included), then connect it to Railway as a Python service from GitHub.
2. Set **Start Command** to `python stock_alert.py --serve` (do not use `--once`). Run exactly **one** replica / worker.
3. In Railway **Variables** add `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`. Don't reveal tokens or put them in commits.
4. Create a Railway **Volume** mounted at `/app/data` (persistent), and add `BOT_STATE_PATH=/app/data/bot_state.json` to Variables. Without a volume, the watchlist may reset on each deployment.
5. **Disable the old GitHub Actions Telegram workflow** after Railway is deployed, or `getUpdates` will conflict or split incoming commands between two processes. In Actions, choose the workflow and use its `...` → Disable workflow.
6. Open Telegram and type `/test`, `/now`, `/new`, `/lifetime`, `/vix` anytime.
7. Railway service must remain running; service usage/pricing may apply.

### Operational notes

- Quotes are from Yahoo Finance via `yfinance`, unofficial and often **delayed**. Intraday quotes are best-effort, not guaranteed true-time tick data.
- The daily signal is `close < previous session close` in **EUR** (USD stocks are FX-converted). It is **not** candle open-to-close red. Use `auto_adjust=True` so corporate actions are adjusted.
- US and European markets have different close times; there is no single 08:30 Lithuania exchange opening for all assets. **08:30 is a morning report time**, not the US market open.
- VIX has no EUR price; report its index level and movement in points/percentage.
- If Telegram returns `409 Conflict`, stop the old polling copy or remove an existing webhook. Never run Railway `--serve` and GitHub `--once` simultaneously with the same token.
- Telegram commands are only accepted from `TELEGRAM_CHAT_ID`, not from strangers.
