#!/usr/bin/env python3
import csv
import io
import json
import logging
from pathlib import Path


class DictionaryManager:
    """カスタム辞書データを管理し、ファイルに永続化するサービス"""

    def __init__(
        self,
        file_path: str | Path = "data/custom_dict.json",
        logger: logging.Logger | None = None,
    ) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger("bot.dictionary_manager")
        self._dict: dict[str, str] = {}
        self.load_dictionary()

    def load_dictionary(self) -> None:
        """辞書ファイルからデータをロードする"""
        if not self.file_path.exists():
            self._dict = {}
            return

        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                self._dict = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            self.logger.error(
                f"辞書ファイル '{self.file_path}' の読み込みに失敗しました。"
                f"データの初期化を行いますが、既存ファイルの上書きに注意してください: {e}"
            )
            self._dict = {}

    def save_dictionary(self) -> None:
        """辞書データをファイルに保存する"""
        try:
            with self.file_path.open("w", encoding="utf-8") as f:
                json.dump(self._dict, f, ensure_ascii=False, indent=4)
        except OSError as e:
            self.logger.error(f"辞書ファイルの保存に失敗しました: {e}")

    def get_all(self) -> dict[str, str]:
        """現在の辞書全体を返す"""
        return self._dict

    def add_word(self, word: str, reading: str) -> bool:
        """単語と読み方を辞書に追加/更新する"""
        lower_word = word.lower()
        self._dict[lower_word] = reading
        self.save_dictionary()
        return True

    def delete_word(self, word: str) -> bool:
        """単語を辞書から削除する"""
        lower_word = word.lower()
        if lower_word in self._dict:
            del self._dict[lower_word]
            self.save_dictionary()
            return True
        return False

    def export_to_csv(self) -> str | None:
        """辞書データをCSV形式の文字列としてエクスポートする"""
        if not self._dict:
            return None

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow(["Original Word", "Reading (Yomi)"])
        for original_word_lower, reading in self._dict.items():
            writer.writerow([original_word_lower, reading])

        return output.getvalue()