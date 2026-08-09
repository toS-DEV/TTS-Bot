import json
import os
import csv
from typing import Dict, Tuple, Optional

class DictionaryManager:
    """カスタム辞書データを管理し、ファイルに永続化するクラス"""
    
    def __init__(self, file_path: str = 'custom_dict.json'):
        self.file_path = file_path
        self._dict: Dict[str, str] = {}
        self.load_dictionary()

    def load_dictionary(self):
        """辞書ファイルからデータをロードする"""
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    self._dict = json.load(f)
            except (json.JSONDecodeError, IOError):
                # ファイルが破損しているか読み込めない場合、空の辞書を初期化
                self._dict = {}
        else:
            self._dict = {}

    def save_dictionary(self):
        """辞書データをファイルに保存する"""
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(self._dict, f, ensure_ascii=False, indent=4)

    def get_all(self) -> Dict[str, str]:
        """現在の辞書全体を返す"""
        return self._dict

    def add_word(self, word: str, reading: str) -> bool:
        """単語と読み方を辞書に追加/更新する"""
        # キー（元の単語）は小文字にして保存し、検索時の大文字・小文字の不一致を防ぐ
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

    def export_to_csv(self) -> Optional[str]:
        """辞書データをCSV形式の文字列としてエクスポートする"""
        if not self._dict:
            return None
        
        # CSVデータを保持するためのバッファを使用
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        
        # ヘッダーの書き込み
        writer.writerow(['Original Word', 'Reading (Yomi)'])
        
        # データの書き込み
        for original_word_lower, reading in self._dict.items():
            # ここでは便宜的にキーを元の単語として使用
            writer.writerow([original_word_lower, reading])
            
        return output.getvalue()