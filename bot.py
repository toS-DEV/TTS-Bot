import logging
import os  # os モジュールを追加
import sys

import coloredlogs
import discord
from discord.ext import commands
from dotenv import load_dotenv

from cog import TTSCog

load_dotenv()


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.logger = logging.getLogger("bot")
        std_handler = logging.StreamHandler(stream=sys.stdout)
        std_handler.setLevel(logging.DEBUG)
        self.logger.addHandler(std_handler)
        coloredlogs.CAN_USE_BOLD_FONT = True
        coloredlogs.DEFAULT_FIELD_STYLES = {
            "asctime": {"color": "black", "bright": True},
            "hostname": {"color": "magenta"},
            "levelname": {"color": "blue", "bright": True},
            "name": {"color": "blue"},
            "programname": {"color": "cyan"},
        }
        coloredlogs.DEFAULT_LEVEL_STYLES = {
            "critical": {"color": "red", "bold": True},
            "error": {"color": "red"},
            "warning": {"color": "yellow"},
            "notice": {"color": "magenta"},
            "info": {},
            "debug": {"color": "green"},
            "spam": {"color": "green", "faint": True},
            "success": {"color": "green", "bold": True},
            "verbose": {"color": "blue"},
        }
        coloredlogs.install(
            level="DEBUG",
            logger=self.logger,
            fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    async def setup_hook(self):
        """ボット起動時に非同期で一度だけ実行される初期化フックだよ"""
        logger = self.logger.getChild("setup_hook")
        try:
            # cog.py を拡張機能としてロードするよ（ファイル名が cog.py なので "cog"）
            await self.load_extension("cog")
            logger.info("Successfully loaded extension: cog")
        except Exception as e:
            logger.error(f"Failed to load extension cog: {e}")
            import traceback
            traceback.print_exc()

    async def on_ready(self):
        logger = self.logger.getChild("on_ready")

        # スラッシュコマンドをDiscord側と同期する
        await self.tree.sync()
        logger.info("Synced command tree to the guild")
        
        activity = discord.Activity(type=discord.ActivityType.playing, name="稼働中")
        await self.change_presence(status=discord.Status.online, activity=activity)
        logger.info(f"Logged in as {self.user}")

    async def close(self):
        logger = self.logger.getChild("on_close")
        # 拡張機能として読み込んでいるので、Botが閉じる前にCogのクリーンアップを呼ぶ
        await super().close()
        logger.info("Closed")

# 最後にボットを起動する部分
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")

    if not TOKEN:
        print("FATAL: DISCORD_TOKEN 環境変数が設定されていません。")
        exit(1)

    try:
        # MyBot() がボットクラスのインスタンス名だと仮定
        my_bot_instance = MyBot()
        my_bot_instance.run(TOKEN)
    except Exception as e:
        import traceback

        print(f"FATAL: ボットの起動中に予期せぬエラーが発生しました: {e}")
        traceback.print_exc()
        exit(1)
