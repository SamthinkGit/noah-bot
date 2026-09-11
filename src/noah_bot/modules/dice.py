"""Tiradas de dados con decoracion automatica estilo D&D."""

import random

import discord

D20_SIDES = 20
D20_CRITICAL_SUCCESS = 20
D20_CRITICAL_FAILURE = 1

D20_TIERS: tuple[tuple[int, str, str, discord.Color], ...] = (
    (
        D20_CRITICAL_FAILURE,
        "PIFIA CRITICA",
        "El dado rueda, se detiene y el silencio lo dice todo.",
        discord.Color.from_rgb(155, 17, 30),
    ),
    (
        5,
        "Fallo estrepitoso",
        "Los dioses del azar miran hacia otro lado.",
        discord.Color.from_rgb(196, 77, 86),
    ),
    (
        10,
        "Fallo",
        "Casi. Pero casi no cuenta dentro de una mazmorra.",
        discord.Color.from_rgb(201, 138, 78),
    ),
    (
        14,
        "Exito ajustado",
        "Sale bien por los pelos. Nadie tiene por que enterarse.",
        discord.Color.from_rgb(212, 187, 96),
    ),
    (
        19,
        "Exito",
        "Limpio, preciso y con estilo.",
        discord.Color.from_rgb(106, 168, 112),
    ),
    (
        D20_CRITICAL_SUCCESS,
        "EXITO CRITICO",
        "El bardo ya esta componiendo la cancion.",
        discord.Color.from_rgb(240, 190, 70),
    ),
)


def roll_d20() -> int:
    return random.randint(1, D20_SIDES)


def resolve_d20_tier(roll: int) -> tuple[str, str, discord.Color]:
    for threshold, title, flavor, color in D20_TIERS:
        if roll <= threshold:
            return title, flavor, color

    _, title, flavor, color = D20_TIERS[-1]
    return title, flavor, color


def render_d20_meter(roll: int) -> str:
    filled = max(1, round(roll / D20_SIDES * 10))
    return "#" * filled + "-" * (10 - filled)


def build_d20_embed(
    author: discord.Member | discord.User,
    roll: int | None = None,
) -> discord.Embed:
    result = roll_d20() if roll is None else roll
    tier_title, flavor, color = resolve_d20_tier(result)

    embed = discord.Embed(
        title=f"\N{GAME DIE}  d20 \N{BULLET} {tier_title}",
        description=f"```\n{result:^23}\n```\n*{flavor}*",
        color=color,
    )
    embed.set_author(
        name=f"{author.display_name} lanza el dado",
        icon_url=author.display_avatar.url,
    )
    embed.add_field(name="Dado", value="`1d20`", inline=True)
    embed.add_field(name="Resultado", value=f"`{result}` / `{D20_SIDES}`", inline=True)
    embed.add_field(
        name="Fortuna",
        value=f"`[{render_d20_meter(result)}]`",
        inline=False,
    )

    if result == D20_CRITICAL_SUCCESS:
        embed.set_footer(text="Critico natural - dano maximo y gloria eterna.")
    elif result == D20_CRITICAL_FAILURE:
        embed.set_footer(text="Pifia natural - el DM sonrie y eso nunca es buena senal.")
    else:
        embed.set_footer(text="Que la suerte acompane tu proxima tirada.")

    return embed
