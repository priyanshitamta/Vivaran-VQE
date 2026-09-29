import os


class AudioTranscriber:
    def __init__(self, model_name="base"):
        import whisper

        self.model = whisper.load_model(model_name)

    def transcribe_audio(self, audio_path):
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file does not exist: {audio_path}")
        result = self.model.transcribe(audio_path)
        return result["text"]


def transcribe_audio(audio_path, model_name=None):
    selected_model = model_name or os.getenv("WHISPER_MODEL", "tiny")
    return AudioTranscriber(selected_model).transcribe_audio(audio_path)