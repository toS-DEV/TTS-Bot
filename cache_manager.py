import os
import hashlib

class VoiceCacheManager:
    def __init__(self, cache_dir="voice_cache", max_size_mb=500):
        self.cache_dir = cache_dir
        self.max_size_bytes = max_size_mb * 1024 * 1024
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

    def get_cache_path(self, text, speaker_id):
        # Create a unique filename from the text and speaker ID using hashing
        hash_str = hashlib.md5(f"{text}_{speaker_id}".encode()).hexdigest()
        return os.path.join(self.cache_dir, f"{hash_str}.wav")

    def clean_cache(self):
        # Check current total cache size
        files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir) if os.path.isfile(os.path.join(self.cache_dir, f))]
        total_size = sum(os.path.getsize(f) for f in files)

        if total_size <= self.max_size_bytes:
            return

        # Sort files by size in descending order (largest first)
        files.sort(key=lambda x: os.path.getatime(x))

        # Remove the largest files until the total size is under the limit
        while total_size > self.max_size_bytes and files:
            file_to_remove = files.pop(0)
            try:
                file_size = os.path.getsize(file_to_remove)
                os.remove(file_to_remove)
                total_size -= file_size
                print(f"Cache cleared: {file_to_remove} ({file_size} bytes)")
            except FileNotFoundError:
                continue
