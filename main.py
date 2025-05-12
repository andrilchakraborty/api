import asyncio
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

# Configuration via environment variables\CHANNEL = os.getenv("TWITCH_CHANNEL", "yourchannel")
REWARD_INTERVAL = int(os.getenv("REWARD_INTERVAL", 300))  # seconds between chat rewards
REWARD_AMOUNT = int(os.getenv("REWARD_AMOUNT", 100))    # points per reward

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
DB = "shrimp.db"

# Initialize database
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        '''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            points   INTEGER NOT NULL DEFAULT 0
        )
        '''
    )
    conn.commit()
    conn.close()

init_db()

# Database helpers
def get_points(user: str) -> int:
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE username = ?", (user,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

async def add_user_points(user: str, amount: int):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        "INSERT INTO users(username, points) VALUES (?, ?) "
        "ON CONFLICT(username) DO UPDATE SET points = points + ?",
        (user, amount, amount)
    )
    conn.commit()
    conn.close()

# Background task: reward chat participants
tasks = []
@app.on_event("startup")
def start_reward_loop():
    async def loop_rewards():
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    url = f"https://tmi.twitch.tv/group/user/{CHANNEL}/chatters"
                    resp = await client.get(url)
                    data = resp.json()
                    chatters = []
                    for role in ("broadcaster","moderators","vips","viewers"):  # include everyone
                        chatters.extend(data.get("chatters", {}).get(role, []))
                    for user in set(chatters):
                        await add_user_points(user, REWARD_AMOUNT)
                    print(f"Rewarded {len(chatters)} users {REWARD_AMOUNT} points each.")
                except Exception as e:
                    print("Reward loop error:", e)
                await asyncio.sleep(REWARD_INTERVAL)
    tasks.append(asyncio.create_task(loop_rewards()))

# Routes
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Add points (mods only)
@app.get("/add")
async def add_points(user: str, amount: int):
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    await add_user_points(user, amount)
    new = get_points(user)
    return PlainTextResponse(f"✅ **{user}** now has **{new}** shrimp points!")

# Check points
@app.get("/points")
async def points(user: str):
    pts = get_points(user)
    return PlainTextResponse(f"**{user}**, you have **{pts}** shrimp points.")

# Gamble: multi-game with animated-style responses
@app.get("/gamble")
async def gamble(user: str, wager: int):
    if wager <= 0:
        raise HTTPException(400, "Wager must be positive")
    current = get_points(user)
    if wager > current:
        return PlainTextResponse(f"❌ **{user}**, you only have **{current}** shrimp points!")

    # Deduct up front
    await add_user_points(user, -wager)

    # Select game
    choice = random.choice(["coinflip", "dice", "roulette"])
    # Simulated animation
    anim = {"coinflip": "🪙 Flipping...", "dice": "🎲 Rolling...", "roulette": "🎡 Spinning..."}[choice]
    await asyncio.sleep(1)

    # Determine result
    if choice == "coinflip":
        win = random.choice([True, False])
        detail = "Heads" if win else "Tails"
    elif choice == "dice":
        roll = random.randint(1, 6)
        win = roll >= 4
        detail = f"Rolled {roll}"
    else:  # roulette
        spin = random.randint(0, 36)
        win = (spin != 0 and spin % 2 == 0)
        detail = f"Landed on {spin}"

    # Apply outcome
    delta = wager * 2 if win else 0
    await add_user_points(user, delta)
    final = get_points(user)

    symbol = "🎉" if win else "💔"
    result = "won" if win else "lost"
    return PlainTextResponse(
        f"{anim}\n{symbol} **{user}** {result} **{wager}** on {choice.upper()} ({detail})!\nFinal balance: **{final}** shrimp points."
    )

# Slots endpoint
@app.get("/slots")
async def slots(user: str, wager: int):
    if wager <= 0:
        raise HTTPException(400, "Wager must be positive")
    current = get_points(user)
    if wager > current:
        return PlainTextResponse(f"❌ **{user}**, you only have **{current}** shrimp points!")

    # Deduct bet
    await add_user_points(user, -wager)

    # Slot symbols
    symbols = ["🍒","🍋","🔔","🍉","⭐","🍀"]
    reels = [random.choice(symbols) for _ in range(3)]
    await asyncio.sleep(1)

    # Check results
    if reels[0] == reels[1] == reels[2]:
        payout = wager * 5
        await add_user_points(user, payout)
        final = get_points(user)
        return PlainTextResponse(
            f"🎰 {' | '.join(reels)} 🎰\n💰 Jackpot! You won **{payout}** shrimp!\nFinal balance: **{final}** shrimp points."
        )
    # Two match
    elif len(set(reels)) == 2:
        payout = wager * 2
        await add_user_points(user, payout)
        final = get_points(user)
        return PlainTextResponse(
            f"🎰 {' | '.join(reels)} 🎰\n😊 Nice, you matched two! You won **{payout}** shrimp!\nFinal balance: **{final}** shrimp points."
        )
    # Lose
    final = get_points(user)
    return PlainTextResponse(
        f"🎰 {' | '.join(reels)} 🎰\n💔 No match. You lost **{wager}** shrimp.\nFinal balance: **{final}** shrimp points."
    )

# Leaderboard
@app.get("/leaderboard")
async def leaderboard(limit: int = 10):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT username, points FROM users ORDER BY points DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        return PlainTextResponse("No shrimp points yet.")
    lines = [f"{i+1}. {r[0]} — {r[1]} shrimp" for i, r in enumerate(rows)]
    return PlainTextResponse("🏆 Shrimp Leaderboard 🏆\n" + "\n".join(lines))
