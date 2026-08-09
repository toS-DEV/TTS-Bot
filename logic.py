import os
import re
import unicodedata
from typing import Protocol, runtime_checkable

import alkana
import discord
import emoji

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
OWNER_DISPLAY_NAME = os.getenv("OWNER_DISPLAY_NAME", "マスター")

REPLACEMENT_PATTERNS = {
    "url": (r"https?://[^\s　]+", "ユーアールエル省略"),
    "spoiler": (r"\|\|(.+?)\|\|", "スポイラー省略"),
    "no": (r"no\.?([0-9])", r"ナンバー\1"),
    "a_particle": (r"a ([a-z])", r"アッ \1"),
}


@runtime_checkable
class DictManager(Protocol):
    def get_all(self) -> dict[str, str]: ...


def get_effective_name(user_id: int, original_name: str) -> str:
    """IDがオーナーなら設定された名前を、そうでなければ元の名前を返す"""
    if user_id == OWNER_ID:
        return OWNER_DISPLAY_NAME
    return original_name


def split_text(text: str) -> list[str]:
    """句読点、改行、空白、括弧などでテキストを分割し、キャッシュ共通化のために記号を剥離する。"""
    if not text:
        return []

    # 分割文字（括弧を含む）
    parts: list[str] = re.split(r"([。！？!？\?、\n\s\(\)（）]|「」)", text)

    combined: list[str] = []
    current: str = ""

    for part in parts:
        if not part:
            continue

        # 空白（スペースや改行）はトリガーとして扱い、中身は捨てる
        if re.match(r"^\s+$", part):
            if current.strip():
                combined.append(current.strip())
            current = ""
            continue

        # 疑問符は保持（COEIROINKのピッチ用）
        if part in ("？", "?"):
            current += "？"
            if current.strip():
                combined.append(current.strip())
            current = ""
            continue

        # 句読点および「括弧」は確定トリガーとし、記号自体は current に含めず破棄
        if part in ("。", ".", "！", "!", "、", "(", ")", "（", "）"):
            if current.strip():
                combined.append(current.strip())
            current = ""
            continue

        current += part

    if current.strip():
        combined.append(current.strip())

    # 後処理：末尾に残った「？」以外の全記号（括弧含む）を剥離
    processed: list[str] = []
    for chunk in combined:
        # 剥離対象に括弧を追加
        chunk = re.sub(r"[。\.！!\,、\(\)（）]+$", "", chunk)
        chunk = chunk.strip()
        if chunk:
            processed.append(chunk)

    return processed


def process_text(
    text: str, guild: discord.Guild | None, dict_manager: object, bot: discord.Client
) -> str:
    """純粋な文字列(text)を受け取り、TTS用に加工して返す

    変更点（ハイブリッド剥離計画）:
    - 最終段階で記号の超正規化を行い、キャッシュの揺れを排除する。
      * 半角/全角の '!'、'.' を '。' に統一（最終的に削除される想定）
      * 半角 '?' を全角 '？' に統一して保持（疑問符アクセントのため）
    """
    if not text:
        return ""

    # 1. メンション置換
    text = _replace_mentions(text, guild, bot)

    # 2. 正規化 / 小文字化
    text = unicodedata.normalize("NFKC", text).lower()
    # 改行は一旦句点に置換しておく（後続の分割や正規化と干渉しないように）
    text = text.replace("\r\n", "。").replace("\n", "。")

    # 3.URL / スポイラー / 特殊記号の置換
    text = apply_basic_replacements(text)
    # 4. カスタム絵文字置換/
    text = _replace_custom_emojis(text)
    # 5.Unicode絵文字の日本語化
    text = _apply_emoji_reading(text)
    # 6.連続した単語（絵文字読み）をまとめる
    text = _summarize_continuous_words(text)

    # 7. カスタム辞書適用
    try:
        if isinstance(dict_manager, DictManager):
            custom_dict = dict_manager.get_all()
            for word, reading in custom_dict.items():
                text = text.replace(word, reading)
    except Exception:
        pass

    # 8. 英語音訳 (alkana)
    text = _apply_alkana(text)

    # 9. 超正規化（記号統一） - キャッシュ揺れを排除
    # 感嘆符やピリオドを句点に集約（キャッシュ共通化、最終的に剥離される想定）
    text = text.replace("!", "。").replace("！", "。").replace(".", "。")
    # 疑問符は全角に統一して保持（例: "元気？" を別チャンク／別キャッシュにするため）
    text = text.replace("?", "？")
    # 連続する句点を1つにまとめる（過剰な差分を減らす）
    text = re.sub(r"。{2,}", "。", text)
    # 前後の空白削除
    text = text.strip()

    return text


def process_name(name: str, dict_manager: object) -> str:
    """名前をTTS向けに加工し、辞書を適用する"""
    # 1. 記号での切り捨て (既存ロジックの移植)
    split_match = re.search(r"[@\[\(\/＼／]", name)
    if split_match:
        name = name[: split_match.start()]

    # 2. 正規化
    name = unicodedata.normalize("NFKC", name).lower().strip()

    # 3. カスタム辞書適用
    try:
        if isinstance(dict_manager, DictManager):
            custom_dict = dict_manager.get_all()
            for word, reading in custom_dict.items():
                name = name.replace(word, reading)
    except Exception:
        pass

    # 4. 英語音訳
    name = _apply_alkana(name)

    # 5. 最終整形
    return name[:10].strip()


def apply_basic_replacements(text: str) -> str:
    """URL省略や記号置換など、汎用的な加工ルール"""
    # パターン置換
    text = re.sub(REPLACEMENT_PATTERNS["url"][0], REPLACEMENT_PATTERNS["url"][1], text)
    text = re.sub(
        REPLACEMENT_PATTERNS["spoiler"][0], REPLACEMENT_PATTERNS["spoiler"][1], text
    )

    # 記号
    text = (
        text.replace("+", " プラス ")
        .replace("-", " マイナス ")
        .replace("=", " イコール ")
    )
    text = re.sub(REPLACEMENT_PATTERNS["no"][0], REPLACEMENT_PATTERNS["no"][1], text)

    # 短縮形
    reduction = [
        ["it's", "イッツ"],
        ["i'm", "アイム"],
        ["you're", "ユーァ"],
        ["he's", "ヒーィズ"],
        ["she's", "シーィズ"],
        ["we're", "ウィーアー"],
        ["they're", "ゼァー"],
        ["that's", "ザッツ"],
        ["who's", "フーズ"],
        ["where's", "フェアーズ"],
        ["i'd", "アイドゥ"],
        ["you'd", "ユードゥ"],
        ["i've", "アイブ"],
        ["i'll", "アイル"],
        ["you'll", "ユール"],
        ["he'll", "ヒール"],
        ["she'll", "シール"],
        ["we'll", "ウィール"],
    ]
    for old, new in reduction:
        text = text.replace(old, f" {new} ")

    text = re.sub(
        REPLACEMENT_PATTERNS["a_particle"][0],
        REPLACEMENT_PATTERNS["a_particle"][1],
        text,
    )

    # 句読点の保護
    for char in [".", "。", "!", "！"]:
        text = text.replace(char, f" {char} ")

    return text


def _replace_mentions(
    text: str, guild: discord.Guild | None, bot: discord.Client
) -> str:
    """メンションを名前文字列に置換"""
    # 1. ユーザー
    user_ids: list[str] = re.findall(r"<@!?([0-9]+)>", text)
    for uid in user_ids:
        uid_int = int(uid)  # ループの中で定義

        # --- ここから下の処理をすべてループの中にインデント（右にずらす）するよ ---
        member = guild.get_member(uid_int) if guild else None
        raw_name = member.display_name if member else "不明なユーザー"

        # OWNER_ID と比較して名前を決定
        name = get_effective_name(uid_int, raw_name)

        # オーナー以外には「さん」を付ける
        suffix = "" if uid_int == OWNER_ID else "さん"

        # 置換処理（正規表現で捕まえた元の uid を使って置換）
        text = text.replace(f"<@{uid}>", f" {name}{suffix} ").replace(
            f"<@!{uid}>", f" {name}{suffix} "
        )
    # 2. ロール
    role_ids: list[str] = re.findall(r"<@&([0-9]+)>", text)
    for rid in role_ids:
        rid_str = str(rid)
        role = guild.get_role(int(rid_str)) if guild else None
        name = role.name if role else "不明なロール"
        text = text.replace(f"<@&{rid_str}>", f" {name}役 ")

    # 3. チャンネル
    channel_ids: list[str] = re.findall(r"<#([0-9]+)>", text)
    for cid in channel_ids:
        cid_str = str(cid)
        channel = bot.get_channel(int(cid_str))

        if isinstance(channel, discord.abc.GuildChannel):
            name = channel.name
        else:
            name = "不明なチャンネル"

        text = text.replace(f"<#{cid_str}>", f" {name}チャンネル ")

    return text


def _replace_custom_emojis(text: str) -> str:
    """
    Discordのカスタム絵文字 <:name:ID> または <a:name:ID> を ' name ' に置換する。
    """
    if not text:
        return ""

    # 正規表現の解説:
    # <       : 開始文字
    # a?      : 動く絵文字の場合の 'a' (任意)
    # :       : 区切り
    # ([^:]+) : 絵文字名（1文字以上のコロン以外の文字）をキャプチャグループ1とする
    # :       : 区切り
    # [0-9]+  : 1文字以上の数字（ID部分）
    # >       : 終了文字
    return re.sub(r"<a?:([^:]+):[0-9]+>", r" \1 ", text)


def _apply_emoji_reading(text: str) -> str:
    """Unicode絵文字を日本語の読み上げテキストに変換する"""
    # 1. 絵文字をエイリアス（:grinning_face:形式）に変換
    # language="ja" を指定することで、直接日本語の名称を取得可能
    text_with_alias = emoji.demojize(text, language="ja")

    # 2. デモジャイズされた結果は「:読み:」の形式なので、コロンを除去する
    # 例: ":笑顔:" -> " 笑顔 "
    # 前後にスペースを入れることで、読み上げエンジンが単語として認識しやすくする
    processed_text = re.sub(r":([^:]+):", r" \1 ", text_with_alias)

    return processed_text


def _summarize_continuous_words(text: str) -> str:
    """
    '笑顔 笑顔 笑顔' を '笑顔 3回連続' のようにまとめる。
    """
    if not text:
        return ""

    # 単語ごとに分割（空白、句読点などで区切られている前提）
    # re.split(r'(\s+)') を使うことで区切り文字を保持し、後で復元可能にする
    tokens = re.split(r"(\s+)", text)

    new_tokens = []
    i = 0
    while i < len(tokens):
        token = tokens[i]

        # 空白や句読点、1文字以下の場合はスキップしてそのまま追加
        if not token.strip() or len(token) <= 1:
            new_tokens.append(token)
            i += 1
            continue

        # 連続回数をカウント
        count = 1
        # 次の単語（空白を挟んだ先）が同じかどうかを確認
        # tokens[i+1]は空白、tokens[i+2]が次の単語
        next_idx = i + 2
        while next_idx < len(tokens) and tokens[next_idx] == token:
            count += 1
            next_idx += 2

        if count >= 3:  # 3回以上連続する場合にまとめる（2回程度ならそのままの方が自然）
            new_tokens.append(f"{token} 。{count}回連続")
            i = next_idx
        else:
            new_tokens.append(token)
            i += 1

    return "".join(new_tokens)


def _apply_alkana(text: str) -> str:
    """アルファベット部分をalkanaでカナ変換"""
    processed = ""
    segments: list[str] = re.findall(r"[a-z]+|[^a-z]+", text)
    for i, segment in enumerate(segments):
        if segment.isalpha() and re.match(r"[a-z]+", segment):
            kana = alkana.get_kana(segment)
            processed += kana if kana else segment
        else:
            if i > 0 and segments[i - 1].isalpha():
                processed += segment.lstrip(" ")
            else:
                processed += segment
    return processed
