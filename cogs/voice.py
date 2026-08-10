import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import TypedDict, cast

import discord
from discord import app_commands
from discord.ext import commands

import logic
from dictionary_manager import DictionaryManager
from models import Models
from services.audio_engine import AudioEngine
from services.config_store import ConfigStore


class VoiceStyle(TypedDict):
    uuid: str
    style_id: int


class VoiceCog(commands.Cog):
    """VCの参加・切断・音声再生・イベント監視を集約した Cog"""

    def __init__(
        self,
        bot: commands.Bot,
        audio_engine: AudioEngine,
        logger: logging.Logger,
    ):
        self.bot = bot
        self.audio_engine = audio_engine
        self.logger = logger.getChild("voice")
        self.config_store = ConfigStore(logger=self.logger)
        self.dict_manager = DictionaryManager()

        self.text_channels: dict[int, discord.TextChannel] = {}
        self.voice_clients: dict[int, discord.VoiceClient] = {}
        self.voice_channels: dict[int, discord.VoiceChannel] = {}
        self.last_speaker_id: dict[int, int] = {}
        self.last_speak_time: dict[int, datetime] = {}

        self.owner_id = int(os.getenv("OWNER_ID", "0"))
        self.owner_display_name = os.getenv("OWNER_DISPLAY_NAME", "マスター")

        self.se_dir = "Extra/EX_Voice"
        self._update_se_keywords()

    def _update_se_keywords(self) -> None:
        self.se_keywords: dict[str, str] = {}
        if os.path.exists(self.se_dir):
            for file in os.listdir(self.se_dir):
                if file.endswith((".wav", ".mp3")):
                    name = os.path.splitext(file)[0]
                    self.se_keywords[name] = os.path.join(self.se_dir, file)

    def get_style(self, user_id: int) -> tuple[str, int]:
        default_uuid, default_style_id = Models.get_default_style()
        def_val = {"uuid": default_uuid, "style_id": default_style_id}
        style_pref = self.config_store.get_pref(
            user_id,
            "style",
            def_val,
            lambda s: isinstance(s, dict) and "uuid" in s and "style_id" in s,
        )
        s_dict = cast(VoiceStyle, style_pref)
        return str(s_dict["uuid"]), int(s_dict["style_id"])

    async def _put_announcement(self, guild_id: int, text: str) -> None:
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None or guild.me is None:
                return
            await self.audio_engine.enqueue({
                "guild_id": guild_id,
                "author_id": guild.me.id,
                "content": text,
                "sequence_number": 0,
                "total_segments": 1,
            })
        except Exception:
            self.logger.exception("Failed to enqueue announcement")

    # --- コマンド群 ---

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
            await interaction.followup.send("有効なボイス/テキストチャンネルで実行してください。", ephemeral=True)
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

        await self._put_announcement(guild_id, f"【{channel.name}】に参加しました。")

    @app_commands.command(name="skip", description="未再生の読み上げキューをクリアします。")
    async def skip_my_voice(self, interaction: discord.Interaction) -> None:
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
    async def leave(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guild = interaction.guild
        if guild and guild.id in self.voice_clients:
            await interaction.followup.send("読み上げを終了して切断します。")
            vc = self.voice_clients[guild.id]
            guild_id = guild.id

            await self._put_announcement(guild_id, "読み上げを終わります")
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

    # --- イベントリスナー群 ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        if guild_id not in self.voice_clients or not message.content:
            return
        if self.text_channels.get(guild_id) != message.channel:
            return

        member = message.author
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await self._send_and_delete_warning(message.channel)
            return

        bot_vc = self.voice_channels.get(guild_id)
        if bot_vc and member.voice.channel.id != bot_vc.id:
            await self._send_and_delete_warning(message.channel)
            return

        now = discord.utils.utcnow()
        raw_last_time = self.last_speak_time.get(guild_id)
        last_time = raw_last_time if isinstance(raw_last_time, datetime) else datetime.fromtimestamp(0, tz=timezone.utc)

        is_continuous = (self.last_speaker_id.get(guild_id) == message.author.id and (now - last_time).total_seconds() < 60)
        segments = []

        if not is_continuous:
            if message.author.id == self.owner_id:
                name = f"{logic.process_name(self.owner_display_name, self.dict_manager)}さん。 "
            else:
                clean_name = logic.process_name(message.author.display_name, self.dict_manager)
                name = f"{clean_name if clean_name else '名無し'}さん。 "
            segments.append(name)

        processed_text, effects = logic.process_text(message.content, message.guild, self.dict_manager, self.bot)
        non_empty_body = [s for s in logic.split_text(processed_text) if s.strip()]
        segments.extend(non_empty_body)

        if len(segments) > 10:
            segments = segments[:10]
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
                await self.audio_engine.enqueue_play_waiting({
                    "guild_id": guild_id, "group_id": group_id, "file_path": se_path,
                    "sequence_number": seq_idx, "total_segments": total_segments, "effects": effects,
                })
            else:
                cache_path = self.audio_engine.cache_manager.get_cache_path(content, style_uuid)
                if os.path.exists(cache_path):
                    await self.audio_engine.enqueue_play_waiting({
                        "guild_id": guild_id, "group_id": group_id, "file_path": cache_path,
                        "sequence_number": seq_idx, "total_segments": total_segments, "effects": effects,
                    })
                else:
                    await self.audio_engine.enqueue({
                        "guild_id": guild_id, "group_id": group_id, "author_id": message.author.id,
                        "content": content, "sequence_number": seq_idx, "total_segments": total_segments, "effects": effects,
                    })

        self.last_speaker_id[guild_id] = message.author.id
        self.last_speak_time[guild_id] = now

    async def _send_and_delete_warning(self, channel: discord.abc.Messageable) -> None:
        try:
            warn_msg = await channel.send("読み上げをスキップしました。読み上げるためには参加してください")
            await asyncio.sleep(10)
            await warn_msg.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            self.logger.error(f"Failed to handle warning message: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        if guild is None:
            return
        guild_id = guild.id

        # 1. 自動参加(Auto-join)チェック
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
                    if isinstance(voice_channel, discord.VoiceChannel) and isinstance(text_channel, discord.TextChannel):
                        vc = await voice_channel.connect()
                        self.voice_clients[guild_id] = vc
                        self.text_channels[guild_id] = text_channel
                        self.voice_channels[guild_id] = voice_channel
                        
                        self.audio_engine.clear_guild_data(guild_id)
                        await asyncio.sleep(0.5)
                        await self._put_announcement(guild_id, "自動参加しました。読み上げを開始します。")
        except Exception:
            self.logger.exception("Auto-join handling failed")

        if guild_id not in self.voice_clients:
            return

        # 2. アナウンス用の名前決定
        if member.id == self.owner_id:
            name_to_read = logic.process_name(self.owner_display_name, self.dict_manager)
        else:
            clean_name = logic.process_name(member.display_name, self.dict_manager)
            name_to_read = f"{clean_name}さん" if clean_name else "名無しさん"

        # 3. アクション判定 (参加・退出・配信・カメラ)
        action_text = ""
        target_vc = self.voice_channels.get(guild_id)

        if target_vc:
            if before.channel is None and after.channel is not None and after.channel.id == target_vc.id:
                action_text = "が参加しました"
            elif before.channel is not None and after.channel is None and before.channel.id == target_vc.id:
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
            await self.audio_engine.enqueue({
                "guild_id": guild_id,
                "author_id": member.id,
                "content": f"{name_to_read}{action_text}",
                "sequence_number": 0,
                "total_segments": 1,
            })

        # 4. 無人状態での自動切断判定
        if self.bot.user and member.id == self.bot.user.id:
            return

        left_channel = before.channel
        if left_channel is not None and guild_id in self.voice_clients:
            managed_channel = self.voice_channels.get(guild_id)
            if managed_channel and left_channel.id == managed_channel.id:
                human_members = [m for m in left_channel.members if not m.bot]
                if len(human_members) == 0:
                    await self.immediate_disconnect(guild_id)

async def immediate_disconnect(self, guild_id: int) -> None:
        """無人化時に即座に切断し、キューを破棄"""
        self.logger.info(f"Immediate disconnect triggered for guild: {guild_id}")
        vc = self.voice_clients.get(guild_id)

        if vc:
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect(force=True)
            except (discord.ClientException, discord.HTTPException, asyncio.TimeoutError) as e:
                self.logger.error(f"Failed to disconnect cleanly: {e}")

        self.audio_engine.clear_guild_data(guild_id)

        if guild_id in self.text_channels:
            try:
                await self.text_channels[guild_id].send("メンバーがいなくなったため、キューを破棄して切断しました。")
            except discord.HTTPException:
                self.logger.exception("Failed to send disconnect message")
            self.text_channels.pop(guild_id, None)

        self.voice_clients.pop(guild_id, None)
        self.voice_channels.pop(guild_id, None)