"""YouTube Insights: an MCP server that reads public YouTube data for an AI model."""

import logging
import os
import re
import threading
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from transcripts import GeminiTranscriptProvider, TranscriptError, TranscriptProvider, TranscriptStore
from youtube_client import YouTubeClient, YouTubeError

log = logging.getLogger("yt-insights")

mcp = MCPServer(
    "yt-insights",
    version="1.0.0",
    instructions="Reads public YouTube data: video details, comments and transcripts. "
                 "Accepts an 11-character video ID or any YouTube URL.",
)

# ---------- configuration: secrets come from the environment, never from the model ----------

CACHE = Path(os.environ.get("YT_INSIGHTS_CACHE", Path.home() / ".yt_insights"))
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
VideoRef = Annotated[str, Field(description="An 11-character YouTube video ID or a YouTube URL")]


def youtube() -> YouTubeClient:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise ToolError("YOUTUBE_API_KEY is not set for this server. "
                        "The person running it must add the key to the server's environment.")
    return YouTubeClient(key)


def transcript_provider() -> TranscriptProvider:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise ToolError("Transcripts are switched off: GEMINI_API_KEY is not set for this server.")
    return GeminiTranscriptProvider(key, GEMINI_MODEL)


store = TranscriptStore(CACHE / "transcripts")

# Sync tools run on worker threads, so two calls can arrive at the same moment.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(video_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(video_id, threading.Lock())


def parse_video_id(video: str) -> str:
    """Accept 'dQw4w9WgXcQ', 'youtu.be/dQw4w9WgXcQ', 'youtube.com/watch?v=...', '/shorts/...'."""
    match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", video) \
        or re.fullmatch(r"\s*([A-Za-z0-9_-]{11})\s*", video)
    if not match:
        raise ToolError(f"'{video}' is not a YouTube video ID or URL. "
                        "A video ID has 11 characters, like dQw4w9WgXcQ.")
    return match.group(1)


def as_int(value: str | None) -> int | None:
    return int(value) if value is not None else None   # YouTube sends counts as strings


# ---------- what the model gets back: typed models become the tool's outputSchema ----------

class Video(BaseModel):
    video_id: str
    url: str
    title: str
    channel: str
    published_at: str
    duration: str = Field(description="ISO 8601 duration, e.g. PT15M33S")
    description: str
    tags: list[str]
    thumbnail_url: str
    views: int | None
    likes: int | None = Field(description="None when the creator hides the like count")
    comment_count: int | None


class Reply(BaseModel):
    author: str
    text: str
    likes: int
    published_at: str


class Comment(BaseModel):
    comment_id: str
    author: str
    text: str
    likes: int
    published_at: str
    reply_count: int
    replies: list[Reply] = Field(description="Filled only when include_replies is true")


class CommentPage(BaseModel):
    video_id: str
    order: str
    returned: int
    quota_units_used: int
    comments: list[Comment]


class TranscriptPage(BaseModel):
    video_id: str
    source: str = Field(description="Who produced the text. ai_transcription means machine-made")
    complete: bool = Field(description="False if transcription stopped early on a very long video")
    total_chars: int
    offset: int
    next_offset: int | None = Field(description="Pass this as offset to read on; None at the end")
    text: str
    resource_uri: str


# ---------- tools ----------

@mcp.tool(annotations=READ_ONLY)
def get_video(video: VideoRef) -> Video:
    """Get a YouTube video's title, channel, description, tags, thumbnail, duration,
    view count, like count and comment count. Call this first for any question about a video."""
    video_id = parse_video_id(video)
    try:
        with youtube() as yt:
            item = yt.video(video_id)
    except YouTubeError as exc:
        raise ToolError(str(exc)) from exc
    snippet, stats = item["snippet"], item.get("statistics", {})
    thumbs = snippet.get("thumbnails", {})
    best = next((thumbs[k]["url"] for k in ("maxres", "standard", "high", "medium", "default")
                 if k in thumbs), "")
    return Video(
        video_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}",
        title=snippet["title"], channel=snippet["channelTitle"], published_at=snippet["publishedAt"],
        duration=item.get("contentDetails", {}).get("duration", ""),
        description=snippet.get("description", ""), tags=snippet.get("tags", []),
        thumbnail_url=best, views=as_int(stats.get("viewCount")),
        likes=as_int(stats.get("likeCount")), comment_count=as_int(stats.get("commentCount")),
    )


@mcp.tool(annotations=READ_ONLY)
def get_comments(
    video: VideoRef,
    max_comments: Annotated[int, Field(ge=1, le=500, description="Top-level comments to fetch")] = 100,
    include_replies: Annotated[bool, Field(description="Also fetch up to 100 replies per comment. "
                                                       "Costs extra quota")] = False,
    order: Literal["relevance", "time"] = "relevance",
) -> CommentPage:
    """Get a video's public comments with author, text, like count and date.
    order='relevance' returns the most engaging comments first; 'time' returns the newest first.
    Set include_replies only when replies matter, because each busy thread costs extra quota."""
    video_id = parse_video_id(video)
    comments = []
    try:
        with youtube() as yt:
            for thread in yt.comment_threads(video_id, max_comments, order, include_replies):
                top = thread["snippet"]["topLevelComment"]
                reply_count = thread["snippet"].get("totalReplyCount", 0)
                replies = []
                if include_replies and reply_count:
                    inline = thread.get("replies", {}).get("comments", [])
                    # The thread carries only a few replies; fetch the rest when it's incomplete.
                    raw = inline if len(inline) >= reply_count else yt.replies(top["id"], limit=100)
                    replies = [Reply(author=r["snippet"]["authorDisplayName"], text=r["snippet"]["textDisplay"],
                                     likes=r["snippet"].get("likeCount", 0),
                                     published_at=r["snippet"]["publishedAt"]) for r in raw]
                s = top["snippet"]
                comments.append(Comment(comment_id=top["id"], author=s["authorDisplayName"], text=s["textDisplay"],
                                        likes=s.get("likeCount", 0), published_at=s["publishedAt"],
                                        reply_count=reply_count, replies=replies))
    except YouTubeError as exc:
        raise ToolError(str(exc)) from exc
    return CommentPage(video_id=video_id, order=order, returned=len(comments),
                       quota_units_used=yt.units_used, comments=comments)


@mcp.tool(annotations=READ_ONLY)
def get_transcript(
    video: VideoRef,
    offset: Annotated[int, Field(ge=0, description="Character to start from")] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=20000, description="Characters to return")] = 8000,
) -> TranscriptPage:
    """Get the transcript of a public video, one page at a time. The first call can take a minute
    while the video is transcribed; later pages come from a cache. Keep calling with next_offset
    to read on, or stop as soon as you have what you need."""
    video_id = parse_video_id(video)
    with lock_for(video_id):   # two calls at once for one video must not pay Gemini twice
        transcript = store.load(video_id)
        if transcript is None:
            log.info("transcribing %s with %s", video_id, GEMINI_MODEL)
            try:
                transcript = transcript_provider().fetch(video_id)
            except TranscriptError as exc:
                raise ToolError(str(exc)) from exc
            store.save(video_id, transcript)
    total = len(transcript.text)
    if offset >= total:
        raise ToolError(f"offset {offset} is past the end; this transcript has {total} characters.")
    end = min(offset + max_chars, total)
    return TranscriptPage(
        video_id=video_id, source=transcript.source, complete=transcript.complete,
        total_chars=total, offset=offset, next_offset=end if end < total else None,
        text=transcript.text[offset:end], resource_uri=f"youtube://transcript/{video_id}",
    )


# ---------- a resource (the app reads it) and a prompt (the user picks it) ----------

@mcp.resource("youtube://transcript/{video_id}", mime_type="text/plain")
def transcript_file(video_id: str) -> str:
    """The full cached transcript of a video. Run get_transcript once to create it."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):   # never let a URI pick a file path
        raise ResourceNotFoundError(f"'{video_id}' is not a YouTube video ID.")
    transcript = store.load(video_id)
    if transcript is None:
        raise ResourceNotFoundError(f"No transcript cached for {video_id} yet. Call get_transcript first.")
    return transcript.text


@mcp.prompt()
def video_brief(video: str) -> str:
    """A one-page brief on a YouTube video: what it covers and how viewers responded."""
    return (
        f"Write a one-page brief on this YouTube video: {video}\n"
        "1. Call get_video for the facts, and get_comments with the defaults.\n"
        "2. Call get_transcript and read only as many pages as you need.\n"
        "3. Then write: a three-sentence summary, the key points in order, the engagement numbers, "
        "and the main themes in the comments with two or three short quotes and their like counts.\n"
        "Say clearly that the transcript is machine-made if its source starts with ai_transcription."
    )


if __name__ == "__main__":
    if not os.environ.get("YOUTUBE_API_KEY"):
        log.warning("YOUTUBE_API_KEY is not set: every YouTube tool will return an error")
    mcp.run()   # stdio by default; logs go to stderr
