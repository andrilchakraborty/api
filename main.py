import os
import json
import random
import asyncio
import time
import sqlite3

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# ——— Configuration —————————————————————————————————————————————
SERVICE_URL       = "https://api-jt5t.onrender.com"
DEFAULT_CHANNEL   = os.getenv("TWITCH_CHANNEL", "shrimpur")
BOT_NICK          = os.getenv("TWITCH_BOT_NICK", "shrimpur")
BOT_OAUTH         = os.getenv("TWITCH_OAUTH", "oauth:your_oauth_token_here")
REWARD_INTERVAL   = int(os.getenv("REWARD_INTERVAL", 300))
REWARD_AMOUNT     = int(os.getenv("REWARD_AMOUNT", 100))
DB_FILE           = "shrimp.db"
ROB_COOLDOWN      = 300  # seconds before you can rob same victim again

# ——— CSGO CRATE CONFIGURATION —————————————————————————————————————
# 50 real CSGO skins sourced from various cases (abbreviated list)
CRATE_CONFIG = {
    "Panther Case": {
        "price": 500,
        "rarities": {
            "Mil-Spec (Blue)": [
                "MP7 | Armor Core", "Desert Eagle | Directive", "P250 | Splash", "SSG 08 | Blue Spruce",
                "FAMAS | Valence", "Glock-18 | Bunsen Burner", "P90 | Cold Blooded", "Nova | Redux",
                "XM1014 | Tranquility", "MAG-7 | Heat"  # 10
            ],
            "Restricted (Purple)": [
                "AK-47 | Redline", "AWP | Man-o'-war", "M4A4 | Griffin", "USP-S | Kill Confirmed",
                "Galil AR | Cerberus", "AUG | Syd Mead", "MAC-10 | Neon Rider", "MP9 | Bulldozer",
                "Nova | Wild Six", "SG 553 | Damascus Steel"  # 10
            ],
            "Classified (Pink)": [
                "M4A1-S | Cyrex", "AK-47 | Cartel", "AWP | Capillary", "Desert Eagle | Blaze",
                "P250 | Muertos", "Five-SeveN | Hyper Beast", "SSG 08 | Dragonfire", "FIVE-SEVEN | Triumvirate",
                "G3SG1 | Orange Crash", "Tec-9 | Fuel Injector"  # 10
            ],
            "Covert (Red)": [
                "AWP | Asiimov", "AK-47 | Fire Serpent", "M4A1-S | Guardian", "USP-S | Neo-Noir",
                "Desert Eagle | Code Red", "Glock-18 | Fade", "P2000 | Imperial Dragon", "SSG 08 | Blood in the Water",
                "MAG-7 | Monster Call", "MP7 | Nemesis"  # 10
            ],
            "Exceedingly Rare (Gold)": [
                "★ Karambit | Doppler", "★ M9 Bayonet | Marble Fade", "★ Butterfly Knife | Slaughter",
                "★ Bayonet | Crimson Web", "★ Huntsman Knife | Fade", "★ Flip Knife | Tiger Tooth",
                "★ Gut Knife | Lore", "★ Shadow Daggers | Damascus Steel", "★ Bowie Knife | Damascus Steel",
                "★ Talon Knife | Rust Coat"  # 10
            ]
        },
        "weights": [0.79, 0.16, 0.03, 0.009, 0.001]
    }
}

# ——— FastAPI setup —————————————————————————————————————————————
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ——— Database Initialization —————————————————————————————————————
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS users (
        channel TEXT NOT NULL,
        username TEXT NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(channel, username)
      )
    """)
    # settings table with literal default value
    conn.execute(f"""
      CREATE TABLE IF NOT EXISTS settings (
        channel TEXT PRIMARY KEY,
        points_name TEXT NOT NULL,
        reward_amount INTEGER NOT NULL DEFAULT {REWARD_AMOUNT}
      )
    """)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS rob_cooldowns (
        channel TEXT NOT NULL,
        robber TEXT NOT NULL,
        victim TEXT NOT NULL,
        last_rob INTEGER NOT NULL,
        PRIMARY KEY(channel, robber, victim)
      )
    """)
    conn.execute("""
      CREATE TABLE IF NOT EXISTS inventory (
        channel TEXT NOT NULL,
        username TEXT NOT NULL,
        skin TEXT NOT NULL,
        obtained_at INTEGER NOT NULL,
        PRIMARY KEY(channel, username, skin, obtained_at)
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

def can_rob(channel: str, robber: str, victim: str) -> (bool, int):
    """Returns (True, 0) if allowed, or (False, secs_remaining)."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      SELECT last_rob FROM rob_cooldowns
      WHERE channel=? AND robber=? AND victim=?
    """, (channel, robber, victim))
    row = c.fetchone()
    now = int(time.time())
    if row:
        last = row[0]
        if now - last < ROB_COOLDOWN:
            return False, ROB_COOLDOWN - (now - last)
    return True, 0

async def update_rob_timestamp(channel: str, robber: str, victim: str):
    now = int(time.time())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
      INSERT INTO rob_cooldowns(channel, robber, victim, last_rob)
      VALUES(?, ?, ?, ?)
      ON CONFLICT(channel, robber, victim) DO UPDATE
        SET last_rob = excluded.last_rob
    """, (channel, robber, victim, now))
    conn.commit()
    conn.close()
async def add_inventory(user: str, channel: str, skin: str):
    now = int(time.time())
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
      INSERT INTO inventory(channel, username, skin, obtained_at)
      VALUES(?, ?, ?, ?)
    """, (channel, user, skin, now))
    conn.commit()
    conn.close()

def get_inventory(user: str, channel: str) -> list:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT skin, obtained_at FROM inventory WHERE channel=? AND username=? ORDER BY obtained_at DESC", (channel, user))
    rows = c.fetchall()
    conn.close()
    return rows

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
@app.get("/crate")
async def api_crates(channel: str = DEFAULT_CHANNEL):
    return PlainTextResponse(json.dumps({k:{'price':v['price']} for k,v in CRATE_CONFIG.items()}))

@app.get("/open_crate")
async def api_open(user: str, crate: str, channel: str = DEFAULT_CHANNEL):
    if crate not in CRATE_CONFIG: raise HTTPException(400)
    price=CRATE_CONFIG[crate]['price']
    if get_points(user.lstrip('@'),channel)<price:
        return PlainTextResponse(f"Need {price} points.")
    await add_points(user.lstrip('@'),channel,-price)
    rar=list(CRATE_CONFIG[crate]['rarities'].keys())
    weights=CRATE_CONFIG[crate]['weights']
    r=random.choices(rar,weights=weights,k=1)[0]
    skin=random.choice(CRATE_CONFIG[crate]['rarities'][r])
    await add_inventory(user.lstrip('@'),channel,skin)
    return PlainTextResponse(f"Opened {crate}: {r} - {skin}")

@app.get("/inventory")
async def api_inventory(user: str, channel: str = DEFAULT_CHANNEL):
    inv=get_inventory(user.lstrip('@'),channel)
    if not inv: return PlainTextResponse(f"{user} has empty inventory.")
    lines=[f"{skin} @ {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}" for skin,ts in inv]
    return PlainTextResponse("\n".join(lines))

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

# ——— /rob ————————————————————————————————————————————————————
@app.get("/rob")
async def rob(robber: str, victim: str, channel: str = DEFAULT_CHANNEL):
    """
    Attempt to rob another user.
    /rob?robber=you&victim=them&channel=foo
    """
    # cleanup usernames
    r = robber.lstrip("@").strip()
    v = victim.lstrip("@").strip()
    name = get_points_name(channel)

    if r.lower() == v.lower():
        raise HTTPException(400, "❌ You can't rob yourself!")

    allowed, wait = can_rob(channel, r, v)
    if not allowed:
        return PlainTextResponse(f"⏳ You must wait {wait}s before robbing {v} again.")

    vic_pts = get_points_table(v, channel)
    if vic_pts <= 0:
        return PlainTextResponse(f"❌ {v} has no {name} to steal.")

    # decide amount to steal (10–50% of their balance)
    amount = random.randint(max(1, vic_pts // 10), max(1, vic_pts // 2))

    # define fun scenarios
    scenarios = [
        f"You tiptoe behind {v} and snatch {amount} {name} before they notice!",
        f"{v} drops a bagscash—lucky you grab {amount} {name}!",
        f"{v} was busy dancing, so you pickpocket {amount} {name} undetected!",
        f"You challenge {v} to rock-paper-scissors and cheat your way to {amount} {name}!",
        f"{v} falls asleep. You gently remove {amount} {name} from their pocket.",
        f"{v} is distracted by a cat video—steal {amount} {name} in the chaos!",
        f"You mug {v} with a fluffy cupcake and get away with {amount} {name}!",
        f"{v} can't resist your puppy eyes and hands you {amount} {name}. Sneaky!",
        f"You sell {v} a 'magic' pebble and pocket {amount} {name}. Oops!",
        f"A friendly ghost helps you lift {amount} {name} from {v}'s wallet."
    ]
    message = random.choice(scenarios)

    # apply the robbery
    await add_user_points(r, channel, amount)
    await add_user_points(v, channel, -amount)
    await update_rob_timestamp(channel, r, v)

    return PlainTextResponse(f"💰 {message}")

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
