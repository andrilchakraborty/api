import os
import sqlite3
import random
import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# ——— Configuration —————————————————————————————————————————————
SERVICE_URL       = "https://api-jt5t.onrender.com"  # keep-alive ping URL
CHANNEL           = os.getenv("TWITCH_CHANNEL", "shrimpur")
BOT_NICK          = os.getenv("TWITCH_BOT_NICK")      # e.g. "mybotname"
BOT_OAUTH         = os.getenv("TWITCH_OAUTH_TOKEN")   # e.g. "oauth:abcd1234"
REWARD_INTERVAL   = int(os.getenv("REWARD_INTERVAL", 300))
REWARD_AMOUNT     = int(os.getenv("REWARD_AMOUNT", 100))
DB_FILE           = "shrimp.db"

# ——— FastAPI setup ——————————————————————————————————————————————
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ——— Database helpers —————————————————————————————————————————————
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS users (
      username TEXT PRIMARY KEY,
      points   INTEGER NOT NULL DEFAULT 0
    )
    """)
    conn.commit()
    conn.close()

def get_points(user: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE username = ?", (user,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

async def add_user_points(user: str, amount: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      INSERT INTO users(username, points) VALUES (?, ?)
      ON CONFLICT(username) DO UPDATE SET points = points + ?
    """, (user, amount, amount))
    conn.commit()
    conn.close()

init_db()

# ——— IRC-based chatter fetcher ————————————————————————————————————
async def fetch_chatters_irc() -> set:
    reader, writer = await asyncio.open_connection('irc.chat.twitch.tv', 6667)
    nick = f'justinfan{random.randint(1000,9999)}'
    # request membership capability *before* JOIN
    writer.write(f"NICK {nick}\r\n".encode())
    writer.write("CAP REQ :twitch.tv/membership\r\n".encode())
    writer.write(f"JOIN #{CHANNEL}\r\n".encode())
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
            # NAMES list
            parts = text.split(" :", 1)
            if len(parts) == 2:
                for raw in parts[1].split():
                    chatters.add(raw.lstrip("@+%~&"))
        elif " 366 " in text:
            # end of NAMES list
            break

    writer.close()
    await writer.wait_closed()
    return chatters

# ——— Background reward loop ——————————————————————————————————————
@app.on_event("startup")
def start_reward_loop():
    async def loop_rewards():
        while True:
            try:
                chatters = await fetch_chatters_irc()
                for user in chatters:
                    await add_user_points(user, REWARD_AMOUNT)
                print(f"Rewarded {len(chatters)} users {REWARD_AMOUNT} points each.")
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

# ——— Raffle state & endpoints ————————————————————————————————————
raffle = {"active": False, "amount": 0, "participants": set(), "task": None}

async def raffle_timer():
    await asyncio.sleep(30)
    entrants = list(raffle["participants"])
    winners  = random.sample(entrants, k=min(3, len(entrants))) if entrants else []
    split    = raffle["amount"] // max(1, len(winners))

    for w in winners:
        await add_user_points(w, split)

    if winners:
        announcement = (
          "🎉 Raffle ended! Winners: " +
          ", ".join(winners) +
          f" — each wins {split} shrimp points! 🎉"
        )
    else:
        announcement = "😢 Raffle ended with no entrants."

    # announce via authenticated IRC
    try:
        r, w = await asyncio.open_connection('irc.chat.twitch.tv', 6667)
        w.write(f"PASS {BOT_OAUTH}\r\n".encode())
        w.write(f"NICK {BOT_NICK}\r\n".encode())
        w.write("CAP REQ :twitch.tv/membership twitch.tv/tags twitch.tv/commands\r\n".encode())
        w.write(f"JOIN #{CHANNEL}\r\n".encode())
        await w.drain()
        # give the server a moment to register our JOIN
        await asyncio.sleep(1)
        w.write(f"PRIVMSG #{CHANNEL} :{announcement}\r\n".encode())
        await w.drain()
        w.close()
        await w.wait_closed()
    except Exception as e:
        print("Failed to announce raffle results:", e)

    raffle.update(active=False, task=None)
    raffle["participants"].clear()
    print("Raffle over:", announcement)

@app.get("/raffle")
async def start_raffle(amount: int):
    if raffle["active"]:
        raise HTTPException(400, "A raffle is already running!")
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    raffle.update(
      active=True, amount=amount,
      participants=set(),
      task=asyncio.create_task(raffle_timer())
    )
    return PlainTextResponse(
      f"🎉 Raffle started for {amount} shrimp points! " +
      "Type !join to enter (30s)."
    )

@app.get("/join")
async def join_raffle(user: str):
    if not raffle["active"]:
        raise HTTPException(400, "No raffle is currently running.")
    raffle["participants"].add(user)
    return PlainTextResponse(
      f"✅ {user} joined the raffle ({len(raffle['participants'])} entrants)."
    )

# ——— Points & admin endpoints —————————————————————————————————————
@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/add")
async def add_points(user: str, amount: int):
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    await add_user_points(user, amount)
    return PlainTextResponse(f"✅ {user} now has {get_points(user)} shrimp points!")

@app.get("/addall")
async def addall(amount: int = REWARD_AMOUNT):
    chatters = await fetch_chatters_irc()
    for u in chatters:
        await add_user_points(u, amount)
    return PlainTextResponse(
      f"✅ Awarded {amount} shrimp points to {len(chatters)} chatters."
    )

@app.get("/points")
async def points(user: str):
    return PlainTextResponse(f"{user}, you have {get_points(user)} shrimp points.")

# ——— Games —————————————————————————————————————————————————————
@app.get("/gamble")
async def gamble(user: str, wager: int):
    if wager <= 0:
        raise HTTPException(400, "Wager must be positive")
    current = get_points(user)
    if wager > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} shrimp points!")
    await add_user_points(user, -wager)
    choice = random.choice(["coinflip", "dice", "roulette"])
    anim   = {"coinflip":"🪙 Flipping...","dice":"🎲 Rolling...","roulette":"🎡 Spinning..."}[choice]
    await asyncio.sleep(1)

    if choice == "coinflip":
        win, detail = random.choice([(True,"Heads"),(False,"Tails")])
    elif choice == "dice":
        roll = random.randint(1,6)
        win = roll >= 4
        detail = f"Rolled {roll}"
    else:
        spin = random.randint(0,36)
        win = (spin != 0 and spin % 2 == 0)
        detail = f"Landed on {spin}"

    payout = wager * 2 if win else 0
    if payout:
        await add_user_points(user, payout)
    final = get_points(user)
    sym = "🎉" if win else "💔"
    res = "won" if win else "lost"

    return PlainTextResponse(
      f"{anim}\n{sym} {user} {res} {wager} on {choice.upper()} ({detail})!\n" +
      f"Final balance: {final} shrimp points."
    )

@app.get("/slots")
async def slots(user: str, wager: int):
    if wager <= 0:
        raise HTTPException(400, "Wager must be positive")
    current = get_points(user)
    if wager > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} shrimp points!")
    await add_user_points(user, -wager)

    symbols = ["🍒","🍋","🔔","🍉","⭐","🍀"]
    reels = [random.choice(symbols) for _ in range(3)]
    await asyncio.sleep(1)

    if len(set(reels)) == 1:
        payout = wager * 5
        await add_user_points(user, payout)
        result = f"💰 Jackpot! You won {payout}!"
    elif len(set(reels)) == 2:
        payout = wager * 2
        await add_user_points(user, payout)
        result = f"😊 You matched two! You won {payout}!"
    else:
        result = f"💔 No match. You lost {wager}."
    final = get_points(user)

    return PlainTextResponse(
      f"🎰 {' | '.join(reels)} 🎰\n{result}\n" +
      f"Final balance: {final} shrimp points."
    )

@app.get("/leaderboard")
async def leaderboard(limit: int = 10):
    conn = sqlite3.connect(DB_FILE)
    c    = conn.cursor()
    c.execute("SELECT username, points FROM users ORDER BY points DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return PlainTextResponse("No shrimp points yet.")
    lines = [f"{i+1}. {u} — {p} shrimp" for i,(u,p) in enumerate(rows)]
    return PlainTextResponse("🏆 Shrimp Leaderboard 🏆\n" + "\n".join(lines))

@app.get("/ping")
async def ping():
    return {"status": "alive"}
