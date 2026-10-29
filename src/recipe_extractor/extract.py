#!/usr/bin/env python3
"""Extract a structured recipe from a social cooking video.

Pipeline, given any TikTok / Instagram / YouTube / Facebook URL (or a Meta IG
Messaging CDN share link):

    download video  ->  sample frames at scene cuts  ->  vision LLM  ->  recipe JSON
                    \\->  persist the MP4 so it can be played back natively

Two download families are supported by :func:`download_video`:

* Public social URLs (TikTok, ``instagram.com/reel/…``, YouTube Shorts) ->
  yt-dlp, optionally through a rotating proxy.
* Meta IG Messaging CDN URLs (``lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=…``)
  served when someone *shares* a reel into your DM inbox. These respond with
  ``Content-Type: video/mp4`` directly (no redirect, no public permalink — Meta
  does not expose one), so we stream them with httpx in
  :func:`_download_lookaside_mp4`. Both paths produce the same
  ``(video_path, metadata, route)`` tuple so the downstream pipeline is identical.

The only required external service is an OpenAI-compatible vision LLM endpoint
(OpenRouter by default). Set ``OPENROUTER_API_KEY``. ``yt-dlp`` and ``ffmpeg``
must be on PATH.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from .prompts import vision_extraction_prompt
except ImportError:  # pragma: no cover - allows running as a loose script
    from prompts import vision_extraction_prompt  # type: ignore

# --- Configuration (all env-driven) -----------------------------------------

# Where downloaded videos, thumbnails and per-run artifacts land.
OUTPUT_DIR = Path(os.environ.get("RECIPE_OUTPUT_DIR", "recipe_output")).resolve()
RUNS_DIR = OUTPUT_DIR / "runs"
THUMBS_DIR = OUTPUT_DIR / "thumbnails"
VIDEOS_DIR = OUTPUT_DIR / "videos"

# OpenAI-compatible chat-completions endpoint with vision support.
OPENROUTER_URL = os.environ.get(
    "RECIPE_LLM_URL", "https://openrouter.ai/api/v1/chat/completions"
)
DEFAULT_MODEL = os.environ.get("RECIPE_MODEL", "google/gemini-2.5-flash")
IMAGE_DETAIL = os.environ.get("RECIPE_IMAGE_DETAIL", "low")

# Frame extraction caps. Scene-change detection drives the actual count (it
# picks the visually informative cuts), but we bracket the result so we never
# feed the LLM 1 frame from a static talking-head nor 60 frames from a fast-cut
# montage.
FRAME_CAP = int(os.environ.get("RECIPE_FRAME_CAP", "20"))
FRAME_FLOOR = int(os.environ.get("RECIPE_FRAME_FLOOR", "6"))
SCENE_THRESHOLD = float(os.environ.get("RECIPE_SCENE_THRESHOLD", "0.3"))
DEFAULT_MAX_FRAMES = FRAME_CAP

# Optional rotating proxy for yt-dlp (helps when a platform geo/rate limits a
# datacenter IP). Generic: pass any proxy URL via RECIPE_PROXY_URL.
PROXY_HOST = os.environ.get("PROXY_HOST", "")
PROXY_PORT = os.environ.get("PROXY_PORT", "")

logger = logging.getLogger("recipe_extractor")


def redact_cmd(cmd: list[str]) -> str:
    """Keep proxy credentials out of logs and run artifacts."""
    redacted: list[str] = []
    hide_next = False
    for part in cmd:
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        redacted.append(part)
        if part in {"--proxy", "--geo-verification-proxy"}:
            hide_next = True
    return " ".join(redacted)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    logger.info("command.start cmd=%s", redact_cmd(cmd))
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        logger.error("command.failed cmd=%s details=%s", redact_cmd(cmd), details)
        raise RuntimeError(details or f"command failed: {redact_cmd(cmd)}") from exc


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"missing required binary: {name}")


def default_cookies_from_browser() -> str | None:
    """Optional yt-dlp ``--cookies-from-browser`` value (e.g. ``chrome``).

    Pulling cookies from a logged-in browser profile lets yt-dlp fetch
    age/login-gated clips. Leave unset for public content.
    """

    return os.environ.get("RECIPE_YTDLP_COOKIES_BROWSER")


def proxy_url() -> str | None:
    """Return an optional rotating proxy URL for yt-dlp.

    Either pass a full URL via ``RECIPE_PROXY_URL`` (recommended), or supply
    ``PROXY_USERNAME`` / ``PROXY_PASSWORD`` (+ optional ``PROXY_HOST`` /
    ``PROXY_PORT`` / ``PROXY_COUNTRY``) and we'll assemble it. Returns None when
    no proxy is configured — direct download is attempted instead.
    """
    explicit = os.environ.get("RECIPE_PROXY_URL")
    if explicit:
        return explicit

    username = os.environ.get("PROXY_USERNAME")
    password = os.environ.get("PROXY_PASSWORD")
    if not (username and password):
        return None
    host = PROXY_HOST or "p.webshare.io"
    port = PROXY_PORT or "80"
    country = os.environ.get("PROXY_COUNTRY")
    if country:
        username = f"{country.upper()}-{username}"
    return f"http://{username}:{password}@{host}:{port}"


# --- Video persistence (so it plays back natively, not as a dying embed) -----


def _upload_via_rsync(video_id: str, source_path: Path) -> bool:
    """Optionally push ``source_path`` to a remote nginx/CDN host via restricted rsync.

    Set ``RECIPE_VIDEO_UPLOAD_SSH`` (e.g. ``recipe@your-host:`` rooted at a
    write-only rrsync command) and ``RECIPE_VIDEO_UPLOAD_SSH_KEY`` (an inline
    private key) to enable. This keeps MP4s off an ephemeral app container —
    the file streams straight to the host that serves it.

    Returns ``True`` on success. Any failure is logged and falls through to the
    local-copy path so an extraction never breaks just because the CDN blipped.
    """

    host = os.environ.get("RECIPE_VIDEO_UPLOAD_SSH")
    key_material = os.environ.get("RECIPE_VIDEO_UPLOAD_SSH_KEY")
    if not host or not key_material:
        return False
    if shutil.which("rsync") is None:
        logger.warning("video.upload_skipped reason=rsync_missing id=%s", video_id)
        return False
    import tempfile

    key_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as fh:
            fh.write(key_material if key_material.endswith("\n") else key_material + "\n")
            key_path = Path(fh.name)
        os.chmod(key_path, 0o600)
        ssh_cmd = (
            f"ssh -i {key_path} "
            "-o StrictHostKeyChecking=accept-new "
            "-o ConnectTimeout=15"
        )
        cmd = [
            "rsync",
            "-az",
            "--timeout=60",
            "-e",
            ssh_cmd,
            str(source_path),
            f"{host}{video_id}.mp4",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.warning(
                "video.upload_failed id=%s rc=%s stderr=%s",
                video_id,
                result.returncode,
                result.stderr.strip()[:400],
            )
            return False
        logger.info("video.upload_ok id=%s host=%s", video_id, host)
        return True
    except Exception as exc:  # pragma: no cover - network / fs errors
        logger.warning("video.upload_error id=%s err=%s", video_id, exc)
        return False
    finally:
        if key_path is not None and key_path.exists():
            try:
                key_path.unlink()
            except OSError:
                pass


def persist_video(video_id: str, source_path: Path) -> str | None:
    """Persist a downloaded MP4 to wherever your app serves video from.

    Two modes, picked at runtime by env:

    * **Remote mode** — when ``RECIPE_VIDEO_UPLOAD_SSH`` is set, rsync the MP4
      to a remote host and skip writing a local copy. Returns the literal
      ``"cdn"`` so the caller knows the file is pinned remotely (resolve it via
      ``RECIPE_VIDEO_BASE_URL`` on the read side).
    * **Local mode** (default) — copy to ``<output>/videos/<id>.mp4`` and return
      the relative path.

    Why self-host at all? Platform iframes are unreliable: TikTok's embed
    silently swaps in random "For You" clips when autoplay is blocked, and
    Instagram's embed forbids autoplay outright. A native ``<video>`` element
    pointing at your own MP4 sidesteps both. Storage is cheap: an average
    TikTok is ~5 MB, so ~12,800 clips fit in 64 GB.
    """

    if not source_path.exists():
        return None

    if _upload_via_rsync(video_id, source_path):
        return "cdn"

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    target = VIDEOS_DIR / f"{video_id}.mp4"
    shutil.copyfile(source_path, target)
    return str(target.relative_to(OUTPUT_DIR))


def persist_cover(video_id: str, run_dir: Path) -> str | None:
    """Copy the run's downloaded thumbnail to a stable path.

    TikTok/Instagram thumbnail URLs in metadata expire (~36h on TikTok), so we
    always persist the JPG locally and serve it ourselves instead of leaning on
    the upstream signed URL.
    """

    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    candidates = [
        path
        for path in run_dir.glob("source.*")
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]
    if not candidates:
        return None
    target = THUMBS_DIR / f"{video_id}.jpg"
    shutil.copyfile(candidates[0], target)
    return str(target.relative_to(OUTPUT_DIR))


def media_payload(
    metadata: dict[str, Any],
    cover_path: str | None,
    video_path: str | None = None,
) -> dict[str, Any]:
    """Distill yt-dlp metadata into a small 'media' shape worth storing."""

    payload: dict[str, Any] = {}
    if cover_path:
        payload["cover_path"] = cover_path
    if video_path:
        payload["video_path"] = video_path
    if metadata.get("duration"):
        payload["duration_seconds"] = float(metadata["duration"])
    if metadata.get("width") and metadata.get("height"):
        payload["width"] = int(metadata["width"])
        payload["height"] = int(metadata["height"])
    if metadata.get("view_count"):
        payload["view_count"] = int(metadata["view_count"])
    if metadata.get("uploader"):
        payload["uploader"] = metadata["uploader"]
    return payload


# --- Download ----------------------------------------------------------------


def _download_video_once(
    url: str,
    run_dir: Path,
    cookies_from_browser: str | None,
    proxy: str | None,
) -> tuple[Path, dict[str, Any]]:
    require_binary("yt-dlp")
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--max-filesize",
        "250m",
        "--merge-output-format",
        "mp4",
        "--write-info-json",
        "--write-description",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "-o",
        str(run_dir / "source.%(ext)s"),
        url,
    ]
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    if proxy:
        cmd.extend(["--proxy", proxy])
    run(cmd)
    candidates = [
        path
        for path in run_dir.glob("source.*")
        if path.suffix.lower() not in {".json", ".description", ".srt", ".vtt", ".part"}
    ]
    if not candidates:
        raise RuntimeError("yt-dlp completed but no video file was found")
    info_path = run_dir / "source.info.json"
    metadata = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return candidates[0], metadata


def _is_lookaside_cdn_url(url: str) -> bool:
    """Detect Meta IG Messaging CDN URLs that serve raw MP4 directly.

    Verified empirically: these URLs respond with ``Content-Type: video/mp4``
    and the MP4 bytes — no redirect, no HTML page. yt-dlp has no extractor for
    them, so we download directly.
    """
    low = (url or "").lower()
    return "lookaside.fbsbx.com" in low or "ig_messaging_cdn" in low


def _download_lookaside_mp4(url: str, run_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Stream a Meta IG Messaging CDN MP4 to ``run_dir/source.mp4``.

    Returns the same ``(Path, metadata)`` shape as :func:`_download_video_once`
    so the rest of the pipeline is unchanged. Metadata is empty — Meta does not
    expose title/uploader/description for third-party reel shares.
    """
    import httpx

    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "source.mp4"
    ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
        "Mobile/15E148 Safari/604.1"
    )
    with httpx.stream(
        "GET",
        url,
        headers={"User-Agent": ua, "Accept": "*/*"},
        follow_redirects=False,
        timeout=60.0,
    ) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"lookaside CDN download failed status={resp.status_code}")
        ctype = (resp.headers.get("content-type") or "").lower()
        if "video" not in ctype:
            raise RuntimeError(f"lookaside CDN unexpected content-type={ctype!r}")
        with target.open("wb") as f:
            for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                f.write(chunk)
    if not target.exists() or target.stat().st_size < 1024:
        raise RuntimeError("lookaside CDN download produced empty file")
    metadata: dict[str, Any] = {}
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return target, metadata


def download_video(
    url: str,
    run_dir: Path,
    cookies_from_browser: str | None,
    proxy: str | None,
) -> tuple[Path, dict[str, Any], str]:
    # Meta IG Messaging CDN URLs serve the MP4 directly — yt-dlp has no
    # extractor for them. Bypass yt-dlp and stream with httpx; produce the same
    # tuple shape so the rest of the pipeline is unchanged.
    if _is_lookaside_cdn_url(url):
        video_path, metadata = _download_lookaside_mp4(url, run_dir / "lookaside")
        return video_path, metadata, "lookaside"

    downloader_proxy = proxy or proxy_url()
    attempts: list[tuple[str, str | None]] = []
    if downloader_proxy:
        attempts.append(("proxy", downloader_proxy))
    attempts.append(("direct", None))

    last_error: Exception | None = None
    for label, attempt_proxy in attempts:
        try:
            video_path, metadata = _download_video_once(
                url, run_dir / label, cookies_from_browser, attempt_proxy
            )
            return video_path, metadata, label
        except Exception as exc:
            last_error = exc
            logger.warning("download.attempt_failed route=%s url=%s err=%s", label, url, exc)
    raise RuntimeError(str(last_error) if last_error else "download failed")


# --- Frame extraction (scene-cut sampling, not uniform) ----------------------


def probe_duration(video_path: Path) -> float | None:
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ]
        )
        return float(result.stdout.strip())
    except Exception as exc:
        logger.warning("ffprobe.failed path=%s err=%s", video_path, exc)
        return None


def _ffmpeg_extract(video_path: Path, frames_dir: Path, vf: str) -> list[Path]:
    """Run ffmpeg with ``vf`` and return the produced jpeg files in order."""
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            vf,
            "-vsync",
            "vfr",
            "-q:v",
            "3",
            str(frames_dir / "frame_%03d.jpg"),
        ]
    )
    return sorted(frames_dir.glob("frame_*.jpg"))


def extract_frames(
    video_path: Path,
    run_dir: Path,
    max_frames: int,
    *,
    floor: int | None = None,
    scene_threshold: float | None = None,
) -> list[Path]:
    """Sample frames at scene cuts rather than at uniform intervals.

    Uniform sampling ("one frame every N seconds") loses the moments that
    actually carry recipe information — the *cut* where the chef pulls the tray
    out of the oven, the on-screen text card listing ingredients, the final
    plated shot. Scene-change detection (``ffmpeg select='gt(scene,T)'``) keeps
    those by definition.

    We always pin the **first** and **last** frames (intro card + finished
    dish) so they're never dropped, then trim or top up to stay between
    ``floor`` and ``max_frames`` so LLM costs are predictable. The scene
    threshold is conservative (0.3) so talking-head intros yield ~6 frames,
    while fast-cut cooking montages cap at ``max_frames``.
    """
    require_binary("ffmpeg")
    frames_dir = run_dir / "frames"
    frames_dir.mkdir(exist_ok=True)
    floor = max(2, floor or FRAME_FLOOR)
    threshold = scene_threshold if scene_threshold is not None else SCENE_THRESHOLD

    scene_vf = (
        f"select='gt(scene,{threshold:.2f})',"
        "scale=768:-1:force_original_aspect_ratio=decrease"
    )
    frames = _ffmpeg_extract(video_path, frames_dir, scene_vf)

    # Pin first and last frames. ffmpeg's `select` skips frame 0 because
    # there's no prior frame to compare against; extract them separately.
    pinned_dir = run_dir / "frames_pinned"
    pinned_dir.mkdir(exist_ok=True)
    duration = probe_duration(video_path)
    pin_first = pinned_dir / "_first.jpg"
    pin_last = pinned_dir / "_last.jpg"
    try:
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-vf",
                "select='eq(n\\,0)',scale=768:-1:force_original_aspect_ratio=decrease",
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(pin_first),
            ]
        )
    except Exception:  # noqa: BLE001
        pin_first = None  # type: ignore[assignment]
    if duration and duration > 1:
        try:
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-sseof",
                    "-0.5",
                    "-i",
                    str(video_path),
                    "-vf",
                    "scale=768:-1:force_original_aspect_ratio=decrease",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(pin_last),
                ]
            )
        except Exception:  # noqa: BLE001
            pin_last = None  # type: ignore[assignment]

    keep_paths: list[Path] = []
    if pin_first and pin_first.exists():
        keep_paths.append(pin_first)
    keep_paths.extend(frames)
    if pin_last and pin_last.exists() and (not keep_paths or keep_paths[-1] != pin_last):
        keep_paths.append(pin_last)

    # Floor: if scene detection was too quiet, top up with uniform sampling so
    # the model doesn't have to extrapolate from 1–2 frames.
    if len(keep_paths) < floor and (duration or 0) > 0:
        topup_dir = run_dir / "frames_topup"
        topup_dir.mkdir(exist_ok=True)
        interval = max(1.5, (duration or 60.0) / floor)
        topup_vf = f"fps=1/{interval:.3f},scale=768:-1:force_original_aspect_ratio=decrease"
        topup_frames = _ffmpeg_extract(video_path, topup_dir, topup_vf)
        existing = set(keep_paths)
        for f in topup_frames:
            if f not in existing and len(keep_paths) < floor:
                keep_paths.append(f)

    # Cap: keep first + last and evenly distributed scene cuts.
    if len(keep_paths) > max_frames:
        head = keep_paths[0]
        tail = keep_paths[-1]
        middle = keep_paths[1:-1]
        slots = max_frames - 2
        if slots <= 0:
            keep_paths = [head, tail] if head != tail else [head]
        else:
            step = len(middle) / slots
            picked = [middle[int(i * step)] for i in range(slots)]
            keep_paths = [head, *picked, tail]
        kept_set = set(keep_paths)
        for f in frames:
            if f not in kept_set:
                try:
                    f.unlink()
                except Exception:  # noqa: BLE001
                    pass

    if not keep_paths:
        raise RuntimeError("ffmpeg completed but no frames were extracted")
    return keep_paths


# --- Vision LLM --------------------------------------------------------------


def image_part(path: Path) -> dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": IMAGE_DETAIL},
    }


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def call_llm(
    video: dict[str, str],
    metadata: dict[str, Any],
    frames: list[Path],
    model: str,
    run_dir: Path,
) -> dict[str, Any]:
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("RECIPE_LLM_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY (or RECIPE_LLM_API_KEY) is required")
    content: list[dict[str, Any]] = [
        {"type": "text", "text": vision_extraction_prompt(video, metadata, len(frames))}
    ]
    content.extend(image_part(frame) for frame in frames)
    payload = {
        "model": model,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "recipe-extractor",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM request failed ({exc.code}): {body}") from exc
    redacted = dict(data)
    if redacted.get("choices"):
        redacted["choices"] = [
            {
                "index": choice.get("index"),
                "finish_reason": choice.get("finish_reason"),
                "message": {
                    "role": (choice.get("message") or {}).get("role"),
                    "content": "<redacted; see recipe.json>",
                },
            }
            for choice in redacted["choices"]
        ]
    (run_dir / "llm-response.json").write_text(
        json.dumps(redacted, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    text = data["choices"][0]["message"]["content"]
    recipe = parse_json_response(text)
    recipe["source_url"] = recipe.get("source_url") or video["url"]
    return recipe


# --- URL parsing -------------------------------------------------------------

_TIKTOK_RE = re.compile(
    r"tiktok\.com/(?:@[^/]+/(?:video|photo)/(?P<tiktok_id>\d+)|v/(?P<tiktok_short>\d+))",
    re.IGNORECASE,
)
_INSTAGRAM_RE = re.compile(
    r"instagram\.com/(?:[^/]+/)?(?:reel|p)/(?P<ig_id>[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?[^ ]*?v=|shorts/|embed/|v/)(?P<yt_id>[A-Za-z0-9_-]{6,})"
    r"|youtu\.be/(?P<yt_short>[A-Za-z0-9_-]{6,}))",
    re.IGNORECASE,
)
_FACEBOOK_RE = re.compile(
    r"facebook\.com/(?:reel/(?P<fb_reel>\d+)|watch/?\?v=(?P<fb_watch>\d+)|[^/]+/videos/(?P<fb_video>\d+))",
    re.IGNORECASE,
)


def _slug_from_url(url: str) -> str:
    """Stable lowercase slug for hosts we don't recognise; used as id suffix."""
    cleaned = re.sub(r"^https?://(?:www\.|m\.)?", "", url, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", cleaned).strip("-").lower()
    return cleaned[:80] or "recipe"


def _ig_messaging_cdn_asset_id(url: str) -> str | None:
    """Parse numeric ``asset_id`` from ``lookaside.fbsbx.com/ig_messaging_cdn`` URLs."""
    low = (url or "").lower()
    if "lookaside.fbsbx.com" not in low and "ig_messaging_cdn" not in low:
        return None
    q = parse_qs(urlparse(url.strip()).query)
    raw = (q.get("asset_id") or [None])[0]
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s.isdigit() else None


def feed_row_from_url(url: str, *, title: str | None = None, added_via: str = "user_share") -> dict[str, str]:
    """Build a minimal ``{id, source, source_id, title, url}`` row for any URL.

    Recognises TikTok, Instagram, YouTube, Facebook, and Meta IG Messaging CDN
    share links. Any other http(s) URL is accepted as a generic ``Web`` source
    and passed through to yt-dlp (which supports hundreds of sites).
    """

    if not url:
        raise ValueError("url is required")
    cleaned = url.split("?", 1)[0].rstrip("/")
    raw = url.strip()
    tiktok = _TIKTOK_RE.search(cleaned)
    if tiktok:
        source_id = tiktok.group("tiktok_id") or tiktok.group("tiktok_short")
        return {
            "id": f"tiktok-{source_id}",
            "source": "TikTok",
            "source_id": source_id,
            "title": title or "Shared TikTok recipe",
            "url": cleaned,
            "added_via": added_via,
        }
    instagram = _INSTAGRAM_RE.search(cleaned)
    if instagram:
        source_id = instagram.group("ig_id")
        return {
            "id": f"instagram-{source_id}",
            "source": "Instagram",
            "source_id": source_id,
            "title": title or "Shared Instagram recipe",
            "url": cleaned,
            "added_via": added_via,
        }
    youtube = _YOUTUBE_RE.search(raw)
    if youtube:
        source_id = youtube.group("yt_id") or youtube.group("yt_short")
        return {
            "id": f"youtube-{source_id}",
            "source": "YouTube",
            "source_id": source_id,
            "title": title or "Shared YouTube recipe",
            "url": raw.split("&", 1)[0],
            "added_via": added_via,
        }
    facebook = _FACEBOOK_RE.search(cleaned)
    if facebook:
        source_id = (
            facebook.group("fb_reel")
            or facebook.group("fb_watch")
            or facebook.group("fb_video")
        )
        return {
            "id": f"facebook-{source_id}",
            "source": "Facebook",
            "source_id": source_id,
            "title": title or "Shared Facebook recipe",
            "url": cleaned,
            "added_via": added_via,
        }
    # Meta IG Messaging CDN (DM reel share) — one stable id per ``asset_id``.
    if _is_lookaside_cdn_url(raw):
        cdn_asset = _ig_messaging_cdn_asset_id(raw)
        if not cdn_asset:
            raise ValueError(
                "Instagram Messaging CDN URL needs a numeric asset_id "
                "(…/ig_messaging_cdn?asset_id=…). Resolve to the instagram.com/reel/… "
                "permalink first (see instagram_cdn.py)."
            )
        return {
            "id": f"instagram-cdn-{cdn_asset}",
            "source": "Instagram",
            "source_id": cdn_asset,
            "title": title or "Shared Instagram recipe",
            "url": raw.strip(),
            "added_via": added_via,
        }
    if not re.match(r"^https?://", raw, flags=re.IGNORECASE):
        raise ValueError(f"unsupported recipe video host: {url}")
    slug = _slug_from_url(cleaned)
    return {
        "id": f"web-{slug}",
        "source": "Web",
        "source_id": slug,
        "title": title or "Shared recipe",
        "url": cleaned,
        "added_via": added_via,
    }


def reject_unfetchable_instagram_cdn_url(url: str) -> None:
    """Reject signed Instagram CDN URLs we genuinely cannot download server-side.

    The distinction that matters:

    - ``lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=…`` — Meta IG Messaging
      CDN MP4. **This IS fetchable** by :func:`_download_lookaside_mp4` (responds
      ``Content-Type: video/mp4`` directly). Never reject these.
    - ``cdninstagram.com`` / ``fbcdn.net`` / ``instagram.f*.fna.fbcdn.net`` —
      signed image/video CDN URLs that are session-bound and short-lived;
      yt-dlp has no extractor and a direct fetch returns 403. Reject early so
      the caller sees a clear error. Resolve the reel to an
      ``instagram.com/reel/…`` permalink first (see ``instagram_cdn.py``).
    """
    u = (url or "").strip().lower()
    if not u.startswith(("http://", "https://")):
        return
    if "lookaside.fbsbx.com" in u or "ig_messaging_cdn" in u:
        return
    if any(x in u for x in ("cdninstagram.com", "fbcdn.net", "instagram.f")):
        raise ValueError(
            "That link is a signed Instagram image/video CDN URL "
            "(cdninstagram.com / fbcdn.net) which is session-bound and cannot be "
            "fetched server-side. Resolve the reel to an instagram.com/reel/… "
            "permalink first (see instagram_cdn.py: resolve_ig_share_url_from_message_mid)."
        )


# --- Top-level orchestration -------------------------------------------------


def process_video(
    video: dict[str, str],
    *,
    model: str,
    max_frames: int,
    cookies_from_browser: str | None,
    proxy: str | None,
    persist: bool = True,
) -> dict[str, Any]:
    """Run the full pipeline for one feed row and return a result dict.

    Result shape on success::

        {
          "id", "source", "source_id", "source_url", "creator", "title",
          "status": "processed",
          "recipe": { ...structured recipe JSON... },
          "media": { "video_path", "cover_path", "duration_seconds", ... },
          "extraction": { "run_dir", "model", "frames", "download_route" }
        }

    On failure ``status`` is ``"failed"`` and ``extraction.error`` is set; the
    function never raises for a single bad video.
    """
    run_dir = RUNS_DIR / video["id"] / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        video_path, metadata, download_route = download_video(
            video["url"], run_dir, cookies_from_browser, proxy
        )
        frames = extract_frames(video_path, run_dir, max_frames)
        recipe = call_llm(video, metadata, frames, model, run_dir)
        (run_dir / "recipe.json").write_text(
            json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        cover = persist_cover(video["id"], run_dir / download_route) if persist else None
        video_local = persist_video(video["id"], video_path) if persist else str(video_path)
        return {
            "id": video["id"],
            "source": video["source"],
            "source_id": video["source_id"],
            "source_url": video["url"],
            "creator": video.get("creator") or metadata.get("uploader") or "@unknown",
            "title": recipe.get("title") or video["title"],
            "summary": recipe.get("description") or "",
            "status": "processed",
            "recipe": recipe,
            "media": media_payload(metadata, cover, video_local),
            "extraction": {
                "run_dir": str(run_dir),
                "model": model,
                "frames": len(frames),
                "download_route": download_route,
            },
        }
    except Exception as exc:
        logger.error("video.extract.failed id=%s url=%s err=%s", video["id"], video["url"], exc)
        return {
            "id": video["id"],
            "source": video["source"],
            "source_id": video["source_id"],
            "source_url": video["url"],
            "creator": video.get("creator") or "unknown",
            "title": video["title"],
            "summary": "",
            "status": "failed",
            "recipe": {},
            "extraction": {"run_dir": str(run_dir), "model": model, "error": str(exc)[:600]},
        }


def extract_recipe(
    url: str,
    *,
    title: str | None = None,
    model: str = DEFAULT_MODEL,
    max_frames: int = DEFAULT_MAX_FRAMES,
    cookies_from_browser: str | None = None,
    proxy: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Extract a structured recipe (and persist the playable MP4) from a URL.

    This is the one function most callers want. Give it any supported social
    URL; get back the result dict described in :func:`process_video`.
    """
    reject_unfetchable_instagram_cdn_url(url)
    feed_row = feed_row_from_url(url, title=title)
    effective_cookies = (
        cookies_from_browser if cookies_from_browser is not None else default_cookies_from_browser()
    )
    return process_video(
        feed_row,
        model=model,
        max_frames=max_frames,
        cookies_from_browser=effective_cookies,
        proxy=proxy,
        persist=persist,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract a recipe from a social cooking video.")
    parser.add_argument("url", help="TikTok / Instagram / YouTube / Facebook video URL")
    parser.add_argument("--title", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES)
    parser.add_argument("--cookies-from-browser")
    parser.add_argument("--proxy", help="Optional proxy URL for yt-dlp.")
    parser.add_argument("--no-persist", action="store_true", help="Don't copy the MP4 into the output dir.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(levelname)s %(message)s",
    )
    result = extract_recipe(
        args.url,
        title=args.title,
        model=args.model,
        max_frames=args.max_frames,
        cookies_from_browser=args.cookies_from_browser,
        proxy=args.proxy,
        persist=not args.no_persist,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "processed" else 1


if __name__ == "__main__":
    sys.exit(main())
