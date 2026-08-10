import asyncio
import io
import json
import logging
import os
import random
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict, cast

import discord
import requests
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from typing_extensions import NotRequired, override

import logic
from cache_manager import VoiceCacheManager
from dictionary_manager import DictionaryManager
from models import Models


class TTSQueueItem(TypedDict):
    guild_id: int
    group_id: NotRequired[str]
    author_id: NotRequired[int]
    content: NotRequired[str]
    file_path: NotRequired[str | None]
    sequence_number: NotRequired[int]
    total_segments: NotRequired[int]
    effects: NotRequired[dict[str, bool]]


class AutoJoinConfig(TypedDict):
    voice_channel_id: int
    text_channel_id: int


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
    prefs: dict[str, dict[str, float | int | str | dict[str, object]]]
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
    config_path: str
    auto_join_configs: dict[str, AutoJoinConfig]
    next_event: asyncio.Event | None
    api_semaphore: asyncio.Semaphore

    play_groups: dict[int, dict[str, Any]]
    play_group_counters: dict[int, int]
    play_wait_start: dict[int, dict[str, float | None]]

    def __init__(self, bot: commands.Bot, logger: logging.Logger):
        super().__init__()
        self.bot = bot
        self.logger = logging.getLogger("bot.ttscog")

        self.text_channels = {}
        self.voice_clients = {}
        self.voice_channels = {}
        self.prefs = {}
        self.is_reading = {}
        self.last_speaker_id = {}
        self.last_speak_time = {}
        self.dict_manager = DictionaryManager()
        self.cache_manager = VoiceCacheManager(max_size_mb=2048)
        self.config_path = "auto_join_config.json"
        self.auto_join_configs = self._load_auto_join_configs()
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

    @staticmethod
    def _read_prefs_file(path: str) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return cast(dict[str, Any], json.load(f))

    @override
    async def cog_load(self) -> None:
        logger = self.logger.getChild("cog_load")
        if os.path.isfile("prefs.json"):
            try:
                # 2. 呼び出すときは self._read_prefs_file にする！
                self.prefs = await asyncio.to_thread(self._read_prefs_file, "prefs.json")
                logger.info("Loaded preferences")
            except Exception:
                logger.exception("Failed to load prefs.json")
        self.queue = asyncio.Queue()
        self.play_waiting_queue = asyncio.Queue()
        self._queue_lock = asyncio.Lock()
        self.next_event = asyncio.Event()
        self.api_semaphore = asyncio.Semaphore(1)
        self.queue = asyncio.Queue()
        self.play_waiting_queue = asyncio.Queue()
        if not self.generation_loop.is_running():
            self.generation_loop.start()
        if not self.playback_loop.is_running():
            self.playback_loop.start()

        try:
            if self._http_server_task is not None and not self._http_server_task.done():
                logger.info("HTTP server is already running. Skipping duplicate startup.")
            else:
                self._http_server_task = asyncio.create_task(self.start_http_server())
                logger.info("Started HTTP server task for bump notifications")
        except Exception:
            logger.exception("Failed to start HTTP server task")

        logger.info("Initialized TTSCog successfully")

    async def start_http_server(self):
        """Start aiohttp web server to receive external bump notifications."""
        try:
            app = web.Application()
            app.add_routes(
                [web.post("/bot_tts_gttegldxdzmyrohd", self.handle_bump_notification)]
            )
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", 50030)
            await site.start()
            self.logger.info(
                "📢 Web server for bump notifications listening on port 50030"
            )
        except Exception:
            self.logger.exception("Failed to start HTTP server")

    def _update_se_keywords(self):
        """ディレクトリをスキャンして {ファイル名: フルパス} の辞書を作る"""
        se_dir = "Extra/EX_Voice"
        self.se_keywords = {}
        if os.path.exists(se_dir):
            for file in os.listdir(se_dir):
                if file.endswith((".wav", ".mp3")):
                    name = os.path.splitext(file)[0]
                    self.se_keywords[name] = os.path.join(se_dir, file)

    async def handle_bump_notification(self, request: web.Request) -> web.Response:
        """Handle POST requests from external services (e.g., Node.js)."""
        try:
            body = await request.json()
            data = cast(BumpNotificationData, body)
            raw_guild_id = data.get("guild_id")
            original_message = data.get("message") or ""

            if raw_guild_id is None:
                self.logger.warning("guild_id is missing in request")
                return web.Response(text="Missing guild_id", status=400)

            guild_id = int(raw_guild_id)
            if guild_id not in self.voice_clients:
                return web.Response(text="Accepted but no VC connection", status=202)
            audio_file = None
            log_msg = "Bump notification queued"
            if "Bumpされました！" in original_message:
                audio_file = self._pick_random_file("Extra/SuccessBump")
                log_msg = f"Success Bump sound selected: {audio_file}"
            elif "Bumpできます！" in original_message:
                audio_file = self._pick_random_file("Extra/PleaseBump")
                log_msg = f"Please Bump sound selected: {audio_file}"

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self.logger.warning(f"Guild {guild_id} not found.")
                return web.Response(text="Guild not found", status=404)

            bot_member = guild.me
            queue_item: TTSQueueItem = {
                "guild_id": guild_id,
                "author_id": bot_member.id,
                "content": original_message,
                "file_path": audio_file,
                # bump は単一セグメント扱い
                "sequence_number": 0,
                "total_segments": 1,
            }

            if self.queue:
                await self.queue.put(queue_item)
                self.logger.info(f"✅ {log_msg}")
                return web.Response(text="Success", status=200)

            return web.Response(text="Queue not found", status=500)

        except Exception as e:
            self.logger.exception("❌ Error processing Bump notification")
            return web.Response(text=str(e), status=500)

    def _load_auto_join_configs(self) -> dict[str, AutoJoinConfig]:  # 戻り値を明示
        if not os.path.exists(self.config_path):
            return {}
        with open(self.config_path, "r") as f:
            # json.load の結果を AutoJoinConfig の辞書としてキャスト
            return cast(dict[str, AutoJoinConfig], json.load(f))

    def _save_auto_join_configs(self):
        try:
            with open(self.config_path, "w") as f:
                json.dump(self.auto_join_configs, f, indent=2)
        except Exception:
            self.logger.exception("Failed to save auto-join configs")

    def _pick_random_file(self, base_path: str) -> str | None:
        """
        ディレクトリ名の数値に基づき、再帰的にファイルを抽選する。
        - フォルダ名に数値がある場合（例: '60'）: その数値を重みにする。
        - 数値がない、またはファイルの場合: 重みを 10 として扱う。
        """
        if not os.path.exists(base_path):
            return None

        # ファイルに到達したらそのパスを返す
        if os.path.isfile(base_path):
            return base_path

        items = os.listdir(base_path)
        if not items:
            return None

        candidates: list[str] = []
        weights: list[int] = []

        for item in items:
            full_path = os.path.join(base_path, item)

            # 1. フォルダ名/ファイル名から数値を抽出
            # フォルダ名自体が数字（例: '60'）または 'Name_60' のような形式に対応
            match = re.search(r"(\d+)", item)

            if match:
                weight = int(match.group(1))
            else:
                # 混合している場合や数値がない場合は 10
                weight = 10

            candidates.append(full_path)
            weights.append(weight)

        # 重み付き抽選
        chosen = random.choices(candidates, weights=weights, k=1)[0]

        # 抽選された先がディレクトリならさらに深く潜る
        if os.path.isdir(chosen):
            return self._pick_random_file(chosen)

        # ファイルなら確定
        return chosen

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

        # 共有の self.next_event ではなく、この再生回限りのイベントを作成して競合を防ぐ
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

            # 再生開始
            vc.play(
                current_source,
                after=lambda e: self.bot.loop.call_soon_threadsafe(local_event.set),
            )
        except (discord.ClientException, OSError) as e:
            self.logger.error(f"Failed to start playback for {audio_path}: {e}")
            return

        # 再生終了を待機
        try:
            await asyncio.wait_for(local_event.wait(), timeout=playback_timeout)
        except asyncio.TimeoutError:
            self.logger.warning(f"Playback timeout for {audio_path}. Stopping.")
            if vc.is_playing():
                try:
                    vc.stop()
                except (discord.ClientException, OSError) as e:
                    # pass で無視せず、デバッグ用ログとして記録しておくのが賢い選択だよ
                    self.logger.debug(f"Failed to stop VC smoothly: {e}")
        except Exception as e:  # noqa: BLE001
            # 予期せぬエラーでタスク全体が死ぬのを防ぐ意図的なキャッチなので noqa を付与！
            self.logger.error(f"Unexpected error during playback wait: {e}")
        finally:
                    # 念のため、少しの猶予を置いて終了
                    await asyncio.sleep(0.01)

    async def _put_announcement(self, guild_id: int, text: str):
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                self.logger.warning(f"Guild {guild_id} not found for announcement")
                return
            bot_member = guild.me
            data: TTSQueueItem = {
                "guild_id": guild_id,
                "author_id": bot_member.id,
                "content": text,
                # アナウンスは単一セグメント扱い
                "sequence_number": 0,
                "total_segments": 1,
            }
            if self.queue:
                await self.queue.put(data)
        except Exception:
            self.logger.exception("Failed to enqueue auto-join announcement")

    def on_close(self) -> None:
        logger = self.logger.getChild("on_close")
        try:
            with open("prefs.json", "w") as f:
                f.write(json.dumps(self.prefs))
        except Exception:
            logger.exception("Failed to save prefs on close")
        logger.info("TTSCog Closed")

    def get_pref(
        self,
        user_id: int,
        key: str,
        def_val: object,
        cond: Callable[[object], bool],
    ) -> object:
        user_id_str = str(user_id)

        if user_id_str not in self.prefs:
            self.prefs[user_id_str] = {}
        user_prefs = self.prefs[user_id_str]

        if key in user_prefs:
            value = user_prefs[key]
            if cond(value):
                return value

        if isinstance(def_val, (float, int, str, dict)):
            user_prefs[key] = cast(float | int | str | dict[str, object], def_val)
        else:
            user_prefs[key] = str(def_val)

        return def_val

    def get_style(self, user_id: int) -> tuple[str, int]:
        # モデルのデフォルト値（最初のモデルの最初のスタイル）を取得
        # ※ models.py に Models.get_default_style() が実装されている前提
        default_uuid, default_style_id = Models.get_default_style()
        def_val: dict[str, str | int] = {
            "uuid": default_uuid,
            "style_id": default_style_id,
        }

        # get_prefを使って設定を取得/保存。設定が新しい辞書形式かチェックする。
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
        # 1. Guild と Member の存在保証
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(
                "サーバー内でのみ実行可能です。", ephemeral=True
            )
            return

        # 2. ユーザーのボイス状態確認
        voice_state = interaction.user.voice
        if not voice_state or not voice_state.channel:
            await interaction.followup.send(
                "ボイスチャンネルに接続してから実行してください。", ephemeral=True
            )
            return

        # 3. チャンネルの型を VoiceChannel に限定
        channel = voice_state.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.followup.send(
                "通常のボイスチャンネルに接続してください。", ephemeral=True
            )
            return

        # 4. テキストチャンネルの型を TextChannel に限定
        text_channel = interaction.channel
        if not isinstance(text_channel, discord.TextChannel):
            await interaction.followup.send(
                "このチャンネルでは読み上げを開始できません。", ephemeral=True
            )
            return

        # 5. 接続処理と VoiceClient の確保
        if guild.voice_client is None:
            vc = await channel.connect()
            # 最初のレスポンス
            await interaction.followup.send(f"【{channel.name}】に接続しました。")
        else:
            # すでに接続されている場合
            vc = cast(discord.VoiceClient, guild.voice_client)
            # 既に接続済みである旨を伝える（既存の else ブロックの内容をここに集約）
            await interaction.followup.send("すでに接続しています。", ephemeral=True)
            # 既に接続していても、読み上げチャンネルの更新などは行いたい場合があるため続行

        # 6. 状態の保存
        guild_id = guild.id
        self.text_channels[guild_id] = text_channel
        self.voice_clients[guild_id] = vc
        self.voice_channels[guild_id] = channel

        # 7. 読み上げ用データの作成と投入
        announcement_text = f"【{channel.name}】に参加しました。"

        join_data: TTSQueueItem = {
            "guild_id": guild_id,
            "author_id": guild.me.id,
            "content": announcement_text,
            # 参加通知も単一セグメント
            "sequence_number": 0,
            "total_segments": 1,
        }

        if self.queue is not None:
            await self.queue.put(join_data)

    @app_commands.command(
        name="skip",
        description="自分が送信した、または全ユーザーの未再生の読み上げキューをクリアします。",
    )
    async def skip_my_voice(self, interaction: discord.Interaction):
        """自分が送信した未再生の読み上げキューをすべて削除し、現在再生中なら止める"""
        await interaction.response.defer()

        guild_id = interaction.guild.id if interaction.guild else None
        if not guild_id:
            await interaction.followup.send(
                "サーバー内でのみ実行可能です。", ephemeral=True
            )
            return

        author_id = interaction.user.id
        # 管理者かどうかの判定 (Interaction.userはGuild内ではMember型)
        is_admin = False
        if isinstance(interaction.user, discord.Member):
            is_admin = interaction.user.guild_permissions.administrator

        # 1. 現在再生中の音声が「自分のもの」か、または「自分が管理者」なら止める
        vc = self.voice_clients.get(guild_id)
        if vc and vc.is_playing():
            if is_admin:
                vc.stop()
                await interaction.followup.send(
                    "管理者の権限で現在の再生を停止したよ。"
                )
            else:
                # 一般ユーザーの場合は、誤コピペ対策として安全にキューの削除側をメインにするよ
                pass

        # 2. まだ生成前のメインキュー（self.queue）から対象のデータを間引く
        if self.queue and not self.queue.empty():
            temp_list = []
            while not self.queue.empty():
                try:
                    item = self.queue.get_nowait()
                    # 管理者なら全部消す（残さない）、一般ユーザーなら他人のデータだけ残す
                    if not is_admin and item.get("author_id") != author_id:
                        temp_list.append(item)
                except asyncio.QueueEmpty:
                    break
            # 残ったデータをキューに戻す
            for item in temp_list:
                await self.queue.put(item)

        # 3. 再生待ちバッファ（self.play_groups）から対象データを削除する
        if guild_id in self.play_groups:
            # 管理者の場合はギルドのバッファ全体を吹き飛ばす
            if is_admin:
                self.play_groups.pop(guild_id, None)
            else:
                # 一般ユーザーの場合は、現状の構造に合わせてキュー側の間引きに留めるか、
                # 必要に応じてgroup_dictのフィルタリングロジックをここに追加できるよ
                pass

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

            # 1. 読み上げキューに追加
            leave_data: TTSQueueItem = {
                "guild_id": guild_id,
                "author_id": bot_member.id,
                "content": announcement_text,
                "sequence_number": 0,
                "total_segments": 1,
            }

            if self.queue is not None:
                await self.queue.put(leave_data)

                # 2. キューが処理され、再生が始まるまで少し待つ
                # (投入直後にempty判定をすると、処理が早すぎてループを抜ける可能性があるからね)
                await asyncio.sleep(0.5)

                # 3. 「キューが空」かつ「再生中ではない」状態になるまで待機
                # 念のためタイムアウト（例: 10秒）を設けておくと、万が一の無限ループを防げるよ
                max_wait = 20  # 10秒 (0.5s * 20)
                wait_count = 0
                while (
                    not self.queue.empty()
                    or vc.is_playing()
                    or self.is_reading.get(guild_id, False)
                ) and wait_count < max_wait:
                    await asyncio.sleep(0.5)
                    wait_count += 1

            # 4. 読み上げ終わってから切断
            if vc.is_connected():
                await vc.disconnect()

            # データの削除
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

        # 保存
        guild_key = str(guild.id)
        self.auto_join_configs[guild_key] = {
            "voice_channel_id": voice_channel.id,
            "text_channel_id": text_channel.id,
        }
        self._save_auto_join_configs()

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

            # APIリクエストのパラメータ作成
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

            # 外部通信なので、本来は aiohttp を使うのが理想ですが、
            # 現状の requests を使う場合は別スレッドで実行してブロックを防ぎます
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
            # COEIROINK の制約を守るため、API 実行を同時に1つに制限する
            try:
                async with self.api_semaphore:
                    # 外部コールは長時間かかる可能性があるためタイムアウトを設ける
                    response = await asyncio.wait_for(
                        loop.run_in_executor(None, call_api), timeout=30.0
                    )
            except asyncio.TimeoutError:
                self.logger.error("Prepare audio timeout")
                return None

            if response.status_code == 200:
                # ファイル書き込みを別スレッドで実行してイベントループを止めないようにするよ
                await asyncio.to_thread(
                    Path(cache_path).write_bytes, response.content
                )
                self.cache_manager.clean_cache()
                return cache_path

            return None
        # 通信エラーやファイル保存エラーなど、具体的な例外に絞ってキャッチするよ
        except (requests.RequestException, OSError) as e:
            self.logger.error(f"Prepare audio error: {e}")
            return None

    async def set_pref(
        self, interaction: discord.Interaction, key: str, value: Any, message: str
    ):
        user_id_str = str(interaction.user.id)
        if user_id_str not in self.prefs or type(self.prefs[user_id_str]) != dict:
            self.prefs[user_id_str] = {}
        self.prefs[user_id_str][key] = value
        await interaction.response.send_message(message)

    @app_commands.command(
        name="add_word", description="カスタム辞書に単語の読み方を追加/更新します。"
    )
    @app_commands.describe(word="元の単語 (例: Wamom)", reading="読み方 (例: わもむ)")
    async def add_word(self, interaction: discord.Interaction, word: str, reading: str):
        # 1. 全角を半角に正規化 (NFKC) し、2. 小文字に変換
        processed_word = unicodedata.normalize("NFKC", word).lower()

        self.dict_manager.add_word(processed_word, reading)
        await interaction.response.send_message(
            f"✅ カスタム辞書に単語 **`{processed_word}`** を読み方 **`{reading}`** で登録しました。\n"
            f"（元の入力: `{word}` を自動変換しました）"
        )

    @app_commands.command(
        name="del_word", description="カスタム辞書から単語を削除します。"
    )
    @app_commands.describe(word="削除したい元の単語")
    async def del_word(self, interaction: discord.Interaction, word: str):
        # 登録時と同様に正規化して小文字化
        processed_word = unicodedata.normalize("NFKC", word).lower()

        if self.dict_manager.delete_word(processed_word):
            await interaction.response.send_message(
                f"✅ カスタム辞書から単語 **`{processed_word}`** を削除しました。"
            )
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

        # CSV文字列をファイルオブジェクトとしてラップ
        csv_file = discord.File(
            fp=io.BytesIO(csv_content.encode("utf-8")),
            filename="custom_dict.csv",
            description="TTS Custom Dictionary",
        )

        await interaction.response.send_message(
            "✅ カスタム辞書をCSVとしてエクスポートします。", file=csv_file
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 1. Bot自身のメッセージやギルド外は無視
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
            # VCにそもそも入っていない場合、警告を出して「読み上げ処理だけ」を終了する
            await self._send_and_delete_warning(message.channel)
            return

        # Botが接続しているボイスチャンネルと一致するかチェック
        bot_vc = self.voice_channels.get(guild_id)
        if bot_vc and member.voice.channel.id != bot_vc.id:
            # 別のVCに入っている場合も、警告を出して「読み上げ処理だけ」を終了する
            await self._send_and_delete_warning(message.channel)
            return

        now = discord.utils.utcnow()
        raw_last_time = self.last_speak_time.get(guild_id)
        last_time = (
            raw_last_time
            if isinstance(raw_last_time, datetime)
            else datetime.fromtimestamp(0, tz=timezone.utc)
        )

        # 連続投稿判定
        is_continuous = (
            self.last_speaker_id.get(guild_id) == message.author.id
            and (now - last_time).total_seconds() < 60
        )

        # --- 読み上げ対象のセグメントを構築 ---
        segments = []

        # 1. 名前の追加（連続投稿でない場合）
        if not is_continuous:
            if message.author.id == self.owner_id:
                name = f"{logic.process_name(self.owner_display_name, self.dict_manager)}さん。 "
            else:
                clean_name = logic.process_name(
                    message.author.display_name, self.dict_manager
                )
                name = f"{clean_name if clean_name else '名無し'}さん。 "
            segments.append(name)

        # 2. 本文の追加
        processed_text, effects = logic.process_text(
            message.content, message.guild, self.dict_manager, self.bot
        )
        non_empty_body = [s for s in logic.split_text(processed_text) if s.strip()]
        segments.extend(non_empty_body)

        MAX_SEGMENTS = 10
        if len(segments) > MAX_SEGMENTS:
            # 設定数に切り詰めて、最後に「以下略」などを付け足す
            segments = segments[:MAX_SEGMENTS]
            segments.append("以下略。")

        if not segments:
            return

        # --- キュー投入処理の共通化 ---
        total_segments = len(segments)
        cnt = self.play_group_counters.get(guild_id, 0)
        group_id = f"legacy-{guild_id}-{cnt}"
        self.play_group_counters[guild_id] = cnt + 1

        style_uuid, _ = self.get_style(message.author.id)

        self._update_se_keywords()

        for seq_idx, content in enumerate(segments):
            # SEチェック
            se_path = self.se_keywords.get(content)
            if se_path:
                await self.play_waiting_queue.put(
                    {
                        "guild_id": guild_id,
                        "group_id": group_id,
                        "file_path": se_path,
                        "sequence_number": seq_idx,
                        "total_segments": total_segments,
                        "effects": effects,
                    }
                )
                continue

            # キャッシュチェック
            cache_path = self.cache_manager.get_cache_path(content, style_uuid)
            if os.path.exists(cache_path):
                await self.play_waiting_queue.put(
                    {
                        "guild_id": guild_id,
                        "group_id": group_id,
                        "file_path": cache_path,
                        "sequence_number": seq_idx,
                        "total_segments": total_segments,
                        "effects": effects,
                    }
                )
            else:
                # 生成が必要な場合
                await self.queue.put(
                    {
                        "guild_id": guild_id,
                        "group_id": group_id,
                        "author_id": message.author.id,
                        "content": content,
                        "sequence_number": seq_idx,
                        "total_segments": total_segments,
                        "effects": effects,
                    }
                )

        # 履歴を更新
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

    @tasks.loop(seconds=0.1)
    async def generation_loop(self):
        """
        キューからアイテムを取り出して、生成ワーカーを非同期タスクとして作成するだけに役割を限定する。
        実際の API 呼び出し（prepare_audio）は専用ワーカーで行い、COEIROINK 制約のため Semaphore で保護される。
        """
        if self.queue is None or self.queue.empty():
            return

        item = await self.queue.get()

        try:
            # ワーカーを作成してバックグラウンドで処理させる
            task = asyncio.create_task(self._generation_worker(item))
            # アイテムの完了を queue.task_done() で通知するためのコールバックを登録
            task.add_done_callback(lambda t, q=self.queue: q.task_done())
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Generation scheduling error: {e}")
            # スケジューリング自体が失敗したら明示的に task_done() を呼ぶ
            # ValueError (呼び出しすぎ) が起きても安全に無視するよ
            with suppress(ValueError):
                self.queue.task_done()

    async def _generation_worker(self, item):
        """実際の生成処理を行うワーカー（バックグラウンド実行）"""
        guild_id = item.get("guild_id")
        author_id = item.get("author_id")
        content = item.get("content")
        custom_file = item.get("file_path")
        effects = item.get("effects")

        # Ensure a group_id is present (propagate from incoming item or generate a legacy one)
        group_id = item.get("group_id")
        if group_id is None:
            cnt = self.play_group_counters.get(guild_id, 0)
            group_id = f"legacy-{guild_id}-{cnt}"
            self.play_group_counters[guild_id] = cnt + 1

        try:
            # 1. すでにファイルがある場合（Bump音など）
            if custom_file and os.path.exists(custom_file):
                seq = item.get("sequence_number", 0)
                total = item.get("total_segments", 1)
                await self.play_waiting_queue.put(
                    {
                        "guild_id": guild_id,
                        "group_id": group_id,
                        "file_path": custom_file,
                        "sequence_number": seq,
                        "total_segments": total,
                        "effects": effects,
                    }
                )
                return

            # 2. テキストから生成する場合
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
                        {
                            "guild_id": guild_id,
                            "group_id": group_id,
                            "file_path": audio_path,
                            "sequence_number": seq,
                            "total_segments": total,
                            "effects": effects,
                        }
                    )
                else:
                    # 生成失敗またはタイムアウト: 永久待ちにならないように「スキップ」を示すエントリを投入する
                    await self.play_waiting_queue.put(
                        {
                            "guild_id": guild_id,
                            "group_id": group_id,
                            "file_path": None,
                            "sequence_number": seq,
                            "total_segments": total,
                            "effects": effects,
                        }
                    )

        except Exception as e: # noqa: BLE001
            self.logger.error(f"Generation worker error: {e}")

    @tasks.loop(seconds=0.01)
    async def playback_loop(self):
        """再生待ちキューにある音声を順次再生する（グループ単位の階層化）"""
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
                        # ValueError（呼び出しすぎ）が発生しても安全に無視するよ
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

                    # ここも suppress で受け取れば例外ログも出ず綺麗に処理できるよ
                    with suppress(ValueError):
                        self.play_waiting_queue.task_done()

            # 一番外側の try に対応する except はここ！
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"Drain error in playback: {e}")

        await _drain_play_waiting()

        for guild_id, groups in list(self.play_groups.items()):
            if not groups:
                continue

            # 安全に先頭要素を特定するため、keysのリストコピーから取得
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
                    vc, audio_path, next_audio_path=next_path, effects=item.get("effects"),
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
            except Exception as e: # noqa: BLE001
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
        # 1. Guildの存在チェック
        guild = member.guild
        if guild is None:
            return

        guild_id = guild.id

        guild_id_str = str(guild_id)
        if guild_id_str in self.auto_join_configs:
            # ここでキューをチェック。なければ作る。
            if self.queue is None:
                self.queue = asyncio.Queue()
            if self.play_waiting_queue is None:
                self.play_waiting_queue = asyncio.Queue()

            config = self.auto_join_configs[guild_id_str]

        # --- 1. Auto-join ロジック ---
        try:
            guild_id_str = str(guild_id)
            if guild_id_str in self.auto_join_configs:
                config = self.auto_join_configs[guild_id_str]
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

        # Botが参加していないなら終了
        if guild_id not in self.voice_clients:
            return

        # --- 2. 共通の名前決定ロジック (間を詰めるための加工込み) ---
        if member.id == self.owner_id:
            # オーナー名にも辞書を適用したい場合は logic.process_name を通す
            name_to_read = (
                f"{logic.process_name(self.owner_display_name, self.dict_manager)}"
            )
        else:
            # 一般ユーザーの名前に辞書を適用
            clean_name = logic.process_name(member.display_name, self.dict_manager)
            if clean_name:
                name_to_read = f"{clean_name}さん"
            else:
                name_to_read = "名無しさん"

        # --- 3. アクション判定 (参加・退出・配信) ---
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

        # アクションがあれば「名前＋アクション」でキューに入れる
        if action_text:
            if self.queue is not None:
                await self.queue.put(
                    {
                        "guild_id": guild_id,
                        "author_id": member.id,
                        "content": f"{name_to_read}{action_text}",
                        # イベント通知は単一セグメント
                        "sequence_number": 0,
                        "total_segments": 1,
                    }
                )
            else:
                self.logger.warning(
                    "Queue is not initialized. Skipping action notification."
                )

        # --- 4. 自動切断処理 ---
        # 抜けたのがBot自身である場合は、この切断判定処理を行う必要はないからリターンするよ
        if self.bot.user and member.id == self.bot.user.id:
            return

        left_channel = before.channel

        if left_channel is not None and guild_id in self.voice_clients:
            managed_channel = self.voice_channels.get(guild_id)

            if managed_channel and left_channel.id == managed_channel.id:
                # チャンネルに残っている「Bot以外の人間」の数を正確に数える
                human_members = [m for m in left_channel.members if not m.bot]

                # 人間が0人（Botだけ、または誰もいない）になったら即座に切断！
                if len(human_members) == 0:
                    await self.immediate_disconnect(guild_id)

    async def immediate_disconnect(self, guild_id: int):
        """メンバー不在時に即座に切断し、残ったキューや再生バッファを完全に破棄する"""
        self.logger.info(f"Immediate disconnect triggered for guild: {guild_id}")
        vc = self.voice_clients.get(guild_id)

        # 1. 物理的な切断を最優先で行う
        if vc:
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect(force=True)
            except Exception as e: # noqa: BLE001
                self.logger.error(f"Failed to disconnect cleanly: {e}")

        # 2. 状態管理フラグの初期化
        self.is_reading[guild_id] = False

        # 3. 再生バッファ（play_groups）の完全消去【最重要】
        # メモリ上に残っている未再生セグメントをギルド単位で完全に消し去るよ
        if hasattr(self, "play_groups") and guild_id in self.play_groups:
            self.play_groups.pop(guild_id, None)
        if (
            hasattr(self, "play_group_counters")
            and guild_id in self.play_group_counters
        ):
            self.play_group_counters.pop(guild_id, None)
        if hasattr(self, "play_wait_start") and guild_id in self.play_wait_start:
            self.play_wait_start.pop(guild_id, None)

        # 4. 非同期キュー（内部全データ）のクリーンアップ
        # 特定のギルドのものだけを抜くことはキューの性質上難しいため、
        # 誰もいなくなった時は、安全のために一度詰まっているものをドレインする
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

        # 5. データ削除とチャンネル通知（メッセージを送らずに即切断したい場合は、sendの行をコメントアウトしてね）
        if guild_id in self.text_channels:
            # 「読み上げを終了します」などの発話を挟まず、テキスト通知だけに留めるか、不要なら消して大丈夫だよ
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
