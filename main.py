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

# (minsta poäng, rollnamn), högst först
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


def load_scores():
    try:
        with open(SCORES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_score(user_id, name, score):
    scores = load_scores()
    uid = str(user_id)
    best = max(scores.get(uid, {}).get("score", 0), score)
    scores[uid] = {"name": name, "score": best}
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(scores, f)


def to_jpeg(data):
    # Full storlek behålls; bara komprimering för att komma under 1 MB
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
    # Första talet efter "Total Score". Kommatecken och punkter läses ofta
    # fel av OCR, så vi använder bara siffrorna.
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


def tier_for(score):
    for limit, name in TIERS:
        if score >= limit:
            return name
    return NAMES[-1]


async def show_leaderboard(message):
    scores = load_scores()
    top = sorted(scores.values(), key=lambda v: v["score"], reverse=True)[:10]
    if not top:
        await message.reply("Inga poäng inskickade ännu.")
        return
    lines = [
        f"{i}. {discord.utils.escape_markdown(v['name'])} — {v['score']:,}"
        for i, v in enumerate(top, 1)
    ]
    await message.reply("**Topp 10 Guild Raid**\n" + "\n".join(lines))


@client.event
async def on_ready():
    print(f"Inloggad som {client.user}")


@client.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    if message.content.strip().lower() == "!leaderboard":
        await show_leaderboard(message)
        return

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
        except Exception as e:
            print("OCR-fel:", e)
            await message.reply("Kunde inte läsa bilden, försök igen.")
            return

    if "debug" in message.content.lower():
        await message.reply(f"```\n{text[:600]}\n```")

    if score is None:
        await message.reply("Hittade inget 'My Total Score' i bilden.")
        return

    member = message.author
    save_score(member.id, member.display_name, score)

    new = tier_for(score)
    new_i = NAMES.index(new)
    have = [NAMES.index(r.name) for r in member.roles if r.name in NAMES]

    # Lägre index = högre roll. Bara uppgraderingar tillåts.
    if have and min(have) <= new_i:
        await message.reply(
            f"Läste {score:,}. Du har redan {NAMES[min(have)]}, "
            "så ingen ändring.")
        return

    try:
        role = discord.utils.get(message.guild.roles, name=new)
        if role is None:
            await message.reply(
                f"Läste {score:,}, men rollen '{new}' finns inte.")
            return
        await member.add_roles(role)
        old = [r for r in member.roles if r.name in NAMES and r.name != new]
        if old:
            await member.remove_roles(*old)
    except discord.Forbidden:
        await message.reply("Jag saknar behörighet att ändra roller.")
        return

    await message.reply(f"Läste {score:,}. Ny roll: {new}")


client.run(TOKEN)
