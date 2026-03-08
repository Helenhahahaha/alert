import asyncio
import json
import time
import requests
import websockets
from datetime import datetime, timezone

WS_URL = "wss://nodes.pepperstonecrypto.com/ws"

# Telegram
BOT_TOKEN = "8788440316:AAF6KP0bEqAUrT7ZXvIUKD-H6UdVN8GS8UU"
CHAT_ID = "-5286369776"  # group chat id, e.g. -100xxxxxxxxxx
MENTION = "@Helenbai"

# Time display UTC
DISPLAY_TZ = timezone.utc
DISPLAY_TZ_NAME = "UTC"

# Alert cooldown
ALERT_COOLDOWN_SECONDS = 300

CONFIG = {
    "BTC_AUD":  {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.80},
    "ETH_AUD":  {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.80},
    "SOL_AUD":  {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.80},
    "USDT_AUD": {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.80},
    "USDC_AUD": {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.80},
}

SYMBOLS = list(CONFIG.keys())

def now_ts():
    return time.time()

state = {}
t0 = now_ts()

for sym in SYMBOLS:
    state[sym] = {
        "bid": None,
        "ask": None,
        "bid_changed_at": t0,
        "ask_changed_at": t0,
        "bid_stuck_active": False,
        "ask_stuck_active": False,
        "spread_active": False,
        "spread_wide_since": None,
        "last_alert_at": {},
    }

def fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")

def tg_send(text, mention=False):

    if mention:
        text = f"{MENTION}\n{text}"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text
        }, timeout=8)
    except Exception as e:
        print("Telegram error:", e)

def notify_startup():
    tg_send(
        f"✅ Alert system started\n"
        f"Time: {fmt_ts(time.time())} {DISPLAY_TZ_NAME}\n"
        f"Pairs: {', '.join(SYMBOLS)}",
        mention=False
    )

def can_alert(sym, alert_type):
    last = state[sym]["last_alert_at"].get(alert_type, 0.0)
    return (now_ts() - last) >= ALERT_COOLDOWN_SECONDS

def mark_alert(sym, alert_type):
    state[sym]["last_alert_at"][alert_type] = now_ts()

def compute_best_bid_ask(book):

    bids = book.get("bids", [])
    asks = book.get("asks", [])

    if not bids or not asks:
        return None, None

    try:
        best_bid = max(level[0] for level in bids)
        best_ask = min(level[0] for level in asks)
    except ValueError:
        return None, None

    return float(best_bid), float(best_ask)

def spread_abs_and_pct(bid, ask):

    abs_spread = ask - bid

    if bid == 0:
        pct = 0
    else:
        pct = round((abs_spread / bid) * 100, 2)

    return abs_spread, pct


async def run_once():

    async with websockets.connect(WS_URL) as ws:

        sub = {
            "method": "subscribe",
            "events": [f"OB.{s}" for s in SYMBOLS]
        }

        await ws.send(json.dumps(sub))

        print("WebSocket connected")
        print("Subscribed:", sub)

        while True:

            raw = await ws.recv()
            msg = json.loads(raw)

            if msg.get("method") != "stream":
                continue

            event = msg.get("event", "")

            if not event.startswith("OB."):
                continue

            sym = event.split(".", 1)[1]

            if sym not in state:
                continue

            bid, ask = compute_best_bid_ask(msg.get("data", {}))

            if bid is None or ask is None:
                continue

            if ask <= bid:
                continue

            st = state[sym]
            cfg = CONFIG[sym]

            now = now_ts()

            if st["bid"] is None:

                st["bid"] = bid
                st["ask"] = ask

                st["bid_changed_at"] = now
                st["ask_changed_at"] = now

                continue

            if bid != st["bid"]:

                st["bid"] = bid
                st["bid_changed_at"] = now

                if st["bid_stuck_active"]:

                    tg_send(
                        f"RECOVERED BID MOVING\n"
                        f"{sym}\n"
                        f"bid {bid}\n"
                        f"time {fmt_ts(now)} {DISPLAY_TZ_NAME}"
                    )

                    st["bid_stuck_active"] = False

            if ask != st["ask"]:

                st["ask"] = ask
                st["ask_changed_at"] = now

                if st["ask_stuck_active"]:

                    tg_send(
                        f"RECOVERED ASK MOVING\n"
                        f"{sym}\n"
                        f"ask {ask}\n"
                        f"time {fmt_ts(now)} {DISPLAY_TZ_NAME}"
                    )

                    st["ask_stuck_active"] = False


            abs_spread, pct_spread = spread_abs_and_pct(st["bid"], st["ask"])

            spread_th = cfg["spread_pct_threshold"]
            spread_s = cfg["spread_seconds"]

            if pct_spread >= spread_th:

                if st["spread_wide_since"] is None:
                    st["spread_wide_since"] = now

                wide_for = now - st["spread_wide_since"]

                if wide_for >= spread_s and not st["spread_active"] and can_alert(sym, "spread"):

                    st["spread_active"] = True
                    mark_alert(sym, "spread")

                    tg_send(
                        f"SPREAD TOO WIDE\n"
                        f"{sym}\n"
                        f"bid {st['bid']}\n"
                        f"ask {st['ask']}\n"
                        f"spread {pct_spread}%\n"
                        f"threshold {spread_th}%\n"
                        f"time {fmt_ts(now)} {DISPLAY_TZ_NAME}",
                        mention=True
                    )

            else:

                st["spread_wide_since"] = None

                if st["spread_active"]:

                    st["spread_active"] = False

                    tg_send(
                        f"RECOVERED SPREAD NORMAL\n"
                        f"{sym}\n"
                        f"spread {pct_spread}%\n"
                        f"time {fmt_ts(now)} {DISPLAY_TZ_NAME}"
                    )


            stuck_s = cfg["stuck_seconds"]

            bid_stuck_for = now - st["bid_changed_at"]
            ask_stuck_for = now - st["ask_changed_at"]

            if bid_stuck_for >= stuck_s:

                if not st["bid_stuck_active"] and can_alert(sym, "bid"):

                    st["bid_stuck_active"] = True
                    mark_alert(sym, "bid")

                    tg_send(
                        f"BID STUCK\n"
                        f"{sym}\n"
                        f"bid {st['bid']}\n"
                        f"unchanged {int(bid_stuck_for)}s\n"
                        f"last update {fmt_ts(st['bid_changed_at'])} {DISPLAY_TZ_NAME}",
                        mention=True
                    )

            if ask_stuck_for >= stuck_s:

                if not st["ask_stuck_active"] and can_alert(sym, "ask"):

                    st["ask_stuck_active"] = True
                    mark_alert(sym, "ask")

                    tg_send(
                        f"ASK STUCK\n"
                        f"{sym}\n"
                        f"ask {st['ask']}\n"
                        f"unchanged {int(ask_stuck_for)}s\n"
                        f"last update {fmt_ts(st['ask_changed_at'])} {DISPLAY_TZ_NAME}",
                        mention=True
                    )


async def main():

    started_sent = False

    while True:

        try:

            if not started_sent:

                notify_startup()
                started_sent = True

            await run_once()

        except Exception as e:

            print("WebSocket error:", e)
            print("Reconnect in 5 seconds")

            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
