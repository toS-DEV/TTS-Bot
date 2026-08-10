# 辞書登録・削除・エクスポートコマンド
import io
import logging
import unicodedata

import discord
from discord import app_commands
from discord.ext import commands

from services.dictionary_manager import DictionaryManager


class DictionaryCog(commands.Cog):
    def __init__(self, bot: commands.Bot, dict_manager: DictionaryManager | None = None):
        self.bot = bot
        self.logger = logging.getLogger("bot.dictionary")
        # DictionaryManagerが渡されなければ新規作成、渡されればそれを使用するよ
        self.dict_manager = dict_manager or DictionaryManager()

    @app_commands.command(
        name="add_word", description="カスタム辞書に単語の読み方を追加/更新します。"
    )
    @app_commands.describe(word="元の単語 (例: Wamom)", reading="読み方 (例: わもむ)")
    async def add_word(
        self, interaction: discord.Interaction, word: str, reading: str
    ):
        processed_word = unicodedata.normalize("NFKC", word).lower()

        self.dict_manager.add_word(processed_word, reading)

        # ★ getattr を使って安全に cache_manager を取得（型警告も消えるよ！）
        deleted_count = 0
        cache_manager = getattr(self.bot, "cache_manager", None)
        if cache_manager and hasattr(cache_manager, "clear_cache_containing"):
            deleted_count = cache_manager.clear_cache_containing(processed_word)

        msg = (
            f"✅ カスタム辞書に単語 **`{processed_word}`** を読み方 **`{reading}`** で登録しました。\n"
            f"（元の入力: `{word}` を自動変換しました）"
        )
        if deleted_count > 0:
            msg += f"\n🗑️ 該当する単語が含まれていた古いキャッシュを **{deleted_count}件** 削除したよ！"

        await interaction.response.send_message(msg)

    @app_commands.command(
        name="del_word", description="カスタム辞書から単語を削除します。"
    )
    @app_commands.describe(word="削除したい元の単語")
    async def del_word(self, interaction: discord.Interaction, word: str):
        processed_word = unicodedata.normalize("NFKC", word).lower()

        if self.dict_manager.delete_word(processed_word):
            # ★ ここも getattr を使用！
            deleted_count = 0
            cache_manager = getattr(self.bot, "cache_manager", None)
            if cache_manager and hasattr(cache_manager, "clear_cache_containing"):
                deleted_count = cache_manager.clear_cache_containing(processed_word)

            msg = f"✅ カスタム辞書から単語 **`{processed_word}`** を削除しました。"
            if deleted_count > 0:
                msg += f"\n🗑️ 関連する古いキャッシュを **{deleted_count}件** 削除したよ！"

            await interaction.response.send_message(msg)
        else:
            await interaction.response.send_message(
                f"❌ 単語 **`{processed_word}`** は辞書に見つかりませんでした。",
                ephemeral=True,
            )

    @app_commands.command(
        name="export_dict",
        description="現在のカスタム辞書をCSVファイルとしてダウンロードします。",
    )
    async def export_dict(self, interaction: discord.Interaction):
        csv_content = self.dict_manager.export_to_csv()

        if csv_content is None:
            await interaction.response.send_message("辞書が空です。", ephemeral=True)
            return

        csv_file = discord.File(
            fp=io.BytesIO(csv_content.encode("utf-8")),
            filename="custom_dict.csv",
            description="TTS Custom Dictionary",
        )

        await interaction.response.send_message(
            "✅ カスタム辞書をCSVとしてエクスポートします。", file=csv_file
        )


async def setup(bot: commands.Bot):
    # botにすでにdict_managerが保持されていればそれを渡す設計にしておくよ
    dict_manager = getattr(bot, "dict_manager", None)
    await bot.add_cog(DictionaryCog(bot, dict_manager=dict_manager))
