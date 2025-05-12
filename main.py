import os
import sqlite3
import random
import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# ——— Configuration —————————————————————————————————————————————
SERVICE_URL       = "https://api-jt5t.onrender.com"  # keep-alive ping URL
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
    # users table
    conn.execute("""
      CREATE TABLE IF NOT EXISTS users (
        channel     TEXT NOT NULL,
        username    TEXT NOT NULL,
        points      INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(channel, username)
      )
    """)
    # settings table for per-channel point names
    conn.execute("""
      CREATE TABLE IF NOT EXISTS settings (
        channel      TEXT PRIMARY KEY,
        points_name  TEXT NOT NULL
      )
    """)
    # ensure default channel has default name
    conn.execute("""
      INSERT OR IGNORE INTO settings(channel, points_name)
      VALUES(?, ?)
    """, (DEFAULT_CHANNEL, "shrimp points"))
    conn.commit()
    conn.close()

init_db()

# ——— Helpers —————————————————————————————————————————————————————
def get_points_table(user: str, channel: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      SELECT points FROM users
      WHERE channel = ? AND username = ?
    """, (channel, user))
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
      INSERT INTO settings(channel, points_name)
      VALUES(?, ?)
      ON CONFLICT(channel) DO UPDATE
        SET points_name = excluded.points_name
    """, (channel, name))
    conn.commit()
    conn.close()

# ——— IRC-based chatter fetcher (authenticated!) —————————————————————
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

# ——— Background reward loop ——————————————————————————————————————
@app.on_event("startup")
async def start_reward_loop():
    async def loop_rewards():
        while True:
            try:
                chan = DEFAULT_CHANNEL
                name = get_points_name(chan)
                chatters = await fetch_chatters_irc(chan)
                for user in chatters:
                    await add_user_points(user, chan, REWARD_AMOUNT)
                print(f"Rewarded {len(chatters)} users {REWARD_AMOUNT} {name} each in {chan}.")
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

# ——— /setpoints ———————————————————————————————————————————————
@app.get("/setpoints")
async def setpoints(channel: str = DEFAULT_CHANNEL, name: str = None):
    if not name or not name.strip():
        raise HTTPException(400, "Must provide non-empty name")
    await set_points_name(channel, name.strip())
    return PlainTextResponse(f"✅ Points currency in '{channel}' set to “{name.strip()}”!")

# ——— /points endpoint —————————————————————————————————————————————
@app.get("/points")
async def points(user: str, channel: str = DEFAULT_CHANNEL):
    pts = get_points_table(user, channel)
    name = get_points_name(channel)
    return PlainTextResponse(f"{user}, you have {pts} {name} in '{channel}'.")

# ——— /add endpoint ————————————————————————————————————————————————
@app.get("/add")
async def add_points(user: str, amount: int, channel: str = DEFAULT_CHANNEL):
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    await add_user_points(user, channel, amount)
    pts  = get_points_table(user, channel)
    name = get_points_name(channel)
    return PlainTextResponse(f"✅ {user} now has {pts} {name}.")

# ——— /addall endpoint —————————————————————————————————————————————
@app.get("/addall")
async def addall(amount: int = REWARD_AMOUNT, channel: str = DEFAULT_CHANNEL):
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    chatters = await fetch_chatters_irc(channel)
    for u in chatters:
        await add_user_points(u, channel, amount)
    name = get_points_name(channel)
    return PlainTextResponse(
      f"✅ Awarded {amount} {name} to {len(chatters)} chatters in '{channel}'."
    )

# ——— /leaderboard ———————————————————————————————————————————————
@app.get("/leaderboard")
async def leaderboard(limit: int = 10, channel: str = DEFAULT_CHANNEL):
    conn = sqlite3.connect(DB_FILE)
    c    = conn.cursor()
    c.execute("""
      SELECT username, points
      FROM users
      WHERE channel = ?
      ORDER BY points DESC
      LIMIT ?
    """, (channel, limit))
    rows = c.fetchall()
    conn.close()

    name = get_points_name(channel)
    if not rows:
        return PlainTextResponse(f"No {name} yet in '{channel}'.")
    lines = [f"{u} — {p} {name}" for u,p in rows]
    return PlainTextResponse("🏆 Leaderboard 🏆\n" + "\n".join(lines))

# ——— /gamble ———————————————————————————————————————————————————
@app.get("/gamble")
async def gamble(user: str, wager: int, channel: str = DEFAULT_CHANNEL):
    if wager <= 0:
        raise HTTPException(400, "Wager must be positive")
    current = get_points_table(user, channel)
    if wager > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} {get_points_name(channel)}!")
    await add_user_points(user, channel, -wager)

    multipliers = [1, 5, 10, 20, 50]
    weights     = [20, 50, 15, 10, 5]
    mul         = random.choices(multipliers, weights=weights, k=1)[0]
    payout      = wager * mul
    await add_user_points(user, channel, payout)

    final = get_points_table(user, channel)
    name  = get_points_name(channel)
    sym   = "🎉" if mul > 1 else "😐"
    msg   = (
        f"{sym} {user} gambled {wager} {name} and hit a ×{mul} multiplier!\n"
        f"Payout: {payout} {name}.\n"
        f"Final balance: {final} {name}."
    )
    return PlainTextResponse(msg)

# ——— /slots ————————————————————————————————————————————————————
@app.get("/slots")
async def slots(user: str, wager: int, channel: str = DEFAULT_CHANNEL):
    if wager <= 0:
        raise HTTPException(400, "Wager must be positive")
    current = get_points_table(user, channel)
    if wager > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} {get_points_name(channel)}!")
    await add_user_points(user, channel, -wager)

    symbols = ["🍒","🍋","🔔","🍉","⭐","🍀"]
    reels = [random.choice(symbols) for _ in range(3)]
    await asyncio.sleep(1)

    multipliers = [0, 1, 2, 5, 10, 20]
    weights     = [50, 20, 15, 10, 4, 1]
    mul         = random.choices(multipliers, weights=weights, k=1)[0]
    payout      = wager * mul

    if payout > 0:
        await add_user_points(user, channel, payout)
        if mul == 1:
            result = f"😐 You got your wager back (×1)."
        else:
            result = f"🎉 You hit a ×{mul} multiplier and won {payout} {get_points_name(channel)}!"
    else:
        result = f"💔 No win this time. You lost your wager of {wager}."

    final = get_points_table(user, channel)
    name  = get_points_name(channel)
    return PlainTextResponse(
        f"🎰 {' | '.join(reels)} 🎰\n"
        f"{result}\n"
        f"Final balance: {final} {name}."
    )

# ——— /raffle and /ping ——————————————————————————————————————————

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
        announcement = (
          "🎉 Raffle ended in #" + channel + "! Winners: " +
          ", ".join(winners) +
          f" — each wins {split} {name}! 🎉"
        )
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
    return PlainTextResponse(
      f"🎉 Raffle started in #{channel} for {amount} {name}! Type !join to enter (30s)."
    )

@app.get("/join")
async def join_raffle(user: str):
    if not raffle["active"]:
        raise HTTPException(400, "No raffle is currently running.")
    raffle["participants"].add(user)
    return PlainTextResponse(f"✅ {user} joined the raffle ({len(raffle['participants'])} entrants).")

@app.get("/ping")
async def ping():
    return {"status": "alive"}
