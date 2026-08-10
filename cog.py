import asyncio
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict, cast

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks
from typing_extensions import NotRequired, override

import logic
from cache_manager import VoiceCacheManager
from dictionary_manager import DictionaryManager
from models import Models
from services.audio_engine import TTSQueueItem
from services.bump_server import BumpServer
from services.config_store import ConfigStore


class BumpNotificationData(TypedDict):
    guild_id: NotRequired[int | str | None]
    message: NotRequired[str | None]


class VoiceStyle(TypedDict):
    uuid: str
    style_id: int


class TTSCog(commands.Cog):
    bot: commands.Bot
    logger: logging.Logger

    text_channels: dict[int, discord.TextChannel]
    voice_clients: dict[int, discord.VoiceClient]
    voice_channels: dict[int, discord.VoiceChannel]
    queue: asyncio.Queue[TTSQueueItem] | None
    is_reading: dict[int, bool]
    _queue_lock: asyncio.Lock | None
    dict_manager: DictionaryManager
    cache_manager: VoiceCacheManager
    last_speaker_id: dict[int, int]
    last_speak_time: dict[int, datetime]
    play_waiting_queue: asyncio.Queue[TTSQueueItem]
    owner_id: int
    owner_display_name: str
    next_event: asyncio.Event | None
    api_semaphore: asyncio.Semaphore

    play_groups: dict[int, dict[str, Any]]
    play_group_counters: dict[int, int]
    play_wait_start: dict[int, dict[str, float | None]]

    def __init__(self, bot: commands.Bot, logger: logging.Logger):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("bot.ttscog")

        self.config_store = ConfigStore(logger=self.logger)

        self.text_channels = {}
        self.voice_clients = {}
        self.voice_channels = {}
        self.is_reading = {}
        self.last_speaker_id = {}
        self.last_speak_time = {}
        self.dict_manager = DictionaryManager()
        self.cache_manager = VoiceCacheManager(max_size_mb=2048)
        self.owner_id = int(os.getenv("OWNER_ID", "0"))
        self.owner_display_name = os.getenv("OWNER_DISPLAY_NAME", "マスター")
        self.queue = asyncio.Queue()
        self.play_waiting_queue = asyncio.Queue()
        self._queue_lock = asyncio.Lock()
        self.next_event = asyncio.Event()
        self.api_semaphore = asyncio.Semaphore(1)
        self.play_groups = {}
        self.play_group_counters = {}
        self.play_wait_start = {}
        self.loop = bot.loop
        self.se_dir = "Extra/EX_Voice"
        self._update_se_keywords()
        self._http_server_task: asyncio.Task | None = None

    async def _enqueue_bump_item(self, item: TTSQueueItem) -> None:
        """BumpServerからのアイテムを安全にキューへ追加する"""
        if self.queue is not None:
            await self.queue.put(item)
        else:
            self.logger.warning("Queue is not initialized. Dropping bump item.")

    @override
    async def cog_load(self) -> None:
        logger = self.logger.getChild("cog_load")
        self.queue = asyncio.Queue()
        self.play_waiting_queue = asyncio.Queue()
        self._queue_lock = asyncio.Lock()
        self.next_event = asyncio.Event()
        self.api_semaphore = asyncio.Semaphore(1)
        if not self.generation_loop.is_running():
            self.generation_loop.start()
        if not self.playback_loop.is_running():
            self.playback_loop.start()

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

    async def _play_audio_and_wait(
        self,
        vc: discord.VoiceClient,
        audio_path: str,
        next_audio_path: str | None = None,
        playback_timeout: float = 30.0,
        effects: dict[str, bool] | None = None,
    ) -> None:
        """再生して終了まで待機する。"""
        if not vc or not vc.is_connected():
            return

        if audio_path is None or not os.path.exists(audio_path):
            self.logger.warning(f"Audio path is invalid or missing: {audio_path}")
            return

        local_event = asyncio.Event()

        ffmpeg_filters = logic.build_ffmpeg_options(effects)

        ffmpeg_options = "-loglevel panic"
        if ffmpeg_filters:
            ffmpeg_options += f" {ffmpeg_filters}"

        try:
            current_source = discord.FFmpegPCMAudio(
                audio_path,
                before_options="-channel_layout mono",
                options=ffmpeg_options,
            )

            vc.play(
                current_source,
                after=lambda e: self.bot.loop.call_soon_threadsafe(local_event.set),
            )
        except (discord.ClientException, OSError) as e:
            self.logger.error(f"Failed to start playback for {audio_path}: {e}")
            return

        try:
            await asyncio.wait_for(local_event.wait(), timeout=playback_timeout)
        except asyncio.TimeoutError:
            self.logger.warning(f"Playback timeout for {audio_path}. Stopping.")
            if vc.is_playing():
                try:
                    vc.stop()
                except (discord.ClientException, OSError) as e:
                    self.logger.debug(f"Failed to stop VC smoothly: {e}")
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Unexpected error during playback wait: {e}")
        finally:
            await asyncio.sleep(0.01)

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
            if self.queue:
                await self.queue.put(data)
        except Exception:
            self.logger.exception("Failed to enqueue auto-join announcement")

    def on_close(self) -> None:
        logger = self.logger.getChild("on_close")
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

        if self.queue is not None:
            await self.queue.put(join_data)

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
            await interaction.followup.send(
                "管理者の権限で現在の再生を停止したよ。"
            )
        if self.queue and not self.queue.empty():
            temp_list = []
            while not self.queue.empty():
                try:
                    item = self.queue.get_nowait()
                    if not is_admin and item.get("author_id") != author_id:
                        temp_list.append(item)
                except asyncio.QueueEmpty:
                    break
            for item in temp_list:
                await self.queue.put(item)

        if guild_id in self.play_groups and is_admin:
            self.play_groups.pop(guild_id, None)

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

            if self.queue is not None:
                await self.queue.put(leave_data)

                await asyncio.sleep(0.5)

                max_wait = 20
                wait_count = 0
                while (
                    not self.queue.empty()
                    or vc.is_playing()
                    or self.is_reading.get(guild_id, False)
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

    async def prepare_audio(self, text, author_id):
        """APIリクエストを行い、音声ファイルを準備してパスを返す"""
        try:
            style_uuid, style_id = self.get_style(author_id)
            cache_path = self.cache_manager.get_cache_path(text, style_uuid)

            if os.path.exists(cache_path):
                return cache_path

            request_body = {
                "text": text,
                "styleId": style_id,
                "volumeScale": 1.0,
                "speedScale": 1.0,
                "pitchScale": 0.0,
                "intonationScale": 1.0,
                "prePhonemeLength": 0.0,
                "postPhonemeLength": 0.0,
                "outputSamplingRate": 48000,
                "outputStereo": False,
                "sampledIntervalValue": 0,
                "adjustedF0": [],
                "processingAlgorithm": "coeiroink",
                "startTrimBuffer": 0,
                "endTrimBuffer": 0,
                "prosodyDetail": [],
                "speakerUuid": style_uuid,
            }

            def call_api():
                return requests.post(
                    url="http://localhost:50032/v1/synthesis",
                    data=json.dumps(request_body),
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "audio/wav",
                    },
                    timeout=10,
                )

            loop = asyncio.get_event_loop()
            try:
                async with self.api_semaphore:
                    response = await asyncio.wait_for(
                        loop.run_in_executor(None, call_api), timeout=30.0
                    )
            except asyncio.TimeoutError:
                self.logger.error("Prepare audio timeout")
                return None

            if response.status_code == 200:
                await asyncio.to_thread(
                    Path(cache_path).write_bytes, response.content
                )
                self.cache_manager.clean_cache()
                return cache_path

            return None
        except (requests.RequestException, OSError) as e:
            self.logger.error(f"Prepare audio error: {e}")
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        if guild_id not in self.voice_clients or not message.content:
            return
        if self.text_channels.get(guild_id) != message.channel:
            return
        if self.queue is None:
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
        cnt = self.play_group_counters.get(guild_id, 0)
        group_id = f"legacy-{guild_id}-{cnt}"
        self.play_group_counters[guild_id] = cnt + 1

        style_uuid, _ = self.get_style(message.author.id)

        self._update_se_keywords()

        for seq_idx, content in enumerate(segments):
            se_path = self.se_keywords.get(content)
            if se_path:
                await self.play_waiting_queue.put(
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
                await self.play_waiting_queue.put(
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
                await self.queue.put(
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

    @tasks.loop(seconds=0.1)
    async def generation_loop(self):
        if self.queue is None or self.queue.empty():
            return

        item = await self.queue.get()

        try:
            task = asyncio.create_task(self._generation_worker(item))
            task.add_done_callback(lambda t, q=self.queue: q.task_done())
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Generation scheduling error: {e}")
            with suppress(ValueError):
                self.queue.task_done()

    async def _generation_worker(self, item):
        guild_id = item.get("guild_id")
        author_id = item.get("author_id")
        content = item.get("content")
        custom_file = item.get("file_path")
        effects = item.get("effects")

        group_id = item.get("group_id")
        if group_id is None:
            cnt = self.play_group_counters.get(guild_id, 0)
            group_id = f"legacy-{guild_id}-{cnt}"
            self.play_group_counters[guild_id] = cnt + 1

        try:
            if custom_file and os.path.exists(custom_file):
                seq = item.get("sequence_number", 0)
                total = item.get("total_segments", 1)
                await self.play_waiting_queue.put(
                    cast(
                        TTSQueueItem,
                        {
                            "guild_id": guild_id,
                            "group_id": group_id,
                            "file_path": custom_file,
                            "sequence_number": seq,
                            "total_segments": total,
                            "effects": effects,
                        },
                    )
                )
                return

            if content:
                try:
                    audio_path = await asyncio.wait_for(
                        self.prepare_audio(content, author_id), timeout=40.0
                    )
                except asyncio.TimeoutError:
                    self.logger.error(
                        f"Generation timeout for guild {guild_id} seq={item.get('sequence_number')}"
                    )
                    audio_path = None
                except Exception as e:  # noqa: BLE001
                    self.logger.error(
                        f"Generation error while awaiting prepare_audio: {e}"
                    )
                    audio_path = None

                seq = item.get("sequence_number", 0)
                total = item.get("total_segments", 1)

                if audio_path:
                    await self.play_waiting_queue.put(
                        cast(
                            TTSQueueItem,
                            {
                                "guild_id": guild_id,
                                "group_id": group_id,
                                "file_path": audio_path,
                                "sequence_number": seq,
                                "total_segments": total,
                                "effects": effects,
                            },
                        )
                    )
                else:
                    await self.play_waiting_queue.put(
                        cast(
                            TTSQueueItem,
                            {
                                "guild_id": guild_id,
                                "group_id": group_id,
                                "file_path": None,
                                "sequence_number": seq,
                                "total_segments": total,
                                "effects": effects,
                            },
                        )
                    )

        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Generation worker error: {e}")

    @tasks.loop(seconds=0.01)
    async def playback_loop(self):
        if not hasattr(self, "play_groups") or not hasattr(self, "play_group_counters"):
            self.play_groups = {}
            self.play_group_counters = {}
            self.play_wait_start = {}

        async def _drain_play_waiting() -> None:
            try:
                while True:
                    try:
                        item = self.play_waiting_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    guild_id = item.get("guild_id")
                    if guild_id is None:
                        with suppress(ValueError):
                            self.play_waiting_queue.task_done()
                        continue

                    group_id = item.get("group_id")
                    if group_id is None:
                        cnt = self.play_group_counters.get(guild_id, 0)
                        group_id = f"legacy-{guild_id}-{cnt}"
                        self.play_group_counters[guild_id] = cnt + 1

                    groups = self.play_groups.setdefault(guild_id, OrderedDict())
                    if group_id not in groups:
                        groups[group_id] = {
                            "items": {},
                            "next_index": 0,
                            "total": None,
                            "generating": True,
                            "start_ts": None,
                        }

                    group = groups[group_id]
                    idx = item.get("sequence_number", 0)
                    group["items"][idx] = item

                    total = item.get("total_segments")
                    if isinstance(total, int):
                        group["total"] = total
                        group["generating"] = False

                    if group["next_index"] == idx:
                        group["start_ts"] = None
                        self.play_wait_start.setdefault(guild_id, {}).pop(
                            group_id, None
                        )

                    with suppress(ValueError):
                        self.play_waiting_queue.task_done()

            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"Drain error in playback: {e}")

        await _drain_play_waiting()

        for guild_id, groups in list(self.play_groups.items()):
            if not groups:
                continue

            group_keys = list(groups.keys())
            if not group_keys:
                continue
            group_id = group_keys[0]
            group = groups[group_id]

            expected_idx = group["next_index"]
            item = group["items"].get(expected_idx)
            if item is None:
                await _drain_play_waiting()
                item = group["items"].get(expected_idx)

            if item is None:
                now = asyncio.get_event_loop().time()
                SKIP_TIMEOUT = getattr(self, "play_skip_timeout", 30.0)
                generation_pending = group.get("generating", False)
                currently_playing = self.is_reading.get(guild_id, False)

                if not generation_pending and not currently_playing:
                    continue

                start_ts = group.get("start_ts")
                if start_ts is None:
                    group["start_ts"] = now
                    self.play_wait_start.setdefault(guild_id, {})[group_id] = now
                    continue
                elif now - start_ts >= SKIP_TIMEOUT:
                    self.logger.warning(
                        f"Skipping missing idx {expected_idx} in group {group_id} guild {guild_id} (Timeout)"
                    )
                    group["next_index"] = expected_idx + 1
                    group["start_ts"] = None
                    self.play_wait_start.setdefault(guild_id, {}).pop(group_id, None)

                    item = group["items"].get(group["next_index"])
                    if item is None:
                        continue

            if item is None:
                continue

            group["start_ts"] = None
            self.play_wait_start.setdefault(guild_id, {}).pop(group_id, None)

            audio_path = item.get("file_path")
            seq = item.get("sequence_number", 0)
            vc = self.voice_clients.get(guild_id)

            if audio_path is None:
                group["next_index"] = expected_idx + 1
                if (
                    isinstance(group.get("total"), int)
                    and group["next_index"] >= group["total"]
                ):
                    groups.pop(group_id, None)
                if not group["items"] and not group.get("generating", False):
                    groups.pop(group_id, None)
                continue

            if not (vc and vc.is_connected()):
                continue

            try:
                self.is_reading[guild_id] = True
                next_path = None
                next_item = group["items"].get(seq + 1)
                if next_item is not None:
                    next_path = next_item.get("file_path")

                await self._play_audio_and_wait(
                    vc,
                    audio_path,
                    next_audio_path=next_path,
                    effects=item.get("effects"),
                )

                group["next_index"] = expected_idx + 1
                group["start_ts"] = None
                self.play_wait_start.setdefault(guild_id, {}).pop(group_id, None)

                total = group.get("total")
                if isinstance(total, int) and group["next_index"] >= total:
                    groups.pop(group_id, None)

                if not group["items"] and not group.get("generating", False):
                    groups.pop(group_id, None)
                return
            except Exception as e:  # noqa: BLE001
                self.logger.error(
                    f"Playback error in guild {guild_id}, group {group_id}: {e}"
                )
            finally:
                self.is_reading[guild_id] = False

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
                if self.queue is None:
                    self.queue = asyncio.Queue()
                if self.play_waiting_queue is None:
                    self.play_waiting_queue = asyncio.Queue()

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
                        self.is_reading[guild_id] = False
                        if self.queue:
                            while not self.queue.empty():
                                try:
                                    self.queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break

                        if self.play_waiting_queue:
                            while not self.play_waiting_queue.empty():
                                try:
                                    self.play_waiting_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break

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
            if self.queue is not None:
                await self.queue.put(
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
            else:
                self.logger.warning(
                    "Queue is not initialized. Skipping action notification."
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

        self.is_reading[guild_id] = False

        if hasattr(self, "play_groups") and guild_id in self.play_groups:
            self.play_groups.pop(guild_id, None)
        if (
            hasattr(self, "play_group_counters")
            and guild_id in self.play_group_counters
        ):
            self.play_group_counters.pop(guild_id, None)
        if hasattr(self, "play_wait_start") and guild_id in self.play_wait_start:
            self.play_wait_start.pop(guild_id, None)

        if self.queue:
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        if self.play_waiting_queue:
            while not self.play_waiting_queue.empty():
                try:
                    self.play_waiting_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

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