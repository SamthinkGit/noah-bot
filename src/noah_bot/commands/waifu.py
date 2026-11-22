import random
import time

import discord
from discord.ext import commands

from noah_bot.modules.bot_context import get_bot_context
from noah_bot.modules.discord_formatter import (
    EmbedTable,
    with_delete_button,
    with_loading,
)
from noah_bot.modules.waifu_game import DOJO_CHARGE_SECONDS, Waifu
from noah_bot.modules.help_ui import HelpEntry, HelpModule, HelpSection, register_help, send_help


HELP = register_help(
    HelpModule(
        key="waifu",
        emoji="⚔️",
        name="Waifu battle",
        summary="Combates entre las waifus de los jugadores",
        description="Crea tu waifu, entrénala y ataca a las de los demás jugadores.",
        sections=(
            HelpSection(
                "Tu waifu",
                (
                    HelpEntry(".noah waifu set <nombre> -special <especial>", "Crea tu waifu con su ataque especial."),
                    HelpEntry(".noah waifu status [-user @user]", "Estado de tu waifu (o de otra)."),
                    HelpEntry(".noah waifu stats [-user @user]", "Sus estadísticas."),
                    HelpEntry(".noah waifu remaining [-user @user]", "Cuánto le queda para volver a estar activa. Alias `rem`."),
                    HelpEntry(".noah waifu setimage", "Respondiendo a un embed con imagen: la usa para tu waifu."),
                    HelpEntry(".noah waifu setcolor <hex | random | reset>", "Color de sus embeds."),
                ),
            ),
            HelpSection(
                "Entrenar y combatir",
                (
                    HelpEntry(".noah waifu attack @user", "Ataca a la waifu de otro jugador."),
                    HelpEntry(".noah waifu attackedby [-user @user]", "Daño recibido y de quién."),
                    HelpEntry(".noah waifu sleep", "La pone a descansar."),
                    HelpEntry(".noah waifu alive", "La vuelve a activar."),
                    HelpEntry(".noah waifu levelup", "Sube de nivel si puede."),
                    HelpEntry(".noah waifu daily <stat>", "Entrenamiento diario de una estadística."),
                    HelpEntry(".noah waifu dojo", "Entrena en el dojo (jugadores de la partida)."),
                    HelpEntry(".noah waifu report", "Informe de todos los jugadores."),
                ),
            ),
            HelpSection(
                "Admin",
                (
                    HelpEntry(".noah waifu setplayers @user1 @user2 ...", "Elige los jugadores de la partida."),
                    HelpEntry(".noah waifu setpeacefulmode @user", "Retira a alguien de forma pacífica."),
                    HelpEntry(".noah waifu forcedojo", "Abre un dojo nuevo al momento."),
                ),
                staff=True,
            ),
        ),
    )
)


def register_waifu_commands(noah_group: commands.Group) -> None:
    @noah_group.group()
    async def waifu(ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send("Use `.noah waifu help` to see commands.")

    @waifu.command()
    async def set(ctx: commands.Context, *, args: str) -> None:
        context = get_bot_context(ctx.bot)

        if "-special" not in args:
            await ctx.send("❌ Usage: `.noah waifu set <name> -special <special name>`")
            return

        name, special = args.split("-special", 1)
        name = name.strip()
        special = special.strip()

        result = context.waifu_manager.waifu_set(
            user_id=str(ctx.author.id),
            waifu_name=name,
            special_name=special,
        )

        if not result["ok"]:
            await ctx.send(f"❌ {result.get('message', 'Error')}")
            return

        waifu_data: Waifu = result["waifu"]
        stats = waifu_data["stats"]
        color = (
            discord.Color(waifu_data.embed_color)
            if waifu_data.embed_color
            else discord.Color.blurple()
        )
        table = EmbedTable(
            headers=["Stat"], title=f"🖤 {waifu_data['name']} created", color=color
        )

        table.add_row([f"❤️ Health: {waifu_data['hp']} / {waifu_data['max_hp']}"])
        table.add_row([f"🤸‍♀️ Agility: {stats['agility']}"])
        table.add_row([f"🔮 Mana: {stats['mana']}"])
        table.add_row([f"💪 Recover: {stats['recover']}"])
        table.add_row([f"🗡️ Damage: {stats['hit_damage']}"])
        table.add_row([f"Dodge Chance: {int(stats['dodge_chance'] * 100)}%"])
        table.add_row([f"Special Chance: {int(stats['special_chance'] * 100)}%"])
        table.add_row([f"Cooldown: {stats['cooldown_seconds'] // 60} min"])
        table.add_row([f"Special Name: {waifu_data['special_name']}"])

        await ctx.send(embed=table.render())

    @waifu.command()
    async def setplayers(
        ctx: commands.Context, players: commands.Greedy[discord.Member]
    ) -> None:
        context = get_bot_context(ctx.bot)

        if not players:
            await ctx.send("❌ Debes mencionar al menos un usuario.")
            return

        player_ids = [str(member.id) for member in players]
        context.waifu_manager.set_players(player_ids)

        mentions = ", ".join(member.mention for member in players)
        await ctx.send(f"✅ Jugadores configurados para el reporte: {mentions}")

    @waifu.command()
    async def report(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        player_ids = context.waifu_manager.get_players()

        if not player_ids:
            await ctx.send(
                "❌ No hay jugadores configurados. Usa `.noah waifu setplayers <@user1> <@user2> ...`."
            )
            return

        now = time.time()
        table = EmbedTable(
            headers=["Player"],
            title="📋 Waifu Player Report",
            color=discord.Color.blurple(),
        )

        for player_id in player_ids:
            member = ctx.guild.get_member(int(player_id))
            name = member.mention if member else f"<@{player_id}>"

            waifu_data = context.waifu_manager.get_waifu(str(player_id))
            if not waifu_data:
                table.add_row([name, "-", "❌ No tiene waifu"])
                continue

            if (
                waifu_data.incapacitated_until
                and waifu_data.incapacitated_until.timestamp() > now
            ):
                status = "🩸"
            elif waifu_data.stunned_until and waifu_data.stunned_until.timestamp() > now:
                status = "😵"
            else:
                status = "✅"

            sleep_available = "🛌" if waifu_data.can_sleep(waifu_data.now()) else "⏳"
            current_hp = (
                f"0{waifu_data.current_hp}"
                if waifu_data.current_hp < 10
                else str(waifu_data.current_hp)
            )
            max_hp = (
                f"0{waifu_data.max_hp()}"
                if waifu_data.max_hp() < 10
                else str(waifu_data.max_hp())
            )
            hp_text = f"{current_hp} / {max_hp}"
            table.add_row([f"`({hp_text})` {status} {sleep_available} {name}"])

        await ctx.send(embed=table.render())

    @waifu.command()
    @with_loading(title="🗡️ Engaging in Waifu Combat...", duration=1.0, steps=15)
    async def attack(ctx: commands.Context, user: discord.Member) -> None:
        context = get_bot_context(ctx.bot)
        defender = context.waifu_manager.get_waifu(str(user.id))

        if not defender:
            await ctx.send(f"❌ {user.display_name} doesn't have a waifu.")
            return

        if defender.is_incapacitated(defender.now()):
            await ctx.send(f"❌ {user.display_name}'s waifu is incapacitated.")
            return

        attacker = context.waifu_manager.get_waifu(str(ctx.author.id))
        if attacker.name == defender.name:
            await ctx.send("❌ You cannot attack your own waifu.")
            return

        result = context.waifu_manager.waifu_attack(
            attacker_id=str(ctx.author.id),
            defender_id=str(user.id),
        )

        if not result["ok"]:
            await ctx.send(f"❌ {result.get('message', 'Attack failed')}")
            return

        table = EmbedTable(
            headers=["Event"],
            title="⚔️ Waifu Battle",
            color=(
                discord.Color(attacker.embed_color)
                if attacker.embed_color
                else discord.Color.blurple()
            ),
        )
        table.description = (
            f"`{ctx.author.display_name}` attacked `{user.display_name}`'s waifu"
        )

        if result["dodged"]:
            table.add_row(["💨 The attack was dodged!"])
        else:
            table.add_row([f"Damage: {result['damage']}"])
            table.add_row([f"Defender HP: {result['defender_hp_after']}"])

            if result["special"]:
                table.add_row([f"💥 Special: {result['special_name']}"])

            if result["stunned_applied"]:
                table.add_row(["😵 Status: Stunned (3h)"])

            if result["killed"]:
                table.add_row(["🩸 Status: Incapacitated (12h)"])
                table.add_row(["Reward: Half heal"])

        attacker = context.waifu_manager.get_waifu(str(ctx.author.id))
        embed = table.render()

        if attacker and attacker.image_url:
            embed.set_image(url=attacker.image_url)

        if defender and defender.image_url:
            embed.set_thumbnail(url=defender.image_url)

        await ctx.send(embed=embed)

    @waifu.command(aliases=["rem"])
    @with_delete_button()
    async def remaining(ctx: commands.Context, *, args: str = "") -> None:
        context = get_bot_context(ctx.bot)
        target_user = ctx.author

        if "-user" in args:
            if not ctx.message.mentions:
                await ctx.send("❌ Please mention a valid user after `-user`.")
                return

            target_user = ctx.message.mentions[0]

        waifu_data = context.waifu_manager.get_waifu(str(target_user.id))
        if not waifu_data:
            await ctx.send("❌ You don't have a waifu.")
            return

        now = time.time()
        table = EmbedTable(
            headers=["Info"],
            title="⏳ Waifu Status",
            color=(
                discord.Color(waifu_data.embed_color)
                if waifu_data.embed_color
                else discord.Color.blurple()
            ),
        )

        if waifu_data.stunned_until and not context.waifu_manager.devmode:
            remaining_seconds = int(waifu_data.stunned_until.timestamp() - now)
            if remaining_seconds > 0:
                table.add_row(["Status: 😵 Stunned"])
                table.add_row(
                    [
                        f"Free in: {remaining_seconds // 3600}h {(remaining_seconds % 3600) // 60}m"
                    ]
                )
                embed = table.render()
                if waifu_data.image_url:
                    embed.set_image(url=waifu_data.image_url)
                await ctx.send(embed=embed)
                return

        if waifu_data.incapacitated_until and not context.waifu_manager.devmode:
            remaining_seconds = int(waifu_data.incapacitated_until.timestamp() - now)
            if remaining_seconds > 0:
                table.add_row(["Status: 🩸 Incapacitated"])
                table.add_row(
                    [
                        f"Recovery in: {remaining_seconds // 3600}h {(remaining_seconds % 3600) // 60}m"
                    ]
                )
                embed = table.render()
                if waifu_data.image_url:
                    embed.set_image(url=waifu_data.image_url)
                await ctx.send(embed=embed)
                return

        if context.waifu_manager.devmode or not waifu_data.last_attack_at:
            table.add_row(["Status: Ready to attack"])
        else:
            cooldown = waifu_data.stats.cooldown_seconds()
            elapsed = int(now - waifu_data.last_attack_at.timestamp())
            remaining_seconds = max(0, cooldown - elapsed)

            if remaining_seconds == 0:
                table.add_row(["Status: Ready to attack"])
            else:
                table.add_row(["Status: Recovering"])
                table.add_row(
                    [f"Remaining: {remaining_seconds // 60}m {remaining_seconds % 60}s"]
                )

        embed = table.render()
        if waifu_data.image_url:
            embed.set_image(url=waifu_data.image_url)

        await ctx.send(embed=embed)

    @waifu.command()
    async def sleep(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        result = context.waifu_manager.waifu_sleep(str(ctx.author.id))

        if not result["ok"]:
            await ctx.send(f"❌ {result.get('message', 'Cannot sleep')}")
            return

        table = EmbedTable(headers=["Info"], title="😴 Waifu Rest")
        table.add_row([f"HP Before: {result['hp_before']}"])
        table.add_row([f"HP After: {result['hp_after']}"])
        table.add_row([f"Healed: {result['healed']}"])

        waifu_data = context.waifu_manager.get_waifu(str(ctx.author.id))
        embed = table.render()

        if waifu_data and waifu_data.image_url:
            embed.set_image(url=waifu_data.image_url)

        await ctx.send(embed=embed)

    @waifu.command()
    async def levelup(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        result = context.waifu_manager.waifu_levelup(str(ctx.author.id))

        if not result["ok"]:
            await ctx.send(f"❌ {result.get('message', 'Cannot level up')}")
            return

        table = EmbedTable(headers=["Info"], title="⬆️ Level Up!")
        table.add_row([f"Upgraded stat: {result['chosen_stat']} +1pt"])
        table.add_row([f"Pending Levelups: {result['pending_levelups_left']}"])

        waifu_data = context.waifu_manager.get_waifu(str(ctx.author.id))
        embed = table.render()

        if waifu_data and waifu_data.image_url:
            embed.set_image(url=waifu_data.image_url)

        await ctx.send(embed=embed)

    @waifu.command()
    @with_delete_button()
    async def status(ctx: commands.Context, *, args: str = "") -> None:
        context = get_bot_context(ctx.bot)
        target_user = ctx.author

        if "-user" in args:
            if not ctx.message.mentions:
                await ctx.send("❌ Please mention a valid user after `-user`.")
                return

            target_user = ctx.message.mentions[0]

        waifu_data = context.waifu_manager.get_waifu(str(target_user.id))
        if not waifu_data:
            if target_user == ctx.author:
                await ctx.send("❌ You don't have a waifu.")
            else:
                await ctx.send(f"❌ {target_user.display_name} doesn't have a waifu.")
            return

        now = time.time()
        color = (
            discord.Color(waifu_data.embed_color)
            if waifu_data.embed_color
            else discord.Color.blurple()
        )
        table = EmbedTable(
            headers=["Stat"],
            title=f"📊 {waifu_data.name} Status ({target_user.display_name})",
            color=color,
        )

        incapacitated = False
        if waifu_data.incapacitated_until and waifu_data.incapacitated_until.timestamp() > now:
            incapacitated = True
            remaining_seconds = int(waifu_data.incapacitated_until.timestamp() - now)
            table.add_row(["Status: 🩸 Incapacitated"])
            table.add_row(
                [
                    f"Recovery in: {remaining_seconds // 3600}h {(remaining_seconds % 3600) // 60}m\n"
                ]
            )
        elif waifu_data.stunned_until and waifu_data.stunned_until.timestamp() > now:
            remaining_seconds = int(waifu_data.stunned_until.timestamp() - now)
            table.add_row(["Status: 😵 Stunned"])
            table.add_row(
                [
                    f"Free in: {remaining_seconds // 3600}h {(remaining_seconds % 3600) // 60}m\n"
                ]
            )
        else:
            table.add_row(["Status: Active\n"])

        table.add_row([f"**Level**: {waifu_data.level()}\n"])
        if not incapacitated:
            table.add_row([f"❤️ **HP**: {waifu_data.current_hp} / {waifu_data.max_hp()}"])
        else:
            table.add_row([f"❤️ **HP**: 0 / {waifu_data.max_hp()} (Incapacitated)"])
        table.add_row([f"🤸‍♀️ **Agility**: {waifu_data.stats.agility}"])
        table.add_row([f"🔮 **Mana**: {waifu_data.stats.mana}"])
        table.add_row([f"💪 **Recover**: {waifu_data.stats.recover}"])
        table.add_row([f"🗡️ **Damage**: {waifu_data.stats.hit_damage()}"])
        table.add_row(
            [f"⏳ **Cooldown**: {waifu_data.stats.cooldown_seconds() // 60} min\n"]
        )
        table.add_row([f"Special: {waifu_data.special_name}"])
        table.add_row([f"Pending Levelups: {waifu_data.pending_levelups}"])
        table.add_row([f"Last Sleep: {waifu_data.last_sleep_date}"])

        embed = table.render()
        if waifu_data.image_url:
            embed.set_image(url=waifu_data.image_url)

        await ctx.send(embed=embed)

    @waifu.command()
    async def alive(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        waifu_data = context.waifu_manager.get_waifu(str(ctx.author.id))

        if not waifu_data:
            await ctx.send("❌ You don't have a waifu.")
            return

        now = time.time()
        if not waifu_data.incapacitated_until:
            await ctx.send("🖤 Your waifu is already active.")
            return

        remaining_seconds = int(waifu_data.incapacitated_until.timestamp() - now)
        if remaining_seconds > 0:
            await ctx.send(
                f"🩸 Still incapacitated for "
                f"{remaining_seconds // 3600}h {(remaining_seconds % 3600) // 60}m."
            )
            return

        waifu_data.incapacitated_until = None
        waifu_data.current_hp = waifu_data.max_hp()
        context.waifu_manager._state["users"][str(ctx.author.id)] = (
            context.waifu_manager._serialize_waifu(waifu_data)
        )
        context.waifu_manager._save()

        await ctx.send("✨ Your waifu has recovered and is active again!")

    @waifu.command()
    @with_delete_button()
    async def stats(ctx: commands.Context, *, args: str = "") -> None:
        context = get_bot_context(ctx.bot)
        target_user = ctx.author

        if "-user" in args:
            if not ctx.message.mentions:
                await ctx.send("❌ Please mention a valid user after `-user`.")
                return

            target_user = ctx.message.mentions[0]

        waifu_data = context.waifu_manager.get_waifu(str(target_user.id))
        if not waifu_data:
            if target_user == ctx.author:
                await ctx.send("❌ You don't have a waifu.")
            else:
                await ctx.send(f"❌ {target_user.display_name} doesn't have a waifu.")
            return

        table = EmbedTable(
            headers=["Advanced Combat Data"],
            title=f"📊 {waifu_data.name} - Advanced Stats ({target_user.display_name})",
            color=(
                discord.Color(waifu_data.embed_color)
                if waifu_data.embed_color
                else discord.Color.blurple()
            ),
        )

        table.add_row(
            [
                f"🗡️ Damage level: **{waifu_data.stats.damage} / 30** "
                f"({waifu_data.stats.hit_damage()} pts per Hit)"
            ]
        )
        table.add_row(
            [f"💨 Dodge chance: **{int(waifu_data.stats.dodge_chance() * 100)}%**"]
        )
        table.add_row(
            [
                f"💥 Special trigger chance: **{int(waifu_data.stats.special_chance() * 100)}%**"
            ]
        )
        table.add_row(
            [f"⏳ Attack cooldown: **{waifu_data.stats.cooldown_seconds() // 60} minutes**"]
        )
        table.add_row(["😵 Stun duration on special: **3 hours**"])
        table.add_row(["🩸 Incapacitation duration (HP = 0): **12 hours**"])
        table.add_row([f"📈 Pending level-ups: **{waifu_data.pending_levelups}**"])
        table.add_row([f"🛌 Latest Sleep: **{waifu_data.last_sleep_date}**"])

        embed = table.render()
        if waifu_data.image_url:
            embed.set_image(url=waifu_data.image_url)

        await ctx.send(embed=embed)

    @waifu.command()
    async def setimage(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)

        if not ctx.message.reference:
            await ctx.send("❌ You must reply to a message with an embed image.")
            return

        replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        image_url = None

        for embed in replied.embeds:
            if embed.image and embed.image.url:
                image_url = embed.image.url
                break
            if embed.thumbnail and embed.thumbnail.url:
                image_url = embed.thumbnail.url
                break

        if not image_url:
            await ctx.send("❌ No image found in the replied embed.")
            return

        result = context.waifu_manager.waifu_set_image(str(ctx.author.id), image_url)

        if not result["ok"]:
            await ctx.send(f"❌ {result['message']}")
            return

        await ctx.send("🖼️ Waifu image set successfully!")

    @waifu.command()
    @with_delete_button()
    async def dojo(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)
        waifu_data = context.waifu_manager.get_waifu(str(ctx.author.id))

        if not waifu_data:
            await ctx.send("❌ You don't have a waifu.")
            return

        if str(ctx.author.id) not in context.waifu_manager.get_players():
            await ctx.send("❌ Only players in the player set can use the dojo.")
            return

        result = context.waifu_manager.dojo_training_action(str(ctx.author.id))
        dojo_data = result.get("dojo") or context.waifu_manager.dojo

        if not dojo_data:
            await ctx.send("⛩️ There is no active dojo right now.")
            return

        embed = EmbedTable(
            headers=["Info"],
            title=f"⛩️ {dojo_data.get('name', 'Mysterious Dojo')}",
            color=(
                discord.Color(waifu_data.embed_color)
                if waifu_data.embed_color
                else discord.Color.blurple()
            ),
        ).render()

        if dojo_data.get("image_url"):
            embed.set_image(url=dojo_data["image_url"])

        selected_lines = []
        for user_id in dojo_data.get("selected_players", []):
            member = ctx.guild.get_member(int(user_id))
            selected_lines.append(member.mention if member else f"<@{user_id}>")

        if selected_lines:
            embed.add_field(
                name="Selected players",
                value=", ".join(selected_lines),
                inline=False,
            )

        code = result.get("code")

        if not result.get("ok"):
            if code == "no_dojo":
                await ctx.send("⛩️ There is no active dojo right now.")
                return
            if code == "not_selected":
                embed.add_field(
                    name="Status",
                    value=(
                        "This dojo is active, but **you were not selected**. "
                        "Selection favors lower-level players."
                    ),
                    inline=False,
                )
                await ctx.send(embed=embed)
                return
            if code == "already_completed":
                embed.add_field(
                    name="Status",
                    value="You have already completed your training in this dojo.",
                    inline=False,
                )
                await ctx.send(embed=embed)
                return

            await ctx.send("❌ Could not use the dojo at this time.")
            return

        if code == "started":
            minutes = DOJO_CHARGE_SECONDS // 60
            embed.add_field(
                name="Training started",
                value=(
                    f"You have started charging energy in the dojo. "
                    f"Come back in **{minutes} minutes** and use `.noah waifu dojo` again "
                    "to claim your reward."
                ),
                inline=False,
            )
        elif code == "charging":
            remaining_seconds = int(result.get("remaining_seconds", 0))
            mins = remaining_seconds // 60
            secs = remaining_seconds % 60
            embed.add_field(
                name="Training...",
                value=f"You still have **{mins}m {secs}s** of training left.",
                inline=False,
            )
        elif code == "completed":
            gained = result.get("gained_levelups", 3)
            pending = result.get("pending_levelups")
            extra = (
                f" You now have **{pending}** pending levelups."
                if pending is not None
                else ""
            )
            embed.add_field(
                name="Training completed",
                value=(
                    f"You have completed **30 minutes** of dojo training and gained "
                    f"**{gained} random levels** (pending levelups)." + extra
                ),
                inline=False,
            )

        await ctx.send(embed=embed)

    @waifu.command()
    async def daily(ctx: commands.Context, stat: str | None = None) -> None:
        context = get_bot_context(ctx.bot)
        waifu_data = context.waifu_manager.get_waifu(str(ctx.author.id))

        if not waifu_data:
            await ctx.send("❌ You don't have a waifu.")
            return

        valid_stats = ["health", "agility", "mana", "recover", "damage"]
        today = waifu_data.now().date().isoformat()

        if waifu_data.last_daily_date == today and not context.waifu_manager.devmode:
            await ctx.send("⏳ You already used your daily training today.")
            return

        if stat:
            stat = stat.lower()
            if stat not in valid_stats:
                await ctx.send(
                    "❌ Invalid stat. Choose one of: "
                    "`health`, `agility`, `mana`, `recover`, `damage`"
                )
                return
        else:
            stat = random.choice(valid_stats)

        before = getattr(waifu_data.stats, stat)
        if before >= 30:
            await ctx.send(f"⚠️ {stat.capitalize()} is already at max (30).")
            return

        waifu_data.last_daily_date = today
        setattr(waifu_data.stats, stat, before + 1)

        if stat == "health":
            waifu_data.current_hp = min(waifu_data.current_hp, waifu_data.max_hp())

        context.waifu_manager._state["users"][str(ctx.author.id)] = (
            context.waifu_manager._serialize_waifu(waifu_data)
        )
        context.waifu_manager._save()

        table = EmbedTable(
            headers=["Daily Training"],
            title="🌅 Daily Training Complete",
            color=(
                discord.Color(waifu_data.embed_color)
                if waifu_data.embed_color
                else discord.Color.blurple()
            ),
        )
        table.add_row([f"📈 Stat upgraded: **{stat.capitalize()} +1**"])
        table.add_row([f"🔢 New value: **{getattr(waifu_data.stats, stat)} / 30**"])
        table.add_row(["⏳ Available again: **Tomorrow**"])

        embed = table.render()
        if waifu_data.image_url:
            embed.set_image(url=waifu_data.image_url)

        await ctx.send(embed=embed)

    @waifu.command()
    @with_delete_button()
    async def attackedby(ctx: commands.Context, *, args: str = "") -> None:
        context = get_bot_context(ctx.bot)
        target_user = ctx.author

        if "-user" in args:
            if not ctx.message.mentions:
                await ctx.send("❌ Please mention a valid user after `-user`.")
                return

            target_user = ctx.message.mentions[0]

        waifu_data = context.waifu_manager.get_waifu(str(target_user.id))
        if not waifu_data:
            if target_user == ctx.author:
                await ctx.send("❌ You don't have a waifu.")
            else:
                await ctx.send(f"❌ {target_user.display_name} doesn't have a waifu.")
            return

        if not waifu_data.received_hits:
            await ctx.send("🛡️ No attacks received since last death.")
            return

        table = EmbedTable(
            headers=["Attacker"],
            title=f"🩸 Damage Received ({target_user.display_name})",
            color=(
                discord.Color(waifu_data.embed_color)
                if waifu_data.embed_color
                else discord.Color.blurple()
            ),
        )

        for attacker_id, damage in sorted(
            waifu_data.received_hits.items(), key=lambda item: item[1], reverse=True
        ):
            member = ctx.guild.get_member(int(attacker_id))
            name = member.mention if member else f"<@{attacker_id}>"
            table.add_row([f"{name}: {damage} HP"])

        embed = table.render()
        if waifu_data.image_url:
            embed.set_image(url=waifu_data.image_url)

        await ctx.send(embed=embed)

    @waifu.command()
    async def setcolor(ctx: commands.Context, color: str) -> None:
        context = get_bot_context(ctx.bot)
        waifu_data = context.waifu_manager.get_waifu(str(ctx.author.id))

        if not waifu_data:
            await ctx.send("❌ You don't have a waifu.")
            return

        if color.lower() == "reset":
            context.waifu_manager.waifu_set_color(str(ctx.author.id), None)
            await ctx.send("🎨 Embed color reset to default.")
            return

        if color.lower() == "random":
            value = random.randint(0x000000, 0xFFFFFF)
        else:
            if color.startswith("#"):
                color = color[1:]
            try:
                value = int(color, 16)
            except ValueError:
                await ctx.send(
                    "❌ Invalid color. Use hex like `#ff00ff`, `random`, or `reset`."
                )
                return

        result = context.waifu_manager.waifu_set_color(str(ctx.author.id), value)
        if not result["ok"]:
            await ctx.send(f"❌ {result['message']}")
            return

        await ctx.send(f"🎨 Waifu embed color set to `#{value:06x}`")

    @waifu.command()
    async def setpeacefulmode(ctx: commands.Context, user: discord.Member) -> None:
        context = get_bot_context(ctx.bot)
        result = context.waifu_manager.waifu_peaceful_kill(
            attacker_id=str(ctx.author.id),
            target_id=str(user.id),
        )

        if not result["ok"]:
            await ctx.send(f"❌ {result['message']}")
            return

        table = EmbedTable(
            headers=["Peaceful Mode"],
            title="☮️ Peaceful Resolution Applied",
        )
        table.add_row([f"Target: **{user.display_name}**"])
        table.add_row(["Status: ☠️ Instantly incapacitated"])
        table.add_row(["Respawn: **1 year**"])

        waifu_data = context.waifu_manager.get_waifu(str(ctx.author.id))
        embed = table.render()

        if waifu_data and waifu_data.embed_color is not None:
            embed.color = waifu_data.embed_color

        await ctx.send(embed=embed)

    @waifu.command()
    async def forcedojo(ctx: commands.Context) -> None:
        context = get_bot_context(ctx.bot)

        if not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Solo un administrador puede usar este comando.")
            return

        result = context.waifu_manager.spawn_dojo(force=True)
        if not result.get("ok"):
            await ctx.send(f"❌ {result.get('message', 'Cannot spawn dojo.')}")
            return

        dojo_data = result["dojo"]
        embed = EmbedTable(
            headers=["Info"],
            title=f"⛩️ {dojo_data.get('name', 'Nuevo Dojo')}",
            color=discord.Color.gold(),
        ).render()

        if dojo_data.get("image_url"):
            embed.set_image(url=dojo_data["image_url"])

        selected_lines = []
        for user_id in dojo_data.get("selected_players", []):
            member = ctx.guild.get_member(int(user_id))
            selected_lines.append(member.mention if member else f"<@{user_id}>")

        if selected_lines:
            embed.add_field(
                name="Players selected",
                value=", ".join(selected_lines),
                inline=False,
            )

        embed.add_field(
            name="How it works",
            value=(
                "Selected players can use `.noah waifu dojo` to start training. After **30 minutes** of charging, they will receive "
                "**3 random levels** (pending levelups)."
            ),
            inline=False,
        )

        await ctx.send(embed=embed)

    @waifu.command()
    async def help(ctx: commands.Context) -> None:
        await send_help(ctx, "waifu")
