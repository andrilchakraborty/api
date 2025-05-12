import os
import sqlite3
import random
import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# ——— Configuration —————————————————————————————————————————————
SERVICE_URL       = "https://api-jt5t.onrender.com"
DEFAULT_CHANNEL   = os.getenv("TWITCH_CHANNEL", "shrimpur")
BOT_NICK          = os.getenv("TWITCH_BOT_NICK", "shrimpur")
BOT_OAUTH         = os.getenv("TWITCH_OAUTH", "oauth:xaz44k12jaiufen1ngyme5bn0lyhca")
REWARD_INTERVAL   = int(os.getenv("REWARD_INTERVAL", 300))
REWARD_AMOUNT     = int(os.getenv("REWARD_AMOUNT", 100))
DB_FILE           = "shrimp.db"

# ——— FastAPI setup ——————————————————————————————————————————————
app = FastAPI()
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ——— Database initialization —————————————————————————————————————
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS users (
        channel     TEXT NOT NULL,
        username    TEXT NOT NULL,
        points      INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(channel, username)
      )
    """)
    conn.execute(f"""
      CREATE TABLE IF NOT EXISTS settings (
        channel        TEXT PRIMARY KEY,
        points_name    TEXT NOT NULL,
        reward_amount  INTEGER NOT NULL DEFAULT {REWARD_AMOUNT}
      )
    """)
    conn.execute("""
      INSERT OR IGNORE INTO settings(channel, points_name, reward_amount)
      VALUES(?, ?, ?)
    """, (DEFAULT_CHANNEL, "shrimp points", REWARD_AMOUNT))
    conn.commit()
    conn.close()

init_db()

# ——— Helpers —————————————————————————————————————————————————————
def get_points_table(user: str, channel: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE channel = ? AND username = ?", (channel, user))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

async def add_user_points(user: str, channel: str, amount: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      INSERT INTO users(channel, username, points)
      VALUES(?, ?, ?)
      ON CONFLICT(channel, username) DO UPDATE
        SET points = points + ?
    """, (channel, user, amount, amount))
    conn.commit()
    conn.close()

def get_points_name(channel: str) -> str:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points_name FROM settings WHERE channel = ?", (channel,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else "points"

async def set_points_name(channel: str, name: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      INSERT INTO settings(channel, points_name, reward_amount)
      VALUES(?, ?, ?)
      ON CONFLICT(channel) DO UPDATE
        SET points_name = excluded.points_name
    """, (channel, name, REWARD_AMOUNT))
    conn.commit()
    conn.close()

def get_reward_amount(channel: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT reward_amount FROM settings WHERE channel = ?", (channel,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else REWARD_AMOUNT

async def set_reward_amount(channel: str, amount: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      UPDATE settings
      SET reward_amount = ?
      WHERE channel = ?
    """, (amount, channel))
    conn.commit()
    conn.close()

# ——— IRC chatter fetcher —————————————————————————————————————
async def fetch_chatters_irc(channel: str) -> set:
    reader, writer = await asyncio.open_connection('irc.chat.twitch.tv', 6667)
    writer.write(f"PASS {BOT_OAUTH}\r\n".encode())
    writer.write(f"NICK {BOT_NICK}\r\n".encode())
    writer.write("CAP REQ :twitch.tv/membership\r\n".encode())
    writer.write(f"JOIN #{channel}\r\n".encode())
    await writer.drain()

    chatters = set()
    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode(errors='ignore').strip()
        if text.startswith("PING"):
            writer.write("PONG :tmi.twitch.tv\r\n".encode())
            await writer.drain()
        elif " 353 " in text:
            parts = text.split(" :", 1)
            if len(parts) == 2:
                for raw in parts[1].split():
                    chatters.add(raw.lstrip("@+%~&"))
        elif " 366 " in text:
            break

    writer.close()
    await writer.wait_closed()
    return chatters

# ——— Background rewards ——————————————————————————————————————
@app.on_event("startup")
async def start_reward_loop():
    async def loop_rewards():
        while True:
            try:
                chan     = DEFAULT_CHANNEL
                name     = get_points_name(chan)
                reward   = get_reward_amount(chan)
                chatters = await fetch_chatters_irc(chan)
                for u in chatters:
                    await add_user_points(u, chan, reward)
                print(f"Rewarded {len(chatters)} users {reward} {name} each in {chan}.")
            except Exception as e:
                print("Reward loop error:", e)
            await asyncio.sleep(REWARD_INTERVAL)
    asyncio.create_task(loop_rewards())

# ——— Keep-alive ping ——————————————————————————————————————————
@app.on_event("startup")
async def schedule_ping():
    async def pinger():
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            while True:
                try:
                    await client.get(f"{SERVICE_URL}/ping")
                except:
                    pass
                await asyncio.sleep(120)
    asyncio.create_task(pinger())

# ——— Serve index.html ——————————————————————————————————————————
@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ——— /setreward ———————————————————————————————————————————————
@app.get("/setreward")
async def setreward(channel: str, amount: int):
    if amount < 0:
        raise HTTPException(400, "Amount must be non-negative")
    await set_reward_amount(channel, amount)
    return PlainTextResponse(f"✅ Reward per interval in '{channel}' set to {amount} points.")

# ——— /setpoints ———————————————————————————————————————————————
@app.get("/setpoints")
async def setpoints(channel: str, name: str):
    if not name.strip():
        raise HTTPException(400, "Must provide non-empty name")
    await set_points_name(channel, name.strip())
    return PlainTextResponse(f"✅ Points currency in '{channel}' set to “{name.strip()}”!")

# ——— /points ————————————————————————————————————————————————
@app.get("/points")
async def points(user: str, channel: str = DEFAULT_CHANNEL):
    pts  = get_points_table(user, channel)
    name = get_points_name(channel)
    return PlainTextResponse(f"{user}, you have {pts} {name} in '{channel}'.")

# ——— /add ————————————————————————————————————————————————
@app.get("/add")
async def add_points(user: str, amount: int, channel: str = DEFAULT_CHANNEL):
    clean_user = user.lstrip("@").strip()
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    await add_user_points(clean_user, channel, amount)
    pts  = get_points_table(clean_user, channel)
    name = get_points_name(channel)
    return PlainTextResponse(f"✅ {clean_user} now has {pts} {name}.")

# ——— /addall —————————————————————————————————————————————
@app.get("/addall")
async def addall(amount: int, channel: str = DEFAULT_CHANNEL):
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    chatters = await fetch_chatters_irc(channel)
    if channel not in chatters:
        chatters.add(channel)
    for u in chatters:
        await add_user_points(u, channel, amount)
    name  = get_points_name(channel)
    count = len(chatters)
    return PlainTextResponse(f"✅ Awarded {amount} {name} to {count} chatters in '{channel}'.")

@app.get("/leaderboard")
async def leaderboard(limit: int = 10, channel: str = DEFAULT_CHANNEL):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT username, points FROM users WHERE channel = ? ORDER BY points DESC LIMIT ?",
        (channel, limit)
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        return PlainTextResponse(f"No points yet in '{channel}'.")

    # simple "user - points" pairs, separated by " | "
    entries = [f"{u} - {p}" for u, p in rows]
    line = " | ".join(entries)

    return PlainTextResponse("🏆 Leaderboard 🏆 " + line)


# ——— /gamble ———————————————————————————————————————————————————
def parse_wager(wager_str: str, current: int) -> int:
    if wager_str.lower() == "all":
        return current
    try:
        return int(wager_str)
    except:
        raise HTTPException(400, "Invalid wager")

@app.get("/gamble")
async def gamble(user: str, wager: str, channel: str = DEFAULT_CHANNEL):
    current = get_points_table(user, channel)
    if current <= 0:
        return PlainTextResponse(f"❌ {user}, you have no {get_points_name(channel)}!")
    amount  = parse_wager(wager, current)
    if amount <= 0:
        raise HTTPException(400, "Wager must be positive")
    if amount > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} {get_points_name(channel)}!")
    await add_user_points(user, channel, -amount)

    multipliers = [1, 5, 10, 20, 50]
    weights     = [20, 50, 15, 10, 5]
    mul         = random.choices(multipliers, weights=weights, k=1)[0]
    payout      = amount * mul
    await add_user_points(user, channel, payout)

    final = get_points_table(user, channel)
    name  = get_points_name(channel)
    sym   = "🎉" if mul > 1 else "😐"
    msg   = (
        f"{sym} {user} gambled {amount} {name} and hit a ×{mul} multiplier!\n"
        f"Payout: {payout} {name}.\n"
        f"Final balance: {final} {name}."
    )
    return PlainTextResponse(msg)

# ——— /slots ————————————————————————————————————————————————————
@app.get("/slots")
async def slots(user: str, wager: str, channel: str = DEFAULT_CHANNEL):
    current = get_points_table(user, channel)
    if current <= 0:
        return PlainTextResponse(f"❌ {user}, you have no {get_points_name(channel)}!")
    amount  = parse_wager(wager, current)
    if amount <= 0:
        raise HTTPException(400, "Wager must be positive")
    if amount > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} {get_points_name(channel)}!")
    await add_user_points(user, channel, -amount)

    symbols = ["🍒","🍋","🔔","🍉","⭐","🍀"]
    reels   = [random.choice(symbols) for _ in range(3)]
    await asyncio.sleep(1)

    multipliers = [0, 1, 2, 5, 10, 20]
    weights     = [50, 20, 15, 10, 4, 1]
    mul         = random.choices(multipliers, weights=weights, k=1)[0]
    payout      = amount * mul

    if payout > 0:
        await add_user_points(user, channel, payout)
        result = (
            "😐 You got your wager back (×1)."
            if mul == 1 else
            f"🎉 You hit a ×{mul} multiplier and won {payout} {get_points_name(channel)}!"
        )
    else:
        result = f"💔 No win this time. You lost your wager of {amount}."

    final = get_points_table(user, channel)
    name  = get_points_name(channel)
    return PlainTextResponse(
        f"🎰 {' | '.join(reels)} 🎰\n"
        f"{result}\n"
        f"Final balance: {final} {name}."
    )

# ——— /blackjack —————————————————————————————————————————————————
@app.get("/blackjack")
async def blackjack(user: str, wager: str, channel: str = DEFAULT_CHANNEL):
    current = get_points_table(user, channel)
    if current <= 0:
        return PlainTextResponse(f"❌ {user}, you have no {get_points_name(channel)}!")
    if wager.lower() == "all":
        wager_amount = current
    else:
        try:
            wager_amount = int(wager)
        except ValueError:
            raise HTTPException(400, "Wager must be a number or 'all'")

    if wager_amount <= 0:
        raise HTTPException(400, "Wager must be positive")
    if wager_amount > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} {get_points_name(channel)}!")

    await add_user_points(user, channel, -wager_amount)

    def draw_card():
        cards = [2,3,4,5,6,7,8,9,10,10,10,10,11]
        return random.choice(cards)

    def best_total(hand):
        total = sum(hand)
        while total > 21 and 11 in hand:
            hand[hand.index(11)] = 1
            total = sum(hand)
        return total

    player = [draw_card(), draw_card()]
    dealer = [draw_card(), draw_card()]
    player_total = best_total(player)
    dealer_total = best_total(dealer)

    while player_total < 17:
        player.append(draw_card())
        player_total = best_total(player)
    while dealer_total < 17:
        dealer.append(draw_card())
        dealer_total = best_total(dealer)

    if player_total > 21:
        result = f"💥 {user} busted with {player_total}!"
    elif dealer_total > 21 or player_total > dealer_total:
        payout = wager_amount * 2
        await add_user_points(user, channel, payout)
        result = f"🎉 {user} wins! {player_total} vs {dealer_total}. Payout: {payout}."
    elif player_total == dealer_total:
        payout = wager_amount
        await add_user_points(user, channel, payout)
        result = f"😐 Push. Both had {player_total}. Wager returned."
    else:
        result = f"💀 Dealer wins. {player_total} vs {dealer_total}."

    name = get_points_name(channel)
    final = get_points_table(user, channel)
    return PlainTextResponse(
        f"🃏 Blackjack 🃏\n"
        f"{user}'s hand: {', '.join(map(str, player))} (Total: {player_total})\n"
        f"Dealer's hand: {', '.join(map(str, dealer))} (Total: {dealer_total})\n"
        f"{result}\n"
        f"Final balance: {final} {name}."
    )

# ——— /raffle & /join & /ping ————————————————————————————————————————
raffle = {"active": False, "amount": 0, "participants": set(), "task": None}

async def raffle_timer(channel: str):
    await asyncio.sleep(30)
    entrants = list(raffle["participants"])
    winners  = random.sample(entrants, k=min(3, len(entrants))) if entrants else []
    split    = raffle["amount"] // max(1, len(winners))
    for w in winners:
        await add_user_points(w, channel, split)

    name = get_points_name(channel)
    if winners:
        announcement = f"🎉 Raffle in #{channel}! Winners: {', '.join(winners)} — each wins {split} {name}! 🎉"
    else:
        announcement = f"😢 Raffle ended with no entrants in #{channel}."

    try:
        r, w = await asyncio.open_connection('irc.chat.twitch.tv', 6667)
        w.write(f"PASS {BOT_OAUTH}\r\n".encode())
        w.write(f"NICK {BOT_NICK}\r\n".encode())
        w.write("CAP REQ :twitch.tv/membership twitch.tv/tags twitch.tv/commands\r\n".encode())
        w.write(f"JOIN #{channel}\r\n".encode())
        await w.drain()
        await asyncio.sleep(1)
        w.write(f"PRIVMSG #{channel} :{announcement}\r\n".encode())
        await w.drain()
        w.close()
        await w.wait_closed()
    except:
        pass

    raffle.update(active=False, task=None)
    raffle["participants"].clear()

@app.get("/raffle")
async def start_raffle(amount: int, channel: str = DEFAULT_CHANNEL):
    if raffle["active"]:
        raise HTTPException(400, "A raffle is already running!")
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    raffle.update(active=True, amount=amount, participants=set(),
                  task=asyncio.create_task(raffle_timer(channel)))
    name = get_points_name(channel)
    return PlainTextResponse(f"🎉 Raffle started in #{channel} for {amount} {name}! Type !join to enter (30s).")

@app.get("/join")
async def join_raffle(user: str):
    if not raffle["active"]:
        raise HTTPException(400, "No raffle is currently running.")
    raffle["participants"].add(user)
    return PlainTextResponse(f"✅ {user} joined the raffle ({len(raffle['participants'])} entrants).")

@app.get("/ping")
async def ping():
    return {"status": "alive"}
