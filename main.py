# main.py
import sqlite3
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
import random

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Allow Nightbot origin or any
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
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            points INTEGER NOT NULL DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Helper
def get_points(username: str) -> int:
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT points FROM users WHERE username = ?", (username,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0


# Root health check
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Add points
@app.get("/add")
async def add_points(user: str, amount: int):
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO users(username, points) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET points = points + ?", (user, amount, amount))
    conn.commit()
    c.execute("SELECT points FROM users WHERE username = ?", (user,))
    new_points = c.fetchone()[0]
    conn.close()
    return JSONResponse({"user": user, "points": new_points})

# Gamble points
@app.get("/gamble")
async def gamble(user: str, wager: int):
    if wager <= 0:
        raise HTTPException(status_code=400, detail="Wager must be positive")
    points = get_points(user)
    if wager > points:
        raise HTTPException(status_code=400, detail="Not enough points to gamble")
    # 50/50 win
    win = random.choice([True, False])
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    if win:
        delta = wager
        message = f"Congrats {user}, you won {wager} shrimp!"
    else:
        delta = -wager
        message = f"Sorry {user}, you lost {wager} shrimp!"
    c.execute("UPDATE users SET points = points + ? WHERE username = ?", (delta, user))
    conn.commit()
    new_points = get_points(user)
    conn.close()
    return JSONResponse({"user": user, "result": message, "points": new_points})

# Leaderboard
@app.get("/leaderboard")
async def leaderboard(limit: int = 10):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT username, points FROM users ORDER BY points DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    board = [{"user": r[0], "points": r[1]} for r in rows]
    return JSONResponse({"leaderboard": board})
