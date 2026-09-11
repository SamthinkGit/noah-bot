import asyncio
from dataclasses import dataclass, field

from discord.ext import commands

from noah_bot.modules.ai import AiResponder
from noah_bot.modules.autogami import AutogamiTokenStore
from noah_bot.modules.daily_stats import DailyStatsManager
from noah_bot.modules.discord_formatter import UserEmojiManager
from noah_bot.modules.leaderboard import Leaderboard
from noah_bot.modules.noah_gochi import NoahGochiManager
from noah_bot.modules.noah_gochi_ai import NoahGochiStoryService
from noah_bot.modules.relics_game import RelicsGameManager
from noah_bot.modules.steallist import StealList
from noah_bot.modules.tts import TTSVoiceStore
from noah_bot.modules.voice_manager import VoiceManager
from noah_bot.modules.waifu_game import WaifuGameManager


@dataclass(slots=True)
class BotContext:
    leaderboard: Leaderboard = field(default_factory=Leaderboard)
    emoji_manager: UserEmojiManager = field(default_factory=UserEmojiManager)
    ai_responder: AiResponder = field(default_factory=AiResponder)
    steallist: StealList = field(default_factory=StealList)
    autogami_tokens: AutogamiTokenStore = field(default_factory=AutogamiTokenStore)
    voice_manager: VoiceManager = field(default_factory=VoiceManager)
    tts_voices: TTSVoiceStore = field(default_factory=TTSVoiceStore)
    waifu_manager: WaifuGameManager = field(
        default_factory=lambda: WaifuGameManager(json_path="waifu_game.json")
    )
    relics_manager: RelicsGameManager = field(
        default_factory=lambda: RelicsGameManager(json_path="noah_relics.json")
    )
    gochi_manager: NoahGochiManager = field(
        default_factory=lambda: NoahGochiManager(json_path="noah_gochi.json")
    )
    gochi_story_service: NoahGochiStoryService = field(
        default_factory=NoahGochiStoryService
    )
    daily_stats: DailyStatsManager = field(default_factory=DailyStatsManager)
    autobump_tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    autogami_claim_messages: dict[int, int] = field(default_factory=dict)
    autogami_chest_messages: dict[int, str] = field(default_factory=dict)
    tts_greet_sessions: dict[int, int] = field(default_factory=dict)
    timeit_sessions: dict[int, float] = field(default_factory=dict)
    latest_time_it: float | None = None


def create_bot_context() -> BotContext:
    return BotContext()


def get_bot_context(bot: commands.Bot) -> BotContext:
    context = getattr(bot, "noah_context", None)
    if context is None:
        raise RuntimeError("Bot context has not been initialized.")
    return context
