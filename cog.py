import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypedDict, cast

import discord
from discord import app_commands
from discord.ext import commands
from typing_extensions import override

import logic
from cache_manager import VoiceCacheManager
from dictionary_manager import DictionaryManager
from models import Models
from services.audio_engine import AudioEngine, TTSQueueItem
from services.bump_server import BumpServer
from services.config_store import ConfigStore


class VoiceStyle(TypedDict):
    uuid: str
    style_id: int


class TTSCog(commands.Cog):
    bot: commands.Bot
    logger: logging.Logger

    text_channels: dict[int, discord.TextChannel]
    voice_clients: dict[int, discord.VoiceClient]
    voice_channels: dict[int, discord.VoiceChannel]
    dict_manager: DictionaryManager
    cache_manager: VoiceCacheManager
    last_speaker_id: dict[int, int]
    last_speak_time: dict[int, datetime]
    owner_id: int
    owner_display_name: str

    def __init__(self, bot: commands.Bot, logger: logging.Logger):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("bot.ttscog")

        self.config_store = ConfigStore(logger=self.logger)
        self.cache_manager = VoiceCacheManager(max_size_mb=2048)
        self.dict_manager = DictionaryManager()

        self.text_channels = {}
        self.voice_clients = {}
        self.voice_channels = {}
        self.last_speaker_id = {}
        self.last_speak_time = {}
        self.owner_id = int(os.getenv("OWNER_ID", "0"))
        self.owner_display_name = os.getenv("OWNER_DISPLAY_NAME", "マスター")

        # AudioEngine の初期化
        self.audio_engine = AudioEngine(
            bot=self.bot,
            cache_manager=self.cache_manager,
            get_style_fn=self.get_style,
            get_voice_client_fn=lambda g_id: self.voice_clients.get(g_id),
            logger=self.logger,
        )

        self.se_dir = "Extra/EX_Voice"
        self._update_se_keywords()

    async def _enqueue_bump_item(self, item: TTSQueueItem) -> None:
        """BumpServerからのアイテムを安全にキューへ追加する"""
        await self.audio_engine.enqueue(item)

    @override
    async def cog_load(self) -> None:
        logger = self.logger.getChild("cog_load")

        # 音声エンジンのループ開始
        self.audio_engine.start()

        self.bump_server = BumpServer(
            bot=self.bot,
            enqueue_func=self._enqueue_bump_item,
            is_vc_connected_func=lambda guild_id: guild_id in self.voice_clients,
            port=50030,
        )
        await self.bump_server.start()

        logger.info("Initialized TTSCog successfully")

    def _update_se_keywords(self):
        """ディレクトリをスキャンして {ファイル名: フルパス} の辞書を作る"""
        se_dir = "Extra/EX_Voice"
        self.se_keywords = {}
        if os.path.exists(se_dir):
            for file in os.listdir(se_dir):
                if file.endswith((".wav", ".mp3")):
                    name = os.path.splitext(file)[0]
                    self.se_keywords[name] = os.path.join(se_dir, file)

    async def _put_announcement(self, guild_id: int, text: str):
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self.logger.warning(f"Guild {guild_id} not found for announcement")
                return
            data = cast(
                TTSQueueItem,
                {
                    "guild_id": guild_id,
                    "author_id": guild.me.id,
                    "content": text,
                    "sequence_number": 0,
                    "total_segments": 1,
                },
            )
            await self.audio_engine.enqueue(data)
        except Exception:
            self.logger.exception("Failed to enqueue auto-join announcement")

    def on_close(self) -> None:
        logger = self.logger.getChild("on_close")
        self.audio_engine.stop()
        self.config_store.save_prefs()
        logger.info("TTSCog Closed")

    def get_pref(
        self,
        user_id: int,
        key: str,
        def_val: object,
        cond: Callable[[object], bool],
    ) -> object:
        return self.config_store.get_pref(user_id, key, def_val, cond)

    async def set_pref(
        self, interaction: discord.Interaction, key: str, value: Any, message: str
    ):
        self.config_store.set_pref(interaction.user.id, key, value)
        await interaction.response.send_message(message)

    def get_style(self, user_id: int) -> tuple[str, int]:
        default_uuid, default_style_id = Models.get_default_style()
        def_val: dict[str, str | int] = {
            "uuid": default_uuid,
            "style_id": default_style_id,
        }

        style_pref = self.get_pref(
            user_id=user_id,
            key="style",
            def_val=def_val,
            cond=lambda s: isinstance(s, dict) and "uuid" in s and "style_id" in s,
        )

        s_dict = cast(VoiceStyle, style_pref)
        return str(s_dict["uuid"]), int(s_dict["style_id"])

    @app_commands.command(name="join", description="ボイスチャンネルに参加します。")
    async def join(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(
                "サーバー内でのみ実行可能です。", ephemeral=True
            )
            return

        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.followup.send(
                "ボイスチャンネルに接続してから実行してください。", ephemeral=True
            )
            return

        channel = voice_state.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.followup.send(
                "通常のボイスチャンネルに接続してください。", ephemeral=True
            )
            return

        text_channel = interaction.channel
        if not isinstance(text_channel, discord.TextChannel):
            await interaction.followup.send(
                "このチャンネルでは読み上げを開始できません。", ephemeral=True
            )
            return

        if guild.voice_client is None:
            vc = await channel.connect()
            await interaction.followup.send(f"【{channel.name}】に接続しました。")
        else:
            vc = cast(discord.VoiceClient, guild.voice_client)
            await interaction.followup.send("すでに接続しています。", ephemeral=True)

        guild_id = guild.id
        self.text_channels[guild_id] = text_channel
        self.voice_clients[guild_id] = vc
        self.voice_channels[guild_id] = channel

        announcement_text = f"【{channel.name}】に参加しました。"

        join_data = cast(
            TTSQueueItem,
            {
                "guild_id": guild_id,
                "author_id": guild.me.id,
                "content": announcement_text,
                "sequence_number": 0,
                "total_segments": 1,
            },
        )

        await self.audio_engine.enqueue(join_data)

    @app_commands.command(
        name="skip",
        description="自分が送信した、または全ユーザーの未再生の読み上げキューをクリアします。",
    )
    async def skip_my_voice(self, interaction: discord.Interaction):
        await interaction.response.defer()

        guild_id = interaction.guild.id if interaction.guild else None
        if not guild_id:
            await interaction.followup.send(
                "サーバー内でのみ実行可能です。", ephemeral=True
            )
            return

        author_id = interaction.user.id
        is_admin = False
        if isinstance(interaction.user, discord.Member):
            is_admin = interaction.user.guild_permissions.administrator

        vc = self.voice_clients.get(guild_id)
        if vc and vc.is_playing() and is_admin:
            vc.stop()
            await interaction.followup.send("管理者の権限で現在の再生を停止したよ。")

        self.audio_engine.filter_user_queue(guild_id, author_id, is_admin)

        await interaction.followup.send(
            f"{interaction.user.display_name}さんの未再生の読み上げキューをクリアしたよ。"
        )

    @app_commands.command(name="leave", description="ボイスチャンネルから切断します。")
    async def leave(self, interaction: discord.Interaction):
        await interaction.response.defer()

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        if guild.id in self.voice_clients:
            await interaction.followup.send("読み上げを終了して切断します。")
            vc = self.voice_clients[guild.id]
            bot_member = guild.me
            guild_id = guild.id

            announcement_text = "読み上げを終わります"

            leave_data = cast(
                TTSQueueItem,
                {
                    "guild_id": guild_id,
                    "author_id": bot_member.id,
                    "content": announcement_text,
                    "sequence_number": 0,
                    "total_segments": 1,
                },
            )

            await self.audio_engine.enqueue(leave_data)
            await asyncio.sleep(0.5)

            max_wait = 20
            wait_count = 0
            while (
                not self.audio_engine.is_empty()
                or vc.is_playing()
                or self.audio_engine.is_reading.get(guild_id, False)
            ) and wait_count < max_wait:
                await asyncio.sleep(0.5)
                wait_count += 1

            if vc.is_connected():
                await vc.disconnect()

            self.text_channels.pop(guild_id, None)
            self.voice_clients.pop(guild_id, None)
            self.voice_channels.pop(guild_id, None)
        else:
            await interaction.followup.send(
                "Botはボイスチャンネルに参加していません。", ephemeral=True
            )

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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        if guild_id not in self.voice_clients or not message.content:
            return
        if self.text_channels.get(guild_id) != message.channel:
            return

        member = message.author
        if (
            not isinstance(member, discord.Member)
            or not member.voice
            or not member.voice.channel
        ):
            await self._send_and_delete_warning(message.channel)
            return

        bot_vc = self.voice_channels.get(guild_id)
        if bot_vc and member.voice.channel.id != bot_vc.id:
            await self._send_and_delete_warning(message.channel)
            return

        now = discord.utils.utcnow()
        raw_last_time = self.last_speak_time.get(guild_id)
        last_time = (
            raw_last_time
            if isinstance(raw_last_time, datetime)
            else datetime.fromtimestamp(0, tz=timezone.utc)
        )

        is_continuous = (
            self.last_speaker_id.get(guild_id) == message.author.id
            and (now - last_time).total_seconds() < 60
        )

        segments = []

        if not is_continuous:
            if message.author.id == self.owner_id:
                name = f"{logic.process_name(self.owner_display_name, self.dict_manager)}さん。 "
            else:
                clean_name = logic.process_name(
                    message.author.display_name, self.dict_manager
                )
                name = f"{clean_name if clean_name else '名無し'}さん。 "
            segments.append(name)

        processed_text, effects = logic.process_text(
            message.content, message.guild, self.dict_manager, self.bot
        )
        non_empty_body = [s for s in logic.split_text(processed_text) if s.strip()]
        segments.extend(non_empty_body)

        MAX_SEGMENTS = 10
        if len(segments) > MAX_SEGMENTS:
            segments = segments[:MAX_SEGMENTS]
            segments.append("以下略。")

        if not segments:
            return

        total_segments = len(segments)
        group_id = self.audio_engine.generate_group_id(guild_id)
        style_uuid, _ = self.get_style(message.author.id)

        self._update_se_keywords()

        for seq_idx, content in enumerate(segments):
            se_path = self.se_keywords.get(content)
            if se_path:
                await self.audio_engine.enqueue_play_waiting(
                    cast(
                        TTSQueueItem,
                        {
                            "guild_id": guild_id,
                            "group_id": group_id,
                            "file_path": se_path,
                            "sequence_number": seq_idx,
                            "total_segments": total_segments,
                            "effects": effects,
                        },
                    )
                )
                continue

            cache_path = self.cache_manager.get_cache_path(content, style_uuid)
            if os.path.exists(cache_path):
                await self.audio_engine.enqueue_play_waiting(
                    cast(
                        TTSQueueItem,
                        {
                            "guild_id": guild_id,
                            "group_id": group_id,
                            "file_path": cache_path,
                            "sequence_number": seq_idx,
                            "total_segments": total_segments,
                            "effects": effects,
                        },
                    )
                )
            else:
                await self.audio_engine.enqueue(
                    cast(
                        TTSQueueItem,
                        {
                            "guild_id": guild_id,
                            "group_id": group_id,
                            "author_id": message.author.id,
                            "content": content,
                            "sequence_number": seq_idx,
                            "total_segments": total_segments,
                            "effects": effects,
                        },
                    )
                )

        self.last_speaker_id[guild_id] = message.author.id
        self.last_speak_time[guild_id] = now

        await self.bot.process_commands(message)

    async def _send_and_delete_warning(self, channel: discord.abc.Messageable) -> None:
        try:
            warn_msg = await channel.send(
                "読み上げをスキップしました。読み上げるためには参加してください"
            )
            await asyncio.sleep(10)
            await warn_msg.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            self.logger.error(f"Failed to handle warning message: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        guild = member.guild
        if guild is None:
            return

        guild_id = guild.id

        # --- 1. Auto-join ロジック ---
        try:
            config = self.config_store.get_auto_join_config(guild_id)
            if config is not None:
                target_vc_id = config.get("voice_channel_id")
                target_tc_id = config.get("text_channel_id")

                if (
                    before.channel is None
                    and after.channel is not None
                    and after.channel.id == target_vc_id
                    and member.guild.voice_client is None
                    and guild_id not in self.voice_clients
                ):
                    voice_channel = self.bot.get_channel(target_vc_id)
                    text_channel = self.bot.get_channel(target_tc_id)
                    if isinstance(
                        voice_channel, discord.VoiceChannel
                    ) and isinstance(text_channel, discord.TextChannel):
                        vc = await voice_channel.connect()
                        self.voice_clients[guild_id] = vc
                        self.text_channels[guild_id] = text_channel
                        self.voice_channels[guild_id] = voice_channel
                        
                        # ギルドデータクリア
                        self.audio_engine.clear_guild_data(guild_id)

                        await asyncio.sleep(0.5)
                        await self._put_announcement(
                            guild_id, "自動参加しました。読み上げを開始します。"
                        )
        except Exception:
            self.logger.exception("Auto-join handling failed")

        if guild_id not in self.voice_clients:
            return

        # --- 2. 共通の名前決定ロジック ---
        if member.id == self.owner_id:
            name_to_read = (
                f"{logic.process_name(self.owner_display_name, self.dict_manager)}"
            )
        else:
            clean_name = logic.process_name(member.display_name, self.dict_manager)
            if clean_name:
                name_to_read = f"{clean_name}さん"
            else:
                name_to_read = "名無しさん"

        # --- 3. アクション判定 (参加・退出・配信) ---
        action_text = ""
        target_vc = self.voice_channels.get(guild_id)

        if target_vc:
            if (
                before.channel is None
                and after.channel is not None
                and after.channel.id == target_vc.id
            ):
                action_text = "が参加しました"
            elif (
                before.channel is not None
                and after.channel is None
                and before.channel.id == target_vc.id
            ):
                action_text = "が退出しました"
            elif after.channel is not None and after.channel.id == target_vc.id:
                b_video = getattr(before, "self_video", False)
                a_video = getattr(after, "self_video", False)
                b_stream = getattr(before, "self_stream", False)
                a_stream = getattr(after, "self_stream", False)

                if not b_video and a_video:
                    action_text = "がカメラを開始しました"
                elif b_video and not a_video:
                    action_text = "がカメラを終了しました"
                elif not b_stream and a_stream:
                    action_text = "がライブ配信を開始しました"
                elif b_stream and not a_stream:
                    action_text = "がライブ配信を終了しました"

        if action_text:
            await self.audio_engine.enqueue(
                cast(
                    TTSQueueItem,
                    {
                        "guild_id": guild_id,
                        "author_id": member.id,
                        "content": f"{name_to_read}{action_text}",
                        "sequence_number": 0,
                        "total_segments": 1,
                    },
                )
            )

        # --- 4. 自動切断処理 ---
        if self.bot.user and member.id == self.bot.user.id:
            return

        left_channel = before.channel
        if left_channel is not None and guild_id in self.voice_clients:
            managed_channel = self.voice_channels.get(guild_id)
            if managed_channel and left_channel.id == managed_channel.id:
                human_members = [m for m in left_channel.members if not m.bot]
                if len(human_members) == 0:
                    await self.immediate_disconnect(guild_id)

    async def immediate_disconnect(self, guild_id: int):
        """メンバー不在時に即座に切断し、残ったキューや再生バッファを完全に破棄する"""
        self.logger.info(f"Immediate disconnect triggered for guild: {guild_id}")
        vc = self.voice_clients.get(guild_id)

        if vc:
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect(force=True)
            except Exception as e:  # noqa: BLE001
                self.logger.error(f"Failed to disconnect cleanly: {e}")

        # エンジン側のギルドデータクリア
        self.audio_engine.clear_guild_data(guild_id)

        if guild_id in self.text_channels:
            try:
                await self.text_channels[guild_id].send(
                    "メンバーがいなくなったため、キューを破棄して切断しました。"
                )
            except Exception:
                self.logger.exception("Failed to send disconnect message")
            self.text_channels.pop(guild_id, None)

        self.voice_clients.pop(guild_id, None)
        self.voice_channels.pop(guild_id, None)


async def setup(bot: commands.Bot):
    logger: logging.Logger = getattr(bot, "logger", logging.getLogger("bot"))
    await bot.add_cog(TTSCog(bot, logger))