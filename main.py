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


def save_score(user_id, name, season, score):
    scores = load_scores()
    entry = scores.setdefault(str(user_id), {"name": name, "history": []})
    entry["name"] = name
    h = entry["history"]
    new = {"season": season, "score": score}
    # Same season + same score twice in a row counts once
    if not h or h[-1] != new:
        h.append(new)
        entry["history"] = h[-KEEP:]
    write_scores(scores)


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


def tier_for(score):
    for limit, name in TIERS:
        if score >= limit:
            return name
    return NAMES[-1]


# ---------- commands ----------

def esc(name):
    return discord.utils.escape_markdown(name)


def movers(rows, up):
    # Top 5 increases (up=True) or decreases (up=False) since last upload
    out = []
    for name, h in rows:
        if len(h) < 2:
            continue
        d = h[-1] - h[-2]
        if (d > 0) if up else (d < 0):
            out.append((abs(d), name, h[-2], h[-1]))
    out.sort(key=lambda r: r[0], reverse=True)
    return out[:5]


def mover_lines(rows, up):
    sign = "+" if up else "-"
    empty = "no more increases" if up else "no more decreases"
    lines = []
    for i in range(5):
        if i < len(rows):
            d, name, a, b = rows[i]
            lines.append(f"{i + 1}. {esc(name)}: {a:,} → {b:,} ({sign}{d:,})")
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


def commands_text(is_owner):
    lines = [
        "**Commands**",
        "`!leaderboard` — top 10, weeks biggest W and weeks biggest "
        "fumble for the current season",
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
    save_score(member.id, member.display_name, season, score)

    new = tier_for(score)
    new_i = NAMES.index(new)
    have = [NAMES.index(r.name) for r in member.roles if r.name in NAMES]
    read = f"Read {score:,} (season {season})."

    # Lower index = higher role. Roles can only go up.
    if have and min(have) <= new_i:
        await message.reply(
            f"{read} You already have {NAMES[min(have)]}, "
            "so no role change.")
        return

    try:
        role = discord.utils.get(message.guild.roles, name=new)
        if role is None:
            await message.reply(f"{read} But the role '{new}' doesn't exist.")
            return
        await member.add_roles(role)
        old = [r for r in member.roles if r.name in NAMES and r.name != new]
        if old:
            await member.remove_roles(*old)
    except discord.Forbidden:
        await message.reply("I don't have permission to change roles.")
        return

    await message.reply(f"{read} New role: {new}")


client.run(TOKEN)
