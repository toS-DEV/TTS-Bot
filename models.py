from enum import Enum
from typing import List, Tuple

# ----------------------------------------------------------------------
# 1. 各スタイル（感情）のデータ構造
# ----------------------------------------------------------------------
class Style:
    """各キャラクターの感情スタイルIDとその表示名を保持するクラス"""
    def __init__(self, style_id: int, name: str):
        self.id: int = style_id
        self.name: str = name

    def __repr__(self) -> str:
        return f"Style(id={self.id}, name='{self.name}')"

# ----------------------------------------------------------------------
# 2. キャラクター（UUID）とスタイルの紐付けを行う Enum
# ----------------------------------------------------------------------
class Models(Enum):
    """
    キャラクター（UUID）を定義し、Styles (Style ID, Name) のリストを保持する Enum。
    Enum の value は UUID に設定されます。
    """
    
    # 実際にはここに正しいUUIDを設定してください
    Wamom = {
        'uuid': "6a423b4e-9648-11ee-a7f5-0242ac1c000c",
        'styles': [
            Style(style_id=129974237, name="V5"),
            Style(style_id=2054078406, name="ーーねおき"),
            Style(style_id=599764237, name="ーー外国人"),
        ]
    }
    
    つくよみちゃん = {
        'uuid': "tsukuyomichan-2.0.0",
        'styles': [
            Style(style_id=0, name="せいれい"),
        ]
    }
    
    # --- クラスメソッド ---
    
    @classmethod
    def get_style_data(cls) -> List[Tuple[str, Style]]:
        """
        全てのキャラクターのスタイルIDとUUIDをタプル (UUID, Style ID, Style Name) の形式でリスト化して返します。
        主に Select Menu の選択肢生成に使用します。
        """
        all_styles = []
        for model in cls:
            uuid = model.value['uuid']
            for style in model.value['styles']:
                # (UUID, Style ID, Style Name)
                all_styles.append((uuid, style.id, style.name))
        return all_styles

    @classmethod
    def get_style_by_id(cls, style_id: int) -> Tuple[str, Style, str] | None:
        """
        Style ID から対応する UUID と Style オブジェクト、キャラクター名を取得します。
        例: (UUID, Style(id=129974237, name='V5 (基本)'), 'Wamom')
        """
        for model in cls:
            uuid = model.value['uuid']
            char_name = model.name
            for style in model.value['styles']:
                if style.id == style_id:
                    return uuid, style, char_name
        return None

    @classmethod
    def get_default_style(cls) -> Tuple[str, int]:
        """
        デフォルトのスタイル（最初のモデルの最初のスタイル）の UUID と ID を返します。
        """
        default_model = list(cls)[0].value # 最初のモデルを取得
        default_style = default_model['styles'][0]
        return default_model['uuid'], default_style.id