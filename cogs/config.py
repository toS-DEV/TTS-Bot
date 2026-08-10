import logging

import discord
from discord import app_commands
from discord.ext import commands
from services.config_store import ConfigStore


class ConfigCog(commands.Cog):
    """設定関係のコマンドと永続化処理を管理する Cog"""

    def __init__(self, bot: commands.Bot, logger: logging.Logger):
        self.bot = bot
        self.logger = logger.getChild("config")
        self.config_store = ConfigStore(logger=self.logger)

    @app_commands.command(
        name="auto_join",
        description="自動参加設定の構成（サーバー管理権限が必要です）",
    )
    @app_commands.describe(
        voice_channel="ボイスチャンネル", text_channel="テキストチャンネル"
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def auto_join(
        self,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel,
        text_channel: discord.TextChannel,
    ):
        """自動参加設定を保存します。"""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        self.config_store.set_auto_join_config(
            guild.id, voice_channel.id, text_channel.id
        )

        await interaction.response.send_message(
            f"自動参加設定を保存しました。ボイス: {voice_channel.name}, テキスト: {text_channel.name}"
        )


async def setup(bot: commands.Bot):
    logger: logging.Logger = getattr(bot, "logger", logging.getLogger("bot"))
    await bot.add_cog(ConfigCog(bot, logger))