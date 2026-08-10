#!/usr/bin/env python3
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Style:
    """各キャラクターの感情スタイルIDとその表示名を保持するデータモデル"""

    id: int
    name: str


@dataclass(frozen=True)
class ModelSpec:
    """キャラクターのUUIDと所属スタイル一覧を保持する不変データモデル"""

    uuid: str
    styles: list[Style]


class Models(Enum):
    """キャラクター定義Enum"""

    Wamom = ModelSpec(
        uuid="6a423b4e-9648-11ee-a7f5-0242ac1c000c",
        styles=[
            Style(id=129974237, name="V5"),
            Style(id=2054078406, name="ーーねおき"),
            Style(id=599764237, name="ーー外国人"),
        ],
    )

    @classmethod
    def get_style_data(cls) -> list[tuple[str, int, str]]:
        """すべてのキャラクターのスタイル情報を (UUID, Style ID, Style Name) の形式で返します。"""
        all_styles: list[tuple[str, int, str]] = []
        for model in cls:
            spec: ModelSpec = model.value
            for style in spec.styles:
                all_styles.append((spec.uuid, style.id, style.name))
        return all_styles

    @classmethod
    def get_style_by_id(cls, style_id: int) -> tuple[str, Style, str] | None:
        """Style ID から対応する UUID、Style オブジェクト、キャラクター名を取得します。"""
        for model in cls:
            spec: ModelSpec = model.value
            char_name: str = model.name
            for style in spec.styles:
                if style.id == style_id:
                    return spec.uuid, style, char_name
        return None

    @classmethod
    def get_default_style(cls) -> tuple[str, int]:
        """デフォルトのスタイル（最初のモデルの最初のスタイル）の UUID と ID を返します。"""
        default_spec: ModelSpec = next(iter(cls)).value
        default_style: Style = default_spec.styles[0]
        return default_spec.uuid, default_style.id