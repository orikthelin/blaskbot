import difflib
import json
import os
import re
import unicodedata

import discord

TOKEN = os.environ["DISCORD_TOKEN"]

# Discord user IDs allowed to use the management commands
OWNER_IDS = {890863615316459540}

# Leaderboard display tweaks (Discord user IDs)
NAME_OVERRIDES = {434338272836190208: "Uranus"}
TOO_LOW = {434338272836190208}

# !list flags a score as unusual if it's more than JUMP_UP times, or less
# than JUMP_DOWN times, the player's score in the previous season
JUMP_UP = 2.0
JUMP_DOWN = 0.5

# Cosmos members: (IGN, Discord ID). New members: use !link <id> IGN
COSMOS_ROSTER = [
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

# Astryx members: (IGN, Discord ID). Empty for now: add members with
# !astlink <id> IGN, or paste them here like the Cosmos list above.
ASTRYX_ROSTER = []

# (minimum score, role name), highest first. The tier roles are shared by
# both guilds.
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


# ---------- guilds ----------
# Each guild has its own data file, member list and commands.
# members.json: { "<discord id>": {"ign": "ᶜᵒˢBlask", "active": true,
#                                  "scores": {"100": 5007263, ...}} }

class GuildData:
    def __init__(self, name, tag, prefix, file, roster):
        self.name = name      # shown in messages
        self.tag = tag        # "" for Cosmos, "Astryx" in Astryx titles
        self.prefix = prefix  # command prefix: "!" or "!ast"
        self.file = file
        self.roster = roster

    def load(self):
        try:
            with open(self.file, encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError):
            data = {}
        for ign, uid in self.roster:  # members missing from the file
            data.setdefault(str(uid),
                            {"ign": ign, "active": True, "scores": {}})
        return data

    def save(self, data):
        with open(self.file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)


COSMOS = GuildData("Cosmos", "", "!", "members.json", COSMOS_ROSTER)
ASTRYX = GuildData("Astryx", "Astryx", "!ast", "astryx_members.json",
                   ASTRYX_ROSTER)
GUILDS = (COSMOS, ASTRYX)


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


# ---------- data helpers ----------

def name_index(data, include_inactive=False):
    """match_key -> discord id."""
    return {match_key(p["ign"]): uid for uid, p in data.items()
            if include_inactive or p.get("active", True)}


def current_season(data):
    seasons = [int(s) for p in data.values() for s in p["scores"]]
    return max(seasons) if seasons else None


def season_from_args(content, data):
    """Optional season in '!command s97' (or '97'); default: current."""
    season = None
    for w in content.split()[1:]:
        m = re.fullmatch(r"s?(\d{1,3})", w, re.I)
        if m:
            season = int(m.group(1))
    return season if season is not None else current_season(data)


def sorted_scores(p):
    """[(season, score), ...] oldest first."""
    return sorted((int(s), v) for s, v in p["scores"].items())


def best_score(p):
    return max(p["scores"].values(), default=None)


def best_overall(uid):
    """Highest score of this Discord user across both guilds."""
    vals = []
    for g in GUILDS:
        p = g.load().get(uid)
        if p:
            b = best_score(p)
            if b is not None:
                vals.append(b)
    return max(vals, default=None)


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


def find_player(g, data, message, text):
    """Look up a player by @mention, Discord ID or IGN in one guild.
    Returns (discord id, player, error)."""
    if message.mentions:
        uid = str(message.mentions[0].id)
        if uid in data:
            return uid, data[uid], None
        return None, None, (f"That Discord user isn't in the {g.name} "
                            f"roster. Use `{g.prefix}link <discord id> IGN`.")
    if not text:
        return None, None, "Please give a player's IGN (or an @mention)."
    if re.fullmatch(r"\d{15,20}", text) and text in data:
        return text, data[text], None
    uid = name_index(data, True).get(match_key(text))
    if uid is None:
        return None, None, f"I don't know a player called {text}."
    return uid, data[uid], None


def lookup_any(message, text):
    """Search Cosmos first, then Astryx.
    Returns (guild, data, discord id, player, error)."""
    first_err = None
    for g in GUILDS:
        data = g.load()
        uid, p, err = find_player(g, data, message, text)
        if p is not None:
            return g, data, uid, p, None
        if first_err is None:
            first_err = err
    return None, None, None, None, first_err


# ---------- roles (shared by both guilds) ----------

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


# ---------- leaderboards (per guild) ----------

def season_rows(data, season):
    """[(display name, score, discord id)] for everyone with a score."""
    rows = []
    for uid, p in data.items():
        score = p["scores"].get(str(season))
        if score is not None:
            name = NAME_OVERRIDES.get(int(uid), strip_tag(p["ign"]))
            rows.append((name, score, int(uid)))
    return rows


def rank_lines(rows, n):
    top = sorted(rows, key=lambda r: r[1], reverse=True)[:n]
    lines = []
    for i, (name, score, did) in enumerate(top, 1):
        tag = " *(soo low)*" if did in TOO_LOW else ""
        lines.append(f"{i}. {esc(name)} — {score:,}{tag}")
    return lines


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


def build_board(g, data, season):
    prev_season = season - 1
    rows = season_rows(data, season)
    if not rows:
        return f"No {g.name} scores have been submitted for season {season}."

    pairs = []  # (display name, previous season score, this season score)
    for uid, p in data.items():
        cur = p["scores"].get(str(season))
        old = p["scores"].get(str(prev_season))
        if cur is not None and old is not None:
            name = NAME_OVERRIDES.get(int(uid), strip_tag(p["ign"]))
            pairs.append((name, old, cur))

    head = f"{g.tag} – " if g.tag else ""
    text = (f"**{head}Top 10 – Guild Raid Season {season}**\n"
            + "\n".join(rank_lines(rows, 10)))
    span = f"(S{prev_season} → S{season})"
    text += f"\n\n**weeks biggest W** {span}\n"
    text += "\n".join(mover_lines(movers(pairs, True), True))
    text += f"\n\n**weeks biggest fumble** {span}\n"
    text += "\n".join(mover_lines(movers(pairs, False), False))
    return text


async def show_leaderboard(message, g):
    data = g.load()
    season = season_from_args(message.content, data)
    if season is None:
        await message.reply(f"No {g.name} scores have been submitted yet.")
        return
    await send_long(message, build_board(g, data, season))


async def show_weeklylb(message, g):
    data = g.load()
    season = season_from_args(message.content, data)
    if season is None:
        await message.reply(f"No {g.name} scores have been submitted yet.")
        return
    rows = season_rows(data, season)
    if not rows:
        await message.reply(
            f"No {g.name} scores have been submitted for season {season}.")
        return
    head = f"{g.tag} " if g.tag else ""
    text = (f"**{head}Season {season} top 20**\n"
            + "\n".join(rank_lines(rows, 20)))
    await send_long(message, text)


# ---------- player commands (search Cosmos first, then Astryx) ----------

async def show_score(message):
    season, text = split_args(message.content)
    g, data, uid, p, err = lookup_any(message, text)
    if err:
        await message.reply(err)
        return
    if season is None:
        season = current_season(data)
    name = esc(strip_tag(p["ign"]))
    where = f" ({g.name})" if g is not COSMOS else ""
    score = p["scores"].get(str(season)) if season is not None else None
    if score is None:
        await message.reply(
            f"{name}{where} has no score for season {season}.")
        return
    await message.reply(f"**{name}**{where} — season {season}: {score:,}")


async def show_average(message):
    _, text = split_args(message.content)
    g, data, uid, p, err = lookup_any(message, text)
    if err:
        await message.reply(err)
        return
    name = esc(strip_tag(p["ign"]))
    where = f" ({g.name})" if g is not COSMOS else ""
    last = [v for _, v in sorted_scores(p)[-5:]]
    if not last:
        await message.reply(f"{name}{where} has no scores yet.")
        return
    avg = round(sum(last) / len(last))
    s = "" if len(last) == 1 else "s"
    await message.reply(
        f"**{name}**{where}: average over the last {len(last)} "
        f"season{s}: {avg:,}")


async def show_compare(message):
    _, text = split_args(message.content)
    g, data, uid, p, err = lookup_any(message, text)
    if err:
        await message.reply(err)
        return
    name = esc(strip_tag(p["ign"]))
    where = f" ({g.name})" if g is not COSMOS else ""
    items = sorted_scores(p)
    if not items:
        await message.reply(f"{name}{where} has no scores yet.")
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
        f"**{name}**{where} — last {len(lines)} seasons (newest first)\n"
        + "\n".join(lines))


# ---------- management commands (owner only, per guild) ----------

async def handle_list(message, g):
    lines = message.content.strip().split("\n")
    head = lines[0].split()
    season = None
    if len(head) > 1:
        m = re.fullmatch(r"s?(\d{1,3})", head[1], re.I)
        if m:
            season = int(m.group(1))
    if season is None:
        await message.reply(
            f"Usage: `{g.prefix}list sNN` on the first line, then one "
            "`IGN score` per line.")
        return
    entries = lines[1:]
    if len(head) > 2:  # an entry on the same line as the command
        entries.insert(0, " ".join(head[2:]))

    data = g.load()
    idx = name_index(data)
    idx_all = name_index(data, True)
    season_before = current_season(data)

    touched = {}  # discord id -> score (the last line for a player wins)
    unreadable, unknown, left, repeated, odd = [], [], [], [], []
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
            if key in idx_all:
                left.append(ign[:40])
            else:
                close = difflib.get_close_matches(
                    key, list(idx), n=1, cutoff=0.75)
                hint = ""
                if close:
                    shown = strip_tag(data[idx[close[0]]]["ign"])
                    hint = f" (did you mean {shown}?)"
                unknown.append(f"{ign[:40]}{hint}")
            continue
        if uid in touched:
            repeated.append(
                f"{strip_tag(data[uid]['ign'])}: listed twice, "
                f"using {score:,}")
        touched[uid] = score

    # Save the scores, and flag unusual ones
    changed = 0
    for uid, score in touched.items():
        p = data[uid]
        if p["scores"].get(str(season)) != score:
            changed += 1
        p["scores"][str(season)] = score
        prev = p["scores"].get(str(season - 1))
        if prev and prev > 0:
            ratio = score / prev
            if ratio > JUMP_UP or ratio < JUMP_DOWN:
                odd.append(
                    f"{strip_tag(p['ign'])}: {score:,} vs {prev:,} in "
                    f"S{season - 1} ({(ratio - 1) * 100:+.0f}%)")
    if touched:
        g.save(data)

    # Update tier roles (from each player's best score in either guild)
    roles = 0
    cache = {}
    for uid in touched:
        member = await get_member(message.guild, int(uid), cache)
        best = best_overall(uid)
        if member and best is not None:
            status, _ = await update_role(member, best)
            if status == "added":
                roles += 1

    out = []
    if touched:
        s = "" if len(touched) == 1 else "s"
        out.append(f"Saved {len(touched)} {g.name} score{s} for season "
                   f"{season} ({changed} new or changed).")
        out.append(f"Current {g.name} season: {current_season(data)}.")
        if roles:
            out.append(f"Roles updated: {roles}.")
    else:
        out.append("I didn't find any score lines I could save.")
    if season_before is not None and (season > season_before + 1
                                      or season < season_before - 5):
        out.append(f"I'm not sure about the season: {season} is far from "
                   f"the current season ({season_before}). Double-check it.")

    def section(title, items, limit=15):
        if not items:
            return
        text = title + "\n" + "\n".join(f"- {i}" for i in items[:limit])
        if len(items) > limit:
            text += f"\n…and {len(items) - limit} more"
        out.append("\n" + text)

    section("**Not saved: I don't recognise these names.** Typo or new "
            f"member? Use `{g.prefix}link <discord id> IGN` for new "
            "members.", unknown)
    section(f"**Not saved: these members were removed with "
            f"{g.prefix}unlink.**", left)
    section("**Saved, but please double-check these scores:**", odd)
    section("**Listed more than once (I used the last score):**", repeated)
    section("**Couldn't read these lines:**", unreadable, 10)

    await send_long(message, "\n".join(out))


async def delete_season(message, g):
    season, text = split_args(message.content)
    if season is None or (not text and not message.mentions):
        await message.reply(
            f"Usage: `{g.prefix}delete sNN IGN`, e.g. "
            f"`{g.prefix}delete s98 Blask`")
        return
    data = g.load()
    uid, p, err = find_player(g, data, message, text)
    if err:
        await message.reply(err)
        return
    name = esc(strip_tag(p["ign"]))
    if str(season) not in p["scores"]:
        await message.reply(f"{name} has no score for season {season}.")
        return

    del p["scores"][str(season)]
    g.save(data)

    reply = f"Deleted season {season} for {name}."
    member = await get_member(message.guild, int(uid), {})
    if member is not None:
        # Lower the role to the best score still left (in either guild)
        status, role_name = await force_role(member, best_overall(uid))
        if status == "ok" and role_name:
            reply += f" Their role is now {role_name}."
        elif status == "ok":
            reply += " No scores left, so their tier role was removed."
        elif status == "missing":
            reply += f" But the role '{role_name}' doesn't exist."
        else:
            reply += " I don't have permission to change their roles."
    await message.reply(reply)


async def link_player(message, g):
    words = message.content.split()[1:]
    m = (re.fullmatch(r"<@!?(\d{15,20})>|(\d{15,20})", words[0])
         if words else None)
    ign = " ".join(words[1:])
    if not m or not ign:
        await message.reply(f"Usage: `{g.prefix}link <discord id> IGN`")
        return
    uid = m.group(1) or m.group(2)

    data = g.load()
    taken = name_index(data).get(match_key(ign))
    if taken and taken != uid:
        await message.reply(
            f"{esc(strip_tag(ign))} is already used by Discord ID {taken}.")
        return
    p = data.setdefault(uid, {"ign": ign, "active": True, "scores": {}})
    p["ign"] = ign
    p["active"] = True
    g.save(data)

    reply = f"Linked **{esc(strip_tag(ign))}** to Discord ID {uid} ({g.name})."
    member = await get_member(message.guild, int(uid), {})
    if member is None:
        reply += " (That user isn't in this server.)"
    else:
        best = best_overall(uid)
        if best is not None:
            status, role_name = await update_role(member, best)
            reply += role_note(status, role_name)
    await message.reply(reply)


async def unlink_player(message, g):
    _, text = split_args(message.content)
    data = g.load()
    uid, p, err = find_player(g, data, message, text)
    if err:
        await message.reply(err)
        return
    p["active"] = False
    g.save(data)
    await message.reply(
        f"**{esc(strip_tag(p['ign']))}** is no longer in the {g.name} "
        f"roster. Their old scores stay in past seasons. "
        f"`{g.prefix}link <discord id> IGN` adds them back.")


def commands_text(is_owner):
    lines = [
        "**Commands**",
        "`!leaderboard [sNN]` — Cosmos top 10 for a season (default: "
        "current), plus weeks biggest W and weeks biggest fumble (% change "
        "vs the season before), e.g. `!leaderboard s97`",
        "`!weeklylb [sNN]` — Cosmos top 20 for a season (default: current)",
        "`!astleaderboard [sNN]` — the same top 10 and W/fumble for Astryx",
        "`!astweeklylb [sNN]` — Astryx top 20",
        "`!score <player> [sNN]` — a player's score in a season "
        "(default: current), e.g. `!score Blask s98`",
        "`!average <player>` — average of a player's last 5 seasons",
        "`!compare <player>` — a player's last 5 season scores",
        "`!commands` — show this list",
        "<player> can be an IGN or an @mention (Cosmos is searched first, "
        "then Astryx).",
    ]
    if is_owner:
        lines += [
            "",
            "**Owner only** (for Astryx use the same command with `ast` "
            "after the `!`, e.g. `!astlist`)",
            "`!list sNN` + one `IGN score` per line — save or update the "
            "scores for a season",
            "`!delete sNN <player>` — delete a player's score for a season "
            "and lower their role to match what's left",
            "`!link <discord id> IGN` — add a new member, or change an IGN",
            "`!unlink <player>` — remove a member who left",
            "`!resetscores confirm` — delete all saved scores",
        ]
    return "\n".join(lines)


# ---------- commands per guild ----------
# name -> (handler, owner only?). Cosmos uses "!list", Astryx "!astlist".

GUILD_COMMANDS = {
    "leaderboard": (show_leaderboard, False),
    "weeklylb": (show_weeklylb, False),
    "list": (handle_list, True),
    "delete": (delete_season, True),
    "link": (link_player, True),
    "unlink": (unlink_player, True),
}

ROUTES = {}
for _g in GUILDS:
    for _name, (_fn, _owner_only) in GUILD_COMMANDS.items():
        ROUTES[_g.prefix + _name] = (_fn, _g, _owner_only)


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

    # Commands that work for both guilds
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

    # Per-guild commands
    route = ROUTES.get(word)
    if route:
        fn, g, owner_only = route
        if owner_only and not is_owner:
            await message.reply("Only the bot owner can use that command.")
            return
        await fn(message, g)
        return

    # Reset: !resetscores confirm / !astresetscores confirm
    for g in GUILDS:
        if cmd == g.prefix + "resetscores confirm":
            if not is_owner:
                await message.reply(
                    "Only the bot owner can use that command.")
                return
            data = g.load()
            for p in data.values():
                p["scores"] = {}
            g.save(data)
            await message.reply(
                f"All saved {g.name} scores have been deleted.")
            return


client.run(TOKEN)
