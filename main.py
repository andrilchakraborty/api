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
app.mount("/static", StaticFiles(directory="static"), name="static")
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
    conn.execute("""
      CREATE TABLE IF NOT EXISTS settings (
        channel      TEXT PRIMARY KEY,
        points_name  TEXT NOT NULL
      )
    """)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS polls (
        channel     TEXT PRIMARY KEY,
        question    TEXT NOT NULL,
        options     TEXT NOT NULL
      )
    """)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS bets (
        channel     TEXT NOT NULL,
        username    TEXT NOT NULL,
        answer      TEXT NOT NULL,
        amount      INTEGER NOT NULL,
        PRIMARY KEY(channel, username)
      )
    """)
    conn.execute("""
      INSERT OR IGNORE INTO settings(channel, points_name)
      VALUES(?, ?)
    """, (DEFAULT_CHANNEL, "shrimp points"))
    conn.commit()
    conn.close()

init_db()

# ——— Helpers —————————————————————————————————————————————————————
def get_points(user: str, channel: str) -> int:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE channel = ? AND username = ?", (channel, user))
    row = c.fetchone(); conn.close()
    return row[0] if row else 0

async def add_points(user: str, channel: str, amount: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      INSERT INTO users(channel, username, points)
      VALUES(?, ?, ?)
      ON CONFLICT(channel, username) DO UPDATE
        SET points = points + ?
    """, (channel, user, amount, amount))
    conn.commit(); conn.close()

def get_currency(channel: str) -> str:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT points_name FROM settings WHERE channel = ?", (channel,))
    row = c.fetchone(); conn.close()
    return row[0] if row else "points"

async def set_currency(channel: str, name: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      INSERT INTO settings(channel, points_name)
      VALUES(?, ?)
      ON CONFLICT(channel) DO UPDATE
        SET points_name = excluded.points_name
    """, (channel, name))
    conn.commit(); conn.close()

def parse_wager(wager_str: str, current: int) -> int:
    if wager_str.lower() == "all":
        return current
    try:
        return int(wager_str)
    except:
        raise HTTPException(400, "Invalid wager")

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
            writer.write("PONG :tmi.twitch.tv\r\n".encode()); await writer.drain()
        elif " 353 " in text:
            parts = text.split(" :", 1)
            if len(parts) == 2:
                for raw in parts[1].split():
                    chatters.add(raw.lstrip("@+%~&"))
        elif " 366 " in text:
            break

    writer.close(); await writer.wait_closed()
    return chatters

# ——— Background rewards ——————————————————————————————————————
@app.on_event("startup")
async def start_reward_loop():
    async def loop_rewards():
        while True:
            try:
                chan = DEFAULT_CHANNEL
                name = get_currency(chan)
                chatters = await fetch_chatters_irc(chan)
                for u in chatters:
                    await add_points(u, chan, REWARD_AMOUNT)
                print(f"Rewarded {len(chatters)} users {REWARD_AMOUNT} {name} in {chan}.")
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
                try: await client.get(f"{SERVICE_URL}/ping")
                except: pass
                await asyncio.sleep(120)
    asyncio.create_task(pinger())

# ——— Serve index.html ——————————————————————————————————————————
@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# ——— /setpoints ———————————————————————————————————————————————
@app.get("/setpoints")
async def setpoints(channel: str, name: str):
    if not name.strip():
        raise HTTPException(400, "Must provide non-empty name")
    await set_currency(channel, name.strip())
    return PlainTextResponse(f"✅ Currency in '{channel}' set to “{name.strip()}”!")

# ——— /points ————————————————————————————————————————————————
@app.get("/points")
async def points(user: str, channel: str = DEFAULT_CHANNEL):
    pts = get_points(user, channel)
    cur = get_currency(channel)
    return PlainTextResponse(f"{user}, you have {pts} {cur} in '{channel}'.")

# ——— /add ————————————————————————————————————————————————
@app.get("/add")
async def add(user: str, amount: int, channel: str = DEFAULT_CHANNEL):
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    await add_points(user, channel, amount)
    pts = get_points(user, channel)
    cur = get_currency(channel)
    return PlainTextResponse(f"✅ {user} now has {pts} {cur}.")

# ——— /addall —————————————————————————————————————————————
@app.get("/addall/{amount}")
async def addall(amount: int, channel: str = DEFAULT_CHANNEL):
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    chatters = await fetch_chatters_irc(channel)
    for u in chatters:
        await add_points(u, channel, amount)
    cur = get_currency(channel)
    return PlainTextResponse(f"✅ Awarded {amount} {cur} to {len(chatters)} chatters in '{channel}'.")

# ——— /leaderboard —————————————————————————————————————————————
@app.get("/leaderboard")
async def leaderboard(limit: int = 10, channel: str = DEFAULT_CHANNEL):
    conn = sqlite3.connect(DB_FILE); c = conn.cursor()
    c.execute("SELECT username, points FROM users WHERE channel = ? ORDER BY points DESC LIMIT ?", (channel, limit))
    rows = c.fetchall(); conn.close()
    if not rows:
        return PlainTextResponse(f"No points yet in '{channel}'.")
    lines = [f"{u} - {p}" for u, p in rows]
    return PlainTextResponse("🏆 Leaderboard 🏆\n" + "\n".join(lines))

# ——— /gamble ———————————————————————————————————————————————————
@app.get("/gamble")
async def gamble(user: str, wager: str, channel: str = DEFAULT_CHANNEL):
    current = get_points(user, channel)
    amount  = parse_wager(wager, current)
    if amount <= 0 or amount > current:
        raise HTTPException(400, "Invalid wager amount")
    await add_points(user, channel, -amount)

    multipliers = [1,5,10,20,50]
    weights     = [20,50,15,10,5]
    mul = random.choices(multipliers, weights=weights, k=1)[0]
    payout = amount * mul
    await add_points(user, channel, payout)

    final = get_points(user, channel)
    cur   = get_currency(channel)
    sym   = "🎉" if mul>1 else "😐"
    msg = (f"{sym} {user} gambled {amount} and hit ×{mul}! "
           f"Payout: {payout} {cur}. Final balance: {final} {cur}.")
    return PlainTextResponse(msg)

# ——— /slots ————————————————————————————————————————————————————
@app.get("/slots")
async def slots(user: str, wager: str, channel: str = DEFAULT_CHANNEL):
    current = get_points(user, channel)
    amount  = parse_wager(wager, current)
    if amount <= 0 or amount > current:
        raise HTTPException(400, "Invalid wager amount")
    await add_points(user, channel, -amount)

    symbols = ["🍒","🍋","🔔","🍉","⭐","🍀"]
    reels   = [random.choice(symbols) for _ in range(3)]
    await asyncio.sleep(1)

    multipliers = [0,1,2,5,10,20]
    weights     = [50,20,15,10,4,1]
    mul = random.choices(multipliers, weights=weights, k=1)[0]
    payout = amount * mul

    if payout>0:
        await add_points(user, channel, payout)
        result = f"🎉 You hit ×{mul} and won {payout}!"
    else:
        result = f"💔 Lost your wager of {amount}."

    final = get_points(user, channel)
    cur   = get_currency(channel)
    return PlainTextResponse(f"🎰 {' | '.join(reels)} 🎰\n{result} Final balance: {final} {cur}.")

# ——— /blackjack —————————————————————————————————————————————————
@app.get("/blackjack")
async def blackjack(user: str, wager: str, channel: str = DEFAULT_CHANNEL):
    current = get_points(user, channel)
    wager_amount = parse_wager(wager, current)
    if wager_amount <= 0 or wager_amount > current:
        raise HTTPException(400, "Invalid wager amount")
    await add_points(user, channel, -wager_amount)

    def draw():
        cards = [2,3,4,5,6,7,8,9,10,10,10,10,11]
        return random.choice(cards)
    def total(hand):
        t = sum(hand)
        while t>21 and 11 in hand:
            hand[hand].remove(11); hand.append(1)
            t=sum(hand)
        return t

    player = [draw(), draw()]; dealer = [draw(), draw()]
    pt = total(player); dt = total(dealer)
    while pt<17:
        player.append(draw()); pt=total(player)
    while dt<17:
        dealer.append(draw()); dt=total(dealer)

    if pt>21:
        res = f"💥 Busted with {pt}!"
    elif dt>21 or pt>dt:
        await add_points(user, channel, wager_amount*2)
        res = f"🎉 You win! {pt} vs {dt}. Payout: {wager_amount*2}."
    elif pt==dt:
        await add_points(user, channel, wager_amount)
        res = f"😐 Push. {pt} vs {dt}. Wager returned."
    else:
        res = f"💀 Dealer wins. {pt} vs {dt}."

    final = get_points(user, channel); cur = get_currency(channel)
    return PlainTextResponse(f"🃏 Blackjack 🃏\nYour hand: {player} ({pt})\nDealer: {dealer} ({dt})\n{res}\nFinal balance: {final} {cur}.")

# ——— /raffle & /join ————————————————————————————————————————
raffle = {"active": False, "amount": 0, "participants": set(), "task": None}

async def raffle_timer(channel: str):
    await asyncio.sleep(30)
    entrants = list(raffle["participants"])
    winners  = random.sample(entrants, min(3,len(entrants))) if entrants else []
    split    = raffle["amount"]//max(1,len(winners))
    for w in winners:
        await add_points(w, channel, split)
    raffle.update(active=False, task=None); raffle["participants"].clear()

@app.get("/raffle")
async def start_raffle(amount: int, channel: str = DEFAULT_CHANNEL):
    if raffle["active"]:
        raise HTTPException(400,"Raffle already running")
    raffle.update(active=True, amount=amount, participants=set(),
                  task=asyncio.create_task(raffle_timer(channel)))
    cur = get_currency(channel)
    return PlainTextResponse(f"🎉 Raffle for {amount} {cur} started in {channel}! Type !join.")

@app.get("/join")
async def join_raffle(user: str):
    if not raffle["active"]:
        raise HTTPException(400,"No raffle active")
    raffle["participants"].add(user)
    return PlainTextResponse(f"✅ {user} joined raffle ({len(raffle['participants'])})")

@app.get("/ping")
async def ping():
    return {"status":"alive"}

# ——— /poll ———————————————————————————————————————————————————
@app.get("/poll")
async def poll(channel: str, raw: str):
    parts = [p.strip() for p in raw.split("|")]
    if len(parts)<3:
        raise HTTPException(400,"Need question|opt1|opt2")
    q, opts = parts[0], ",".join(parts[1:])
    conn = sqlite3.connect(DB_FILE); c=conn.cursor()
    c.execute("DELETE FROM bets WHERE channel=?", (channel,))
    c.execute("""
      INSERT INTO polls(channel,question,options)
      VALUES(?,?,?)
      ON CONFLICT(channel) DO UPDATE
        SET question=excluded.question,options=excluded.options
    """,(channel,q,opts))
    conn.commit(); conn.close()
    return PlainTextResponse(f"✅ Poll in {channel}: {q} — opts: {', '.join(parts[1:])}")

# ——— /bet ———————————————————————————————————————————————————
@app.get("/bet")
async def bet(user: str, channel: str, answer: str, amount: int):
    current = get_points(user, channel)
    if amount<=0 or amount>current:
        raise HTTPException(400,"Invalid bet")
    conn=sqlite3.connect(DB_FILE); c=conn.cursor()
    c.execute("SELECT options FROM polls WHERE channel=?", (channel,))
    row=c.fetchone()
    if not row or answer not in row[0].split(","):
        conn.close(); raise HTTPException(400,"Invalid or no poll")
    await add_points(user, channel, -amount)
    c.execute("REPLACE INTO bets(channel,username,answer,amount) VALUES(?,?,?,?)",
              (channel,user,answer,amount))
    conn.commit(); conn.close()
    cur=get_currency(channel)
    return PlainTextResponse(f"✅ {user} bet {amount} {cur} on '{answer}'")

# ——— /payup —————————————————————————————————————————————————
@app.get("/payup")
async def payup(channel: str, answer: str):
    conn=sqlite3.connect(DB_FILE); c=conn.cursor()
    c.execute("SELECT username,amount FROM bets WHERE channel=? AND answer=?", (channel,answer))
    winners=c.fetchall()
    if not winners:
        conn.close(); return PlainTextResponse(f"No winners for '{answer}'")
    cur=get_currency(channel)
    for u,amt in winners:
        asyncio.create_task(add_points(u, channel, amt*2))
    c.execute("DELETE FROM bets WHERE channel=?", (channel,))
    c.execute("DELETE FROM polls WHERE channel=?", (channel,))
    conn.commit(); conn.close()
    names=", ".join(u for u,_ in winners)
    return PlainTextResponse(f"🎉 Payup: {names} each won double their bet ({cur})")
