import unicodedata

import discord
from discord import app_commands
from discord.ext import commands

from services.user_dictionary_manager import UserDictionaryManager

ALLOWED_ROLES = ["鯖民"]
ALLOWED_ADMIN_ROLES = ["管理者"]

EFFECT_CHOICES = [
    app_commands.Choice(name="なし (通常)", value="none"),
    app_commands.Choice(name="🔊 大音量 (特大+低音)", value="header_1"),
    app_commands.Choice(name="🤫 ひそひそ (小音量)", value="subtext"),
    app_commands.Choice(name="🎵 高音 (ピッチ上げ)", value="header_3"),
    app_commands.Choice(name="🎙️ 低音 (ピッチ下げ)", value="low"),
    app_commands.Choice(name="🗣️ エコー (やまびこ)", value="quote"),
    app_commands.Choice(name="⚡ 早口 (1.25倍)", value="fast"),
]


def has_allowed_role(member: discord.Member) -> bool:
    """指定されたロールを持っているか判定"""
    return any(role.name in ALLOWED_ROLES for role in member.roles)

def has_allowed_admin_role(member: discord.Member) -> bool:
    """指定されたロールを持っているか判定"""
    return any(role.name in ALLOWED_ADMIN_ROLES for role in member.roles)


class UserDictionaryCog(commands.Cog):
    """ユーザー個別の読み方を設定する Cog"""

    def __init__(
        self, bot: commands.Bot, user_dict_manager: UserDictionaryManager | None = None
    ):
        self.bot = bot
        self.user_dict_manager = user_dict_manager or UserDictionaryManager()

    @app_commands.command(
        name="my_name",
        description="自分の名前の読み方とエフェクトを設定・削除します。",
    )
    @app_commands.describe(
        reading="新しい読み方 (例: わもむ)。空欄で削除",
        effect="名前を呼ばれる時のボイスエフェクト",
    )
    @app_commands.choices(effect=EFFECT_CHOICES)
    async def set_my_name(
        self,
        interaction: discord.Interaction,
        reading: str | None = None,
        effect: app_commands.Choice[str] | None = None,
    ):
        if not isinstance(interaction.user, discord.Member) or not has_allowed_role(interaction.user):
            await interaction.response.send_message("❌ 権限がありません。", ephemeral=True)
            return

        user_id = interaction.user.id
        selected_effect = effect.value if effect else None

        if reading and reading.strip():
            clean_reading = unicodedata.normalize("NFKC", reading).strip()
            self.user_dict_manager.set_reading(user_id, clean_reading, selected_effect)

            effect_label = effect.name if effect else "なし"
            await interaction.response.send_message(
                f"✅ 名前設定を更新したよ！\n"
                f"・読み方: **`{clean_reading}`**\n"
                f"・エフェクト: **`{effect_label}`**"
            )
        else:
            if self.user_dict_manager.delete_reading(user_id):
                await interaction.response.send_message("✅ 設定を削除したよ！")
            else:
                await interaction.response.send_message("❌ 設定は見つからなかったよ。", ephemeral=True)

    @app_commands.command(
        name="user_name",
        description="他のユーザーの名前の読み方を設定・削除します（読み方を空で送信すると削除）。",
    )
    @app_commands.describe(
        target_user="対象のユーザー",
        reading="新しい読み方 (例: わもむ)。空欄で削除",
    )
    async def set_user_name(
        self,
        interaction: discord.Interaction,
        target_user: discord.User,
        reading: str | None = None,
    ):
        # 1. ロール権限チェック
        if not isinstance(interaction.user, discord.Member) or not has_allowed_admin_role(interaction.user):
            await interaction.response.send_message(
                "❌ このコマンドを実行する権限（ロール）がありません。", ephemeral=True
            )
            return

        user_id = target_user.id

        # 2. 登録 or 削除の分岐
        if reading and reading.strip():
            clean_reading = unicodedata.normalize("NFKC", reading).strip()
            self.user_dict_manager.set_reading(user_id, clean_reading)
            await interaction.response.send_message(
                f"✅ {target_user.mention} さんの名前の読み方を **`{clean_reading}`** に設定したよ！"
            )
        else:
            if self.user_dict_manager.delete_reading(user_id):
                await interaction.response.send_message(
                    f"✅ {target_user.mention} さんの特定読み設定を削除したよ！"
                )
            else:
                await interaction.response.send_message(
                    f"❌ {target_user.mention} さんの設定された読み方は見つからなかったよ。",
                    ephemeral=True,
                )
