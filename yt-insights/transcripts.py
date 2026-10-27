"""Transcripts: one interface, swappable providers, and a disk cache so we never pay twice."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

TRANSCRIBE_PROMPT = (
    "Transcribe the spoken audio of this video word for word. "
    "Output plain text only. Start each paragraph with a [mm:ss] timestamp. "
    "Do not summarise or add commentary. If nobody speaks, reply exactly: NO_SPEECH"
)


class TranscriptError(Exception):
    """A transcript could not be produced; the message says why."""


@dataclass
class Transcript:
    text: str
    source: str      # who produced it, e.g. "ai_transcription:gemini-3.8-flash"
    complete: bool   # False if the provider stopped early (very long videos)


class TranscriptProvider(Protocol):
    def fetch(self, video_id: str) -> Transcript: ...


class GeminiTranscriptProvider:
    """Public videos only: Gemini watches the YouTube URL and transcribes the audio."""

    def __init__(self, api_key: str, model: str):
        from google import genai   # imported here, so the server runs without it
        self.client = genai.Client(api_key=api_key)
        self.model = model

    def fetch(self, video_id: str) -> Transcript:
        try:
            interaction = self.client.interactions.create(
                model=self.model,
                input=[
                    {"type": "text", "text": TRANSCRIBE_PROMPT},
                    {"type": "video", "uri": f"https://www.youtube.com/watch?v={video_id}"},
                ],
            )
        except Exception as exc:  # network errors, private videos, Gemini quota...
            raise TranscriptError(
                f"Gemini could not transcribe this video ({type(exc).__name__}: {exc}). "
                "Only public videos work; private and unlisted ones are refused."
            ) from exc
        # "incomplete" means Gemini stopped early (very long video): keep the part we got.
        if interaction.status not in ("completed", "incomplete"):
            raise TranscriptError(f"Gemini stopped with status '{interaction.status}'. Try again later.")
        text = (interaction.output_text or "").strip()
        if not text or text == "NO_SPEECH":
            raise TranscriptError("This video has no speech to transcribe.")
        return Transcript(text=text, source=f"ai_transcription:{self.model}",
                          complete=interaction.status == "completed")


class TranscriptStore:
    """Caches transcripts as files: <folder>/<video_id>.txt (+ a tiny .source file)."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)

    def load(self, video_id: str) -> Transcript | None:
        text_file = self.folder / f"{video_id}.txt"
        if not text_file.exists():
            return None
        source, _, complete = (self.folder / f"{video_id}.source").read_text("utf-8").partition("|")
        return Transcript(text=text_file.read_text("utf-8"), source=source, complete=complete == "complete")

    def save(self, video_id: str, transcript: Transcript) -> None:
        # Write the small .source file first: load() only trusts a cache entry once the .txt exists.
        status = "complete" if transcript.complete else "partial"
        (self.folder / f"{video_id}.source").write_text(f"{transcript.source}|{status}", "utf-8")
        (self.folder / f"{video_id}.txt").write_text(transcript.text, "utf-8")
