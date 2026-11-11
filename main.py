import difflib
import json
import os
import re
import unicodedata

import discord

TOKEN = os.environ["DISCORD_TOKEN"]
DATA_FILE = "members.json"

# Discord user IDs allowed to use the management commands
OWNER_IDS = {890863615316459540}

# Leaderboard display tweaks (Discord user IDs)
NAME_OVERRIDES = {434338272836190208: "Uranus"}
TOO_LOW = {434338272836190208}

# Member roster: (IGN, Discord ID). New members: use !link <id> IGN
ROSTER = [
    ("PBIRD", 1170638503055474819),
    ("HASSEN", 1549509748741636207),
    ("ᶜᵒˢSabo", 333119924056621059),
    ("Al3in", 680539772615131187),
    ("ᶜᵒˢDenji", 659674891221598230),
    ("GetPaid3", 703291504667656243),
    ("ᶜᵒˢStewie", 332160994728083456),
    ("Txy", 1204733258584301589),
    ("ᴺᵒᵗHelios", 542754186929111040),
    ("ᶜSino", 431884023741284352),
    ("wTal", 362643593745268736),
    ("ᶜᵒˢBlask", 890863615316459540),
    ("Mfbl", 250838045106896909),
    ("Zxccxz", 442691984763912208),
    ("ᶜᵒˢMalcolm", 111681841034973184),
    ("Tobacco", 540967732884013056),
    ("Peachtea", 583552760721571870),
    ("Xrossheart", 407928878225424394),
    ("Lousy", 434338272836190208),
    ("ᶜ유나", 235755201536065536),
    ("ᶜᵒˢᵐᵒKapec", 337375673616171010),
    ("RoTo1", 562284302897774592),
    ("2Pain4", 231541093081153536),
    ("D1N0무적공룡", 718481970698518648),
    ("moon959", 489759592675278858),
    ("ᶜᵒˢCrabby", 426908105431842817),
    ("ᴬˢᵀZepc", 148638515972931585),
    ("ᶜᵒˢExødia", 763002105992052736),
    ("ᶜᵒˢNoodles", 169857089731035136),
    ("ZeNos", 1237067558000132306),
    ("jjaee", 205628346078461952),
    ("Propeliz", 279813353444999168),
    ("Dslayersss", 546472642987687956),
    ("ᶜᵒˢCrazy", 236847113588310016),
    ("ᴺᵒᵗProofy", 137822664508047360),
]

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

SEASON_RE = re.compile(r"s(\d{1,3})", re.I)

intents = discord.Intents.default()
intents.message_content = True
# The bot never pings anyone, even if a name contains @everyone
client = discord.Client(
    intents=intents, allowed_mentions=discord.AllowedMentions.none())


# ---------- names ----------

_FOLD = str.maketrans({"ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
                       "ł": "l", "Ł": "L"})


def strip_tag(name):
    """Drop leading guild-tag letters such as ᶜᵒˢ / ᴺᵒᵗ / ᴬˢᵀ."""
    i = 0
    while i < len(name) and unicodedata.category(name[i]) == "Lm":
        i += 1
    return name[i:].strip() or name


def match_key(name):
    """Name normalised for matching: no tag, no accents, any case."""
    s = strip_tag(name).translate(_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split()).casefold()


# ---------- storage ----------
# members.json: { "<discord id>": {"ign": "ᶜᵒˢBlask", "active": true,
#                                  "scores": {"100": 5007263, ...}} }

def load_data():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    for ign, uid in ROSTER:  # members missing from the file are added
        data.setdefault(str(uid),
                        {"ign": ign, "active": True, "scores": {}})
    return data


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def name_index(data, include_inactive=False):
    """match_key -> discord id."""
    return {match_key(p["ign"]): uid for uid, p in data.items()
            if include_inactive or p.get("active", True)}


def current_season(data):
    seasons = [int(s) for p in data.values() for s in p["scores"]]
    return max(seasons) if seasons else None


def sorted_scores(p):
    """[(season, score), ...] oldest first."""
    return sorted((int(s), v) for s, v in p["scores"].items())


def best_score(p):
    return max(p["scores"].values(), default=None)


def parse_entry(line):
    """'Blask 5007263' -> ('Blask', 5007263), or None if it doesn't parse."""
    line = line.strip()
    line = re.sub(r"^(?:[-*•]\s+|\d{1,3}[.)]\s+)", "", line)  # bullets, "1. "
    m = re.fullmatch(r"(.+?)\s*[:=\-–—]?\s+(\d[\d,._]*)", line)
    if not m:
        return None
    return m.group(1).strip(), int(re.sub(r"\D", "", m.group(2)))


def split_args(content):
    """Words after the command -> (season or None, player text)."""
    season, rest = None, []
    for w in content.strip().split()[1:]:
        m = SEASON_RE.fullmatch(w)
        if m and season is None:
            season = int(m.group(1))
        else:
            rest.append(w)
    return season, " ".join(rest)


def find_player(data, message, text):
    """Look up a player by @mention, Discord ID or IGN.
    Returns (discord id, player, error)."""
    if message.mentions:
        uid = str(message.mentions[0].id)
        if uid in data:
            return uid, data[uid], None
        return None, None, ("That Discord user isn't in the roster. "
                            "Use `!link <discord id> IGN`.")
    if not text:
        return None, None, "Please give a player's IGN (or an @mention)."
    if re.fullmatch(r"\d{15,20}", text) and text in data:
        return text, data[text], None
    uid = name_index(data, True).get(match_key(text))
    if uid is None:
        return None, None, f"I don't know a player called {text}."
    return uid, data[uid], None


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


def role_note(status, role_name):
    if status == "added":
        return f" New role: {role_name}."
    if status == "missing":
        return f" The role '{role_name}' doesn't exist."
    if status == "forbidden":
        return " I don't have permission to change roles."
    return ""


# ---------- output helpers ----------

def esc(name):
    return discord.utils.escape_markdown(name)


async def send_long(message, text):
    # Discord messages max out at 2000 characters, so split on line breaks
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    for i, c in enumerate(chunks):
        if i == 0:
            await message.reply(c)
        else:
            await message.channel.send(c)


# ---------- leaderboard ----------

def movers(pairs, up):
    # Top 5 percentage increases (up=True) or decreases (up=False) from
    # the previous season's score (a) to this season's score (b)
    out = []
    for name, a, b in pairs:
        if a <= 0:
            continue
        pct = (b - a) / a * 100
        if up and pct > 0:
            out.append((pct, name, a, b))
        elif not up and pct < 0:
            out.append((-pct, name, a, b))
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


def build_board(data, season):
    prev_season = season - 1
    rows = []   # (display name, score, discord id)
    pairs = []  # (display name, previous season score, this season score)
    for uid, p in data.items():
        cur = p["scores"].get(str(season))
        if cur is None:
            continue
        name = NAME_OVERRIDES.get(int(uid), strip_tag(p["ign"]))
        rows.append((name, cur, int(uid)))
        old = p["scores"].get(str(prev_season))
        if old is not None:
            pairs.append((name, old, cur))

    if not rows:
        return f"No scores have been submitted for season {season}."

    top = sorted(rows, key=lambda r: r[1], reverse=True)[:10]
    lines = []
    for i, (n, score, did) in enumerate(top, 1):
        tag = " *(soo low)*" if did in TOO_LOW else ""
        lines.append(f"{i}. {esc(n)} — {score:,}{tag}")
    text = f"**Top 10 – Guild Raid Season {season}**\n" + "\n".join(lines)

    span = f"(S{prev_season} → S{season})"
    text += f"\n\n**weeks biggest W** {span}\n"
    text += "\n".join(mover_lines(movers(pairs, True), True))
    text += f"\n\n**weeks biggest fumble** {span}\n"
    text += "\n".join(mover_lines(movers(pairs, False), False))
    return text


async def show_leaderboard(message):
    data = load_data()
    season = None
    for w in message.content.split()[1:]:
        m = re.fullmatch(r"s?(\d{1,3})", w, re.I)
        if m:
            season = int(m.group(1))
    if season is None:
        season = current_season(data)
    if season is None:
        await message.reply("No scores have been submitted yet.")
        return
    await send_long(message, build_board(data, season))


# ---------- player commands ----------

async def show_score(message):
    season, text = split_args(message.content)
    data = load_data()
    uid, p, err = find_player(data, message, text)
    if err:
        await message.reply(err)
        return
    if season is None:
        season = current_season(data)
    name = esc(strip_tag(p["ign"]))
    score = p["scores"].get(str(season))
    if score is None:
        await message.reply(f"{name} has no score for season {season}.")
        return
    await message.reply(f"**{name}** — season {season}: {score:,}")


async def show_average(message):
    _, text = split_args(message.content)
    data = load_data()
    uid, p, err = find_player(data, message, text)
    if err:
        await message.reply(err)
        return
    name = esc(strip_tag(p["ign"]))
    last = [v for _, v in sorted_scores(p)[-5:]]
    if not last:
        await message.reply(f"{name} has no scores yet.")
        return
    avg = round(sum(last) / len(last))
    s = "" if len(last) == 1 else "s"
    await message.reply(
        f"**{name}**: average over the last {len(last)} season{s}: {avg:,}")


async def show_compare(message):
    _, text = split_args(message.content)
    data = load_data()
    uid, p, err = find_player(data, message, text)
    if err:
        await message.reply(err)
        return
    name = esc(strip_tag(p["ign"]))
    items = sorted_scores(p)
    if not items:
        await message.reply(f"{name} has no scores yet.")
        return
    lines = []
    for n, i in enumerate(range(len(items) - 1,
                                max(len(items) - 6, -1), -1), 1):
        season, score = items[i]
        line = f"{n}. S{season}: {score:,}"
        if i > 0:  # change vs the previous listed season
            d = score - items[i - 1][1]
            line += f" ({'+' if d >= 0 else '-'}{abs(d):,})"
        lines.append(line)
    await message.reply(
        f"**{name} — last {len(lines)} seasons** (newest first)\n"
        + "\n".join(lines))


# ---------- management commands (owner only) ----------

async def handle_list(message):
    lines = message.content.strip().split("\n")
    head = lines[0].split()
    season = None
    if len(head) > 1:
        m = re.fullmatch(r"s?(\d{1,3})", head[1], re.I)
        if m:
            season = int(m.group(1))
    if season is None:
        await message.reply(
            "Usage: `!list sNN` on the first line, then one `IGN score` "
            "per line.")
        return
    entries = lines[1:]
    if len(head) > 2:  # an entry on the same line as the command
        entries.insert(0, " ".join(head[2:]))

    data = load_data()
    idx = name_index(data)
    touched, unreadable, unknown = set(), [], []
    changed = 0
    for raw in entries:
        if not raw.strip():
            continue
        parsed = parse_entry(raw)
        if parsed is None:
            unreadable.append(raw.strip()[:60])
            continue
        ign, score = parsed
        key = match_key(ign)
        uid = idx.get(key)
        if uid is None:
            close = difflib.get_close_matches(key, list(idx), n=1, cutoff=0.75)
            hint = ""
            if close:
                hint = f" (did you mean {strip_tag(data[idx[close[0]]]['ign'])}?)"
            unknown.append(f"{ign[:40]}{hint}")
            continue
        p = data[uid]
        if p["scores"].get(str(season)) != score:
            changed += 1
        p["scores"][str(season)] = score
        touched.add(uid)

    if touched:
        save_data(data)

    # Update tier roles
    roles = 0
    cache = {}
    for uid in touched:
        member = await get_member(message.guild, int(uid), cache)
        best = best_score(data[uid])
        if member and best is not None:
            status, _ = await update_role(member, best)
            if status == "added":
                roles += 1

    if touched:
        s = "" if len(touched) == 1 else "s"
        out = (f"Saved {len(touched)} score{s} for season {season} "
               f"({changed} new or changed).")
        out += f"\nCurrent season: {current_season(data)}."
        if roles:
            out += f"\nRoles updated: {roles}."
    else:
        out = "I didn't find any score lines I could save."
    if unknown:
        out += ("\n\nUnknown names (not saved). Typo? New member? Use "
                "`!link <discord id> IGN`:\n"
                + "\n".join(f"- {u}" for u in unknown[:15]))
        if len(unknown) > 15:
            out += f"\n…and {len(unknown) - 15} more"
    if unreadable:
        out += ("\n\nCouldn't read these lines:\n"
                + "\n".join(f"- {b}" for b in unreadable[:10]))
    await send_long(message, out)


async def delete_season(message):
    season, text = split_args(message.content)
    if season is None or (not text and not message.mentions):
        await message.reply(
            "Usage: `!delete sNN IGN`, e.g. `!delete s98 Blask`")
        return
    data = load_data()
    uid, p, err = find_player(data, message, text)
    if err:
        await message.reply(err)
        return
    name = esc(strip_tag(p["ign"]))
    if str(season) not in p["scores"]:
        await message.reply(f"{name} has no score for season {season}.")
        return

    del p["scores"][str(season)]
    save_data(data)

    reply = f"Deleted season {season} for {name}."
    member = await get_member(message.guild, int(uid), {})
    if member is not None:
        # Lower the role to the best score still left
        status, role_name = await force_role(member, best_score(p))
        if status == "ok" and role_name:
            reply += f" Their role is now {role_name}."
        elif status == "ok":
            reply += " No scores left, so their tier role was removed."
        elif status == "missing":
            reply += f" But the role '{role_name}' doesn't exist."
        else:
            reply += " I don't have permission to change their roles."
    await message.reply(reply)


async def link_player(message):
    words = message.content.split()[1:]
    m = (re.fullmatch(r"<@!?(\d{15,20})>|(\d{15,20})", words[0])
         if words else None)
    ign = " ".join(words[1:])
    if not m or not ign:
        await message.reply("Usage: `!link <discord id> IGN`")
        return
    uid = m.group(1) or m.group(2)

    data = load_data()
    taken = name_index(data).get(match_key(ign))
    if taken and taken != uid:
        await message.reply(
            f"{esc(strip_tag(ign))} is already used by Discord ID {taken}.")
        return
    p = data.setdefault(uid, {"ign": ign, "active": True, "scores": {}})
    p["ign"] = ign
    p["active"] = True
    save_data(data)

    reply = f"Linked **{esc(strip_tag(ign))}** to Discord ID {uid}."
    member = await get_member(message.guild, int(uid), {})
    if member is None:
        reply += " (That user isn't in this server.)"
    else:
        best = best_score(p)
        if best is not None:
            status, role_name = await update_role(member, best)
            reply += role_note(status, role_name)
    await message.reply(reply)


async def unlink_player(message):
    _, text = split_args(message.content)
    data = load_data()
    uid, p, err = find_player(data, message, text)
    if err:
        await message.reply(err)
        return
    p["active"] = False
    save_data(data)
    await message.reply(
        f"**{esc(strip_tag(p['ign']))}** is no longer in the roster. Their "
        "old scores stay in past seasons. `!link <discord id> IGN` adds "
        "them back.")


def commands_text(is_owner):
    lines = [
        "**Commands**",
        "`!leaderboard [sNN]` — top 10 for a season (default: current), "
        "plus weeks biggest W and weeks biggest fumble (% change vs the "
        "season before), e.g. `!leaderboard s97`",
        "`!score <player> [sNN]` — a player's score in a season "
        "(default: current), e.g. `!score Blask s98`",
        "`!average <player>` — average of a player's last 5 seasons",
        "`!compare <player>` — a player's last 5 season scores",
        "`!commands` — show this list",
        "<player> can be an IGN or an @mention.",
    ]
    if is_owner:
        lines += [
            "",
            "**Owner only**",
            "`!list sNN` + one `IGN score` per line — save or update the "
            "scores for a season",
            "`!delete sNN <player>` — delete a player's score for a season "
            "and lower their role to match what's left",
            "`!link <discord id> IGN` — add a new member, or change an IGN",
            "`!unlink <player>` — remove a member who left",
            "`!resetscores confirm` — delete all saved scores",
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
    is_owner = message.author.id in OWNER_IDS

    # Public commands
    if word == "!leaderboard":
        await show_leaderboard(message)
        return
    if word == "!score":
        await show_score(message)
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

    # Owner-only commands
    owner_commands = {
        "!list": handle_list,
        "!delete": delete_season,
        "!link": link_player,
        "!unlink": unlink_player,
    }
    if word in owner_commands:
        if is_owner:
            await owner_commands[word](message)
        else:
            await message.reply("Only the bot owner can use that command.")
        return
    if cmd == "!resetscores confirm":
        if is_owner:
            data = load_data()
            for p in data.values():
                p["scores"] = {}
            save_data(data)
            await message.reply("All saved scores have been deleted.")
        else:
            await message.reply("Only the bot owner can use that command.")
        return


client.run(TOKEN)
