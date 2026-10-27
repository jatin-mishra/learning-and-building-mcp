"""Tests with a fake YouTube and a fake transcriber: no network, no API keys, no quota."""

import os
import tempfile

import httpx
import pytest

os.environ["YT_INSIGHTS_CACHE"] = tempfile.mkdtemp()   # keep test files out of your home folder
import yt_server  # noqa: E402
from mcp import Client
from transcripts import Transcript, TranscriptError, TranscriptStore
from youtube_client import YouTubeClient

VID = "dQw4w9WgXcQ"


def comment(cid, text, likes=0):
    return {"id": cid, "snippet": {"authorDisplayName": "@viewer", "textDisplay": text,
                                   "likeCount": likes, "publishedAt": "2026-10-01T10:00:00Z"}}


def fake_youtube(request: httpx.Request) -> httpx.Response:
    """Pretends to be the YouTube Data API."""
    assert request.headers["X-Goog-Api-Key"] == "test-key"   # key travels in a header
    assert "key=" not in str(request.url)                      # ...never in the URL
    p = request.url.params
    if request.url.path.endswith("/videos"):
        if p["id"] == "NNNNNNNNNNN":
            raise httpx.ConnectError("network down")
        if p["id"] != VID:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [{
            "snippet": {"title": "Never Gonna Give You Up", "channelTitle": "Rick Astley",
                        "publishedAt": "2009-10-25T06:57:33Z", "description": "The official video",
                        "tags": ["rick astley", "80s"],
                        "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/x/hqdefault.jpg"}}},
            "statistics": {"viewCount": "1700000000", "likeCount": "18000000", "commentCount": "2400000"},
            "contentDetails": {"duration": "PT3M33S"}}]})
    if request.url.path.endswith("/commentThreads"):
        if p["videoId"] == "AAAAAAAAAAA":
            return httpx.Response(403, json={"error": {"code": 403, "message": "disabled",
                                                       "errors": [{"reason": "commentsDisabled"}]}})
        start = int(p.get("pageToken", "0"))
        size = int(p["maxResults"])
        items = []
        for i in range(start, start + size):
            thread = {"snippet": {"topLevelComment": comment(f"c{i}", f"comment {i}", likes=i),
                                  "totalReplyCount": 3 if i == 0 else 0}}
            if "replies" in p["part"] and i == 0:
                thread["replies"] = {"comments": [comment("r0", "inline reply")]}  # only 1 of 3
            items.append(thread)
        nxt = start + size
        body = {"items": items}
        if nxt < 250:                       # this video has 250 top-level comments
            body["nextPageToken"] = str(nxt)
        return httpx.Response(200, json=body)
    if request.url.path.endswith("/comments"):
        return httpx.Response(200, json={"items": [comment(f"r{i}", f"reply {i}") for i in range(3)]})
    return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})


class FakeTranscriber:
    calls = 0

    def fetch(self, video_id):
        FakeTranscriber.calls += 1
        if video_id == "BBBBBBBBBBB":
            raise TranscriptError("Gemini could not transcribe this video.")
        return Transcript(text="[00:00] " + "never gonna give you up " * 1000,
                          source="ai_transcription:fake", complete=True)


@pytest.fixture(autouse=True)
def fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(yt_server, "youtube",
                        lambda: YouTubeClient("test-key", transport=httpx.MockTransport(fake_youtube)))
    monkeypatch.setattr(yt_server, "transcript_provider", lambda: FakeTranscriber())
    monkeypatch.setattr(yt_server, "store", TranscriptStore(tmp_path))
    FakeTranscriber.calls = 0


@pytest.mark.anyio
async def test_tools_are_listed_with_schemas():
    async with Client(yt_server.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"get_video", "get_comments", "get_transcript"}
    assert tools["get_comments"].input_schema["properties"]["max_comments"]["maximum"] == 500
    assert "likes" in tools["get_video"].output_schema["properties"]
    assert tools["get_video"].annotations.read_only_hint is True


@pytest.mark.anyio
@pytest.mark.parametrize("ref", [VID, f"https://www.youtube.com/watch?v={VID}&t=42",
                                 f"https://youtu.be/{VID}", f"https://youtube.com/shorts/{VID}"])
async def test_get_video_accepts_ids_and_urls(ref):
    async with Client(yt_server.mcp) as client:
        result = await client.call_tool("get_video", {"video": ref})
    assert not result.is_error
    data = result.structured_content
    assert data["title"] == "Never Gonna Give You Up"
    assert data["likes"] == 18_000_000 and data["tags"] == ["rick astley", "80s"]
    assert data["thumbnail_url"].endswith("hqdefault.jpg")


@pytest.mark.anyio
async def test_bad_ids_become_tool_errors():
    async with Client(yt_server.mcp) as client:
        not_an_id = await client.call_tool("get_video", {"video": "hello"})
        missing = await client.call_tool("get_video", {"video": "ZZZZZZZZZZZ"})
        offline = await client.call_tool("get_video", {"video": "NNNNNNNNNNN"})
    assert not_an_id.is_error and "11 characters" in not_an_id.content[0].text
    assert missing.is_error and "No video found" in missing.content[0].text
    assert offline.is_error and "Could not reach YouTube" in offline.content[0].text


@pytest.mark.anyio
async def test_comments_paginate_up_to_the_limit():
    async with Client(yt_server.mcp) as client:
        result = await client.call_tool("get_comments", {"video": VID, "max_comments": 230})
    data = result.structured_content
    assert data["returned"] == 230
    assert data["quota_units_used"] == 3          # pages of 100 + 100 + 30
    assert all(c["replies"] == [] for c in data["comments"])


@pytest.mark.anyio
async def test_replies_are_fetched_when_the_thread_is_incomplete():
    async with Client(yt_server.mcp) as client:
        result = await client.call_tool("get_comments",
                                        {"video": VID, "max_comments": 5, "include_replies": True})
    first = result.structured_content["comments"][0]
    assert first["reply_count"] == 3 and len(first["replies"]) == 3
    assert result.structured_content["quota_units_used"] == 2   # 1 thread page + 1 replies page


@pytest.mark.anyio
async def test_disabled_comments_and_limits():
    async with Client(yt_server.mcp) as client:
        disabled = await client.call_tool("get_comments", {"video": "AAAAAAAAAAA"})
        too_many = await client.call_tool("get_comments", {"video": VID, "max_comments": 5000})
    assert disabled.is_error and "turned off" in disabled.content[0].text
    assert too_many.is_error


@pytest.mark.anyio
async def test_transcript_pages_and_cache():
    async with Client(yt_server.mcp) as client:
        page1 = (await client.call_tool("get_transcript", {"video": VID})).structured_content
        page2 = (await client.call_tool("get_transcript",
                                        {"video": VID, "offset": page1["next_offset"]})).structured_content
        resource = await client.read_resource(f"youtube://transcript/{VID}")
    assert page1["total_chars"] == 24008 and len(page1["text"]) == 8000
    assert page1["source"] == "ai_transcription:fake" and page1["complete"] is True
    assert page2["offset"] == 8000 and page2["next_offset"] == 16000
    assert FakeTranscriber.calls == 1                       # second page came from the cache
    assert resource.contents[0].text.startswith("[00:00] never gonna")


@pytest.mark.anyio
async def test_transcript_resource_rejects_unknown_and_bad_ids():
    from mcp import MCPError
    async with Client(yt_server.mcp) as client:
        for uri in ("youtube://transcript/ZZZZZZZZZZZ", "youtube://transcript/not-an-id"):
            with pytest.raises(MCPError) as caught:
                await client.read_resource(uri)
            assert caught.value.error.code == -32602            # "not found" in 2026-07-28


@pytest.mark.anyio
async def test_transcript_failures_are_tool_errors():
    async with Client(yt_server.mcp) as client:
        failed = await client.call_tool("get_transcript", {"video": "BBBBBBBBBBB"})
    assert failed.is_error and "Gemini could not transcribe" in failed.content[0].text


@pytest.mark.anyio
async def test_missing_keys_explain_themselves(monkeypatch):
    monkeypatch.undo()                                       # back to the real functions
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    async with Client(yt_server.mcp) as client:
        no_yt = await client.call_tool("get_video", {"video": VID})
        no_gemini = await client.call_tool("get_transcript", {"video": "CCCCCCCCCCC"})
    assert no_yt.is_error and "YOUTUBE_API_KEY is not set" in no_yt.content[0].text
    assert no_gemini.is_error and "GEMINI_API_KEY is not set" in no_gemini.content[0].text


def test_gemini_provider_sends_the_youtube_url():
    """Run the real Gemini SDK against a fake HTTP endpoint and inspect what it sends."""
    import json
    from google import genai
    from google.genai import types
    from transcripts import GeminiTranscriptProvider

    sent = {}

    def fake_gemini(request: httpx.Request) -> httpx.Response:
        sent["url"], sent["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"id": "v1_test", "status": "completed", "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": "[00:00] Hello world"}]}]})

    provider = GeminiTranscriptProvider("test-key", "gemini-3.8-flash")
    provider.client = genai.Client(api_key="test-key", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=httpx.MockTransport(fake_gemini))))
    transcript = provider.fetch(VID)

    assert sent["url"].endswith("/v1beta/interactions")
    video_part = sent["body"]["input"][0]["content"][1]
    assert video_part == {"type": "video", "uri": f"https://www.youtube.com/watch?v={VID}"}
    assert transcript.text == "[00:00] Hello world" and transcript.complete


def test_gemini_failures_are_not_saved_as_transcripts():
    from types import SimpleNamespace
    from transcripts import GeminiTranscriptProvider

    provider = GeminiTranscriptProvider("test-key", "gemini-3.8-flash")
    provider.client = SimpleNamespace(interactions=SimpleNamespace(
        create=lambda **body: SimpleNamespace(status="failed", output_text="")))
    with pytest.raises(TranscriptError, match="status 'failed'"):
        provider.fetch(VID)


def test_parallel_transcript_calls_pay_once(monkeypatch):
    import threading
    import time

    class SlowTranscriber:
        calls = 0

        def fetch(self, video_id):
            SlowTranscriber.calls += 1
            time.sleep(0.3)                                   # pretend Gemini is busy
            return Transcript(text="[00:00] hello " * 500, source="ai_transcription:fake", complete=True)

    monkeypatch.setattr(yt_server, "transcript_provider", lambda: SlowTranscriber())
    workers = [threading.Thread(target=yt_server.get_transcript, args=(VID,)) for _ in range(3)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert SlowTranscriber.calls == 1


@pytest.mark.anyio
async def test_prompt_is_listed():
    async with Client(yt_server.mcp) as client:
        prompts = (await client.list_prompts()).prompts
        brief = await client.get_prompt("video_brief", {"video": VID})
    assert [p.name for p in prompts] == ["video_brief"]
    assert VID in brief.messages[0].content.text
