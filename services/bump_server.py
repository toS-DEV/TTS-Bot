# aiohttp による Bump 通知受信サーバー
import logging
import os
import random
import re
from collections.abc import Callable
from typing import Any, TypedDict, cast

import discord
from aiohttp import web


class BumpNotificationData(TypedDict):
    guild_id: int | str | None
    message: str | None


class BumpServer:
    def __init__(
        self,
        bot: discord.Client,
        enqueue_func: Callable[[dict[str, Any]], Any],
        is_vc_connected_func: Callable[[int], bool],
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.bot = bot
        self.enqueue_func = enqueue_func
        self.is_vc_connected_func = is_vc_connected_func
        self.host = host
        self.port = port
        self.logger = logging.getLogger("bot.bump_server")
        self.app = web.Application()
        self.runner: web.AppRunner | None = None

        # ルーティング設定
        self.app.router.add_post("/bump", self.handle_bump_notification)

    @staticmethod
    def _pick_random_file(base_path: str) -> str | None:
        """ディレクトリ名の数値に基づき、再帰的にファイルを抽選する。"""
        if not os.path.exists(base_path):
            return None

        if os.path.isfile(base_path):
            return base_path

        items = os.listdir(base_path)
        if not items:
            return None

        candidates: list[str] = []
        weights: list[int] = []

        for item in items:
            full_path = os.path.join(base_path, item)
            match = re.search(r"(\d+)", item)
            weight = int(match.group(1)) if match else 10

            candidates.append(full_path)
            weights.append(weight)

        chosen = random.choices(candidates, weights=weights, k=1)[0]

        if os.path.isdir(chosen):
            return BumpServer._pick_random_file(chosen)

        return chosen

    async def handle_bump_notification(self, request: web.Request) -> web.Response:
        """外部サービスからの POST リクエストを受け取りキューへ追加する"""
        try:
            body = await request.json()
            data = cast(BumpNotificationData, body)
            raw_guild_id = data.get("guild_id")
            original_message = data.get("message") or ""

            if raw_guild_id is None:
                self.logger.warning("guild_id is missing in request")
                return web.Response(text="Missing guild_id", status=400)

            guild_id = int(raw_guild_id)

            # VC接続状態のチェック (コールバック経由)
            if not self.is_vc_connected_func(guild_id):
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
            queue_item = {
                "guild_id": guild_id,
                "author_id": bot_member.id,
                "content": original_message,
                "file_path": audio_file,
                "sequence_number": 0,
                "total_segments": 1,
            }

            # キュー投入処理 (コールバック経由)
            await self.enqueue_func(queue_item)
            self.logger.info(f"✅ {log_msg}")
            return web.Response(text="Success", status=200)

        except Exception as e:
            self.logger.exception("❌ Error processing Bump notification")
            return web.Response(text=str(e), status=500)

    async def start(self) -> None:
        """HTTP サーバーを開始する"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        self.logger.info(f"Bump notification HTTP server running on {self.host}:{self.port}")

    async def stop(self) -> None:
        """HTTP サーバーを停止する"""
        if self.runner:
            await self.runner.cleanup()
            self.logger.info("Bump notification HTTP server stopped")