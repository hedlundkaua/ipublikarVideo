"""Shared limits for CPU-heavy media processes."""
import os


def ffmpeg_threads():
    default = max(1, (os.cpu_count() or 2) // 2)
    try:
        return max(1, int(os.getenv("PUBLI_FFMPEG_THREADS", default)))
    except (TypeError, ValueError):
        return default


def tts_concurrency():
    try:
        value = int(os.getenv("PUBLI_TTS_CONCURRENCY", "3"))
    except ValueError:
        value = 3
    return min(6, max(1, value))


def with_ffmpeg_threads(command):
    command = list(command)
    if command and os.path.basename(os.fspath(command[0])) == "ffmpeg" and "-threads" not in command:
        command[1:1] = ["-threads", str(ffmpeg_threads())]
    return command
