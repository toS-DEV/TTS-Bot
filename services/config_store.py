# services/config_store.py
import json
import logging
import os
from collections.abc import Callable
from typing import Any, TypedDict, cast


class AutoJoinConfig(TypedDict):
    voice_channel_id: int
    text_channel_id: int


class ConfigStore:
    def __init__(
        self,
        prefs_path: str = "prefs.json",
        auto_join_path: str = "auto_join_config.json",
        logger: logging.Logger | None = None,
    ):
        self.prefs_path = prefs_path
        self.auto_join_path = auto_join_path
        self.logger = logger or logging.getLogger("bot.config_store")

        self.prefs: dict[str, dict[str, Any]] = self.load_prefs()
        self.auto_join_configs: dict[str, AutoJoinConfig] = self.load_auto_join_configs()

    # --- Prefs (ユーザー設定) 関連 ---

    def load_prefs(self) -> dict[str, dict[str, Any]]:
        """prefs.json をファイルから読み込む"""
        if not os.path.isfile(self.prefs_path):
            return {}
        try:
            with open(self.prefs_path, "r", encoding="utf-8") as f:
                return cast(dict[str, dict[str, Any]], json.load(f))
        except Exception:
            self.logger.exception(f"Failed to load {self.prefs_path}")
            return {}

    def save_prefs(self) -> None:
        """prefs.json に現在の設定を書き込む"""
        try:
            with open(self.prefs_path, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f, indent=2, ensure_ascii=False)
            self.logger.info(f"Saved preferences to {self.prefs_path}")
        except Exception:
            self.logger.exception(f"Failed to save {self.prefs_path}")

    def get_pref(
        self,
        user_id: int,
        key: str,
        def_val: object,
        cond: Callable[[object], bool],
    ) -> object:
        """ユーザー設定を取得し、存在しない場合やバリデーション失敗時はデフォルト値を補完・設定する"""
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

    def set_pref(self, user_id: int, key: str, value: Any) -> None:
        """ユーザー設定を更新する"""
        user_id_str = str(user_id)
        if user_id_str not in self.prefs or not isinstance(self.prefs[user_id_str], dict):
            self.prefs[user_id_str] = {}
        self.prefs[user_id_str][key] = value

    # --- AutoJoin 関連 ---

    def load_auto_join_configs(self) -> dict[str, AutoJoinConfig]:
        """auto_join_config.json をファイルから読み込む"""
        if not os.path.exists(self.auto_join_path):
            return {}
        try:
            with open(self.auto_join_path, "r", encoding="utf-8") as f:
                return cast(dict[str, AutoJoinConfig], json.load(f))
        except Exception:
            self.logger.exception(f"Failed to load {self.auto_join_path}")
            return {}

    def save_auto_join_configs(self) -> None:
        """auto_join_config.json に現在の設定を書き込む"""
        try:
            with open(self.auto_join_path, "w", encoding="utf-8") as f:
                json.dump(self.auto_join_configs, f, indent=2, ensure_ascii=False)
            self.logger.info(f"Saved auto-join configs to {self.auto_join_path}")
        except Exception:
            self.logger.exception(f"Failed to save {self.auto_join_path}")

    def set_auto_join_config(
        self, guild_id: int, voice_channel_id: int, text_channel_id: int
    ) -> None:
        """ギルドの自動参加設定を登録・保存する"""
        guild_key = str(guild_id)
        self.auto_join_configs[guild_key] = {
            "voice_channel_id": voice_channel_id,
            "text_channel_id": text_channel_id,
        }
        self.save_auto_join_configs()

    def get_auto_join_config(self, guild_id: int) -> AutoJoinConfig | None:
        """ギルドの自動参加設定を取得する"""
        return self.auto_join_configs.get(str(guild_id))