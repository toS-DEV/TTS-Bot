#!/usr/bin/env python3
import hashlib
import logging
from pathlib import Path


class VoiceCacheManager:
    """音声キャッシュファイルの作成・取得・自動削除を管理するサービス"""

    def __init__(
        self,
        cache_dir: str | Path = "voice_cache",
        max_size_mb: int = 500,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.logger = logger or logging.getLogger("bot.cache_manager")

        # キャッシュディレクトリが存在しなければ作成
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_cache_path(self, text: str, speaker_id: str | int) -> Path:
        """テキストとスピーカーIDのハッシュからキャッシュパスを生成"""
        hash_str = hashlib.md5(f"{text}_{speaker_id}".encode()).hexdigest()
        return self.cache_dir / f"{hash_str}.wav"

    def clean_cache(self) -> None:
        """キャッシュ全体の容量が上限を超えている場合、古い順に削除"""
        files = [p for p in self.cache_dir.iterdir() if p.is_file()]
        total_size = sum(f.stat().st_size for f in files)

        if total_size <= self.max_size_bytes:
            return

        # 最終アクセス日時（atime）が古い順にソート
        files.sort(key=lambda p: p.stat().st_atime)

        while total_size > self.max_size_bytes and files:
            file_to_remove = files.pop(0)
            try:
                file_size = file_to_remove.stat().st_size
                file_to_remove.unlink()
                total_size -= file_size
                self.logger.info(
                    f"Cache cleared: {file_to_remove.name} ({file_size} bytes)"
                )
            except FileNotFoundError:
                continue