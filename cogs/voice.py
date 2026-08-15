import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import TypedDict, cast

import discord
from discord import app_commands
from discord.ext import commands

import utils.text_processor as logic
from models.voice_style import Models
from services.audio_engine import AudioEngine
from services.config_store import ConfigStore
from services.dictionary_manager import DictionaryManager
from services.user_dictionary_manager import UserDictionaryManager


class VoiceStyle(TypedDict):
    uuid: str
    style_id: int


class VoiceCog(commands.Cog):
    """VCの参加・切断・読み上げイベント監視を行う Cog"""

    def __init__(
        self,
        bot: commands.Bot,
        audio_engine: AudioEngine,
        logger: logging.Logger,
        user_dict_manager: UserDictionaryManager | None = None,
    ):
        self.bot = bot
        self.audio_engine = audio_engine
        self.logger = logger.getChild("voice")
        self.config_store = ConfigStore(logger=self.logger)
        self.dict_manager = DictionaryManager()
        self.user_dict_manager = user_dict_manager or getattr(bot, "user_dict_manager", None)
        self.text_channels: dict[int, discord.TextChannel] = {}
        self.voice_clients: dict[int, discord.VoiceClient] = {}
        self.voice_channels: dict[int, discord.VoiceChannel] = {}
        self.last_speaker_id: dict[int, int] = {}
        self.last_speak_time: dict[int, datetime] = {}

        self.se_dir = "Extra/EX_Voice"
        self._update_se_keywords()

    def _update_se_keywords(self):
        self.se_keywords = {}
        if os.path.exists(self.se_dir):
            for file in os.listdir(self.se_dir):
                if file.endswith((".wav", ".mp3")):
                    name = os.path.splitext(file)[0]
                    self.se_keywords[name] = os.path.join(self.se_dir, file)

    def get_style(self, user_id: int) -> tuple[str, int]:
        default_uuid, default_style_id = Models.get_default_style()
        def_val = {"uuid": default_uuid, "style_id": default_style_id}
        style_pref = self.config_store.get_pref(
            user_id, "style", def_val, lambda s: isinstance(s, dict) and "uuid" in s
        )
        s_dict = cast(VoiceStyle, style_pref)
        return str(s_dict["uuid"]), int(s_dict["style_id"])

    # ------------------------------------------------------------------
    # 自動参加 & ボイス状態更新イベント
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """ユーザーのVC入退出を監視し、自動参加および無人切断を行う"""
        if member.bot or not member.guild:
            return

        guild = member.guild
        guild_id = guild.id

        # 1. 自動参加ロジック (VCに入室したとき)
        if after.channel and before.channel != after.channel:
            config = self.config_store.get_auto_join_config(guild_id)
            is_connected = guild_id in self.voice_clients and self.voice_clients[guild_id].is_connected()
            if (
                config
                and config.get("voice_channel_id") == after.channel.id
                and not is_connected
            ):
                text_channel = guild.get_channel(config.get("text_channel_id"))
                if isinstance(text_channel, discord.TextChannel) and isinstance(
                    after.channel, discord.VoiceChannel

                ):
                    try:
                        vc = await after.channel.connect()
                        self.voice_clients[guild_id] = vc
                        self.text_channels[guild_id] = text_channel
                        self.voice_channels[guild_id] = after.channel

                        await self.audio_engine.enqueue({
                            "guild_id": guild_id,
                            "author_id": guild.me.id,
                            "content": f"自動参加しました。【{after.channel.name}】の読み上げを開始します。",
                            "sequence_number": 0,
                            "total_segments": 1,
                        })
                        self.logger.info(
                            f"Auto-joined {after.channel.name} in {guild.name}"
                        )
                    except Exception:
                        self.logger.exception(
                            f"Failed to auto-join VC in {guild.name}"
                        )

        # 2. 参加・退出・カメラ・配信通知の読み上げロジック
        action_text = ""
        target_vc = self.voice_channels.get(guild_id)

        if target_vc:
            # 参加
            if (
                before.channel is None
                and after.channel is not None
                and after.channel.id == target_vc.id
            ):
                action_text = "が参加しました"

            # 退出
            elif (
                before.channel is not None
                and after.channel is None
                and before.channel.id == target_vc.id
            ):
                action_text = "が退出しました"

            # カメラ・配信（参加・退出以外のイベントで、対象VCにいる場合）
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
            # ユーザー辞書から読みとエフェクトを取得
            user_reading, user_effect = (
                self.user_dict_manager.get_user_data(member.id)
                if self.user_dict_manager
                else (None, None)
            )

            name_effects: dict[str, bool] = {}
            if user_effect:
                name_effects[user_effect] = True

            if user_reading:
                name_to_read = user_reading
            else:
                clean_name = logic.process_name(member.display_name, self.dict_manager)
                name_to_read = f"{clean_name if clean_name else '名無し'}さん"

            full_text = f"{name_to_read}{action_text}"

            await self.audio_engine.enqueue(
                {
                    "guild_id": guild_id,
                    "author_id": member.id,
                    "content": full_text,
                    "sequence_number": 0,
                    "total_segments": 1,
                    "effects": cast(dict[str, bool] | None, name_effects),
                }
            )

        # 2. 自動切断ロジック (VCにBot以外のメンバーがいなくなったとき)
        if before.channel and guild_id in self.voice_clients:
            bot_vc = self.voice_channels.get(guild_id)
            if bot_vc and before.channel.id == bot_vc.id:
                # Bot以外のメンバー（非Bot）数をカウント
                non_bot_members = [m for m in bot_vc.members if not m.bot]
                if len(non_bot_members) == 0:
                    vc = self.voice_clients[guild_id]
                    if vc.is_connected():
                        await vc.disconnect()

                    self.text_channels.pop(guild_id, None)
                    self.voice_clients.pop(guild_id, None)
                    self.voice_channels.pop(guild_id, None)
                    self.logger.info(
                        f"Auto-left {bot_vc.name} in {guild.name} (No human members left)"
                    )


    # ------------------------------------------------------------------
    # 自動参加設定コマンド
    # ------------------------------------------------------------------
    @app_commands.command(
        name="autojoin", description="自動参加するボイスチャンネルと読み上げテキストチャンネルを設定します。"
    )
    @app_commands.describe(
        voice_channel="自動参加対象のボイスチャンネル",
        text_channel="読み上げ対象のテキストチャンネル (指定しない場合は現在のチャンネル)",
    )
    async def set_autojoin(
        self,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel,
        text_channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer()
        guild = interaction.guild
        if not guild:
            await interaction.followup.send("サーバー内でのみ実行可能です。", ephemeral=True)
            return

        target_text = text_channel or interaction.channel
        if not isinstance(target_text, discord.TextChannel):
            await interaction.followup.send("有効なテキストチャンネルを指定してください。", ephemeral=True)
            return

        self.config_store.set_auto_join_config(
            guild_id=guild.id,
            voice_channel_id=voice_channel.id,
            text_channel_id=target_text.id,
        )

        await interaction.followup.send(
            f"自動参加設定を更新しました！\n"
            f"・対象VC: **{voice_channel.name}**\n"
            f"・読み上げテキスト: **{target_text.name}**"
        )

    # ------------------------------------------------------------------
    # 既存コマンド (join / skip / leave)
    # ------------------------------------------------------------------
    @app_commands.command(name="join", description="ボイスチャンネルに参加します。")
    async def join(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("サーバー内でのみ実行可能です。", ephemeral=True)
            return

        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.followup.send("ボイスチャンネルに接続してから実行してください。", ephemeral=True)
            return

        channel = voice_state.channel
        text_channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel) or not isinstance(text_channel, discord.TextChannel):
            await interaction.followup.send("有効なテキスト/ボイスチャンネルで実行してください。", ephemeral=True)
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

        await self.audio_engine.enqueue({
            "guild_id": guild_id,
            "author_id": guild.me.id,
            "content": f"【{channel.name}】に参加しました。",
            "sequence_number": 0,
            "total_segments": 1,
        })

    @app_commands.command(name="skip", description="未再生の読み上げキューをクリアします。")
    async def skip_my_voice(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = interaction.guild.id if interaction.guild else None
        if not guild_id:
            await interaction.followup.send("サーバー内でのみ実行可能です。", ephemeral=True)
            return

        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator
        vc = self.voice_clients.get(guild_id)
        if vc and vc.is_playing() and is_admin:
            vc.stop()
            await interaction.followup.send("管理者の権限で現在の再生を停止したよ。")

        self.audio_engine.filter_user_queue(guild_id, interaction.user.id, is_admin)
        await interaction.followup.send(f"{interaction.user.display_name}さんの未再生キューをクリアしたよ。")

    @app_commands.command(name="leave", description="ボイスチャンネルから切断します。")
    async def leave(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild
        if guild and guild.id in self.voice_clients:
            await interaction.followup.send("読み上げを終了して切断します。")
            vc = self.voice_clients[guild.id]
            guild_id = guild.id

            await self.audio_engine.enqueue({
                "guild_id": guild_id,
                "author_id": guild.me.id,
                "content": "読み上げを終わります",
                "sequence_number": 0,
                "total_segments": 1,
            })
            await asyncio.sleep(0.5)

            wait_count = 0
            while (not self.audio_engine.is_empty() or vc.is_playing() or self.audio_engine.is_reading.get(guild_id, False)) and wait_count < 20:
                await asyncio.sleep(0.5)
                wait_count += 1

            if vc.is_connected():
                await vc.disconnect()

            self.text_channels.pop(guild_id, None)
            self.voice_clients.pop(guild_id, None)
            self.voice_channels.pop(guild_id, None)
        else:
            await interaction.followup.send("Botはボイスチャンネルに参加していません。", ephemeral=True)

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
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            return

        bot_vc = self.voice_channels.get(guild_id)
        if bot_vc and member.voice.channel.id != bot_vc.id:
            return

        now = discord.utils.utcnow()
        raw_last_time = self.last_speak_time.get(guild_id)
        last_time = raw_last_time if isinstance(raw_last_time, datetime) else datetime.fromtimestamp(0, tz=timezone.utc)

        is_continuous = (self.last_speaker_id.get(guild_id) == message.author.id and (now - last_time).total_seconds() < 60)

        # 1. テキストの基本整形（文字置換や辞書適用）
        processed_text = logic.process_text(message.content, message.guild, self.dict_manager, self.bot)

        # 2. 句読点や改行で本文を文ごとに分割
        raw_segments = [s for s in logic.split_text(processed_text) if s.strip()]

        if len(raw_segments) > 50:
            raw_segments = raw_segments[:50]
            raw_segments.append("以下略。")

        segments_with_effects: list[tuple[str, dict[str, bool]]] = []

        # 連続発言でない場合は名前を追加
        if not is_continuous:
            user_reading, user_effect = (
                self.user_dict_manager.get_user_data(message.author.id)
                if self.user_dict_manager
                else (None, None)
            )

            name_effects: dict[str, bool] = {}
            if user_effect:
                name_effects[user_effect] = True

            if user_reading:
                name = f"{user_reading}。 "
            else:
                clean_name = logic.process_name(message.author.display_name, self.dict_manager)
                name = f"{clean_name if clean_name else '名無し'}さん。 "

            segments_with_effects.append((name, name_effects))
        lines = message.content.splitlines()

        for line in lines:
            if not line.strip():
                continue

            # 1. まず「行頭記号 (#, ##, ###, -#, >)」を抽出・除去
            line_text, line_effects = logic.extract_line_effects(line)

            # 2. テキストの基本整形（辞書適用や文字置換など）
            processed_line = logic.process_text(line_text, message.guild, self.dict_manager, self.bot)

            # 3. 句読点などで細かくセグメント分割
            raw_segments = [s for s in logic.split_text(processed_line) if s.strip()]

            # 4. 各セグメントに対して「インライン記号 (**, ~~ など)」を抽出
            for seg in raw_segments:
                clean_seg, inline_effects = logic.extract_inline_effects(seg)
                if clean_seg.strip():
                    # 行頭エフェクトとインラインエフェクトを合体！
                    combined_effects = {**line_effects, **inline_effects}
                    segments_with_effects.append((clean_seg, combined_effects))

        if not segments_with_effects:
            return

        total_segments = len(segments_with_effects)
        group_id = self.audio_engine.generate_group_id(guild_id)
        style_uuid, _ = self.get_style(message.author.id)

        for seq_idx, (content, effects) in enumerate(segments_with_effects):
            se_path = self.se_keywords.get(content)
            if se_path:
                await self.audio_engine.enqueue_play_waiting({
                    "guild_id": guild_id,
                    "group_id": group_id,
                    "file_path": se_path,
                    "sequence_number": seq_idx,
                    "total_segments": total_segments,
                    "effects": effects,
                })
            else:
                cache_path = self.audio_engine.cache_manager.get_cache_path(content, style_uuid)
                if cache_path.exists():
                    await self.audio_engine.enqueue_play_waiting({
                        "guild_id": guild_id,
                        "group_id": group_id,
                        "file_path": str(cache_path),
                        "sequence_number": seq_idx,
                        "total_segments": total_segments,
                        "effects": effects,
                    })
                else:
                    await self.audio_engine.enqueue({
                        "guild_id": guild_id,
                        "group_id": group_id,
                        "author_id": message.author.id,
                        "content": content,
                        "sequence_number": seq_idx,
                        "total_segments": total_segments,
                        "effects": effects,
                    })

        self.last_speaker_id[guild_id] = message.author.id
        self.last_speak_time[guild_id] = now

        await self.bot.process_commands(message)

    async def _send_and_delete_warning(self, channel: discord.abc.Messageable) -> None:
            """警告メッセージを送信し、10秒後に自動削除する"""
            try:
                warn_msg = await channel.send(
                    "読み上げをスキップしました。読み上げるためには参加してください"
                )
                await asyncio.sleep(10)
                await warn_msg.delete()
            except discord.NotFound:
                # 10秒経つ前にユーザーがメッセージを消していた場合のクリーンアップ
                pass
            except discord.HTTPException as e:
                self.logger.error(f"Failed to handle warning message: {e}")
