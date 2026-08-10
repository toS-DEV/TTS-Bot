# 音声生成(COEIROINK)・再生キュー・FFmpeg再生管理
from typing import TypedDict

from typing_extensions import NotRequired


class TTSQueueItem(TypedDict):
    guild_id: int
    group_id: NotRequired[str]
    author_id: NotRequired[int]
    content: NotRequired[str]
    file_path: NotRequired[str | None]
    sequence_number: NotRequired[int]
    total_segments: NotRequired[int]
    effects: NotRequired[dict[str, bool]]