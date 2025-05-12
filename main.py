import sqlite3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import random

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# CORS for Nightbot
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
            points INTEGER NOT NULL DEFAULT 0
        )
        '''
    )
    conn.commit()
    conn.close()

init_db()

# Helper: get points
def get_points(username: str) -> int:
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

# Root: health check
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Add points (mods only)
@app.get("/add")
async def add_points(user: str, amount: int):
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute(
        "INSERT INTO users(username, points) VALUES (?, ?) "
        "ON CONFLICT(username) DO UPDATE SET points = points + ?",
        (user, amount, amount)
    )
    conn.commit()
    new = get_points(user)
    conn.close()
    return PlainTextResponse(f"✅ {user} now has {new} shrimp points!")

# Gamble points
@app.get("/gamble")
async def gamble(user: str, wager: int):
    if wager <= 0:
        raise HTTPException(status_code=400, detail="Wager must be positive")
    current = get_points(user)
    if wager > current:
        return PlainTextResponse(f"❌ {user}, you only have {current} shrimp points.")
    win = random.choice([True, False])
    delta = wager if win else -wager
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("UPDATE users SET points = points + ? WHERE username = ?", (delta, user))
    conn.commit()
    new = get_points(user)
    conn.close()
    if win:
        return PlainTextResponse(f"🎉 {user} won {wager}! New balance: {new} shrimp points.")
    else:
        return PlainTextResponse(f"💔 {user} lost {wager}. New balance: {new} shrimp points.")

# Check points
@app.get("/points")
async def points(user: str):
    current = get_points(user)
    return PlainTextResponse(f"{user}, you have {current} shrimp points.")

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
