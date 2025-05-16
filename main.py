import os
import sqlite3
import random
import asyncio
import time
from pydantic import BaseModel, Field
from typing import List, Dict
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

# how long before you can rob the same victim again (in seconds)
ROB_COOLDOWN      = 300  

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
    # settings table
    conn.execute(f"""
      CREATE TABLE IF NOT EXISTS settings (
        channel        TEXT PRIMARY KEY,
        points_name    TEXT NOT NULL,
        reward_amount  INTEGER NOT NULL DEFAULT {REWARD_AMOUNT}
      )
    """)
    # rob cooldowns
    conn.execute("""
      CREATE TABLE IF NOT EXISTS rob_cooldowns (
        channel     TEXT NOT NULL,
        robber      TEXT NOT NULL,
        victim      TEXT NOT NULL,
        last_rob    INTEGER NOT NULL,
        PRIMARY KEY(channel, robber, victim)
      )
    """)
    # seed default channel settings
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

# --- Data Models ---
class Driver(BaseModel):
    id: int
    name: str
    team: str
    skill: float = Field(ge=0.0, le=1.0, description="Driver skill coefficient 0-1")
    points: int = 0
    podiums: int = 0
    races: int = 0

class RaceResult(BaseModel):
    race_id: int
    positions: Dict[int, int]  # driver_id -> finishing position

class Race(BaseModel):
    id: int
    name: str
    track: str
    laps: int
    completed: bool = False
    result: RaceResult = None

# --- In-memory stores ---
drivers: Dict[int, Driver] = {}
races: Dict[int, Race] = {}
next_driver_id = 1
next_race_id = 1

# F1 points system for top 10
F1_POINTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]

# --- API Endpoints ---
@app.post("/drivers", response_model=Driver)
async def create_driver(name: str, team: str, skill: float = 0.5):
    global next_driver_id
    if skill < 0 or skill > 1:
        raise HTTPException(400, "Skill must be between 0 and 1")
    d = Driver(id=next_driver_id, name=name, team=team, skill=skill)
    drivers[next_driver_id] = d
    next_driver_id += 1
    return d

@app.get("/drivers", response_model=List[Driver])
async def list_drivers():
    return list(drivers.values())

@app.get("/drivers/{driver_id}", response_model=Driver)
async def get_driver(driver_id: int):
    if driver_id not in drivers:
        raise HTTPException(404, "Driver not found")
    return drivers[driver_id]

@app.post("/races", response_model=Race)
async def schedule_race(name: str, track: str, laps: int = 58):
    global next_race_id
    r = Race(id=next_race_id, name=name, track=track, laps=laps)
    races[next_race_id] = r
    next_race_id += 1
    return r

@app.get("/races", response_model=List[Race])
async def list_races():
    return list(races.values())

@app.get("/races/{race_id}", response_model=Race)
async def get_race(race_id: int):
    if race_id not in races:
        raise HTTPException(404, "Race not found")
    return races[race_id]

@app.post("/races/{race_id}/run", response_model=RaceResult)
async def run_race(race_id: int):
    if race_id not in races:
        raise HTTPException(404, "Race not found")
    race = races[race_id]
    if race.completed:
        raise HTTPException(400, "Race already completed")

    # Simulate race: drivers sorted by performance = skill + random noise
    performance = []
    for d in drivers.values():
        # random gaussian around skill to reflect variability
        score = random.gauss(d.skill, 0.1)
        performance.append((d.id, score))
    performance.sort(key=lambda x: x[1], reverse=True)

    positions = {}
    for idx, (driver_id, _) in enumerate(performance, start=1):
        positions[driver_id] = idx
        # update driver stats
        driver = drivers[driver_id]
        driver.races += 1
        if idx <= 10:
            pts = F1_POINTS[idx-1]
            driver.points += pts
        if idx <= 3:
            driver.podiums += 1

    result = RaceResult(race_id=race_id, positions=positions)
    race.result = result
    race.completed = True
    return result

@app.get("/standings/drivers", response_model=List[Driver])
async def driver_standings():
    # Sort by points desc, then podiums, then races
    sorted_drivers = sorted(
        drivers.values(),
        key=lambda d: (d.points, d.podiums, -d.races),
        reverse=True
    )
    return sorted_drivers

@app.get("/standings/teams")
async def team_standings():
    # Sum points by team
    team_totals: Dict[str, int] = {}
    for d in drivers.values():
        team_totals.setdefault(d.team, 0)
        team_totals[d.team] += d.points
    # Sort
    sorted_teams = sorted(team_totals.items(), key=lambda x: x[1], reverse=True)
    return [{"team": t, "points": p} for t, p in sorted_teams]

@app.delete("/reset")
async def reset_league():
    drivers.clear()
    races.clear()
    global next_driver_id, next_race_id
    next_driver_id = 1
    next_race_id = 1
    return {"status": "reset completed"}


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

def play_dice(amount: int):
    """
    Dice (6-sided):
      • Roll a single d6.
      • Payouts:
          – Roll =   6 → ×5 (≈16.7% chance)  
          – Roll = 4-5 → ×2 (≈33.3% chance)  
          – Roll ≤3  → lose (≈50% chance)
      • House edge: ~25%
      • Flavor: “You grip the ivory cube, pray to Fortuna…”
    """
    roll = random.randint(1, 6)
    if roll == 6:
        return 5, f"🎲 You rolled a **6**! Fortune smiles. Payout ×5."
    if roll >= 4:
        return 2, f"🎲 You rolled a **{roll}**. You double up! ×2 reward."
    return 0, f"🎲 You rolled a **{roll}**… nothing this time. You lose."

def play_slot(amount: int):
    """
    3-Reel Classic Slot:
      • Symbols: 🍒 ×3, 🍋, 🔔, ⭐, BAR  
      • Hit any “BAR” = instant loss.  
      • 2×🍒 = ×3, 3×🍒 = ×10, mixed (no BAR) = push ×1  
      • Chance of BAR per reel = ~14%; cherries ~43%; mixed ~43%  
      • House edge: ~5%
      • Reels spin with mechanical clatter and flashing lights.
    """
    symbols = ["🍒", "🍒", "🍒", "🍋", "🔔", "⭐", "BAR"]
    spin = [random.choice(symbols) for _ in range(3)]
    display = " ".join(spin)
    if "BAR" in spin:
        return 0, f"🎰 {display} → BAR appears. House takes it all."
    cherries = spin.count("🍒")
    if cherries == 3:
        return 10, f"🎰 {display} → TRIPLE CHERRIES! JACKPOT ×10!"
    if cherries == 2:
        return 3, f"🎰 {display} → Double cherries! Nice ×3."
    return 1, f"🎰 {display} → Mixed symbols. You push (×1)."

def play_texas(amount: int):
    """
    Simplified Texas Hold’em Draw:
      • Player “dealt” two cards, “flop” three community cards.
      • 15% → straight/flush → ×5  
      • 20% → any pair → ×2  
      • 65% → nothing → lose  
      • This abstracts full hand ranking.
    """
    r = random.random()
    if r < 0.15:
        return 5, "🃏 Flop gives you a straight or flush! Huge ×5 win!"
    if r < 0.35:
        return 2, "🃏 You paired up on the flop! Double ×2 payout."
    return 0, "🃏 Your flop misses. House wins."

def play_roulette(amount: int):
    """
    European Roulette (single zero):
      • Numbers 0–36; zero = house wins all color bets.  
      • Bet color (red/black):
          – Win ≈48.6% → ×2  
          – Lose ≈51.4% → lose  
      • Straight number hit (≈2.7%) → ×36  
      • House edge: ≈2.7%
    """
    pocket = random.randint(0, 36)
    if pocket == 0:
        return 0, f"🎡 Ball lands on **0**. House sweeps your bet."
    # simulate number hit first
    if random.random() < 1/37:
        return 36, f"🎡 Unbelievable! Exact hit **{pocket}** → ×36 jackpot!"
    color = "red" if pocket % 2 else "black"
    if random.random() < 18/37:  # exact red/black probability
        return 2, f"🎡 Ball on **{pocket} {color}**. You win color bet ×2!"
    return 0, f"🎡 Ball on **{pocket} {color}**. You lose."

def play_blackjack(amount: int):
    """
    Mini‐Blackjack:
      • One‐draw vs dealer:  
          – 5% hit natural blackjack → ×2.5  
          – 25% beat dealer → ×2  
          – 70% lose → lose  
      • No splits, no insurance, single deck abstraction.  
      • House edge: ~0.5% (artificially generous!)
    """
    r = random.random()
    if r < 0.05:
        return 2.5, "🂡 Blackjack! You get paid 3:2 (×2.5)."
    if r < 0.30:
        return 2, "🂱 You beat the dealer’s 20. Double up!"
    return 0, "🂲 Dealer’s hand wins. You lose."

def play_baccarat(amount: int):
    """
    Banker Bet Baccarat:
      • Banker win ≈45.8% → ×1.95 (5% commission)  
      • Player win ≈44.6% → lose on banker bet  
      • Tie ≈9.6% → push (×1)  
      • House edge (banker) ≈1.06%
    """
    r = random.random()
    if r < 0.096:
        return 1, "🎴 It’s a tie. Push — your wager is returned."
    if r < 0.096 + 0.458:
        return 1.95, "🎴 Banker hand wins. You net ×1.95."
    return 0, "🎴 Player hand wins. You lose."

def play_craps(amount: int):
    """
    Pass Line Bet (Craps):
      • Come‐out roll:
          – 7 or 11 (≈22.2%) → ×2.5  
          – 2,3,12 (≈11.1%) → lose  
          – else → point (→ push ×1)  
      • Simplified: on point we push.
    """
    die1, die2 = random.randint(1,6), random.randint(1,6)
    total = die1 + die2
    if total in (7, 11):
        return 2.5, f"🎲 You rolled **{total}** on come‐out. Win ×2.5!"
    if total in (2, 3, 12):
        return 0, f"🎲 Craps! You rolled **{total}**. House wins."
    return 1, f"🎲 Rolled **{total}**. Point established — push."

def play_keno(amount: int):
    """
    Keno (pick 3):
      • Hit all 3 numbers (≈0.3%) → ×40  
      • Hit 2 (≈3%) → ×5  
      • Hit 1 (≈23%) → ×1 (push)  
      • Hit 0 → lose  
      • House edge ~25%
    """
    hits = sum(random.random() < 3/80 for _ in range(3))  # rough odds
    if hits == 3:
        return 40, "🔢 All 3 numbers! Rare ×40 Keno jackpot!"
    if hits == 2:
        return 5, "🔢 2 hits! You win ×5."
    if hits == 1:
        return 1, "🔢 Single hit. You push (×1)."
    return 0, "🔢 No hits. You lose."

def play_video_poker(amount: int):
    """
    Jacks or Better Video Poker:
      • Deals “hand quality” by tier probabilities:  
          – Royal Flush (≈0.003%) → ×800  
          – Straight Flush (≈0.01%) → ×50  
          – Four of a Kind (≈0.02%) → ×25  
          – Full House (≈0.1%) → ×9  
          – Flush (≈0.2%) → ×6  
          – Straight (≈0.4%) → ×4  
          – Three of a Kind (≈2.1%) → ×3  
          – Two Pair (≈4.8%) → ×2  
          – Jacks+ Pair (≈7%) → ×1  
          – Else → lose  
      • House edge ~0.5%
    """
    r = random.random()
    if r < 0.00003:
        return 800, "🎮 Royal Flush! Mythic ×800 payout!"
    if r < 0.00013:
        return 50, "🎮 Straight Flush! ×50 win!"
    if r < 0.00033:
        return 25, "🎮 Four of a Kind! ×25 payout!"
    if r < 0.00133:
        return 9, "🎮 Full House! ×9 reward!"
    if r < 0.00333:
        return 6, "🎮 Flush! ×6 payout!"
    if r < 0.00733:
        return 4, "🎮 Straight! ×4 payout!"
    if r < 0.03033:
        return 3, "🎮 Three of a Kind! ×3 win!"
    if r < 0.07833:
        return 2, "🎮 Two Pair! ×2 payoff!"
    if r < 0.14833:
        return 1, "🎮 Pair of Jacks or better. Push (×1)."
    return 0, "🎮 No winning combination. You lose."

def play_hi_lo(amount: int):
    """
    High-Low Card:
      • Draw a card 1–13 uniformly:
          – Card >7 (≈46.2%) → ×2  
          – Card ≤7 → lose  
      • Fast, flip-and-see action.
    """
    card = random.randint(1, 13)
    if card > 7:
        return 2, f"🃏 You drew **{card}** (>7). You double up!"
    return 0, f"🃏 You drew **{card}**. Too low. You lose."

# ───────────────────────────────────────────────────────────────────────────────
# Assemble and weight the games
GAMES = [
    (play_dice,        10, "Dice"),
    (play_slot,        10, "Slot Machine"),
    (play_texas,       10, "Texas Hold'em"),
    (play_roulette,    15, "Roulette"),
    (play_blackjack,   15, "Blackjack"),
    (play_baccarat,    10, "Baccarat"),
    (play_craps,       10, "Craps"),
    (play_keno,         5, "Keno"),
    (play_video_poker, 10, "Video Poker"),
    (play_hi_lo,        5, "High-Low Card"),
]

@app.get("/gamble")
async def gamble(user: str, wager: str, channel: str = DEFAULT_CHANNEL):
    # 1) Balance check
    current = get_points_table(user, channel)
    if current <= 0:
        return PlainTextResponse(f"❌ {user}, you have no {get_points_name(channel)}!")
    # 2) Parse & validate wager
    amount = parse_wager(wager, current)
    if amount <= 0:
        raise HTTPException(400, "Wager must be positive")
    if amount > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} {get_points_name(channel)}!")
    await add_user_points(user, channel, -amount)

    # 3) Choose game
    funcs, weights, names = zip(*GAMES)
    idx = random.choices(range(len(GAMES)), weights=weights, k=1)[0]
    game_fn, game_name = funcs[idx], names[idx]

    # 4) Play
    mul, detail = game_fn(amount)
    payout = int(amount * mul)

    # 5) Payout if win/push
    if payout > 0:
        await add_user_points(user, channel, payout)

    # 6) Build response
    final = get_points_table(user, channel)
    pname = get_points_name(channel)
    emoji = "🎉" if mul > 1 else ("😐" if mul == 1 else "💀")
    msg = (
        f"{emoji} {user} played **{game_name}** for {amount} {pname}.\n"
        f"{detail}\n"
        f"Payout: {payout} {pname}.\n"
        f"Final balance: {final} {pname}."
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
