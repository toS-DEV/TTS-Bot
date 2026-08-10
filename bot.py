#!/usr/bin/env python3
import logging
import os
import sys

import coloredlogs
import discord
from discord.ext import commands
from dotenv import load_dotenv

from cache_manager import VoiceCacheManager
from cogs.config import ConfigCog
from cogs.voice import VoiceCog
from services.audio_engine import AudioEngine
from services.bump_server import BumpServer

_ = load_dotenv()


class MyBot(commands.Bot):
    logger: logging.Logger

    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self._setup_logger()

        # 共通マネージャー・サービスの初期化
        self.cache_manager = VoiceCacheManager(max_size_mb=2048)
        self.voice_cog: VoiceCog | None = None

        # AudioEngine の初期化（VoiceCog の voice_clients を安全に参照）
        self.audio_engine = AudioEngine(
            bot=self,
            cache_manager=self.cache_manager,
            get_style_fn=self._get_user_style,
            get_voice_client_fn=self._get_voice_client,
            logger=self.logger,
        )

    def _setup_logger(self):
        self.logger = logging.getLogger("bot")
        std_handler = logging.StreamHandler(stream=sys.stdout)
        std_handler.setLevel(logging.DEBUG)
        self.logger.addHandler(std_handler)
        
        coloredlogs.install(
            level="DEBUG",
            logger=self.logger,
            fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def _get_voice_client(self, guild_id: int) -> discord.VoiceClient | None:
        if self.voice_cog:
            return self.voice_cog.voice_clients.get(guild_id)
        return None

    def _get_user_style(self, user_id: int) -> tuple[str, int]:
        if self.voice_cog:
            return self.voice_cog.get_style(user_id)
        return ("default_uuid", 0)

    async def setup_hook(self):
        """ボット起動時の初期化処理"""
        logger = self.logger.getChild("setup_hook")

        # 1. 音声エンジンのスタート
        self.audio_engine.start()

        # 2. Cog の追加（明示的にインスタンス化して追加）
        config_cog = ConfigCog(self, self.logger)
        self.voice_cog = VoiceCog(self, self.audio_engine, self.logger)

        await self.add_cog(config_cog)
        await self.add_cog(self.voice_cog)
        logger.info("Successfully loaded ConfigCog and VoiceCog")

        # 3. BumpServer の起動
        self.bump_server = BumpServer(
            bot=self,
            enqueue_func=self.audio_engine.enqueue,
            is_vc_connected_func=lambda g_id: self._get_voice_client(g_id) is not None,
            port=50030,
        )
        await self.bump_server.start()

    async def on_ready(self):
        logger = self.logger.getChild("on_ready")
        await self.tree.sync()
        logger.info("Synced command tree to the guild")

        activity = discord.Activity(type=discord.ActivityType.playing, name="稼働中")
        await self.change_presence(status=discord.Status.online, activity=activity)
        logger.info(f"Logged in as {self.user}")

    async def close(self):
        logger = self.logger.getChild("on_close")
        # リソースの破棄処理
        if hasattr(self, "audio_engine"):
            self.audio_engine.stop()
        if self.voice_cog and hasattr(self.voice_cog, "config_store"):
            self.voice_cog.config_store.save_prefs()
        await super().close()
        logger.info("Closed")


if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        print("FATAL: DISCORD_TOKEN 環境変数が設定されていません。")
        sys.exit(1)

    my_bot_instance = MyBot()
    my_bot_instance.run(TOKEN)