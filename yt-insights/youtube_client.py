"""A small YouTube Data API v3 client. It knows HTTP and YouTube, and nothing about MCP."""

import httpx

API = "https://www.googleapis.com/youtube/v3"

FRIENDLY_ERRORS = {
    "quotaExceeded": "The daily YouTube API quota is used up. It resets every day; try again tomorrow.",
    "commentsDisabled": "Comments are turned off for this video.",
    "videoNotFound": "No video found with that ID. Check the ID or URL.",
    "keyInvalid": "The server's YOUTUBE_API_KEY is not valid. The person running this server must fix it.",
    "forbidden": "YouTube refused access. The video may be private.",
}


class YouTubeError(Exception):
    """A YouTube API failure, with a message a model (or a human) can act on."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class YouTubeClient:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None):
        # The key goes in a header, not the URL: URLs end up in logs.
        # `transport` lets tests swap the real network for a fake one.
        self.http = httpx.Client(base_url=API, headers={"X-Goog-Api-Key": api_key},
                                 timeout=20, transport=transport)
        self.units_used = 0   # every list call below costs 1 quota unit

    # `with YouTubeClient(key) as yt:` closes the connection when the block ends.
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.http.close()

    def _get(self, endpoint: str, **params) -> dict:
        try:
            response = self.http.get(endpoint, params=params)
        except httpx.HTTPError as exc:
            raise YouTubeError("network", f"Could not reach YouTube ({type(exc).__name__}). Try again.") from exc
        self.units_used += 1
        if response.is_success:
            return response.json()
        try:
            error = response.json()["error"]
            reason = error["errors"][0]["reason"]
            detail = error.get("message", "")
        except (ValueError, KeyError, IndexError):
            reason, detail = f"http{response.status_code}", response.text[:200]
        raise YouTubeError(reason, FRIENDLY_ERRORS.get(reason, f"YouTube API error ({reason}): {detail}"))

    def video(self, video_id: str) -> dict:
        data = self._get("/videos", part="snippet,statistics,contentDetails", id=video_id)
        if not data.get("items"):
            raise YouTubeError("videoNotFound", FRIENDLY_ERRORS["videoNotFound"])
        return data["items"][0]

    def comment_threads(self, video_id: str, limit: int, order: str, with_replies: bool):
        """Yield top-level comment threads, 100 per API call, until `limit` is reached."""
        part = "snippet,replies" if with_replies else "snippet"
        page_token, fetched = None, 0
        while fetched < limit:
            data = self._get("/commentThreads", part=part, videoId=video_id, order=order,
                             textFormat="plainText", maxResults=min(100, limit - fetched),
                             **({"pageToken": page_token} if page_token else {}))
            for thread in data.get("items", []):
                fetched += 1
                yield thread
            page_token = data.get("nextPageToken")
            if not page_token:
                return

    def replies(self, parent_id: str, limit: int) -> list[dict]:
        """Up to `limit` replies to one top-level comment (a thread itself carries only a few)."""
        replies, page_token = [], None
        while len(replies) < limit:
            data = self._get("/comments", part="snippet", parentId=parent_id, textFormat="plainText",
                             maxResults=min(100, limit - len(replies)),
                             **({"pageToken": page_token} if page_token else {}))
            replies.extend(data.get("items", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return replies[:limit]
