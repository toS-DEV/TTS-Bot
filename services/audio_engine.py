# 音声生成(COEIROINK)・再生キュー・FFmpeg再生管理
import asyncio
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypedDict, cast

import discord
import requests
from discord.ext import tasks

import utils.text_processor as logic
from services.cache_manager import VoiceCacheManager


class TTSQueueItem(TypedDict, total=False):
    guild_id: int
    group_id: str
    author_id: int
    content: str
    file_path: str | None
    sequence_number: int
    total_segments: int
    effects: dict[str, bool] | None


class AudioEngine:
    """音声合成リクエストの生成キュー・再生キュー・FFmpeg再生制御を担うエンジンクラス"""

    def __init__(
        self,
        bot: discord.Client,
        cache_manager: VoiceCacheManager,
        get_style_fn: Callable[[int], tuple[str, int]],
        get_voice_client_fn: Callable[[int], discord.VoiceClient | None],
        logger: logging.Logger,
    ):
        self.bot = bot
        self.cache_manager = cache_manager
        self.get_style_fn = get_style_fn
        self.get_voice_client_fn = get_voice_client_fn
        self.logger = logger.getChild("audio_engine")

        self.queue: asyncio.Queue[TTSQueueItem] = asyncio.Queue()
        self.play_waiting_queue: asyncio.Queue[TTSQueueItem] = asyncio.Queue()
        self.api_semaphore = asyncio.Semaphore(1)

        self.is_reading: dict[int, bool] = {}
        self.play_groups: dict[int, dict[str, Any]] = {}
        self.play_group_counters: dict[int, int] = {}
        self.play_wait_start: dict[int, dict[str, float | None]] = {}

    def start(self) -> None:
        """生成・再生ループタスクを開始する"""
        if not self.generation_loop.is_running():
            self.generation_loop.start()
        if not self.playback_loop.is_running():
            self.playback_loop.start()

    def stop(self) -> None:
        """生成・再生ループタスクを停止する"""
        if self.generation_loop.is_running():
            self.generation_loop.stop()
        if self.playback_loop.is_running():
            self.playback_loop.stop()

    async def enqueue(self, item: TTSQueueItem) -> None:
        """音声生成キューにアイテムを追加"""
        await self.queue.put(item)

    async def enqueue_play_waiting(self, item: TTSQueueItem) -> None:
        """生成済み（SEやキャッシュ済み）音声ファイルを再生待ちキューに直接追加"""
        await self.play_waiting_queue.put(item)

    def generate_group_id(self, guild_id: int) -> str:
        """ギルドごとの再生グループIDを生成"""
        cnt = self.play_group_counters.get(guild_id, 0)
        group_id = f"legacy-{guild_id}-{cnt}"
        self.play_group_counters[guild_id] = cnt + 1
        return group_id

    async def prepare_audio(self, text: str, author_id: int) -> str | None:
        """音声合成APIを呼び出し、ローカルキャッシュに保存してパスを返す"""
        try:
            style_uuid, style_id = self.get_style_fn(author_id)
            cache_path = self.cache_manager.get_cache_path(text, style_uuid)
                
            if cache_path.exists():
                return str(cache_path)

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
                # 1. cache_path は既に Path なのでそのまま write_bytes を渡せるよ
                await asyncio.to_thread(cache_path.write_bytes, response.content)
                self.cache_manager.clean_cache()
                
                # 2. 関数の戻り値型 (str | None) に合わせて str にキャストして返す！
                return str(cache_path)

            return None
        except (requests.RequestException, OSError) as e:
            self.logger.error(f"Prepare audio error: {e}")
            return None

    async def _play_audio_and_wait(
        self,
        vc: discord.VoiceClient,
        audio_path: str,
        next_audio_path: str | None = None,
        playback_timeout: float = 30.0,
        effects: dict[str, bool] | None = None,
    ) -> None:
        """音声ファイルを再生し、終了まで待機する"""
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

    @tasks.loop(seconds=0.1)
    async def generation_loop(self):
        """生成キューを監視しバックグラウンドで音声変換を行う"""
        if self.queue.empty():
            return

        item = await self.queue.get()

        try:
            task = asyncio.create_task(self._generation_worker(item))
            task.add_done_callback(lambda t: self.queue.task_done())
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Generation scheduling error: {e}")
            with suppress(ValueError):
                self.queue.task_done()

    async def _generation_worker(self, item: TTSQueueItem):
        guild_id = item.get("guild_id")
        author_id = item.get("author_id", 0)
        content = item.get("content")
        custom_file = item.get("file_path")
        effects = item.get("effects")

        group_id = item.get("group_id")
        if group_id is None and guild_id is not None:
            group_id = self.generate_group_id(guild_id)

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

        except Exception as e:  # noqa: BLE001
            self.logger.error(f"Generation worker error: {e}")

    @tasks.loop(seconds=0.01)
    async def playback_loop(self):
        """再生待ちキューの順序制御と実際の音声再生を行う"""
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
                        group_id = self.generate_group_id(guild_id)

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
            vc = self.get_voice_client_fn(guild_id)

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

    def clear_guild_data(self, guild_id: int) -> None:
        """ギルドの再生状態、再生待ちグループ、キュー内のデータを全て破棄する"""
        self.is_reading[guild_id] = False
        self.play_groups.pop(guild_id, None)
        self.play_group_counters.pop(guild_id, None)
        self.play_wait_start.pop(guild_id, None)

        self._drain_queue(self.queue, guild_id)
        self._drain_queue(self.play_waiting_queue, guild_id)

    def filter_user_queue(self, guild_id: int, author_id: int, is_admin: bool = False) -> None:
        """特定のユーザー（または管理者権限での全ユーザー）の未再生キューを破棄する"""
        if is_admin:
            self.play_groups.pop(guild_id, None)

        self._filter_queue(self.queue, guild_id, author_id, is_admin)

    def is_empty(self) -> bool:
        """生成キューが空かどうか判定"""
        return self.queue.empty()

    def _drain_queue(self, q: asyncio.Queue[TTSQueueItem], guild_id: int) -> None:
        temp = []
        while not q.empty():
            try:
                item = q.get_nowait()
                if item.get("guild_id") != guild_id:
                    temp.append(item)
            except asyncio.QueueEmpty:
                break
        for item in temp:
            q.put_nowait(item)

    def _filter_queue(
        self, q: asyncio.Queue[TTSQueueItem], guild_id: int, author_id: int, is_admin: bool
    ) -> None:
        temp = []
        while not q.empty():
            try:
                item = q.get_nowait()
                item_guild = item.get("guild_id")
                item_author = item.get("author_id")

                if is_admin:
                    if item_guild != guild_id:
                        temp.append(item)
                else:
                    if item_guild != guild_id or item_author != author_id:
                        temp.append(item)
            except asyncio.QueueEmpty:
                break
        for item in temp:
            q.put_nowait(item)
