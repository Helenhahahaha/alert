import asyncio
import json
import ssl
import time
import requests
import websockets

# ======================
# CONFIG
# ======================
WS_URL = "wss://nodes.pepperstonecrypto.com/ws"
SYMBOLS = ["BTC_AUD", "ETH_AUD", "SOL_AUD", "USDT_AUD", "USDC_AUD"]

TELEGRAM_BOT_TOKEN = "8788440316:AAF6KP0bEqAUrT7ZXvIUKD-H6UdVN8GS8UU"
TELEGRAM_CHAT_ID = "-5286369776"
MENTION_USER_ID = 471062145

ALERT_COOLDOWN_SECONDS = 300

SPREAD_CONFIG = {
    "BTC_AUD": 0.50,
    "ETH_AUD": 0.50,
    "SOL_AUD": 0.50,
    "USDT_AUD": 0.50,
    "USDC_AUD": 0.50,
}

STUCK_SECONDS_CONFIG = {
    "BTC_AUD": 60,
    "ETH_AUD": 60,
    "SOL_AUD": 60,
    "USDT_AUD": 60,
    "USDC_AUD": 60,
}

ssl_context = ssl._create_unverified_context()
state = {}

# ======================
# HELPERS
# ======================

def mention_me():
    return f'<a href="tg://user?id={MENTION_USER_ID}">Helen</a>'

def send_telegram(message: str, mention=False):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    if mention:
        text = f"{mention_me()}\n{message}"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }
    else:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }

    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print("Telegram error:", e)

def compute_best_bid_ask(book):
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None

    best_bid = max(level[0] for level in bids)
    best_ask = min(level[0] for level in asks)
    return float(best_bid), float(best_ask)

def compute_spread_pct(bid, ask):
    if bid == 0:
        return 0.0
    return round(((ask - bid) / bid) * 100, 2)

def can_alert(symbol, alert_type):
    last = state[symbol]["alert_at"].get(alert_type, 0)
    return time.time() - last >= ALERT_COOLDOWN_SECONDS

def mark_alert(symbol, alert_type):
    state[symbol]["alert_at"][alert_type] = time.time()

# ======================
# CORE
# ======================

async def run_once():
    async with websockets.connect(WS_URL, ssl=ssl_context) as ws:

        subscribe_msg = {"method": "subscribe", "events": [f"OB.{s}" for s in SYMBOLS]}
        await ws.send(json.dumps(subscribe_msg))

        print("✅ Connected & Subscribed")

        while True:
            raw = await ws.recv()
            msg = json.loads(raw)

            if msg.get("method") != "stream":
                continue

            event = msg.get("event", "")
            if not event.startswith("OB."):
                continue

            symbol = event.split(".", 1)[1]
            if symbol not in SYMBOLS:
                continue

            book = msg.get("data", {})
            bid, ask = compute_best_bid_ask(book)
            if bid is None or ask is None:
                continue

            if ask <= bid:
                continue

            spread_pct = compute_spread_pct(bid, ask)
            now = time.time()

            if symbol not in state:
                state[symbol] = {
                    "bid": bid,
                    "ask": ask,
                    "bid_changed_at": now,
                    "ask_changed_at": now,
                    "alert_at": {},
                    "bid_stuck_active": False,
                    "ask_stuck_active": False,
                    "spread_active": False,
                }
                continue

            st = state[symbol]

            # Update change time
            if bid != st["bid"]:
                st["bid"] = bid
                st["bid_changed_at"] = now

                if st["bid_stuck_active"]:
                    send_telegram(f"[RECOVERED] {symbol} Bid moving again\nbid={bid}", mention=False)
                    st["bid_stuck_active"] = False

            if ask != st["ask"]:
                st["ask"] = ask
                st["ask_changed_at"] = now

                if st["ask_stuck_active"]:
                    send_telegram(f"[RECOVERED] {symbol} Ask moving again\nask={ask}", mention=False)
                    st["ask_stuck_active"] = False

            stuck_seconds = STUCK_SECONDS_CONFIG.get(symbol, 60)

            bid_stuck_time = now - st["bid_changed_at"]
            ask_stuck_time = now - st["ask_changed_at"]

            # BID_STUCK
            if bid_stuck_time >= stuck_seconds:
                if not st["bid_stuck_active"] and can_alert(symbol, "BID_STUCK"):
                    st["bid_stuck_active"] = True
                    mark_alert(symbol, "BID_STUCK")
                    send_telegram(
                        f"[BID_STUCK]\n{symbol}\nBid unchanged {int(bid_stuck_time)}s\nspread={spread_pct}%",
                        mention=True,
                    )

            # ASK_STUCK
            if ask_stuck_time >= stuck_seconds:
                if not st["ask_stuck_active"] and can_alert(symbol, "ASK_STUCK"):
                    st["ask_stuck_active"] = True
                    mark_alert(symbol, "ASK_STUCK")
                    send_telegram(
                        f"[ASK_STUCK]\n{symbol}\nAsk unchanged {int(ask_stuck_time)}s\nspread={spread_pct}%",
                        mention=True,
                    )

            # SPREAD
            threshold = SPREAD_CONFIG.get(symbol)
            if threshold and spread_pct >= threshold:
                if not st["spread_active"] and can_alert(symbol, "SPREAD"):
                    st["spread_active"] = True
                    mark_alert(symbol, "SPREAD")
                    send_telegram(
                        f"[SPREAD_WIDE]\n{symbol}\nspread={spread_pct}% >= {threshold}%",
                        mention=True,
                    )
            else:
                if st["spread_active"]:
                    send_telegram(
                        f"[RECOVERED] {symbol} Spread normal\nspread={spread_pct}%",
                        mention=False,
                    )
                    st["spread_active"] = False


async def monitor():
    while True:
        try:
            await run_once()
        except Exception as e:
            print("Reconnecting...", e)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(monitor())