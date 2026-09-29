import os
import subprocess


def extract_audio(video_path, output_audio_path):
    command = ["ffmpeg", "-y", "-i", video_path, "-q:a", "0", "-map", "a", output_audio_path]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"ffmpeg audio extraction failed: {error.stderr}") from error
    return output_audio_path


def process_video(video_file):
    if not os.path.exists(video_file):
        raise FileNotFoundError(f"Video file does not exist: {video_file}")
    audio_file = os.path.splitext(video_file)[0] + ".mp3"
    return extract_audio(video_file, audio_file)