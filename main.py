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
    # Keep full size; only compress to stay under the 1 MB free-tier limit
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


def movers(rows, up):
    # Top 5 percentage increases (up=True) or decreases (up=False)
    # between a player's last two uploads in the season
    out = []
    for name, h in rows:
        if len(h) < 2 or h[-2] <= 0:
            continue
        a, b = h[-2], h[-1]
        pct = (b - a) / a * 100
        if (pct > 0) if up else (pct < 0):
            out.append((abs(pct), name, a, b))
    out.sort(key=lambda r: r[0], reverse=True)
    return out[:5]


def mover_lines(rows, up):
    sign = "+" if up else "-"
    empty = "no more increases" if up else "no more decreases"
    lines = []
    for i in range(5):
        if i < len(rows):
            pct, name, a, b = rows[i]
            lines.append(f"{i + 1}. {esc(name)}: {a:,} → {b:,} "
                         f"({sign}{pct:.2f}%)")
        else:
            lines.append(f"{i + 1}. {empty}")
    return lines


async def show_leaderboard(message):
    scores = load_scores()
    season = current_season(scores)
    if season is None:
        await message.reply("No scores have been submitted yet.")
        return

    rows = []
    for v in scores.values():
        h = season_scores(v, season)
        if h:
            rows.append((v["name"], h))

    top = sorted(rows, key=lambda r: r[1][-1], reverse=True)[:10]
    lines = [f"{i}. {esc(n)} — {h[-1]:,}"
             for i, (n, h) in enumerate(top, 1)]
    text = f"**Top 10 – Guild Raid Season {season}**\n" + "\n".join(lines)

    text += "\n\n**weeks biggest W**\n"
    text += "\n".join(mover_lines(movers(rows, True), True))
    text += "\n\n**weeks biggest fumble**\n"
    text += "\n".join(mover_lines(movers(rows, False), False))

    await message.reply(text)


async def show_score(message, parts):
    if not message.mentions:
        await message.reply("Usage: `!score @player [sNN]`, e.g. "
                            "`!score @blask s98`")
        return
    target = message.mentions[0]
    name = esc(target.display_name)
    scores = load_scores()

    season = None
    for p in parts[1:]:
        m = re.fullmatch(r"s?(\d{1,3})", p)
        if m:
            season = int(m.group(1))
    if season is None:
        season = current_season(scores)
    if season is None:
        await message.reply("No scores have been submitted yet.")
        return

    entry = scores.get(str(target.id))
    h = season_scores(entry, season) if entry else []
    if not h:
        await message.reply(
            f"{name} hasn't uploaded any score from season {season}.")
        return
    extra = f" (latest of {len(h)} uploads)" if len(h) > 1 else ""
    await message.reply(f"**{name}** — season {season}: {h[-1]:,}{extra}")


async def show_average(message):
    if not message.mentions:
        await message.reply("Usage: `!average @player`")
        return
    target = message.mentions[0]
    name = esc(target.display_name)
    entry = load_scores().get(str(target.id))
    if not entry or not entry["history"]:
        await message.reply(f"{name} hasn't uploaded any scores yet.")
        return
    last = [e["score"] for e in entry["history"][-5:]]
    avg = round(sum(last) / len(last))
    s = "" if len(last) == 1 else "s"
    await message.reply(
        f"**{name}**: average of last {len(last)} upload{s}: {avg:,}")


async def show_compare(message):
    if not message.mentions:
        await message.reply("Usage: `!compare @player`")
        return
    target = message.mentions[0]
    name = esc(target.display_name)
    entry = load_scores().get(str(target.id))
    if not entry or not entry["history"]:
        await message.reply(f"{name} hasn't uploaded any scores yet.")
        return

    h = entry["history"]
    lines = []
    for n, i in enumerate(range(len(h) - 1, max(len(h) - 6, -1), -1), 1):
        e = h[i]
        label = f"S{e['season']}" if e["season"] is not None else "S?"
        line = f"{n}. {label}: {e['score']:,}"
        # Show the change if the previous upload was from the same season
        if i > 0 and h[i - 1]["season"] == e["season"]:
            d = e["score"] - h[i - 1]["score"]
            line += f" ({'+' if d >= 0 else '-'}{abs(d):,})"
        lines.append(line)
    await message.reply(
        f"**{name} — last {len(lines)} uploads** (newest first)\n"
        + "\n".join(lines))


async def scan_channel(message):
    global scanning
    if scanning:
        await message.reply("A scan is already running.")
        return
    scanning = True
    try:
        await message.reply(
            "Scanning this channel for earlier uploads. "
            "This can take a while…")

        scores = load_scores()
        known = {uid: {e.get("hash") for e in v["history"] if e.get("hash")}
                 for uid, v in scores.items()}
        stats = {"images": 0, "added": 0, "dupes": 0, "failed": 0, "roles": 0}
        seen = set()
        cache = {}
        fails_in_a_row = 0
        stopped = False

        try:
            async with message.channel.typing():
                async for msg in message.channel.history(
                        limit=None, oldest_first=True):
                    if msg.author.bot:
                        continue
                    images = [a for a in msg.attachments
                              if (a.content_type or "").startswith("image/")]
                    if not images:
                        continue
                    stats["images"] += 1
                    uid = str(msg.author.id)
                    seen.add(uid)

                    try:
                        data = await images[0].read()
                        digest = fingerprint(data)
                        if digest in known.get(uid, set()):
                            stats["dupes"] += 1  # already saved, skip OCR
                            continue
                        text = await ocr(to_jpeg(data))
                    except Exception as e:
                        print("Scan error:", e)
                        stats["failed"] += 1
                        fails_in_a_row += 1
                        if fails_in_a_row >= 5:
                            stopped = True
                            break
                        continue
                    fails_in_a_row = 0

                    score = parse_score(text)
                    season = parse_season(text)
                    if score is None or season is None:
                        stats["failed"] += 1
                        continue

                    member = await get_member(message.guild, msg.author.id,
                                              cache)
                    name = (member or msg.author).display_name
                    if save_score(msg.author.id, name, season, score,
                                  digest, msg.created_at.timestamp()):
                        stats["added"] += 1
                    else:
                        stats["dupes"] += 1
                    known.setdefault(uid, set()).add(digest)
        except discord.Forbidden:
            await message.reply(
                "I can't read this channel's history. "
                "I need the 'Read Message History' permission.")
            return

        # Update roles from each player's highest saved score
        scores = load_scores()
        for uid in seen:
            entry = scores.get(uid)
            if not entry or not entry["history"]:
                continue
            member = await get_member(message.guild, int(uid), cache)
            if member is None:
                continue
            best = max(e["score"] for e in entry["history"])
            status, _ = await update_role(member, best)
            if status == "added":
                stats["roles"] += 1

        summary = (
            "**Scan finished**\n"
            f"Screenshots found: {stats['images']}\n"
            f"New uploads saved: {stats['added']}\n"
            f"Duplicates skipped: {stats['dupes']}\n"
            f"Couldn't read: {stats['failed']}\n"
            f"Roles updated: {stats['roles']}"
        )
        if stopped:
            summary += (
                "\n\nStopped early after 5 failures in a row (possibly the "
                "OCR daily limit). Run `!scan` again later; uploads that "
                "are already saved are skipped.")
        await message.reply(summary)
    finally:
        scanning = False


def commands_text(is_owner):
    lines = [
        "**Commands**",
        "`!leaderboard` — top 10, weeks biggest W and weeks biggest "
        "fumble (% change) for the current season",
        "`!score @player [sNN]` — a player's score in a season "
        "(default: current season), e.g. `!score @blask s98`",
        "`!average @player` — average of a player's last 5 uploads",
        "`!compare @player` — a player's last 5 uploaded scores",
        "`!commands` — show this list",
        f"Post a screenshot in #{CHANNEL_NAME} to submit your score.",
    ]
    if is_owner:
        lines += [
            "",
            "**Owner only**",
            "`!scan` — scan all earlier uploads in this channel",
            "`!resetscores confirm` — delete all saved scores",
            "Add `debug` as a caption on a screenshot to see the raw OCR text",
        ]
    return "\n".join(lines)


# ---------- events ----------

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    cmd = message.content.strip().lower()
    parts = cmd.split()
    word = parts[0] if parts else ""
    is_owner = message.author.id == OWNER_ID

    if word == "!leaderboard":
        await show_leaderboard(message)
        return
    if word == "!score":
        await show_score(message, parts)
        return
    if word == "!average":
        await show_average(message)
        return
    if word == "!compare":
        await show_compare(message)
        return
    if word == "!commands":
        await message.reply(commands_text(is_owner))
        return
    if word == "!scan":
        if is_owner:
            await scan_channel(message)
        else:
            await message.reply("Only the bot owner can use that command.")
        return
    if cmd == "!resetscores confirm":
        if is_owner:
            write_scores({})
            await message.reply("All saved scores have been deleted.")
        else:
            await message.reply("Only the bot owner can use that command.")
        return

    # Score submissions
    if getattr(message.channel, "name", None) != CHANNEL_NAME:
        return
    images = [a for a in message.attachments
              if (a.content_type or "").startswith("image/")]
    if not images:
        return

    async with message.channel.typing():
        try:
            data = await images[0].read()
            digest = fingerprint(data)
            text = await ocr(to_jpeg(data))
            score = parse_score(text)
            season = parse_season(text)
        except Exception as e:
            print("OCR error:", e)
            await message.reply("Couldn't read the image, please try again.")
            return

    if is_owner and "debug" in message.content.lower():
        await message.reply(f"```\n{text[:600]}\n```")

    if score is None:
        await message.reply("Couldn't find 'My Total Score' in the image.")
        return
    if season is None:
        await message.reply(
            "Couldn't find the season number in the image. "
            "Please upload the full Guild Raid screen.")
        return

    member = message.author
    added = save_score(member.id, member.display_name, season, score,
                       digest, message.created_at.timestamp())
    read = f"Read {score:,} (season {season})."
    if not added:
        read += " Already recorded, so it wasn't counted again."

    status, role_name = await update_role(member, score)
    if status == "kept":
        await message.reply(
            f"{read} You already have {role_name}, so no role change.")
    elif status == "missing":
        await message.reply(f"{read} But the role '{role_name}' doesn't exist.")
    elif status == "forbidden":
        await message.reply("I don't have permission to change roles.")
    else:
        await message.reply(f"{read} New role: {role_name}")


client.run(TOKEN)
