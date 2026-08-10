import json
import os
import re
import subprocess
import unicodedata
import wave
from typing import Protocol, runtime_checkable

import alkana
import discord
import emoji

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
OWNER_DISPLAY_NAME = os.getenv("OWNER_DISPLAY_NAME", "マスター")

# テキスト置換用正規表現パターン
REPLACEMENT_PATTERNS = {
    "url": (r"https?://[^\s ]+", "ユーアールエル省略"),
    "no": (r"no\.?([0-9])", r"ナンバー\1"),
    "a_particle": (r"a ([a-z])", r"アッ \1"),
}


@runtime_checkable
class DictManager(Protocol):
    """辞書マネージャーのプロトコル定義"""
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

    parts: list[str] = re.split(r"([。！？!？\?、\n\s\(\)（）]|「」)", text)

    combined: list[str] = []
    current: str = ""

    for part in parts:
        if not part:
            continue

        if re.match(r"^\s+$", part):
            if current.strip():
                combined.append(current.strip())
            current = ""
            continue

        if part in ("？", "?"):
            current += "？"
            if current.strip():
                combined.append(current.strip())
            current = ""
            continue

        if part in ("。", ".", "！", "!", "、", "(", ")", "（", "）"):
            if current.strip():
                combined.append(current.strip())
            current = ""
            continue

        current += part

    if current.strip():
        combined.append(current.strip())

    processed: list[str] = []
    for chunk in combined:
        chunk = re.sub(r"[。\.！!\,、\(\)（）]+$", "", chunk).strip()
        if chunk:
            processed.append(chunk)

    return processed

def extract_line_effects(line: str) -> tuple[str, dict[str, bool]]:
    """行頭のMarkdown記法（#, ##, ###, -#, >）を検出・除去し、エフェクトフラグを返す"""
    effects = {
        "header_1": False,
        "header_2": False,
        "header_3": False,
        "subtext": False,
        "quote": False,
    }
    line = line.strip()

    if re.match(r"^###\s+", line):
        effects["header_3"] = True
        line = re.sub(r"^###\s+", "", line)
    elif re.match(r"^##\s+", line):
        effects["header_2"] = True
        line = re.sub(r"^##\s+", "", line)
    elif re.match(r"^#\s+", line):
        effects["header_1"] = True
        line = re.sub(r"^#\s+", "", line)

    if re.match(r"^-#\s+", line):
        effects["subtext"] = True
        line = re.sub(r"^-#\s+", "", line)

    if re.match(r"^>\s+", line):
        effects["quote"] = True
        line = re.sub(r"^>\s+", "", line)

    return line, effects

def extract_inline_effects(text: str) -> tuple[str, dict[str, bool]]:
    """インラインのMarkdown記法（**, *, ~~, ||, `）を検出・除去し、エフェクトフラグを返す"""
    effects = {
        "loud": False,
        "fast": False,
        "low": False,
        "spoiler": False,
        "code": False,
    }

    if not text:
        return "", effects

    if re.search(r"\*\*.*?\*\*", text):
        effects["loud"] = True
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)

    if re.search(r"(\*|_).*?(\*|_)", text):
        effects["fast"] = True
        text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)

    if re.search(r"~~.*?~~", text):
        effects["low"] = True
        text = re.sub(r"~~(.*?)~~", r"\1", text)

    if re.search(r"\|\|.*?\|\|", text):
        effects["spoiler"] = True
        text = re.sub(r"\|\|(.*?)\|\|", r"\1", text)

    if re.search(r"`.*?`", text):
        effects["code"] = True
        text = re.sub(r"`(.*?)`", r"\1", text)

    return text, effects

def extract_markdown_effects(text: str) -> tuple[str, dict[str, bool]]:
    """Markdown記法を検出し、エフェクトフラグと記号除去後のテキストを返す"""
    effects = {
        "header_1": False,
        "header_2": False,
        "header_3": False,
        "subtext": False,
        "loud": False,
        "fast": False,
        "low": False,
        "spoiler": False,
        "code": False,
        "quote": False,
    }

    if not text:
        return "", effects

    # 行頭装飾
    if re.match(r"^###\s+", text):
        effects["header_3"] = True
        text = re.sub(r"^###\s+", "", text)
    elif re.match(r"^##\s+", text):
        effects["header_2"] = True
        text = re.sub(r"^##\s+", "", text)
    elif re.match(r"^#\s+", text):
        effects["header_1"] = True
        text = re.sub(r"^#\s+", "", text)

    if re.match(r"^-#\s+", text):
        effects["subtext"] = True
        text = re.sub(r"^-#\s+", "", text)

    if re.match(r"^>\s+", text):
        effects["quote"] = True
        text = re.sub(r"^>\s+", "", text)

    # インライン装飾
    if re.search(r"\*\*.*?\*\*", text):
        effects["loud"] = True
        text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)

    if re.search(r"(\*|_).*?(\*|_)", text):
        effects["fast"] = True
        text = re.sub(r"(\*|_)(.*?)\1", r"\2", text)

    if re.search(r"~~.*?~~", text):
        effects["low"] = True
        text = re.sub(r"~~(.*?)~~", r"\1", text)

    if re.search(r"\|\|.*?\|\|", text):
        effects["spoiler"] = True
        text = re.sub(r"\|\|(.*?)\|\|", r"\1", text)

    if re.search(r"`.*?`", text):
        effects["code"] = True
        text = re.sub(r"`(.*?)`", r"\1", text)

    return text, effects

def process_text(
    text: str,
    guild: discord.Guild | None,
    dict_manager: DictManager | object,
    bot: discord.Client,
) -> str:  # ← 戻り値を str に変更するよ
    """読み上げ用テキストの整形・変換メイン処理"""
    if not text:
        return ""

    # 1. メンション置換
    text = _replace_mentions(text, guild, bot)

    # 2. 正規化 / 小文字化
    text = unicodedata.normalize("NFKC", text).lower()

    # 3. 改行を句点にして分割しやすくする
    text = text.replace("\r\n", "。").replace("\n", "。")

    # 4. URL・記号置換
    text = apply_basic_replacements(text)

    # 5. カスタム絵文字置換
    text = _replace_custom_emojis(text)

    # 6. Unicode絵文字の日本語化
    text = _apply_emoji_reading(text)

    # 7. 連続単語の集約
    text = _summarize_continuous_words(text)

    # 8. カスタム辞書適用
    if isinstance(dict_manager, DictManager):
        custom_dict = dict_manager.get_all()
        for word, reading in custom_dict.items():
            text = text.replace(word, reading)

    # 9. 英語音訳 (alkana)
    text = _apply_alkana(text)

    # 10. 記号統一処理
    text = text.replace("!", "。").replace("！", "。").replace(".", "。")
    text = text.replace("?", "？")
    text = re.sub(r"。{2,}", "。", text)

    return text.strip()


def process_name(name: str, dict_manager: DictManager | object) -> str:
    """ユーザー表示名を読み上げ用に整形"""
    split_match = re.search(r"[@\[\(\/＼／]", name)
    if split_match:
        name = name[: split_match.start()]

    name = unicodedata.normalize("NFKC", name).lower().strip()

    if isinstance(dict_manager, DictManager):
        custom_dict = dict_manager.get_all()
        for word, reading in custom_dict.items():
            name = name.replace(word, reading)

    name = _apply_alkana(name)
    return name[:10].strip()


def apply_basic_replacements(text: str) -> str:
    """URL省略や記号置換などの基本パターン処理"""
    text = re.sub(REPLACEMENT_PATTERNS["url"][0], REPLACEMENT_PATTERNS["url"][1], text)

    text = (
        text.replace("+", " プラス ")
        .replace("-", " マイナス ")
        .replace("=", " イコール ")
    )
    text = re.sub(REPLACEMENT_PATTERNS["no"][0], REPLACEMENT_PATTERNS["no"][1], text)

    reduction = [
        ["it's", "イッツ"], ["i'm", "アイム"], ["you're", "ユーァ"],
        ["he's", "ヒーィズ"], ["she's", "シーィズ"], ["we're", "ウィーアー"],
        ["they're", "ゼァー"], ["that's", "ザッツ"], ["who's", "フーズ"],
        ["where's", "フェアーズ"], ["i'd", "アイドゥ"], ["you'd", "ユードゥ"],
        ["i've", "アイブ"], ["i'll", "アイル"], ["you'll", "ユール"],
        ["he'll", "ヒール"], ["she'll", "シール"], ["we'll", "ウィール"],
    ]
    for old, new in reduction:
        text = text.replace(old, f" {new} ")

    text = re.sub(
        REPLACEMENT_PATTERNS["a_particle"][0],
        REPLACEMENT_PATTERNS["a_particle"][1],
        text,
    )

    for char in [".", "。", "!", "！"]:
        text = text.replace(char, f" {char} ")

    return text


def _replace_mentions(
    text: str, guild: discord.Guild | None, bot: discord.Client
) -> str:
    """メンションタグを名前テキストへ変換"""
    user_ids: list[str] = re.findall(r"<@!?([0-9]+)>", text)
    for uid in user_ids:
        uid_int = int(uid)
        member = guild.get_member(uid_int) if guild else None
        raw_name = member.display_name if member else "不明なユーザー"

        name = get_effective_name(uid_int, raw_name)
        suffix = "" if uid_int == OWNER_ID else "さん"

        text = text.replace(f"<@{uid}>", f" {name}{suffix} ").replace(
            f"<@!{uid}>", f" {name}{suffix} "
        )

    role_ids: list[str] = re.findall(r"<@&([0-9]+)>", text)
    for rid in role_ids:
        role = guild.get_role(int(rid)) if guild else None
        name = role.name if role else "不明なロール"
        text = text.replace(f"<@&{rid}>", f" {name}役 ")

    channel_ids: list[str] = re.findall(r"<#([0-9]+)>", text)
    for cid in channel_ids:
        channel = bot.get_channel(int(cid))
        name = channel.name if isinstance(channel, discord.abc.GuildChannel) else "不明なチャンネル"
        text = text.replace(f"<#{cid}>", f" {name}チャンネル ")

    return text


def _replace_custom_emojis(text: str) -> str:
    """Discordカスタム絵文字を名前テキストに置換"""
    if not text:
        return ""
    return re.sub(r"<a?:([^:]+):[0-9]+>", r" \1 ", text)


def _apply_emoji_reading(text: str) -> str:
    """Unicode絵文字を日本語に変換"""
    text_with_alias = emoji.demojize(text, language="ja")
    return re.sub(r":([^:]+):", r" \1 ", text_with_alias)


def _summarize_continuous_words(text: str) -> str:
    """連続する単語のカウント＆集約"""
    if not text:
        return ""

    tokens = re.split(r"(\s+)", text)
    new_tokens = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.strip() or len(token) <= 1:
            new_tokens.append(token)
            i += 1
            continue

        count = 1
        next_idx = i + 2
        while next_idx < len(tokens) and tokens[next_idx] == token:
            count += 1
            next_idx += 2

        if count >= 3:
            new_tokens.append(f"{token} 。{count}回連続")
            i = next_idx
        else:
            new_tokens.append(token)
            i += 1

    return "".join(new_tokens)


def _apply_alkana(text: str) -> str:
    """英単語のカタカナ変換"""
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

def get_sample_rate(file_path: str) -> int:
    """音声ファイルから動的にサンプリングレート(Hz)を取得する"""
    if not file_path or not os.path.exists(file_path):
        return 48000  # デフォルト値

    try:
        # VOICEVOXなどの生成ファイル(WAV)なら標準ライブラリで取得
        if file_path.endswith(".wav"):
            with wave.open(file_path, "rb") as wf:
                return wf.getframerate()

        # MP3などの場合は ffprobe を使って取得
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate",
            "-of", "json",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        return int(data["streams"][0]["sample_rate"])

    # 想定される具体的な例外のみをキャッチする
    except (
        wave.Error,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        ValueError,
        OSError,
    ):
        return 48000


def build_ffmpeg_options(effects: dict[str, bool] | None, file_path: str | None = None) -> str:
    """エフェクト情報から FFmpeg の -af オプション文字列を動的に生成"""
    if not effects:
        return ""

    # ★ 音声ファイルから元のサンプリングレートを動的に取得！
    sample_rate = get_sample_rate(file_path) if file_path else 48000

    filters = []

    if effects.get("spoiler"):
        filters.append("areverse")

    if effects.get("header_1"):
        filters.append("volume=1.8,equalizer=f=100:width_type=h:width=200:g=8")
    elif effects.get("header_2"):
        filters.append("volume=1.4")
    elif effects.get("header_3"):
        # 元のサンプリングレートの 1.2倍（高音化）を動的計算！
        target_rate = int(sample_rate * 1.2)
        filters.append(f"asetrate={target_rate},aresample={sample_rate}")

    if effects.get("subtext"):
        filters.append("volume=0.6,lowpass=f=1500")

    if effects.get("quote"):
        filters.append("aecho=0.8:0.88:60:0.4")

    if effects.get("loud"):
        filters.append("volume=1.3")

    if effects.get("fast"):
        filters.append("atempo=1.25")

    if effects.get("low"):
        # 元のサンプリングレートの 0.85倍（低音化）を動的計算！
        target_rate = int(sample_rate * 0.85)
        filters.append(f"asetrate={target_rate},aresample={sample_rate}")

    if effects.get("code"):
        filters.append("flanger=delay=2:depth=5")

    if not filters:
        return ""

    return f'-af "{",".join(filters)}"'
