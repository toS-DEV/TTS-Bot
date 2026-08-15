#!/usr/bin/env python3
import hashlib
import json
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
        
        # ★ キャッシュ生成の準備として、メタデータ(JSON)も保存しておく
        meta_path = self.cache_dir / f"{hash_str}.json"
        if not meta_path.exists():
            try:
                with meta_path.open("w", encoding="utf-8") as f:
                    json.dump({"text": text}, f, ensure_ascii=False)
            except OSError as e:
                self.logger.error(f"メタデータの保存に失敗: {e}")

        return self.cache_dir / f"{hash_str}.wav"

    def clean_cache(self) -> None:
        """キャッシュ全体の容量が上限を超えている場合、古い順に削除"""
        # .wav ファイルを対象にする
        files = [p for p in self.cache_dir.glob("*.wav") if p.is_file()]
        total_size = sum(f.stat().st_size for f in files)

        if total_size <= self.max_size_bytes:
            return

        # 最終アクセス日時（atime）が古い順にソート
        files.sort(key=lambda p: p.stat().st_atime)

        while total_size > self.max_size_bytes and files:
            file_to_remove = files.pop(0)
            try:
                file_size = file_to_remove.stat().st_size
                meta_file = file_to_remove.with_suffix(".json")

                # .wav と対になる .json も削除
                file_to_remove.unlink()
                if meta_file.exists():
                    meta_file.unlink()

                total_size -= file_size
                self.logger.info(
                    f"Cache cleared: {file_to_remove.name} ({file_size} bytes)"
                )
            except FileNotFoundError:
                continue

    def clear_cache_containing(self, word: str) -> int:
        """指定された単語を含むテキストの音声キャッシュを検索して削除する"""
        if not word:
            return 0

        target_word = word.lower()
        deleted_count = 0

        # メタデータファイル(.json)を検索
        for meta_file in list(self.cache_dir.glob("*.json")):
            if not meta_file.is_file():
                continue

            try:
                # 1. まずファイルを開いて中身を読む（withブロックを出て確実にファイルを閉じる）
                with meta_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)

                cached_text = data.get("text", "").lower()

                # 2. 登録単語が含まれているか判定
                if target_word in cached_text:
                    wav_file = meta_file.with_suffix(".wav")
                    
                    # 3. wavファイルがあれば削除（失敗してもjsonの削除へ進めるように分離）
                    if wav_file.exists():
                        try:
                            wav_file.unlink()
                        except OSError as e:
                            self.logger.error(f"WAVファイル削除失敗 ({wav_file.name}): {e}")

                    # 4. jsonファイル（メタデータ）を削除
                    try:
                        meta_file.unlink()
                    except OSError as e:
                        self.logger.error(f"JSONファイル削除失敗 ({meta_file.name}): {e}")

                    deleted_count += 1

            except (json.JSONDecodeError, OSError) as e:
                self.logger.error(f"キャッシュ検索中のエラー ({meta_file.name}): {e}")

        return deleted_count