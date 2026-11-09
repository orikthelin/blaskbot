import hashlib
import io
import json
import os
import re

import aiohttp
import discord
from PIL import Image

TOKEN = os.environ["DISCORD_TOKEN"]
OCR_KEY = os.environ["OCR_API_KEY"]
CHANNEL_NAME = "score-submissions"
SCORES_FILE = "scores.json"
OWNER_ID = 890863615316459540  # only this user can use management commands
KEEP = 200  # uploads kept per player

# Leaderboard display tweaks (Discord user IDs)
NAME_OVERRIDES = {434338272836190208: "Uranus"}
TOO_LOW = {434338272836190208}

# (minimum score, role name), highest first
TIERS = [
    (4_000_000_000, "Solar"),
    (3_500_000_000, "Nebula"),
    (3_000_000_000, "Eclipse"),
    (2_500_000_000, "Meteor"),
    (2_000_000_000, "Comet"),
    (1_500_000_000, "Stellar"),
    (1_000_000_000, "Astral"),
    (500_000_000, "Lunar"),
    (250_000_000, "Orbit"),
    (0, "Pluto"),
]
NAMES = [n for _, n in TIERS]

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

scanning = False


# ---------- storage ----------

def normalize(entry):
    # Convert older save formats to: history = [{"season": n, "score": n}]
    h = entry.get("history")
    if h is None:
        h = []
        if entry.get("prev") is not None:
            h.append(entry["prev"])
        h.append(entry.get("last", entry.get("score", 0)))
    entry["history"] = [
        x if isinstance(x, dict) else {"season": None, "score": x}
        for x in h
    ]


def load_scores():
    try:
        with open(SCORES_FILE, encoding="utf-8") as f:
            scores = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    for e in scores.values():
        normalize(e)
    return scores


def write_scores(scores):
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(scores, f)


def fingerprint(data):
    return hashlib.sha256(data).hexdigest()[:20]


def save_score(user_id, name, season, score, digest, ts):
    """Returns True if saved, False if it was a duplicate."""
    scores = load_scores()
    entry = scores.setdefault(str(user_id), {"name": name, "history": []})
    entry["name"] = name
    h = entry["history"]
    # Duplicate = same image file, or same season + same score already saved
    dup = any(
        e.get("hash") == digest
        or (e["season"] == season and e["score"] == score)
        for e in h
    )
    if not dup:
        h.append({"season": season, "score": score,
                  "hash": digest, "t": ts})
        h.sort(key=lambda e: e.get("t", 0))  # keep chronological order
        entry["history"] = h[-KEEP:]
    write_scores(scores)
    return not dup


def current_season(scores):
    seasons = [e["season"] for v in scores.values()
               for e in v["history"] if e["season"] is not None]
    return max(seasons) if seasons else None


def season_scores(entry, season):
    return [e["score"] for e in entry["history"] if e["season"] == season]


# ---------- OCR ----------

def to_jpeg(data):
    # Full size is kept; only compress to stay under the 1 MB free-tier limit
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((1400, 3000))
    for q in (90, 80, 65, 50, 40):
        out = io.BytesIO()
        img.save(out, "JPEG", quality=q)
        if out.tell() < 950_000:
            break
    return out.getvalue()


async def ocr(jpeg):
    form = aiohttp.FormData()
    form.add_field("apikey", OCR_KEY)
    form.add_field("language", "eng")
    form.add_field("OCREngine", "2")
    form.add_field("file", jpeg, filename="s.jpg", content_type="image/jpeg")
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.ocr.space/parse/image",
                          data=form, timeout=timeout) as r:
            j = await r.json()
    if j.get("IsErroredOnProcessing"):
        raise RuntimeError(j.get("ErrorMessage"))
    return "\n".join(p["ParsedText"] for p in j["ParsedResults"])


def parse_score(text):
    # First number after "Total Score". OCR often mangles commas and dots,
    # so only the digits are used.
    m = re.search(r"total\s*score", text, re.I)
    if not m:
        return None
    pattern = r"(?<![A-Za-z])[\dIl|Oo][\dIl|Oo,.]*"
    for tok in re.findall(pattern, text[m.end():]):
        if not any(c.isdigit() for c in tok):
            continue
        tok = tok.translate(str.maketrans("Il|Oo", "11100"))
        return int(re.sub(r"\D", "", tok))
    return None


def parse_season(text):
    # "Guild Raid Season 99" -> 99
    m = re.search(r"season\s*([\dIl|Oo]{1,3})(?![A-Za-z])", text, re.I)
    if not m:
        return None
    tok = m.group(1).translate(str.maketrans("Il|Oo", "11100"))
    return int(tok)


# ---------- roles ----------

def tier_for(score):
    for limit, name in TIERS:
        if score >= limit:
            return name
    return NAMES[-1]


async def update_role(member, score):
    """Roles can only go up. Returns (status, role_name)."""
    new = tier_for(score)
    new_i = NAMES.index(new)
    have = [NAMES.index(r.name) for r in member.roles if r.name in NAMES]
    # Lower index = higher role
    if have and min(have) <= new_i:
        return "kept", NAMES[min(have)]
    role = discord.utils.get(member.guild.roles, name=new)
    if role is None:
        return "missing", new
    try:
        await member.add_roles(role)
        old = [r for r in member.roles if r.name in NAMES and r.name != new]
        if old:
            await member.remove_roles(*old)
    except discord.Forbidden:
        return "forbidden", new
    return "added", new


async def force_role(member, score):
    """Set the tier role to exactly match score, even if that means going
    down. score=None removes all tier roles. Returns (status, role_name)."""
    target = tier_for(score) if score is not None else None
    try:
        old = [r for r in member.roles
               if r.name in NAMES and r.name != target]
        if old:
            await member.remove_roles(*old)
        if target:
            role = discord.utils.get(member.guild.roles, name=target)
            if role is None:
                return "missing", target
            if role not in member.roles:
                await member.add_roles(role)
    except discord.Forbidden:
        return "forbidden", target
    return "ok", target


async def get_member(guild, user_id, cache):
    if user_id in cache:
        return cache[user_id]
    m = guild.get_member(user_id)
    if m is None:
        try:
            m = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            m = None
    cache[user_id] = m
    return m


# ---------- commands ----------

def esc(name):
    return discord.utils.escape_markdown(name)


def movers(pairs, up):
    # Top 5 percentage increases (up=True) or decreases (up=False) from
    # the previous season's score (a) to the current season's score (b)
    out = []
    for name, a, b in pairs:
        if a <= 0:
            continue
        pct = (b - a) / a * 100
        if (pct > 0) if up else (pct 
