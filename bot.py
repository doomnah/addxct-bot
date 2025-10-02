import discord
from discord.ext import commands, tasks
from discord import ui, ButtonStyle, Interaction
from discord.ui import Button, View
import json
import os
import random
import asyncio
import re
from datetime import datetime, timedelta
from collections import defaultdict
from discord.utils import utcnow
import itertools
import difflib
import aiohttp
import pytz
import requests

#keeping bot alive
from keep_alive import keep_alive

keep_alive()

# -------------------------
# CONFIG / INTENTS / BOT
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

def get_prefix(bot, message):
    return ["x", "X"]

bot = commands.Bot(command_prefix=get_prefix, intents=intents, case_insensitive=True)

# -------------------------
# GLOBAL STATE
# -------------------------
purge_channels = {}     # {guild_id: channel_id}
log_channel_id = None   # set via logset
jail_channel_id = None  # set via jailset
jail_role_id = None     # set via jailrole
deleted_messages = {}   # {channel_id: [ {author, avatar, content, attachments, time}, ... ]}
MAX_STORE_PER_CHANNEL = 500
WARN_FILE = "warnings.json"
bot.remove_command("help")
# -------------------------
# HELPERS
# -------------------------
def is_staff(member: discord.Member):
    perms = member.guild_permissions
    return (
        perms.manage_messages
        or perms.kick_members
        or perms.ban_members
        or perms.manage_roles
        or perms.mute_members
    )

def find_member(ctx, user: str):
    """Find member by mention, id, name#discrim or username (first match)."""
    member = None
    if ctx.message.mentions:
        member = ctx.message.mentions[0]
    elif user.isdigit():
        member = ctx.guild.get_member(int(user))
    elif "#" in user:
        parts = user.split("#")
        if len(parts) >= 2:
            name = parts[0]
            discrim = parts[1]
            member = discord.utils.get(ctx.guild.members, name=name, discriminator=discrim)
    else:
        member = discord.utils.find(lambda m: m.name == user, ctx.guild.members)
    return member

def parse_duration(time_str: str):
    """
    Parse time strings like '10s', '5m', '2h', '1d' into timedelta.
    Returns None on invalid input.
    """
    if time_str is None:
        return None
    match = re.match(r"^\s*(\d+)\s*([smhd])\s*$", time_str, flags=re.I)
    if not match:
        return None
    val, unit = match.groups()
    val = int(val)
    unit = unit.lower()
    if unit == "s":
        return timedelta(seconds=val)
    if unit == "m":
        return timedelta(minutes=val)
    if unit == "h":
        return timedelta(hours=val)
    if unit == "d":
        return timedelta(days=val)
    return None

def make_embed(title: str, description: str = None, color: discord.Color = discord.Color.blue()):
    e = discord.Embed(title=title, description=description or "", color=color, timestamp=datetime.utcnow())
    return e

async def send_error(ctx, message: str):
    e = make_embed("❌ Error", message, discord.Color.red())
    await ctx.send(embed=e)

async def send_success(ctx, title: str, message: str = None):
    e = make_embed(title, message, discord.Color.green())
    await ctx.send(embed=e)

async def send_log_embed(guild, title: str, embed: discord.Embed):
    """
    Sends embed to the configured log_channel_id (if set and valid).
    embed should already be prepared.
    """
    global log_channel_id
    if not log_channel_id:
        return
    ch = guild.get_channel(log_channel_id)
    if not ch:
        return
    await ch.send(embed=embed)

# -------------------------
# WARN FILE HELPERS
# -------------------------
def load_warnings():
    if not os.path.exists(WARN_FILE):
        with open(WARN_FILE, "w") as f:
            json.dump({}, f)
    with open(WARN_FILE, "r") as f:
        return json.load(f)

def save_warnings(data):
    with open(WARN_FILE, "w") as f:
        json.dump(data, f, indent=4)

# -------------------------
# EVENTS
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.event
async def on_message_delete(message):
    try:
        if message.author.bot:
            return
        now = datetime.utcnow()
        ch_id = message.channel.id
        entry = {
            "author": str(message.author),
            "author_id": getattr(message.author, "id", None),
            "avatar": getattr(getattr(message.author, "display_avatar", None), "url", None),
            "content": message.content or "",
            "attachments": [a.url for a in message.attachments] if message.attachments else [],
            "time": now.isoformat()
        }
        deleted_messages.setdefault(ch_id, []).append(entry)
        # trim
        if len(deleted_messages[ch_id]) > MAX_STORE_PER_CHANNEL:
            deleted_messages[ch_id].pop(0)
    except Exception as e:
        print("on_message_delete error:", e)

# -------------------------
# MODERATION COMMANDS (embeds for all outputs)
# -------------------------
appeal_links = {}  # {guild_id: link}
# appeal sys
@bot.command(aliases=["xappealset"])
@commands.has_permissions(administrator=True)
async def appealset(ctx, *, link: str):
    appeal_links[ctx.guild.id] = link
    embed = discord.Embed(
        title="✅ Ban Appeal Link Set",
        description=f"The ban appeal link for this server has been set to:\n{link}",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

# ban appeal dm on ban
@bot.event
async def on_member_ban(guild, user):
    # Fetch audit logs to get the ban reason and moderator
    reason = None
    moderator = None
    try:
        async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.ban):
            if entry.target.id == user.id:
                reason = entry.reason
                moderator = entry.user
                break
    except Exception:
        reason = None
        moderator = None

    # Fetch the user object via bot (more reliable for DM)
    try:
        user_obj = await bot.fetch_user(user.id)
    except Exception:
        print(f"Could not fetch user {user.id}")
        return

    # Get the server-specific appeal link
    appeal_link = appeal_links.get(guild.id, "No appeal form set by server admins")

    # Create the DM embed
    embed = discord.Embed(
        title="You were banned from the server",
        description=(
            f"You were banned from **{guild.name}**.\n"
            f"**Reason:** {reason or 'No reason provided'}\n\n"
            f"If you believe this was a mistake or want another chance you can appeal right here:\n"
            f"{appeal_link}"
        ),
        color=discord.Color.red()
    )
    if moderator:
        embed.set_footer(text=f"Banned by: {moderator}")

    # Send DM
    try:
        await user_obj.send(embed=embed)
        print(f"Sent ban DM to {user_obj}")
    except Exception:
        print(f"Could not DM {user_obj}.")

# BAN
@bot.command(aliases=["fuckoff", "doom", "apple"])
@commands.has_permissions(ban_members=True)
async def ban(ctx, *users_and_reason: str):
    if not users_and_reason:
        return await send_error(ctx, "You must specify at least one user.")

    # Separate user arguments and reason arguments
    potential_users = []
    reason_parts = []

    for arg in users_and_reason:
        member = find_member(ctx, arg)
        if member:
            potential_users.append(member)
        else:
            reason_parts.append(arg)

    if not potential_users:
        return await send_error(ctx, "Could not find any valid users to ban.")

    reason = " ".join(reason_parts) if reason_parts else "No reason provided"

    for member in potential_users:
        # 🚫 Prevent banning yourself
        if member.id == ctx.author.id:
            funny = make_embed(
                "😂 Nice Try",
                "You can’t ban yourself, buddy. Sit down 🤡",
                discord.Color.orange()
            )
            await ctx.send(embed=funny)
            continue

        # 🛡️ Staff immunity (unless executor has admin perms)
        if is_staff(member) and not ctx.author.guild_permissions.administrator:
            immune = make_embed(
                "🛡️ Staff Immunity",
                f"{member.mention} is staff and cannot be banned by you.",
                discord.Color.gold()
            )
            await ctx.send(embed=immune)
            continue

        try:
            await member.ban(reason=reason)

            # ✅ Feedback embed
            embed = make_embed(
                "🚫 User Banned",
                f"**{member}** has been banned.\n**Reason:** {reason}",
                discord.Color.red()
            )
            embed.set_author(
                name=str(ctx.author),
                icon_url=getattr(ctx.author.display_avatar, "url", None)
            )
            await ctx.send(embed=embed)

            # 📜 Log embed
            log_embed = make_embed(
                "🚫 Ban Issued",
                f"**User:** {member} (`{member.id}`)\n"
                f"**Moderator:** {ctx.author} (`{ctx.author.id}`)\n"
                f"**Reason:** {reason}",
                discord.Color.dark_red()
            )
            await send_log_embed(ctx.guild, "Ban Log", log_embed)

        except Exception as e:
            await send_error(ctx, f"Failed to ban {member}. Error: {e}")

# ==========================
# KICK (multi-target, immunity, no-self, DMs, logs)
# ==========================
@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, *users_and_reason: str):
    if not users_and_reason:
        return await send_error(ctx, "You must specify at least one user.")

    potential_users = []
    reason_parts = []

    for arg in users_and_reason:
        member = find_member(ctx, arg)
        if member:
            potential_users.append(member)
        else:
            reason_parts.append(arg)

    if not potential_users:
        return await send_error(ctx, "Could not find any valid users to kick.")

    reason = " ".join(reason_parts) if reason_parts else "No reason provided"

    for member in potential_users:
        # prevent self kick
        if member.id == ctx.author.id:
            funny = make_embed(
                "😂 Nice Try",
                "You can’t kick yourself, buddy. Sit down 🤡",
                discord.Color.orange()
            )
            await ctx.send(embed=funny)
            continue

        # staff immunity check (unless executor is admin)
        perms = member.guild_permissions
        is_staff = (
            perms.manage_messages
            or perms.kick_members
            or perms.ban_members
            or perms.manage_roles
            or getattr(perms, "moderate_members", False)
        )
        if is_staff and not ctx.author.guild_permissions.administrator:
            immune = make_embed(
                "🛡️ Staff Immunity",
                f"{member.mention} is staff and cannot be kicked by you.",
                discord.Color.gold()
            )
            await ctx.send(embed=immune)
            continue

        try:
            await member.kick(reason=reason)

            # DM target
            try:
                dm = make_embed(
                    "👢 You were kicked",
                    f"You were kicked from **{ctx.guild.name}**.\n**Reason:** {reason}",
                    discord.Color.orange()
                )
                await member.send(embed=dm)
            except Exception:
                pass

            # Feedback embed in channel
            embed = make_embed(
                "👢 User Kicked",
                f"**{member}** has been kicked.\n**Reason:** {reason}",
                discord.Color.orange()
            )
            embed.set_author(name=str(ctx.author), icon_url=getattr(ctx.author.display_avatar, "url", None))
            await ctx.send(embed=embed)

            # Log embed
            log_embed = make_embed(
                "👢 Kick Issued",
                f"**User:** {member} (`{member.id}`)\n**Moderator:** {ctx.author} (`{ctx.author.id}`)\n**Reason:** {reason}",
                discord.Color.dark_orange()
            )
            await send_log_embed(ctx.guild, "Kick Log", log_embed)

        except Exception as e:
            # continue with the rest, report individual failure
            await send_error(ctx, f"Failed to kick {member}. Error: {e}")

# UNBAN (ID only)
@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user)
        embed = make_embed("✅ User Unbanned", f"**{user}** (`{user.id}`) has been unbanned.", discord.Color.green())
        embed.set_author(name=str(ctx.author), icon_url=getattr(ctx.author.display_avatar, "url", None))
        await ctx.send(embed=embed)
        log_embed = make_embed("✅ Unban", f"**User:** {user} (`{user.id}`)\n**Moderator:** {ctx.author} (`{ctx.author.id}`)", discord.Color.green())
        await send_log_embed(ctx.guild, "Unban Log", log_embed)
    except Exception as e:
        return await send_error(ctx, f"Failed to unban. Error: {e}")

# -------------------------
# MUTE / UNMUTE (uses parse_duration)
# -------------------------
# ==========================
# MUTE (multi-target, time parsing, immunity, no-self, DMs, logs)
# ==========================
@bot.command(aliases=["stfu"])
@commands.has_permissions(moderate_members=True)
async def mute(ctx, *args: str):
    if not args:
        return await send_error(ctx, "You must specify at least one user (and optional time and reason).")

    potential_users = []
    reason_parts = []
    duration_td = None
    duration_str = None

    for arg in args:
        # try parse time first (only first valid time token is used)
        if duration_td is None:
            try_td = None
            try:
                try_td = parse_duration(arg)  # your parse_duration -> returns timedelta or None
            except Exception:
                try_td = None
            if try_td:
                duration_td = try_td
                duration_str = arg
                continue

        member = find_member(ctx, arg)
        if member:
            potential_users.append(member)
        else:
            reason_parts.append(arg)

    if not potential_users:
        return await send_error(ctx, "Could not find any valid users to mute.")

    reason = " ".join(reason_parts) if reason_parts else "No reason provided"

    for member in potential_users:
        # prevent self-mute
        if member.id == ctx.author.id:
            funny = make_embed(
                "😂 Nice Try",
                "You can’t mute yourself, buddy. Sit down 🤡",
                discord.Color.orange()
            )
            await ctx.send(embed=funny)
            continue

        # staff immunity
        perms = member.guild_permissions
        is_staff = (
            perms.manage_messages
            or perms.kick_members
            or perms.ban_members
            or perms.manage_roles
            or getattr(perms, "moderate_members", False)
        )
        if is_staff and not ctx.author.guild_permissions.administrator:
            immune = make_embed(
                "🛡️ Staff Immunity",
                f"{member.mention} is staff and cannot be muted by you.",
                discord.Color.gold()
            )
            await ctx.send(embed=immune)
            continue

        try:
            if duration_td:
                until = discord.utils.utcnow() + duration_td
                await member.timeout(until, reason=reason)
                # channel feedback
                embed = make_embed(
                    "🔇 User Muted",
                    f"**{member}** muted for **{duration_str}**.\n**Reason:** {reason}",
                    discord.Color.gold()
                )
                await ctx.send(embed=embed)

                # DM user
                try:
                    dm = make_embed(
                        "🔇 You were muted",
                        f"You were muted in **{ctx.guild.name}** for **{duration_str}**.\n**Reason:** {reason}",
                        discord.Color.gold()
                    )
                    await member.send(embed=dm)
                except:
                    pass

                # log
                log_embed = make_embed(
                    "🔇 Mute Issued",
                    f"**User:** {member} (`{member.id}`)\n**Moderator:** {ctx.author} (`{ctx.author.id}`)\n**Duration:** {duration_str}\n**Reason:** {reason}",
                    discord.Color.gold()
                )
                await send_log_embed(ctx.guild, "Mute Log", log_embed)

            else:
                # indefinite mute (timeout with None unsets? your original used None for indefinite)
                await member.timeout(None, reason=reason)
                embed = make_embed(
                    "🔇 User Muted (Indefinite)",
                    f"**{member}** muted indefinitely.\n**Reason:** {reason}",
                    discord.Color.gold()
                )
                await ctx.send(embed=embed)

                try:
                    dm = make_embed(
                        "🔇 You were muted",
                        f"You were muted indefinitely in **{ctx.guild.name}**.\n**Reason:** {reason}",
                        discord.Color.gold()
                    )
                    await member.send(embed=dm)
                except:
                    pass

                log_embed = make_embed(
                    "🔇 Mute Issued",
                    f"**User:** {member} (`{member.id}`)\n**Moderator:** {ctx.author} (`{ctx.author.id}`)\n**Duration:** Infinite\n**Reason:** {reason}",
                    discord.Color.gold()
                )
                await send_log_embed(ctx.guild, "Mute Log", log_embed)

        except Exception as e:
            await send_error(ctx, f"Failed to mute {member}. Error: {e}")


# ==========================
# UNMUTE (multi-target, immunity, no-self, DMs, logs)
# ==========================
@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, *users_and_reason: str):
    if not users_and_reason:
        return await send_error(ctx, "You must specify at least one user.")

    potential_users = []
    reason_parts = []
    for arg in users_and_reason:
        member = find_member(ctx, arg)
        if member:
            potential_users.append(member)
        else:
            reason_parts.append(arg)

    if not potential_users:
        return await send_error(ctx, "Could not find any valid users to unmute.")

    reason = " ".join(reason_parts) if reason_parts else "No reason provided"

    for member in potential_users:
        # prevent self-unmute
        if member.id == ctx.author.id:
            funny = make_embed(
                "😂 Nice Try",
                "You can’t unmute yourself, buddy. Sit down 🤡",
                discord.Color.orange()
            )
            await ctx.send(embed=funny)
            continue

        # staff immunity
        perms = member.guild_permissions
        is_staff = (
            perms.manage_messages
            or perms.kick_members
            or perms.ban_members
            or perms.manage_roles
            or getattr(perms, "moderate_members", False)
        )
        if is_staff and not ctx.author.guild_permissions.administrator:
            immune = make_embed(
                "🛡️ Staff Immunity",
                f"{member.mention} is staff and cannot be unmuted by you.",
                discord.Color.gold()
            )
            await ctx.send(embed=immune)
            continue

        try:
            await member.timeout(None, reason=reason)
            # DM target
            try:
                dm = make_embed(
                    "✅ You were unmuted",
                    f"You were unmuted in **{ctx.guild.name}**.\n**Reason:** {reason}",
                    discord.Color.green()
                )
                await member.send(embed=dm)
            except:
                pass

            # feedback embed
            embed = make_embed(
                "✅ User Unmuted",
                f"**{member}** has been unmuted.\n**Reason:** {reason}",
                discord.Color.green()
            )
            embed.set_author(name=str(ctx.author), icon_url=getattr(ctx.author.display_avatar, "url", None))
            await ctx.send(embed=embed)

            # log
            log_embed = make_embed(
                "✅ Unmute",
                f"**User:** {member} (`{member.id}`)\n**Moderator:** {ctx.author} (`{ctx.author.id}`)\n**Reason:** {reason}",
                discord.Color.green()
            )
            await send_log_embed(ctx.guild, "Unmute Log", log_embed)

        except Exception as e:
            await send_error(ctx, f"Failed to unmute {member}. Error: {e}")

# -------------------------
# PURGE & PURGESET (embedded responses & logs)
# -------------------------
@bot.command()
@commands.has_permissions(manage_messages=True)
async def purgeset(ctx, channel: discord.TextChannel):
    purge_channels[ctx.guild.id] = channel.id
    embed = make_embed("✅ Purge Channel Set", f"Purge log channel set to {channel.mention}", discord.Color.green())
    embed.set_author(name=str(ctx.author), icon_url=getattr(ctx.author.display_avatar, "url", None))
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, *args):
    log_channel_id_local = purge_channels.get(ctx.guild.id)
    if not log_channel_id_local:
        return await send_error(ctx, "No purge log channel set. Use `xpurgeset #channel` first.")
    log_channel = ctx.guild.get_channel(log_channel_id_local)

    # Case 1: purge <amount>
    if len(args) == 1 and args[0].isdigit():
        amount = int(args[0])
        messages = await ctx.channel.purge(limit=amount)
        embed = make_embed("🧹 Purge", f"Purged {len(messages)} messages in {ctx.channel.mention}", discord.Color.orange())
        await ctx.send(embed=embed, delete_after=5)
        # Log them (as embed)
        log_text = "\n".join([f"{m.author}: {m.content}" for m in messages if m.content])
        if log_text:
            log_embed = make_embed("📝 Purge Log", f"Channel: {ctx.channel.mention}\n\n```\n{log_text}\n```", discord.Color.orange())
            await log_channel.send(embed=log_embed)

    # Case 2: purge <user> <amount>
    elif len(args) == 2 and args[1].isdigit():
        user = find_member(ctx, args[0])
        if not user:
            return await send_error(ctx, "Could not find that user.")
        amount = int(args[1])
        messages = []
        async for m in ctx.channel.history(limit=1000):
            if m.author == user:
                messages.append(m)
            if len(messages) >= amount:
                break
        if not messages:
            return await send_error(ctx, "No messages found from that user.")
        await ctx.channel.delete_messages(messages)
        embed = make_embed("🧹 Purge", f"Purged {len(messages)} messages from {user.mention} in {ctx.channel.mention}", discord.Color.orange())
        await ctx.send(embed=embed, delete_after=5)
        log_text = "\n".join([f"{m.author}: {m.content}" for m in messages if m.content])
        if log_text:
            log_embed = make_embed("📝 Purge Log", f"Channel: {ctx.channel.mention}\nUser: {user}\n\n```\n{log_text}\n```", discord.Color.orange())
            await log_channel.send(embed=log_embed)
    else:
        return await send_error(ctx, "Usage: `xpurge <amount>` or `xpurge <user> <amount>`")

# -------------------------
# LOGSET (global mod-log channel for many commands)
# -------------------------
@bot.command()
@commands.has_permissions(administrator=True)
async def logset(ctx, channel: discord.TextChannel):
    global log_channel_id
    log_channel_id = channel.id
    embed = make_embed("✅ Log Channel Set", f"All moderation logs will now be sent to {channel.mention}", discord.Color.green())
    embed.set_author(name=str(ctx.author), icon_url=getattr(ctx.author.display_avatar, "url", None))
    await ctx.send(embed=embed)

# -------------------------
# SNIPE / XS (embeds + attachments support)
# -------------------------
def _parse_period(period_str: str) -> timedelta:
    if not period_str:
        return timedelta(hours=2)
    try:
        num = int(period_str[:-1])
        unit = period_str[-1].lower()
    except:
        return timedelta(hours=2)
    if unit == "s":
        return timedelta(seconds=num)
    if unit == "m":
        return timedelta(minutes=num)
    if unit == "h":
        return timedelta(hours=num)
    if unit == "d":
        return timedelta(days=num)
    return timedelta(hours=2)

@bot.command(name="s", aliases=["snipe", "xs"])
@commands.has_permissions(manage_messages=True)
async def xs(ctx, period: str = "2h"):
    ch_id = ctx.channel.id
    if ch_id not in deleted_messages or not deleted_messages[ch_id]:
        return await send_error(ctx, "No deleted messages recorded in this channel.")
    cutoff = datetime.utcnow() - _parse_period(period)
    msgs = [m for m in deleted_messages[ch_id] if datetime.fromisoformat(m["time"]) >= cutoff]
    if not msgs:
        return await send_error(ctx, "No deleted messages in that period.")
    if not log_channel_id:
        return await send_error(ctx, "Mod log channel not set. Use `logset` first.")
    log_ch = ctx.guild.get_channel(log_channel_id)
    if not log_ch:
        return await send_error(ctx, "Could not find the mod log channel.")
    to_send = msgs[-50:]
    sent_count = 0
    for m in to_send:
        try:
            ts = datetime.fromisoformat(m["time"])
        except:
            ts = None
        embed = discord.Embed(title="🕵️ Deleted Message", description=m["content"] or "*[no content]*", timestamp=ts, color=discord.Color.dark_red())
        if m.get("avatar"):
            try:
                embed.set_author(name=m["author"], icon_url=m["avatar"])
            except:
                embed.set_author(name=m["author"])
        else:
            embed.set_author(name=m["author"])
        embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
        if m.get("attachments"):
            urls = m["attachments"]
            if len(urls) == 1:
                embed.add_field(name="Attachment", value=urls[0], inline=False)
                try:
                    embed.set_image(url=urls[0])
                except:
                    pass
            else:
                embed.add_field(name="Attachments", value="\n".join(urls), inline=False)
                try:
                    embed.set_image(url=urls[0])
                except:
                    pass
        await log_ch.send(embed=embed)
        sent_count += 1
    await send_success(ctx, "🕵️ Sniped Messages Sent", f"Sent {sent_count} deleted messages from the last {period} to the mod logs.")

# -------------------------
# WARN SYSTEM (full embed styling, same logic)
# -------------------------
@bot.command()
@commands.has_permissions(manage_messages=True)
async def warn(ctx, *users_and_reason: str):
    if not users_and_reason:
        return await send_error(ctx, "You must specify at least one user.")

    warnings = load_warnings()
    guild_id = str(ctx.guild.id)

    potential_users = []
    reason_parts = []
    for arg in users_and_reason:
        member = find_member(ctx, arg)
        if member:
            potential_users.append(member)
        else:
            reason_parts.append(arg)

    if not potential_users:
        return await send_error(ctx, "Could not find any valid users to warn.")

    reason = " ".join(reason_parts) if reason_parts else "No reason provided"

    for member in potential_users:
        # no self-warn
        if member.id == ctx.author.id:
            funny = make_embed(
                "😂 Nice Try",
                "You can’t warn yourself, buddy. Sit down 🤡",
                discord.Color.orange()
            )
            await ctx.send(embed=funny)
            continue

        # staff immunity
        perms = member.guild_permissions
        is_staff = (
            perms.manage_messages
            or perms.kick_members
            or perms.ban_members
            or perms.manage_roles
            or getattr(perms, "moderate_members", False)
        )
        if is_staff and not ctx.author.guild_permissions.administrator:
            immune = make_embed(
                "🛡️ Staff Immunity",
                f"{member.mention} is staff and cannot be warned by you.",
                discord.Color.gold()
            )
            await ctx.send(embed=immune)
            continue

        user_id = str(member.id)
        if guild_id not in warnings:
            warnings[guild_id] = {}
        if user_id not in warnings[guild_id]:
            warnings[guild_id][user_id] = []

        case_id = random.randint(1000, 9999)
        warnings[guild_id][user_id].append({
            "case_id": case_id,
            "reason": reason,
            "moderator": str(ctx.author),
            "time": datetime.utcnow().isoformat()
        })
        save_warnings(warnings)

        # server embed
        embed = make_embed(
            "⚠️ Warn Issued",
            f"User: {member} (`{member.id}`)\nModerator: {ctx.author}\nCase ID: `{case_id}`\nReason: {reason}\nTotal warns: {len(warnings[guild_id][user_id])}",
            discord.Color.orange()
        )
        await ctx.send(embed=embed)
        await send_log_embed(ctx.guild, "Warn Issued", embed)

        # DM the user
        try:
            dm_embed = make_embed(
                "⚠️ You Have Been Warned",
                f"You were warned in **{ctx.guild.name}**.\n\n**Moderator:** {ctx.author}\n**Reason:** {reason}\n**Case ID:** `{case_id}`\n**Total Warnings:** {len(warnings[guild_id][user_id])}",
                discord.Color.orange()
            )
            await member.send(embed=dm_embed)
        except Exception:
            await ctx.send(f"⚠️ Could not DM {member.mention}. They might have DMs disabled.")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def warnings(ctx, user: discord.Member):
    warnings = load_warnings()
    guild_id = str(ctx.guild.id)
    user_id = str(user.id)

    if guild_id not in warnings or user_id not in warnings[guild_id] or len(warnings[guild_id][user_id]) == 0:
        embed = make_embed("📋 Warnings", f"{user.mention} has no warnings.", discord.Color.green())
        return await ctx.send(embed=embed)

    warn_list = warnings[guild_id][user_id]
    embed = make_embed(f"📋 Warnings for {user}", None, discord.Color.orange())
    for w in warn_list:
        embed.add_field(name=f"Case `{w['case_id']}`", value=f"**Reason:** {w['reason']}\n**Moderator:** {w['moderator']}\n**Time:** {w.get('time','N/A')}", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clearwarns(ctx, user: discord.Member):
    warnings = load_warnings()
    guild_id = str(ctx.guild.id)
    user_id = str(user.id)

    if guild_id in warnings and user_id in warnings[guild_id]:
        warnings[guild_id][user_id] = []
        save_warnings(warnings)
        embed = make_embed("🗑️ Cleared All Warnings", f"All warnings cleared for {user.mention}.", discord.Color.green())
        await ctx.send(embed=embed)
        await send_log_embed(ctx.guild, "Warnings Cleared", embed)
    else:
        embed = make_embed("ℹ️ No Warnings", f"{user.mention} has no warnings.", discord.Color.blue())
        await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clearwarn(ctx, user: discord.Member, case_id: int):
    warnings = load_warnings()
    guild_id = str(ctx.guild.id)
    user_id = str(user.id)

    if guild_id in warnings and user_id in warnings[guild_id]:
        warn_list = warnings[guild_id][user_id]
        new_list = [w for w in warn_list if w["case_id"] != case_id]
        if len(warn_list) == len(new_list):
            return await send_error(ctx, f"No warning found with Case ID `{case_id}` for {user.mention}.")
        warnings[guild_id][user_id] = new_list
        save_warnings(warnings)
        embed = make_embed("🗑️ Cleared Warning", f"Cleared warning `{case_id}` for {user.mention}.", discord.Color.green())
        await ctx.send(embed=embed)
        await send_log_embed(ctx.guild, "Warning Cleared", embed)
    else:
        return await send_error(ctx, f"{user.mention} has no warnings.")

# -------------------------
# JAIL SYSTEM (embeds, same logic; uses parse_duration)
# -------------------------
@bot.command()
@commands.has_permissions(administrator=True)
async def jailset(ctx, channel: discord.TextChannel):
    global jail_channel_id
    jail_channel_id = channel.id
    embed = make_embed("✅ Jail Channel Set", f"Jail channel set to {channel.mention}", discord.Color.green())
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def jailrole(ctx, role_id: int):
    global jail_role_id
    jail_role_id = role_id
    # show role mention
    embed = make_embed("✅ Jail Role Set", f"Jail role set to <@&{role_id}>", discord.Color.green())
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(manage_roles=True)
async def jail(ctx, user: discord.Member, time: str = None, *, reason: str = "No reason provided"):
    global jail_channel_id, jail_role_id

    if not jail_channel_id or not jail_role_id:
        return await send_error(ctx, "You must set both a jail channel (`xjailset`) and a jail role (`xjailrole`) first.")

    jail_role = ctx.guild.get_role(jail_role_id)
    jail_channel = ctx.guild.get_channel(jail_channel_id)

    if not jail_role or not jail_channel:
        return await send_error(ctx, "Jail role or channel is invalid. Please set them again.")

    # apply role
    await user.add_roles(jail_role, reason=reason)

    # restrict permissions: text & voice (we preserve existing logic: iterate channels)
    for channel in ctx.guild.channels:
        # text channels: control send_messages/read_messages
        if isinstance(channel, discord.TextChannel):
            if channel.id == jail_channel_id:
                await channel.set_permissions(jail_role, send_messages=True, read_messages=True)
            else:
                await channel.set_permissions(jail_role, send_messages=False, read_messages=False)
        # voice channels: control connect/speak
        elif isinstance(channel, discord.VoiceChannel):
            if channel.id == jail_channel_id:
                await channel.set_permissions(jail_role, connect=True, speak=True)
            else:
                await channel.set_permissions(jail_role, connect=False, speak=False)

    duration_td = parse_duration(time) if time else None
    until_display = "Infinite"
    if duration_td:
        until_display = time

    embed = make_embed("🚨 User Jailed", f"**{user}** has been jailed.\n**Reason:** {reason}\n**Duration:** {until_display}", discord.Color.dark_red())
    embed.set_author(name=str(ctx.author), icon_url=getattr(ctx.author.display_avatar, "url", None))
    await ctx.send(embed=embed)
    # log
    log_embed = make_embed("🚨 Jail Issued", f"User: {user} (`{user.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}\nDuration: {until_display}", discord.Color.dark_red())
    await send_log_embed(ctx.guild, "Jail Log", log_embed)

    # auto unjail
    if duration_td:
        await asyncio.sleep(duration_td.total_seconds())
        # attempt to remove role if still present
        try:
            if jail_role in user.roles:
                await user.remove_roles(jail_role, reason="Jail time expired")
                embed_unjail = make_embed("✅ Auto Unjailed", f"{user.mention} has been unjailed (time expired).", discord.Color.green())
                await ctx.send(embed=embed_unjail)
                await send_log_embed(ctx.guild, "Auto Unjail", embed_unjail)
        except Exception as e:
            print("auto unjail error:", e)

@bot.command()
@commands.has_permissions(manage_roles=True)
async def unjail(ctx, user: discord.Member, *, reason: str = "No reason provided"):
    jail_role = ctx.guild.get_role(jail_role_id) if jail_role_id else None
    if not jail_role:
        return await send_error(ctx, "Jail role is not set or invalid.")
    if jail_role not in user.roles:
        return await send_error(ctx, f"{user.mention} is not jailed.")
    try:
        await user.remove_roles(jail_role, reason=reason)
        embed = make_embed("✅ User Unjailed", f"{user.mention} has been released from jail.\n**Reason:** {reason}", discord.Color.green())
        await ctx.send(embed=embed)
        await send_log_embed(ctx.guild, "Unjail", embed)
    except Exception as e:
        return await send_error(ctx, f"Failed to unjail. Error: {e}")

# -------------------------
# ROLE TOGGLE COMMAND (xrole / xr) - embed outputs
# -------------------------
@bot.command(aliases=["r", "xr"])
@commands.has_permissions(manage_roles=True)
async def role(ctx, user: str, *, role: str):
    # Find member by ID or partial name
    member_obj = None
    if user.isdigit():
        member_obj = ctx.guild.get_member(int(user))
    else:
        user_lower = user.lower()
        for m in ctx.guild.members:
            if user_lower in m.name.lower() or user_lower in getattr(m, "display_name", "").lower():
                member_obj = m
                break

    if not member_obj:
        return await send_error(ctx, "User not found. Use the user ID or part of their username/display name.")

    # Find role by ID or partial name
    role_obj = None
    if role.isdigit():
        role_obj = ctx.guild.get_role(int(role))
    else:
        role_lower = role.lower()
        for r in ctx.guild.roles:
            if role_lower in r.name.lower():
                role_obj = r
                break

    if not role_obj:
        return await send_error(ctx, "Role not found. Use the role ID, exact name, or part of the role name.")

    # Check hierarchy
    if role_obj >= ctx.guild.me.top_role:
        return await send_error(ctx, "I cannot manage that role because it is higher than or equal to my top role.")

    try:
        if role_obj in member_obj.roles:
            await member_obj.remove_roles(role_obj)
            embed = make_embed("❌ Role Removed", f"Removed **{role_obj.name}** from {member_obj.mention}.", discord.Color.red())
            await ctx.send(embed=embed)
            await send_log_embed(ctx.guild, "Role Removed", embed)
        else:
            await member_obj.add_roles(role_obj)
            embed = make_embed("✅ Role Added", f"Added **{role_obj.name}** to {member_obj.mention}.", discord.Color.green())
            await ctx.send(embed=embed)
            await send_log_embed(ctx.guild, "Role Added", embed)
    except Exception as e:
        return await send_error(ctx, f"Failed to toggle role. Error: {e}")
# say cmd
# Custom dropdown for choosing colors
class ColorSelect(discord.ui.Select):
    def __init__(self, message: str, author: discord.Member):
        self.message = message
        self.author = author

        options = [
            discord.SelectOption(label="Red", value="red", emoji="🟥"),
            discord.SelectOption(label="Blue", value="blue", emoji="🟦"),
            discord.SelectOption(label="Green", value="green", emoji="🟩"),
            discord.SelectOption(label="Yellow", value="yellow", emoji="🟨"),
            discord.SelectOption(label="Purple", value="purple", emoji="🟪"),
            discord.SelectOption(label="Orange", value="orange", emoji="🟧"),
            discord.SelectOption(label="Pink", value="pink", emoji="🌸"),
            discord.SelectOption(label="Black", value="black", emoji="⬛"),
            discord.SelectOption(label="White", value="white", emoji="⬜"),
            discord.SelectOption(label="Cyan", value="cyan", emoji="🟦"),
            discord.SelectOption(label="Teal", value="teal", emoji="🌊"),
            discord.SelectOption(label="Grey", value="grey", emoji="⚪"),
            discord.SelectOption(label="Brown", value="brown", emoji="🟫"),
            discord.SelectOption(label="Gold", value="gold", emoji="🏅"),
            discord.SelectOption(label="Silver", value="silver", emoji="💿"),
            discord.SelectOption(label="Default", value="default", emoji="🎨"),
        ]

        super().__init__(placeholder="Choose a color...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        # Restrict usage to the command invoker
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Only the command invoker can use this.", ephemeral=True)
            return

        # Map color names to discord.Color
        colors = {
            "red": discord.Color.red(),
            "blue": discord.Color.blue(),
            "green": discord.Color.green(),
            "yellow": discord.Color.gold(),
            "purple": discord.Color.purple(),
            "orange": discord.Color.orange(),
            "pink": discord.Color.magenta(),
            "black": discord.Color.dark_theme(),   # closest to black
            "white": discord.Color.light_gray(),   # lightest available
            "cyan": discord.Color.teal(),
            "teal": discord.Color.teal(),
            "grey": discord.Color.dark_gray(),
            "brown": discord.Color.dark_orange(),
            "gold": discord.Color.gold(),
            "silver": discord.Color.light_gray(),
            "default": discord.Color.default(),
        }

        chosen_color = colors[self.values[0]]
        embed = discord.Embed(description=self.message, color=chosen_color)

        # ✅ Send as standalone message (not a reply)
        await interaction.channel.send(embed=embed, reference=None, mention_author=False)

        # Delete the dropdown message immediately after selection
        try:
            await interaction.message.delete()
        except discord.Forbidden:
            pass

        # Disable dropdown (safety, in case deletion fails)
        self.disabled = True
        await interaction.message.edit(view=self.view)


class ColorSelectView(discord.ui.View):
    def __init__(self, message: str, author: discord.Member, timeout=60):
        super().__init__(timeout=timeout)
        self.add_item(ColorSelect(message, author))


@bot.command()
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, message: str):
    """Make the bot say something in an embed with customizable color via dropdown."""

    try:
        await ctx.message.delete()  # delete the command message
    except discord.Forbidden:
        pass

    view = ColorSelectView(message, ctx.author)
    await ctx.send("🎨 Choose a color for your embed:", view=view)

# suicide cmd
@bot.command()
async def suicide(ctx):
    try:
        await ctx.message.delete()  # delete the user's command
    except discord.Forbidden:
        pass  # if the bot doesn't have permission to delete, just ignore

    user1 = ctx.author

    embed = discord.Embed(
        description=f"💀 Uh oh... looks like **{user1.display_name}** has committed suicide. May they rest in peace!",
        color=discord.Color.red()
    )
    await ctx.send(embed=embed)

#dm cmd
@bot.command()
@commands.has_permissions(administrator=True)
async def dm(ctx, user: str, *, content: str):
    await ctx.message.delete()  # delete the command message

    member = find_member(ctx, user)  # works with mention, ID, or username
    if not member:
        return await ctx.send("❌ Failed to dm (User not found)", delete_after=5)

    try:
        embed = discord.Embed(
            title="📩 You Have a New Message",
            description=content,
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"Sent from {ctx.guild.name}")
        await member.send(embed=embed)

        await ctx.send("✅ User dmed", delete_after=5)

    except Exception as e:
        await ctx.send("❌ Failed to dm", delete_after=5)

#help
@bot.command()
async def help(ctx):
    embed = discord.Embed(
        title="📜 Shitchan Help Menu",
        description="Here are all the commands you can use with Shitchan. Commands are organized by category for easier navigation.",
        color=discord.Color.blurple()
    )
    
    # Moderation commands
    embed.add_field(
        name="🛠️ Moderation",
        value=(
            "`xban [user] [reason]` - Ban a user\n"
            "`xunban [user]` - Unban a user\n"
            "`xkick [user] [reason]` - Kick a user\n"
            "`xmute [user] [time] [reason]` - Mute a user\n"
            "`xunmute [user]` - Unmute a user\n"
            "`xwarn [user] [reason]` - Warn a user\n"
            "`xrole [user] [role]` - Toggle role for user\n"
            "`xpurge [user] [number]` - Purges a user's number of messages\n"
            "`xpurgeset [channel]` - Sets a channel to log purged messages\n"
            "`xlogset [channel]` - Sets a log channel\n"
            "`xjail [user] [period] [reason]` - Jails a user for a period\n"
            "`xclearwarn [user] [case number]` - Clears a warning\n"
            "`xwarnings [user]` - Checks warnings of user"
        ),
        inline=False
    )

    # Utility commands
    embed.add_field(
        name="🔧 Utility",
        value=(
            "`xnuke` - Nukes a channel\n"
            "`xrevivechat` - Auto revive chat\n"
            "`xs [period]` - Snipe last deleted messages\n"
            "`xhelp` - Show this help menu\n"
            "`xinfo` - Get info about a user\n"
            "`xsuggest [idea]` - Suggest an idea for the bot to get it dmed to the creator!\n"
            "`xafk [reason]` - Makes people know when ur afk\n"
            "`xtz` - Displays your timezone\n"
            "`xremind [period] [what to remind of]` - Reminds you of something\n"
            "`xtimer [time]` - Counts down a set time\n"
            "`xserverstats` - Stats of server"
        ),
        inline=False
    )

    # Fun commands
    embed.add_field(
        name="🎉 Fun",
        value=(
            "`xsay [message]` - Make the bot say something\n"
            "`xjoke` - Get a random joke\n"
            "`xmeme` - Sends a funny meme"
        ),
        inline=False
    )

    embed.set_footer(
        text=f"Requested by {ctx.author}", 
        icon_url=getattr(ctx.author.display_avatar, "url", None)
    )

    embed.set_thumbnail(url=bot.user.display_avatar.url)
    await ctx.send(embed=embed)
# ====== xinfo command ======
@bot.command()
async def info(ctx, user: discord.Member = None):
    """Shows info about a user or the author if no user is mentioned."""
    user = user or ctx.author
    embed = discord.Embed(
        title=f"ℹ️ User Info - {user}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="ID", value=user.id, inline=True)
    embed.add_field(name="Username", value=f"{user}", inline=True)
    embed.add_field(name="Bot?", value=user.bot, inline=True)
    embed.add_field(name="Account Created", value=user.created_at.strftime("%d %b %Y"), inline=False)
    embed.add_field(name="Joined Server", value=user.joined_at.strftime("%d %b %Y"), inline=False)
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)


# ====== xjoke command ======
@bot.command()
async def joke(ctx):
    """Sends a random joke."""
    jokes = [
    "Why don't scientists trust atoms? Because they make up everything!",
    "I told my computer I needed a break, and now it won't stop sending me Kit-Kats.",
    "Why did the scarecrow win an award? Because he was outstanding in his field!",
    "Parallel lines have so much in common… it’s a shame they’ll never meet.",
    "Why did the math book look sad? Because it had too many problems.",
    "Why do bees have sticky hair? Because they use honeycombs!",
    "I would tell you a joke about infinity… but it doesn’t have an end.",
    "Why don’t skeletons fight each other? They don’t have the guts.",
    "What do you call fake spaghetti? An impasta!",
    "Why can’t your nose be 12 inches long? Because then it would be a foot.",
    "What’s orange and sounds like a parrot? A carrot!",
    "Why did the tomato turn red? Because it saw the salad dressing!",
    "Why did the computer go to the doctor? It caught a virus!",
    "I told my computer a joke… but it didn’t find it very funny.",
    "Why don’t eggs tell jokes? They’d crack each other up.",
    "Why did the bicycle fall over? Because it was two-tired!",
    "Why did the golfer bring an extra pair of pants? In case he got a hole in one.",
    "What do you call cheese that isn’t yours? Nacho cheese!",
    "Why was the math lecture so long? The professor kept going off on a tangent.",
    "Why did the chicken join a band? Because it had the drumsticks!",
    "Why did the coffee file a police report? It got mugged.",
    "Why did the cookie go to the hospital? Because it felt crummy.",
    "What do you call a snowman with a six-pack? An abdominal snowman.",
    "Why don’t some couples go to the gym? Because some relationships don’t work out.",
    "Why did the belt go to jail? Because it held up a pair of pants!",
    "Why did the computer cross the road? To get to the other website.",
    "Why was the math book sad? Too many problems.",
    "Why did the stadium get hot after the game? All of the fans left.",
    "Why do cows have hooves instead of feet? Because they lactose.",
    "Why did the skeleton go to the party alone? He had no body to go with.",
    "Why don’t scientists trust stairs? They’re always up to something.",
    "Why did the tomato turn to the dark side? Because it was ketchup-ing up.",
    "Why did the music teacher go to jail? Because she got caught with the treble.",
    "Why do seagulls fly over the ocean? Because if they flew over the bay, they’d be bagels.",
    "Why did the pencil get detention? It was too sharp.",
    "Why was the broom late? It overswept!",
    "What do you call a factory that makes okay products? A satisfactory.",
    "Why did the computer go to art school? It wanted to learn to draw its graphics.",
    "What do you call a bear with no teeth? A gummy bear!",
    "Why was the robot so bad at soccer? It kept kicking up sparks.",
    "Why did the calendar go to therapy? Its days were numbered.",
    "Why did the cell phone go to school? It wanted to be smarter.",
    "What did the zero say to the eight? Nice belt!",
    "Why did the lion eat the tightrope walker? He wanted a well-balanced meal.",
    "Why did the banana go to the doctor? It wasn’t peeling well.",
    "Why did the man put his money in the freezer? He wanted cold hard cash!",
    "Why did the skeleton stay in bed all day? His heart wasn’t in it.",
    "Why was the stadium so cool? It was filled with fans.",
    "Why do ducks have feathers? To cover their butt quacks.",
    "Why did the grape stop in the middle of the road? It ran out of juice.",
    "Why did the mushroom go to the party alone? Because he’s a fungi.",
    "What did the ocean say to the beach? Nothing, it just waved.",
    "Why did the hipster burn his tongue? He drank his coffee before it was cool.",
    "Why did the scarecrow become a motivational speaker? He was outstanding in his field.",
    "Why did the orange stop? It ran out of juice.",
    "Why do elephants never use computers? They’re afraid of the mouse.",
    "Why did the frog take the bus to work? His car got toad away.",
    "Why did the man sit on the clock? He wanted to be on time.",
    "Why did the student eat his homework? Because the teacher said it was a piece of cake.",
    "Why did the fish blush? Because it saw the ocean’s bottom.",
    "Why did the cat sit on the computer? To keep an eye on the mouse!",
    "Why did the computer go to therapy? Too many bytes of stress.",
    "Why did the tomato blush? It saw the salad dressing.",
    "Why don’t scientists trust atoms? They make up everything.",
    "Why did the chicken sit on the drum? To lay it on the beat.",
    "Why did the golfer bring two pairs of pants? In case he got a hole in one.",
    "Why do bicycles fall over? Because they are two-tired.",
    "Why did the calendar apply for a job? It wanted to work its days off.",
    "Why did the cookie go to the doctor? He felt crummy.",
    "Why did the computer go to art class? To improve its graphics.",
    "Why did the ghost go to school? He wanted to be a smartie.",
    "Why did the tomato turn red? Because it saw the salad dressing!",
    "Why did the skeleton go to the party alone? He had no body to go with.",
    "Why did the man run around his bed? He was trying to catch up on sleep.",
    "Why did the chicken cross the playground? To get to the other slide.",
    "Why did the stadium get hot? All of the fans left.",
    "Why did the mushroom get invited to the party? Because he was a fungi.",
    "Why did the computer go on a diet? Too many bytes.",
    "Why did the coffee file a police report? It got mugged.",
    "Why did the music teacher need a ladder? To reach the high notes.",
    "Why did the robot go on vacation? To recharge its batteries.",
    "Why was the math book sad? It had too many problems.",
    "Why did the duck go to therapy? He had quack issues.",
    "Why did the bicycle fall over? It was two tired.",
    "Why did the man put his money in the blender? He wanted liquid assets.",
    "Why did the chicken join a band? Because it had drumsticks.",
    "Why did the cow win an award? For outstanding performances.",
    "Why did the computer go to the doctor? It had a virus.",
    "Why did the cat bring a ladder? To reach the meow-tains.",
    "Why was the robot angry? Someone pushed its buttons.",
    "Why did the man bring a pencil to the party? To draw attention.",
    "Why did the tomato go to school? To ketchup on studies.",
    "Why did the scarecrow win a medal? For being outstanding.",
    "Why did the computer stay home? It had too many tabs open.",
    "Why did the man take a ladder to work? Because he wanted to climb the corporate ladder.",
    "Why did the skeleton go to the concert? He wanted to hear the boooom!"
]

    joke = random.choice(jokes)
    embed = discord.Embed(
        title="😂 Joke Time!",
        description=joke,
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)
#suggest
@bot.command()
async def suggest(ctx, *, idea: str = None):
    """Send a suggestion to Bot owner via DM!"""

    # ✅ Delete the user's command message
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass  # Ignore if bot lacks permission

    if not idea:
        embed = discord.Embed(
            title="❓ How to Use",
            description="Use this command like:\n```xsuggest [your idea here]```",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return

    owner_id = 584395248512532480
    owner = ctx.guild.get_member(owner_id) or await bot.fetch_user(owner_id)

    embed = discord.Embed(
        title="💡 New Suggestion",
        description=f"**From:** {ctx.author} (`{ctx.author.id}`)\n\n**Suggestion:** {idea}",
        color=discord.Color.green()
    )

    try:
        await owner.send(embed=embed)
        confirm = discord.Embed(
            description="✅ Your suggestion has been sent!",
            color=discord.Color.green()
        )
        await ctx.send(embed=confirm)
    except Exception:
        error = discord.Embed(
            description="❌ Failed to send your suggestion. Please try again later.",
            color=discord.Color.red()
        )
        await ctx.send(embed=error)

# nuke cmd
# --- xnuke ---
@bot.command()
@commands.has_permissions(administrator=True)
async def nuke(ctx):
    confirm_message = await ctx.send(
        f"⚠️ {ctx.author.mention}, are you sure you want to nuke this channel? Type `confirm` to proceed."
    )

    def check(m):
        return (
            m.author == ctx.author
            and m.channel == ctx.channel
            and m.content.lower() == "confirm"
        )

    try:
        msg = await bot.wait_for("message", timeout=15, check=check)
        await ctx.send("💣 Nuking channel...")

        await ctx.channel.purge(limit=None)  # delete all messages
        await ctx.send("✅ Channel has been nuked by an Administrator.")

    except TimeoutError:
        await confirm_message.edit(content="❌ Nuke cancelled (no confirmation).")

# --- Error handling ---
@nuke.error
async def nuke_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need Administrator permissions to use this command.")
# afk cmd
AFK_FILE = "afk.json"

# Load AFK data from file
def load_afk():
    try:
        with open(AFK_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

# Save AFK data to file
def save_afk(data):
    with open(AFK_FILE, "w") as f:
        json.dump(data, f, indent=4)

afk_users = load_afk()  # {user_id: {"reason": str, "time": iso_str}}


@bot.command()
async def afk(ctx, *, reason: str = "AFK"):
    user_id = str(ctx.author.id)
    afk_users[user_id] = {
        "reason": reason,
        "time": datetime.utcnow().isoformat()
    }
    save_afk(afk_users)

    embed = discord.Embed(
        title="💤 AFK Activated",
        description=f"{ctx.author.mention}, you are now AFK.\n**Reason:** {reason}",
        color=discord.Color.blue()
    )
    embed.set_author(name=str(ctx.author), icon_url=getattr(ctx.author.display_avatar, "url", None))
    await ctx.send(embed=embed)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = str(message.author.id)

    # If user was AFK, remove it when they talk
    if user_id in afk_users:
        reason = afk_users[user_id]["reason"]
        del afk_users[user_id]
        save_afk(afk_users)

        embed = discord.Embed(
            title="✅ Welcome Back!",
            description=f"{message.author.mention}, you are no longer AFK.\n**Reason was:** {reason}",
            color=discord.Color.green()
        )
        embed.set_author(name=str(message.author), icon_url=getattr(message.author.display_avatar, "url", None))
        await message.channel.send(embed=embed , delete_after=10)

    # Check mentions for AFK users
    for mention in message.mentions:
        mention_id = str(mention.id)
        if mention_id in afk_users:
            reason = afk_users[mention_id]["reason"]
            since = datetime.fromisoformat(afk_users[mention_id]["time"])
            delta = datetime.utcnow() - since
            mins = delta.seconds // 60
            hrs = mins // 60

            if hrs > 0:
                afk_time = f"{hrs}h {mins % 60}m ago"
            elif mins > 0:
                afk_time = f"{mins}m ago"
            else:
                afk_time = "just now"

            embed = discord.Embed(
                title="💤 User is AFK",
                description=f"{mention.mention} is currently AFK.\n**Reason:** {reason}\n**Since:** {afk_time}",
                color=discord.Color.orange()
            )
            embed.set_author(name=str(mention), icon_url=getattr(mention.display_avatar, "url", None))
            await message.channel.send(embed=embed)

    await bot.process_commands(message)
# revive chat system

CONFIG_FILE = "revive_config.json"

# Helper: load/save JSON config
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {"role_id": None, "revive_enabled": False, "interval": None}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

config = load_config()
revive_task = None  # background loop reference


# Helper: parse time like 1h30m → seconds
def parse_time(timestr: str):
    pattern = re.compile(r'((?P<hours>\d+)h)?((?P<minutes>\d+)m)?')
    match = pattern.fullmatch(timestr.strip().lower())
    if not match:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    return hours * 3600 + minutes * 60

# Load topics from file
def load_topics():
    try:
        with open("topics.txt", "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        # Fallback if file missing
        return [
            "What's your favorite movie?",
            "If you could travel anywhere right now, where would you go?",
            "What's the best advice you've ever received?",
            "If you had a superpower, what would it be?",
            "What's your favorite food and why?"
        ]

TOPICS = load_topics()

#tz cmd

TIMEZONES_FILE = "timezones.json"

def load_timezones():
    if not os.path.exists(TIMEZONES_FILE):
        return {}
    with open(TIMEZONES_FILE, "r") as f:
        return json.load(f)

def save_timezones(data):
    with open(TIMEZONES_FILE, "w") as f:
        json.dump(data, f, indent=2)

@bot.command()
async def tz(ctx, *args):
    user_id = str(ctx.author.id)
    tzdata = load_timezones()

    # ---- SET COMMAND ----
    if len(args) >= 2 and args[0].lower() == "set":
        keyword = " ".join(args[1:]).lower()
        all_tz = pytz.all_timezones
        matches = [tz for tz in all_tz if keyword in tz.lower()]

        if not matches:
            return await ctx.send("⚠️ No timezone found with that keyword. Try something like `Europe/London` or `Africa/Cairo`.")
        if len(matches) > 1:
            # Show first 10
            preview = "\n".join(matches[:10])
            return await ctx.send(f"⚠️ Multiple matches found, be more specific:\n```{preview}```")

        chosen = matches[0]
        tzdata[user_id] = chosen
        save_timezones(tzdata)
        return await ctx.send(f"✅ Timezone set to **{chosen}**.")

    # ---- CHECK SOMEONE ELSE ----
    if len(args) >= 1:
        # Try mention
        target = None
        if ctx.message.mentions:
            target = ctx.message.mentions[0]
        else:
            # Try match by username or display name
            name = " ".join(args)
            for member in ctx.guild.members:
                if member.name.lower() == name.lower() or member.display_name.lower() == name.lower():
                    target = member
                    break

        if not target:
            return await ctx.send("⚠️ Could not find that user.")

        target_id = str(target.id)
        if target_id not in tzdata:
            return await ctx.send(f"🌍 {target.display_name} hasn’t set a timezone.")
        location = tzdata[target_id]
        try:
            tz = pytz.timezone(location)
            now = datetime.now(tz)
            time_str = now.strftime("%H:%M:%S")
            return await ctx.send(f"🕒 {target.display_name}'s local time is **{time_str}** ({location})")
        except Exception as e:
            return await ctx.send(f"⚠️ Error fetching {target.display_name}'s time: {e}")

    # ---- CHECK YOURSELF ----
    if user_id not in tzdata:
        return await ctx.send("⚠️ You haven’t set a timezone yet. Use `tz set <location>`.\nExample: `tz set London`")

    location = tzdata[user_id]
    try:
        tz = pytz.timezone(location)
        now = datetime.now(tz)
        time_str = now.strftime("%H:%M:%S")
        await ctx.send(f"🕒 Your current local time is **{time_str}** ({location})")
    except Exception as e:
        await ctx.send(f"⚠️ Error fetching time: {e}")



# Revive loop
async def start_revive_loop(ctx, interval: int, role_id: int):
    global revive_task

    async def loop_func():
        await ctx.send(embed=discord.Embed(
            title="✅ Chat revive system toggled ON",
            description=f"Reviving chat every **{config['interval']}**",
            color=discord.Color.green()
        ))
        await asyncio.sleep(interval)  # wait before first ping

        while config.get("revive_enabled", False):
            role = ctx.guild.get_role(role_id)
            topic = random.choice(TOPICS)
            if role:
                await ctx.send(f"{role.mention} 💬 Chat topic: **{topic}**")
            await asyncio.sleep(interval)

    revive_task = asyncio.create_task(loop_func())

def stop_revive_loop():
    global revive_task
    if revive_task and not revive_task.done():
        revive_task.cancel()
    revive_task = None


# Command: set role
@commands.has_permissions(administrator=True)
@bot.command()
async def reviveset(ctx, role_id: int):
    config["role_id"] = role_id
    save_config(config)

    embed = discord.Embed(
        title="✅ Revive Role Set",
        description=f"Revive role has been set to <@&{role_id}>",
        color=discord.Color.blue()
    )
    await ctx.send(embed=embed)


# Command: toggle revive
@commands.has_permissions(administrator=True)
@bot.command()
async def revivechat(ctx):
    if not config.get("role_id"):
        embed = discord.Embed(
            title="⚠️ Revive Role Not Set",
            description="Please set a revive role first using `xreviveset [role_id]`.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return

    if not config.get("revive_enabled", False):
        # Ask user for time interval
        await ctx.send("⏰ Enter the time interval (e.g., `1h`, `30m`, `2h30m`):")

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await ctx.send("❌ You took too long to provide a time interval.")
            return

        seconds = parse_time(msg.content)
        if not seconds or seconds <= 0:
            await ctx.send("❌ Invalid time format. Use formats like `1h`, `30m`, `2h30m`.")
            return

        config["interval"] = msg.content
        config["revive_enabled"] = True
        save_config(config)

        await start_revive_loop(ctx, seconds, config["role_id"])

    else:
        # Disable
        stop_revive_loop()
        config["revive_enabled"] = False
        save_config(config)

        embed = discord.Embed(
            title="❌ Chat revive system disabled",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)

# remind cmd
@commands.has_permissions(manage_messages=True)
@bot.command()
async def remind(ctx, time: str = None, *, reminder: str = None):
    """Reminds you of something via DM after a period (s/m/h)"""
    if not time or not reminder:
        embed = discord.Embed(
            title="📝 Usage",
            description="`xremind [time] [reminder]`\nExample: `xremind 10m Drink water`",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return

    seconds = parse_time(time)
    if not seconds or seconds > 86400:  # 24h max
        await ctx.send("❌ Invalid time or exceeds 24h.")
        return

    confirm = discord.Embed(
        description=f"✅ I'll remind you in **{time}** about: **{reminder}**",
        color=discord.Color.green()
    )
    await ctx.send(embed=confirm)

    # Wait without blocking other commands
    await asyncio.sleep(seconds)

    try:
        await ctx.author.send(f"⏰ Reminder: **{reminder}** (set {time} ago)")
    except discord.Forbidden:
        await ctx.send(f"{ctx.author.mention} I couldn't DM you, but here's your reminder:\n**{reminder}**")

# ---------------- xtimer ----------------
@commands.has_permissions(manage_messages=True)
@bot.command()
async def timer(ctx, time: str = None):
    """Starts a countdown timer (s/m/h) and shows live updates"""
    if not time:
        embed = discord.Embed(
            title="⏳ Usage",
            description="`xtimer [time]`\nExample: `xtimer 30s` or `xtimer 2m`",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)
        return

    seconds = parse_time(time)
    if not seconds or seconds > 86400:
        await ctx.send("❌ Invalid time or exceeds 24h.")
        return

    embed = discord.Embed(
        title="⏳ Timer",
        description=f"Time remaining: **{seconds}**s",
        color=discord.Color.blue()
    )
    msg = await ctx.send(embed=embed)

    while seconds > 0:
        await asyncio.sleep(1)
        seconds -= 1
        embed.description = f"Time remaining: **{seconds}**s"
        await msg.edit(embed=embed)

    done = discord.Embed(
        title="⏰ Time's up!",
        description="The countdown has finished.",
        color=discord.Color.green()
    )
    await msg.edit(embed=done)

#meme
@bot.command()
async def meme(ctx):
    url = "https://meme-api.com/gimme"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            embed = discord.Embed(title=data['title'], url=data['postLink'])
            embed.set_image(url=data['url'])
            await ctx.send(embed=embed)
# profile
@bot.command()
async def profile(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"{member.name}'s Profile", color=discord.Color.blue())
    embed.set_thumbnail(url=member.avatar.url)
    embed.add_field(name="Username", value=member.name, inline=True)
    embed.add_field(name="ID", value=member.id, inline=True)
    embed.add_field(name="Joined Server", value=member.joined_at.strftime("%b %d, %Y"), inline=False)
    embed.add_field(name="Account Created", value=member.created_at.strftime("%b %d, %Y"), inline=False)
    await ctx.send(embed=embed)

# ----------------- Server Stats Dashboard -----------------
@bot.command()
async def serverstats(ctx):
    guild = ctx.guild

    # Members
    total_members = guild.member_count
    online_members = sum(1 for m in guild.members if m.status != discord.Status.offline)
    bots = sum(1 for m in guild.members if m.bot)

    # Channels
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    categories = len(guild.categories)

    # Roles & Emojis
    roles = len(guild.roles)
    emojis = len(guild.emojis)

    # Boost info
    boosts = guild.premium_subscription_count
    boost_level = guild.premium_tier

    # Server owner
    owner = guild.owner

    # Creation date
    created_at = guild.created_at.strftime("%d %b %Y")

    # Embed
    embed = discord.Embed(
        title=f"📊 {guild.name} Server Stats",
        color=discord.Color.purple()
    )
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    embed.add_field(name="Owner", value=owner, inline=True)
    embed.add_field(name="Server ID", value=guild.id, inline=True)
    embed.add_field(name="Created On", value=created_at, inline=True)
    embed.add_field(name="Total Members", value=total_members, inline=True)
    embed.add_field(name="Online Members", value=online_members, inline=True)
    embed.add_field(name="Bots", value=bots, inline=True)
    embed.add_field(name="Text Channels", value=text_channels, inline=True)
    embed.add_field(name="Voice Channels", value=voice_channels, inline=True)
    embed.add_field(name="Categories", value=categories, inline=True)
    embed.add_field(name="Roles", value=roles, inline=True)
    embed.add_field(name="Emojis", value=emojis, inline=True)
    embed.add_field(name="Server Boosts", value=f"{boosts} (Level {boost_level})", inline=True)

    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.avatar.url)

    await ctx.send(embed=embed)

# fortune
fortunes = [
    "A beautiful, smart, and loving person will be coming into your life.",
    "Your life will be happy and peaceful.",
    "Now is a good time to try something new.",
    "Someone will call you today with exciting news.",
    "You will find great luck in unexpected places.",
    "A thrilling time is in your immediate future.",
    "You will conquer obstacles to achieve success.",
    "Your creativity will lead to amazing opportunities.",
    "A new friendship is on the horizon.",
    "You will achieve the goals you set for yourself.",
    "Unexpected wealth will soon find its way to you.",
    "A surprise encounter will bring joy to your day.",
    "Your talents will be recognized and rewarded.",
    "Happiness begins with a small act of kindness.",
    "Adventure awaits you this week.",
    "Someone will appreciate your generosity today.",
    "Your confidence will inspire others around you.",
    "You will discover something you thought was lost.",
    "A new hobby will bring you joy and relaxation.",
    "Today is perfect for making a bold move.",
    "A long-awaited message will bring clarity.",
    "You will make a difference in someone's life.",
    "Your hard work will soon pay off.",
    "A small act of courage will have a big impact.",
    "You will be pleasantly surprised by a friend.",
    "A fun opportunity is coming your way.",
    "Your kindness will come back to you multiplied.",
    "A challenge will reveal your hidden strengths.",
    "Someone special is thinking about you today.",
    "Good news will arrive when you least expect it.",
    "You will find inspiration in an unlikely place.",
    "An old friend will reach out soon.",
    "Your perseverance will be admired by others.",
    "A positive change is coming in your life.",
    "You will be asked to help someone in need.",
    "Your intuition will guide you wisely today.",
    "A joyful experience is headed your way.",
    "You will learn something new and exciting soon.",
    "A moment of laughter will lift your spirits.",
    "Your honesty will earn you trust and respect.",
    "A creative idea will bring unexpected success.",
    "You will receive recognition for your efforts.",
    "Someone will surprise you with a kind gesture.",
    "Your optimism will attract good things.",
    "You will find a solution that eluded you.",
    "A dream you have will soon come true.",
    "You will make a new connection that matters.",
    "Today is perfect for trying something different.",
    "You will find peace in a hectic situation.",
    "A small victory will boost your confidence.",
    "Someone will share exciting news with you.",
    "Your hard work will inspire someone else.",
    "You will make a discovery that excites you.",
    "A positive twist will turn things in your favor.",
    "You will enjoy a moment of pure happiness.",
    "Someone will compliment you unexpectedly.",
    "You will gain clarity on a confusing matter.",
    "Your charm will open doors you never expected.",
    "A long-term goal will take a big step forward.",
    "You will find joy in a simple activity.",
    "Someone will offer you help when you need it most.",
    "Your dreams will spark a creative project.",
    "A new opportunity will present itself soon.",
    "You will be pleasantly surprised by an event.",
    "Your patience will be rewarded in an unexpected way.",
    "A little risk will lead to a big reward.",
    "You will receive encouragement from a friend.",
    "An exciting invitation is coming your way.",
    "You will experience something magical today.",
    "Your hard work will soon be noticed publicly.",
    "You will find happiness in an unexpected encounter.",
    "Someone will teach you an important lesson.",
    "A joyful memory will return to brighten your day.",
    "You will achieve something you thought impossible.",
    "Your determination will inspire admiration.",
    "A creative solution will solve a problem easily.",
    "You will make someone’s day with your kindness.",
    "An opportunity will challenge you in the best way.",
    "You will be rewarded for helping others.",
    "A new perspective will help you solve a dilemma.",
    "You will find something valuable in an unexpected place.",
    "A celebration is coming that will lift your spirits.",
    "You will discover a hidden talent in yourself.",
    "Someone will express gratitude that warms your heart.",
    "A moment of clarity will lead to an important decision.",
    "Your sense of humor will bring joy to others.",
    "You will meet someone who changes your outlook.",
    "A small act of bravery will have lasting effects.",
    "You will receive a gift that brightens your day.",
    "Your kindness will be returned in an unexpected way.",
    "A spontaneous adventure will bring excitement.",
    "You will feel proud of something you recently accomplished.",
    "Someone will surprise you with an act of love.",
    "Your optimism will help you overcome a challenge.",
    "You will find peace in making a difficult choice.",
    "A fun opportunity to learn will come your way.",
    "You will inspire someone to follow their dreams.",
    "A secret will be revealed that delights you.",
    "You will experience a joyful coincidence soon.",
    "Your compassion will make a big difference.",
    "An idea you have will lead to success.",
    "You will find yourself laughing more than usual.",
    "Someone will admire your courage quietly.",
    "A lucky event will make your day memorable.",
    "You will make a decision that positively changes your future.",
    "A small surprise will brighten your week.",
    "You will achieve recognition for a hidden talent.",
    "Someone’s advice will help you in an unexpected way.",
    "You will create a memory that lasts forever.",
    "A positive change will bring peace to your heart."
]

@bot.command()
async def fortune(ctx):
    await ctx.send(f"🔮 {ctx.author.mention}, your fortune is:\n**{random.choice(fortunes)}**")
# -------------------------
# RUN BOT
# -------------------------
# status
statuses = itertools.cycle([
    "Beta Version",
    "Testing Features",
    "Shit-chan in Development",
    "Join the adventure",
    "Thanks to doom",
    "Apple is the goat",
    "Wait, I'm alive!?",
    "Hopefully I stay online!",
    "Wait that's the end?",
    "Dm doomnah for suggestions",
    "Apple and doom are cool",
    "Beta",
    "WIP",
    "Not finished yet..",
    "Working still..",
    "Be patient!",
    "Loading script..",
    "You are doom-ed! ha!",
    "Apple is my favourite fruit!",
    "xsuggest for a suggestion!",
    "Doom created me",
    "Add me to your server!!",
    "I can be customized for your server",
    "For custom bots dm doomnah!",
    "For custom bots dm doomnah!",
    "For custom bots dm doomnah!",
    "For custom bots dm doomnah!",
    "For custom bots dm doomnah!",
    "For custom bots dm doomnah!"
])

async def change_status():
    await bot.wait_until_ready()
    while True:
        current_status = next(statuses)
        await bot.change_presence(activity=discord.Game(name=current_status))
        await asyncio.sleep(60)  # change every 15 seconds
async def setup():
    bot.loop.create_task(change_status())
bot.setup_hook = setup

import os
Token = os.getenv("Token")
bot.run(Token)
