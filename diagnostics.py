"""Text reports for /neuro status and /neuro debug."""
import datetime as dt

import app_profiles
import config


def status_report(bot, guild) -> str:
    me, gp = guild.me, guild.me.guild_permissions
    lines = []

    def check(ok: bool, label: str, fix: str = "") -> None:
        lines.append(("✅ " if ok else "❌ ") + label + ("" if ok else f" — {fix}"))

    check(gp.administrator, "Administrator permission",
          "without it, grant Ban/Kick/Timeout Members, Manage Messages, View Channels, Send Messages")
    if not gp.administrator:
        for perm, label in (("ban_members", "Ban Members"), ("kick_members", "Kick Members"),
                            ("moderate_members", "Timeout Members"), ("manage_messages", "Manage Messages"),
                            ("read_message_history", "Read Message History")):
            check(getattr(gp, perm), label, "give Neuro's role this permission")

    above = [r.name for r in guild.roles if r.position > me.top_role.position and not r.managed
             and r.name != "@everyone"]
    check(not above, f"Neuro's role ({me.top_role.name}) is above every normal role",
          f"{len(above)} roles are higher (e.g. {', '.join(above[:3])}); Neuro can't moderate people "
          "who have them. Drag Neuro's role up in Server Settings > Roles")

    cached, total = len(guild.members), guild.member_count or 0
    check(total == 0 or cached >= total * 0.9, f"Member list loaded ({cached}/{total})",
          "enable the Server Members intent in the Developer Portal and restart")
    check(bot.intents.message_content, "Message Content intent requested", "enable it in code + portal")

    if config.MOD_LOG_CHANNEL_ID:
        ch = guild.get_channel(config.MOD_LOG_CHANNEL_ID)
        check(bool(ch and ch.permissions_for(me).send_messages), "Mod-log channel reachable",
              "MOD_LOG_CHANNEL_ID is wrong or Neuro can't post there")
    else:
        lines.append("ℹ️ Mod-log channel not set (MOD_LOG_CHANNEL_ID); actions only go to the console")

    apps = [m for m in guild.members if m.bot and m.id != bot.user.id]
    if apps:
        lines.append("**Other apps:**")
        for m in apps[:15]:
            lines.append(f"• {m.display_name}: {app_profiles.describe(app_profiles.get(m))}")
        lines.append("Tip: `/neuro appprofile` saves the prefix an app uses.")
    lines.append(f"Tool permission: apps={config.APP_TOOL_PERMISSION}, moderation={config.MOD_TOOL_PERMISSION}"
                 f" | AutoMod={'on' if config.AUTOMOD else 'off'}")
    return "\n".join(lines)[:1900]


def debug_report(traces) -> str:
    if not traces:
        return "No tool runs recorded since the last restart."
    out = []
    for t in list(traces)[-5:]:
        when = dt.datetime.fromtimestamp(t["ts"]).strftime("%H:%M:%S")
        head = f"**{when} {t['user']}**: {t['text']}" + (" _(strong model)_" if t["strong_first"] else "")
        steps = t["steps"] or ["(no tool used)"]
        body = "\n".join(s if isinstance(s, str) else f"  • `{s['tool']}` {s['args']} → {s['result']}"
                         for s in steps)
        out.append(head + "\n" + body)
    return "\n\n".join(out)[:1900]
