import json
import logging
from pathlib import Path


class UserDictionaryManager:
    """Discord ID と読み方の対応関係を管理するサービス"""

    def __init__(
        self,
        file_path: str | Path = "data/username_dict.json",
        logger: logging.Logger | None = None,
    ) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger("bot.user_dict_manager")
        self._dict: dict[str, str | dict[str, str]] = {}
        self.load_dictionary()

    def load_dictionary(self) -> None:
        """JSONファイルからデータをロード"""
        if not self.file_path.exists():
            self._dict = {}
            return

        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                self._dict = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            self.logger.error(f"ユーザー辞書の読み込みに失敗しました: {e}")
            self._dict = {}

    def save_dictionary(self) -> None:
        """JSONファイルへ保存"""
        try:
            with self.file_path.open("w", encoding="utf-8") as f:
                json.dump(self._dict, f, ensure_ascii=False, indent=4)
        except OSError as e:
            self.logger.error(f"ユーザー辞書の保存に失敗しました: {e}")

    def get_user_data(self, user_id: int) -> tuple[str | None, str | None]:
        """指定ユーザーの (読み方, エフェクト名) を取得する"""
        val = self._dict.get(str(user_id))
        if not val:
            return None, None

        # 新データ構造: dict型の場合
        if isinstance(val, dict):
            return val.get("reading"), val.get("effect")

        # 旧データ構造: 文字列単体の場合（互換性維持）
        return str(val), None

    def set_reading(self, user_id: int, reading: str, effect: str | None = None) -> None:
        """ユーザーID、読み方、エフェクトを保存"""
        data = {"reading": reading}
        if effect and effect != "none":
            data["effect"] = effect

        self._dict[str(user_id)] = data
        self.save_dictionary()

    def delete_reading(self, user_id: int) -> bool:
        """ユーザーIDの読み方を削除（存在すれば True）"""
        str_id = str(user_id)
        if str_id in self._dict:
            del self._dict[str_id]
            self.save_dictionary()
            return True
        return False
