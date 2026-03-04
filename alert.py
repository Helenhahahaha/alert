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

# Time display: UTC0
DISPLAY_TZ = timezone.utc
DISPLAY_TZ_NAME = "UTC"

# Avoid spamming: same alert type per symbol at most once every X seconds
ALERT_COOLDOWN_SECONDS = 300

# Per-symbol config
# stuck_seconds: 180s (3 minutes)
# spread_seconds: 60s continuous wide before alert
# spread_pct_threshold: percent, bid-based (0.5 means 0.5%)
CONFIG = {
    "BTC_AUD":  {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.50},
    "ETH_AUD":  {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.50},
    "SOL_AUD":  {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.50},
    "USDT_AUD": {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.50},
    "USDC_AUD": {"stuck_seconds": 180, "spread_seconds": 60, "spread_pct_threshold": 0.50},
}
SYMBOLS = list(CONFIG.keys())

def now_ts() -> float:
    return time.time()

# State
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
        "last_alert_at": {},  # type -> ts
    }

def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")

def tg_send(text: str, mention: bool):
    if mention:
        text = f"{MENTION}\n{text}"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=8)
    except Exception as e:
        print("❌ Telegram error:", repr(e))

def can_alert(sym: str, alert_type: str) -> bool:
    last = state[sym]["last_alert_at"].get(alert_type, 0.0)
    return (now_ts() - last) >= ALERT_COOLDOWN_SECONDS

def mark_alert(sym: str, alert_type: str):
    state[sym]["last_alert_at"][alert_type] = now_ts()

def compute_best_bid_ask(book: dict):
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return None, None
    try:
        best_bid = max(level[0] for level in bids if isinstance(level, (list, tuple)) and len(level) >= 2)
        best_ask = min(level[0] for level in asks if isinstance(level, (list, tuple)) and len(level) >= 2)
    except ValueError:
        return None, None
    return float(best_bid), float(best_ask)

def spread_abs_and_pct(bid: float, ask: float):
    abs_spread = ask - bid
    pct = 0.0 if bid == 0 else round((abs_spread / bid) * 100.0, 2)  # bid-based
    return abs_spread, pct

async def run_once():
    async with websockets.connect(WS_URL) as ws:
        sub = {"method": "subscribe", "events": [f"OB.{s}" for s in SYMBOLS]}
        await ws.send(json.dumps(sub))
        print("✅ WebSocket connected")
        print("📡 Subscribed:", sub)

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

            # init
            if st["bid"] is None or st["ask"] is None:
                st["bid"] = bid
                st["ask"] = ask
                st["bid_changed_at"] = now
                st["ask_changed_at"] = now
                continue

            # update bid + recover
            if bid != st["bid"]:
                st["bid"] = bid
                st["bid_changed_at"] = now
                if st["bid_stuck_active"]:
                    tg_send(
                        f"✅ [RECOVERED] BID MOVING AGAIN\n"
                        f"Pair: {sym}\n"
                        f"Bid: {bid}\n"
                        f"Time: {fmt_ts(now)} {DISPLAY_TZ_NAME}",
                        mention=False
                    )
                    st["bid_stuck_active"] = False

            # update ask + recover
            if ask != st["ask"]:
                st["ask"] = ask
                st["ask_changed_at"] = now
                if st["ask_stuck_active"]:
                    tg_send(
                        f"✅ [RECOVERED] ASK MOVING AGAIN\n"
                        f"Pair: {sym}\n"
                        f"Ask: {ask}\n"
                        f"Time: {fmt_ts(now)} {DISPLAY_TZ_NAME}",
                        mention=False
                    )
                    st["ask_stuck_active"] = False

            # SPREAD (continuous wide for 60s)
            abs_spread, pct_spread = spread_abs_and_pct(st["bid"], st["ask"])
            spread_th = cfg["spread_pct_threshold"]
            spread_s = cfg["spread_seconds"]

            if pct_spread >= spread_th:
                if st["spread_wide_since"] is None:
                    st["spread_wide_since"] = now
                wide_for = now - st["spread_wide_since"]

                if (wide_for >= spread_s) and (not st["spread_active"]) and can_alert(sym, "SPREAD_WIDE"):
                    st["spread_active"] = True
                    mark_alert(sym, "SPREAD_WIDE")
                    tg_send(
                        f"⚠️ [SPREAD TOO WIDE]\n"
                        f"Pair: {sym}\n"
                        f"Bid: {st['bid']}\n"
                        f"Ask: {st['ask']}\n"
                        f"Spread(abs): {abs_spread}\n"
                        f"Spread(% bid-based): {pct_spread}%\n"
                        f"Threshold: {spread_th}%\n"
                        f"Wide for: {int(wide_for)}s (need {spread_s}s)\n"
                        f"Time: {fmt_ts(now)} {DISPLAY_TZ_NAME}",
                        mention=True
                    )
            else:
                st["spread_wide_since"] = None
                if st["spread_active"]:
                    st["spread_active"] = False
                    tg_send(
                        f"✅ [RECOVERED] SPREAD NORMAL\n"
                        f"Pair: {sym}\n"
                        f"Bid: {st['bid']}\n"
                        f"Ask: {st['ask']}\n"
                        f"Spread(abs): {abs_spread}\n"
                        f"Spread(% bid-based): {pct_spread}%\n"
                        f"Time: {fmt_ts(now)} {DISPLAY_TZ_NAME}",
                        mention=False
                    )

            # STUCK (180s)
            stuck_s = cfg["stuck_seconds"]
            bid_stuck_for = now - st["bid_changed_at"]
            ask_stuck_for = now - st["ask_changed_at"]

            if bid_stuck_for >= stuck_s:
                if (not st["bid_stuck_active"]) and can_alert(sym, "BID_STUCK"):
                    st["bid_stuck_active"] = True
                    mark_alert(sym, "BID_STUCK")
                    tg_send(
                        f"⚠️ [BID STUCK]\n"
                        f"Pair: {sym}\n"
                        f"Bid: {st['bid']}\n"
                        f"Unchanged: {int(bid_stuck_for)}s (threshold {stuck_s}s)\n"
                        f"Last bid update: {fmt_ts(st['bid_changed_at'])} {DISPLAY_TZ_NAME}",
                        mention=True
                    )

            if ask_stuck_for >= stuck_s:
                if (not st["ask_stuck_active"]) and can_alert(sym, "ASK_STUCK"):
                    st["ask_stuck_active"] = True
                    mark_alert(sym, "ASK_STUCK")
                    tg_send(
                        f"⚠️ [ASK STUCK]\n"
                        f"Pair: {sym}\n"
                        f"Ask: {st['ask']}\n"
                        f"Unchanged: {int(ask_stuck_for)}s (threshold {stuck_s}s)\n"
                        f"Last ask update: {fmt_ts(st['ask_changed_at'])} {DISPLAY_TZ_NAME}",
                        mention=True
                    )

async def main():
    while True:
        try:
            await run_once()
        except Exception as e:
            print("❌ WebSocket error:", repr(e))
            print("🔁 Reconnecting in 5s...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
