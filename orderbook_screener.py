#!/usr/bin/env python3
"""
Order Book Pressure Screener — Binance -> Telegram

Стежить за стаканом топ-N пар USDT на Binance Spot і шле алерт у Telegram,
коли на якомусь ціновому рівні (bid або ask) обʼєм у $ суттєво перевищує
медіану рівнів тієї ж сторони — тобто зʼявляється "стіна".

Працює цілодобово (poll loop), не залежить від відкритого браузера чи
конкретного компʼютера — головне, щоб процес був запущений (VPS / Raspberry Pi
/ будь-яка машина, що працює 24/7). Дивись README.md для інструкції запуску.
"""

import os
import sys
import json
import time
import logging
import statistics
from datetime import datetime, timezone

import requests

# ============================== КОНФІГУРАЦІЯ ==============================
# Усе можна або відредагувати тут напряму, або задати через змінні оточення
# (env vars мають пріоритет, якщо задані).

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

TOP_N_COINS        = int(os.environ.get("TOP_N_COINS", 30))       # скільки монет моніторити
POLL_INTERVAL_SEC  = int(os.environ.get("POLL_INTERVAL_SEC", 20)) # пауза між повними циклами
SYMBOL_REQ_DELAY   = float(os.environ.get("SYMBOL_REQ_DELAY", 0.2))  # пауза між запитами по монетах (rate-limit)
REFRESH_LIST_EVERY = int(os.environ.get("REFRESH_LIST_EVERY", 1800))  # як часто оновлювати топ-N список (сек)

SENSITIVITY   = float(os.environ.get("SENSITIVITY", 5.0))   # у скільки разів обʼєм рівня має перевищувати медіану
MIN_USD_WALL  = float(os.environ.get("MIN_USD_WALL", 30000))  # мінімальний обʼєм рівня в $, щоб рахувати стіною
DEPTH_LIMIT   = int(os.environ.get("DEPTH_LIMIT", 50))       # скільки рівнів стакану тягнути з кожної сторони

# Файл, у якому зберігається стан (список активних стін) між окремими
# запусками процесу. Потрібен для режиму --once (GitHub Actions): кожен
# запуск там — новий процес "з нуля", тож без цього файлу довелось би або
# слати алерт про одну й ту саму стіну щоразу, або взагалі не порівнювати.
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

STABLE_BLACKLIST = {
    "USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "DAIUSDT", "USDPUSDT",
    "EURUSDT", "GBPUSDT", "TRYUSDT", "AEURUSDT", "BFUSDUSDT", "USTCUSDT",
    "PAXGUSDT", "XAUTUSDT",
}
LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

BINANCE_BASE = "https://data-api.binance.vision"
TELEGRAM_API = f"https://api.telegram.org/bot{{token}}/sendMessage"

# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("obp-screener")

session = requests.Session()
session.headers.update({"User-Agent": "orderbook-pressure-screener/1.0"})


def fetch_top_symbols(n=TOP_N_COINS):
    """Тягне топ-N пар USDT за обʼємом за 24г, відфільтрувавши стейблкоїни/лівередж."""
    resp = session.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    def is_valid(d):
        sym = d["symbol"]
        if not sym.endswith("USDT"):
            return False
        if sym in STABLE_BLACKLIST:
            return False
        if any(sym.endswith(suf) for suf in LEVERAGED_SUFFIXES):
            return False
        try:
            return float(d["quoteVolume"]) > 0
        except (KeyError, ValueError):
            return False

    filtered = [d for d in data if is_valid(d)]
    filtered.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
    return [d["symbol"] for d in filtered[:n]]


def fetch_depth(symbol, limit=DEPTH_LIMIT):
    resp = session.get(
        f"{BINANCE_BASE}/api/v3/depth",
        params={"symbol": symbol, "limit": limit},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def analyze_side(levels, side, best_price, sensitivity, min_usd):
    """levels: список [price_str, qty_str]. Повертає список знайдених стін."""
    parsed = []
    for price_s, qty_s in levels:
        price = float(price_s)
        qty = float(qty_s)
        parsed.append((price, qty, price * qty))

    notionals = [p[2] for p in parsed]
    if not notionals:
        return []
    med = statistics.median(notionals) or 1.0

    walls = []
    for price, qty, notional in parsed:
        if notional >= med * sensitivity and notional >= min_usd:
            dist_pct = ((price - best_price) / best_price * 100) if best_price else 0.0
            walls.append({
                "side": side,
                "price": price,
                "notional": notional,
                "dist_pct": dist_pct,
            })
    return walls


def fmt_usd(n):
    if n >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:.0f}"


def fmt_price(p):
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}".rstrip("0")


def send_telegram(text):
    if "PASTE_YOUR" in TELEGRAM_BOT_TOKEN or "PASTE_YOUR" in TELEGRAM_CHAT_ID:
        log.warning("Telegram не налаштований (токен/chat_id не задані) — алерт лише в лог:\n%s", text)
        return
    url = TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN)
    try:
        resp = session.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            log.error("Telegram API помилка %s: %s", resp.status_code, resp.text[:300])
    except requests.RequestException as e:
        log.error("Не вдалося надіслати Telegram-повідомлення: %s", e)


def wall_key(wall):
    # округлюємо ціну, щоб дрібні коливання останнього знаку не створювали "новий" рівень
    return (wall["side"], round(wall["price"], 8))


def load_state(path):
    """symbol -> set of wall_key tuples. Порожньо, якщо файлу ще нема (перший запуск)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {sym: {tuple(k) for k in keys} for sym, keys in raw.items()}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Не вдалося прочитати файл стану %s (%s) — стартую з чистого аркуша.", path, e)
        return {}


def save_state(path, prev_wall_keys):
    serializable = {sym: [list(k) for k in keys] for sym, keys in prev_wall_keys.items()}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serializable, f)
    except OSError as e:
        log.error("Не вдалося записати файл стану %s: %s", path, e)


def build_alert_message(new_walls_by_symbol):
    """new_walls_by_symbol: dict symbol -> list of wall dicts (нові за цей цикл)."""
    lines = ["🧱 <b>Нові стіни в стакані</b>"]
    for symbol, walls in new_walls_by_symbol.items():
        base = symbol.replace("USDT", "")
        for w in walls:
            side_label = "🟢 BID" if w["side"] == "bid" else "🔴 ASK"
            dist = w["dist_pct"]
            dist_txt = f"{dist:+.2f}%" if dist else "на best-ціні"
            lines.append(
                f"{side_label} <b>{base}/USDT</b> — {fmt_price(w['price'])} "
                f"· {fmt_usd(w['notional'])} · {dist_txt}"
            )
    return "\n".join(lines)


def run():
    log.info("Старт скринера. Топ монет: %d, чутливість: %sx медіани, мін. обʼєм: %s",
              TOP_N_COINS, SENSITIVITY, fmt_usd(MIN_USD_WALL))

    symbols = []
    last_symbol_refresh = 0
    prev_wall_keys = {}  # symbol -> set(wall_key)

    while True:
        cycle_start = time.time()

        if not symbols or (cycle_start - last_symbol_refresh) > REFRESH_LIST_EVERY:
            try:
                symbols = fetch_top_symbols(TOP_N_COINS)
                last_symbol_refresh = cycle_start
                log.info("Оновлено список монет (%d шт.): %s", len(symbols), ", ".join(symbols[:8]) + ("…" if len(symbols) > 8 else ""))
            except requests.RequestException as e:
                log.error("Не вдалося отримати список монет: %s. Повторю наступного циклу.", e)
                time.sleep(POLL_INTERVAL_SEC)
                continue

        new_walls_by_symbol = {}

        for symbol in symbols:
            try:
                depth = fetch_depth(symbol)
                bids = depth.get("bids", [])
                asks = depth.get("asks", [])
                best_bid = float(bids[0][0]) if bids else 0.0
                best_ask = float(asks[0][0]) if asks else 0.0

                bid_walls = analyze_side(bids, "bid", best_bid, SENSITIVITY, MIN_USD_WALL)
                ask_walls = analyze_side(asks, "ask", best_ask, SENSITIVITY, MIN_USD_WALL)
                all_walls = bid_walls + ask_walls

                current_keys = {wall_key(w) for w in all_walls}
                previous_keys = prev_wall_keys.get(symbol, set())

                fresh = [w for w in all_walls if wall_key(w) not in previous_keys]
                if fresh:
                    new_walls_by_symbol[symbol] = fresh

                prev_wall_keys[symbol] = current_keys

            except requests.RequestException as e:
                log.warning("Пропуск %s через мережеву помилку: %s", symbol, e)
            except Exception as e:
                log.exception("Неочікувана помилка для %s: %s", symbol, e)

            time.sleep(SYMBOL_REQ_DELAY)

        if new_walls_by_symbol:
            msg = build_alert_message(new_walls_by_symbol)
            log.info("Знайдено нові стіни у %d монет(и) — надсилаю в Telegram.", len(new_walls_by_symbol))
            send_telegram(msg)
        else:
            log.info("Цикл завершено, нових стін не знайдено.")

        elapsed = time.time() - cycle_start
        sleep_left = max(0.0, POLL_INTERVAL_SEC - elapsed)
        time.sleep(sleep_left)


def run_once():
    """Один цикл опитування + вихід. Для cron-подібних середовищ (GitHub Actions),
    де процес не живе між запусками — стан підвантажується/зберігається у STATE_FILE."""
    log.info("Одноразовий запуск. Топ монет: %d, чутливість: %sx медіани, мін. обʼєм: %s",
              TOP_N_COINS, SENSITIVITY, fmt_usd(MIN_USD_WALL))

    try:
        symbols = fetch_top_symbols(TOP_N_COINS)
        log.info("Отримано %d монет.", len(symbols))
    except requests.RequestException as e:
        log.error("Не вдалося отримати список монет: %s. Завершення.", e)
        sys.exit(1)

    prev_wall_keys = load_state(STATE_FILE)
    new_walls_by_symbol = {}

    for symbol in symbols:
        try:
            depth = fetch_depth(symbol)
            bids = depth.get("bids", [])
            asks = depth.get("asks", [])
            best_bid = float(bids[0][0]) if bids else 0.0
            best_ask = float(asks[0][0]) if asks else 0.0

            bid_walls = analyze_side(bids, "bid", best_bid, SENSITIVITY, MIN_USD_WALL)
            ask_walls = analyze_side(asks, "ask", best_ask, SENSITIVITY, MIN_USD_WALL)
            all_walls = bid_walls + ask_walls

            current_keys = {wall_key(w) for w in all_walls}
            previous_keys = prev_wall_keys.get(symbol, set())

            fresh = [w for w in all_walls if wall_key(w) not in previous_keys]
            if fresh:
                new_walls_by_symbol[symbol] = fresh

            prev_wall_keys[symbol] = current_keys

        except requests.RequestException as e:
            log.warning("Пропуск %s через мережеву помилку: %s", symbol, e)
        except Exception as e:
            log.exception("Неочікувана помилка для %s: %s", symbol, e)

        time.sleep(SYMBOL_REQ_DELAY)

    if new_walls_by_symbol:
        msg = build_alert_message(new_walls_by_symbol)
        log.info("Знайдено нові стіни у %d монет(и) — надсилаю в Telegram.", len(new_walls_by_symbol))
        send_telegram(msg)
    else:
        log.info("Нових стін не знайдено.")

    save_state(STATE_FILE, prev_wall_keys)
    log.info("Стан збережено у %s. Завершення процесу.", STATE_FILE)


if __name__ == "__main__":
    once = "--once" in sys.argv or os.environ.get("RUN_ONCE") == "1"
    if once:
        run_once()
    else:
        try:
            run()
        except KeyboardInterrupt:
            log.info("Зупинено користувачем.")
