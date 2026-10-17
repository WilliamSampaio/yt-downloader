import glob
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import imageio_ffmpeg
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask
from ytmusicapi import YTMusic

FFMPEG_PATH = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

STANDARD_HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
AUDIO_BITRATES = [320, 256, 192, 128, 96, 64]

app = FastAPI(title="YT Downloader")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# job_id -> {status, playlist_title, items: [{title, status, error}], zip_path, tmpdir}
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


class LiveStreamError(Exception):
    """Levantada quando o link aponta para uma transmissão que ainda não terminou."""


LIVE_BLOCK_MESSAGES = {
    "is_live": (
        "Esta transmissão está ao vivo agora. Só é possível baixar depois que ela terminar."
    ),
    "is_upcoming": (
        "Esta transmissão ainda não começou. Tente novamente depois que ela terminar."
    ),
}


def live_block_message(info: dict) -> str | None:
    status = info.get("live_status")
    if status is None and info.get("is_live"):
        status = "is_live"
    return LIVE_BLOCK_MESSAGES.get(status)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    return name[:150] or "download"


def is_music_url(url: str) -> bool:
    return urlparse(url).netloc == "music.youtube.com"


def get_playlist_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if query.get("list"):
        return query["list"][0]
    # YouTube Music também abre playlists como /browse/VL<id da playlist>
    if parsed.path.startswith("/browse/VL"):
        return parsed.path[len("/browse/VL"):].split("/")[0] or None
    return None


def get_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if query.get("v"):
        return query["v"][0]
    if parsed.netloc in ("youtu.be", "www.youtu.be"):
        return parsed.path.lstrip("/").split("/")[0] or None
    return None


def extract_info(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(status_code=400, detail=f"Não foi possível ler esse link: {exc}")


def extract_playlist(list_id: str) -> dict:
    playlist_url = f"https://www.youtube.com/playlist?list={list_id}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(playlist_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise HTTPException(status_code=400, detail=f"Não foi possível ler essa playlist: {exc}")

    entries = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        vid = entry.get("id")
        video_url = entry.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
        if video_url and not video_url.startswith("http"):
            video_url = f"https://www.youtube.com/watch?v={video_url}"
        if not video_url:
            continue
        entries.append({"id": vid, "title": entry.get("title") or vid or "vídeo", "url": video_url})

    if not entries:
        raise HTTPException(status_code=400, detail="Playlist vazia ou não foi possível listar os vídeos.")

    thumbnails = info.get("thumbnails") or []
    return {
        "title": info.get("title") or "Playlist",
        "thumbnail": thumbnails[-1]["url"] if thumbnails else None,
        "entries": entries,
    }


def join_artists(artists: list[dict] | None) -> str:
    return ", ".join(a["name"] for a in artists or [] if a.get("name"))


def extract_music_playlist(list_id: str) -> dict:
    """Lê a playlist pela API do YouTube Music, que traz título e artista de cada faixa
    (o yt-dlp só devolve o título do vídeo, ex.: "Artista - Música (Official Video)")."""
    try:
        playlist = YTMusic().get_playlist(list_id, limit=None)
    except Exception as exc:  # noqa: BLE001 - ytmusicapi não tem uma exceção base própria
        raise HTTPException(status_code=400, detail=f"Não foi possível ler essa playlist do YouTube Music: {exc}")

    entries = []
    for track in playlist.get("tracks") or []:
        vid = track.get("videoId")
        if not vid or track.get("isAvailable") is False:
            continue
        title = track.get("title") or vid
        artist = join_artists(track.get("artists"))
        entries.append({
            "id": vid,
            "title": f"{artist} - {title}" if artist else title,
            "track": title,
            "artist": artist,
            "url": f"https://www.youtube.com/watch?v={vid}",
        })

    if not entries:
        raise HTTPException(status_code=400, detail="Playlist vazia ou não foi possível listar as músicas.")

    thumbnails = playlist.get("thumbnails") or []
    return {
        "title": playlist.get("title") or "Playlist",
        "thumbnail": thumbnails[-1]["url"] if thumbnails else None,
        "entries": entries,
    }


def load_playlist(url: str, list_id: str) -> dict:
    if is_music_url(url):
        return extract_music_playlist(list_id)
    return extract_playlist(list_id)


def build_ydl_opts(type_: str, quality: int, outtmpl: str) -> tuple[dict, str, str]:
    if type_ == "video":
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "outtmpl": outtmpl,
            "ffmpeg_location": FFMPEG_PATH,
            "format": (
                f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"
            ),
            "merge_output_format": "mp4",
        }
        return ydl_opts, "video/mp4", "mp4"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "ffmpeg_location": FFMPEG_PATH,
        "format": "bestaudio/best",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(quality),
            }
        ],
    }
    return ydl_opts, "audio/mpeg", "mp3"


def run_download(url: str, type_: str, quality: int, outdir: str) -> tuple[str, str]:
    outtmpl = str(Path(outdir) / "%(title)s.%(ext)s")
    ydl_opts, _media_type, expected_ext = build_ydl_opts(type_, quality, outtmpl)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        probe = ydl.extract_info(url, download=False)
        blocked = live_block_message(probe)
        if blocked:
            raise LiveStreamError(blocked)
        info = ydl.extract_info(url, download=True)

    candidates = glob.glob(str(Path(outdir) / f"*.{expected_ext}"))
    if not candidates:
        candidates = glob.glob(str(Path(outdir) / "*"))
    if not candidates:
        raise RuntimeError("Arquivo gerado não encontrado.")

    return candidates[0], info.get("title") or "download"


def heights_from_formats(info: dict) -> set:
    return {
        int(fmt["height"])
        for fmt in info.get("formats", [])
        if fmt.get("vcodec") not in (None, "none") and fmt.get("height")
    }


@app.get("/api/info")
def get_info(url: str = Query(..., min_length=1)):
    list_id = get_playlist_id(url)
    video_id = get_video_id(url)

    video_data = None
    playlist_data = None
    heights: set = set()

    if video_id or not list_id:
        info = extract_info(url)
        video_data = {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "live_message": live_block_message(info),
        }
        heights |= heights_from_formats(info)

    if list_id:
        playlist = load_playlist(url, list_id)
        playlist_data = {
            "title": playlist["title"],
            "thumbnail": playlist["thumbnail"],
            "count": len(playlist["entries"]),
            "is_music": is_music_url(url),
        }
        if not heights:
            try:
                first_info = extract_info(playlist["entries"][0]["url"])
                heights |= heights_from_formats(first_info)
            except HTTPException:
                pass

    if not video_data and not playlist_data:
        raise HTTPException(status_code=400, detail="Não foi possível identificar um vídeo ou playlist nesse link.")

    video_qualities = [h for h in STANDARD_HEIGHTS if h in heights] or sorted(heights, reverse=True) or STANDARD_HEIGHTS

    return {
        "video": video_data,
        "playlist": playlist_data,
        "video_qualities": video_qualities,
        "audio_qualities": AUDIO_BITRATES,
    }


@app.get("/api/download")
def download(
    url: str = Query(..., min_length=1),
    type: str = Query(..., pattern="^(video|audio)$"),
    quality: int = Query(...),
):
    tmpdir = tempfile.mkdtemp(prefix="ytdl_")
    _ydl_opts, media_type, expected_ext = build_ydl_opts(type, quality, "")

    try:
        filepath, title = run_download(url, type, quality, tmpdir)
    except LiveStreamError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except yt_dlp.utils.DownloadError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Falha ao baixar: {exc}")
    except RuntimeError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))

    download_name = f"{sanitize_filename(title)}.{expected_ext}"

    return FileResponse(
        path=filepath,
        media_type=media_type,
        filename=download_name,
        background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
    )


class PlaylistRequest(BaseModel):
    url: str
    type: str
    quality: int


def run_playlist_job(job_id: str, entries: list[dict], type_: str, quality: int):
    job = JOBS[job_id]
    outdir = job["tmpdir"]

    try:
        for idx, entry in enumerate(entries):
            with JOBS_LOCK:
                job["items"][idx]["status"] = "downloading"

            # Each video gets its own scratch directory so that picking "the file
            # that was just created" can never accidentally grab a file left over
            # from a previous (or still-running) entry in this same job.
            item_dir = str(Path(outdir) / f"item_{idx:05d}")
            Path(item_dir).mkdir(parents=True, exist_ok=True)
            try:
                filepath, title = run_download(entry["url"], type_, quality, item_dir)
                ext = Path(filepath).suffix
                # Faixas do YouTube Music já vêm como "Artista - Música"; prefira isso
                # ao título do vídeo, que costuma ter "(Official Video)" e afins.
                base_name = sanitize_filename(entry["title"] if entry.get("artist") else title)
                new_path = str(Path(outdir) / f"{base_name}{ext}")
                dedup = 2
                while Path(new_path).exists():
                    new_path = str(Path(outdir) / f"{base_name} ({dedup}){ext}")
                    dedup += 1
                shutil.move(filepath, new_path)
                with JOBS_LOCK:
                    job["items"][idx]["status"] = "done"
            except Exception as exc:  # noqa: BLE001 - surface any failure per item
                with JOBS_LOCK:
                    job["items"][idx]["status"] = "error"
                    job["items"][idx]["error"] = str(exc)
            finally:
                shutil.rmtree(item_dir, ignore_errors=True)

        files = glob.glob(str(Path(outdir) / "*"))
        if not files:
            with JOBS_LOCK:
                job["status"] = "error"
                job["error"] = "Nenhum vídeo pôde ser baixado."
            return

        zip_path = str(Path(tempfile.gettempdir()) / f"ytdl_playlist_{job_id}.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=Path(f).name)

        with JOBS_LOCK:
            job["status"] = "completed"
            job["zip_path"] = zip_path
    except Exception as exc:  # noqa: BLE001 - never leave the job stuck at "running"
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = f"Falha inesperada ao processar a playlist: {exc}"


@app.post("/api/playlist/start")
def start_playlist(req: PlaylistRequest):
    if req.type not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="Tipo inválido.")

    list_id = get_playlist_id(req.url)
    if not list_id:
        raise HTTPException(status_code=400, detail="O link informado não é uma playlist.")

    playlist = load_playlist(req.url, list_id)
    entries = playlist["entries"]

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "running",
        "playlist_title": playlist["title"],
        "items": [{"title": e["title"], "status": "pending", "error": None} for e in entries],
        "zip_path": None,
        "tmpdir": tempfile.mkdtemp(prefix=f"ytdl_pl_{job_id}_"),
    }

    thread = threading.Thread(
        target=run_playlist_job,
        args=(job_id, entries, req.type, req.quality),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id, "total": len(entries)}


@app.get("/api/playlist/status/{job_id}")
def playlist_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado.")

    with JOBS_LOCK:
        items = [dict(item) for item in job["items"]]
        done = sum(1 for i in items if i["status"] in ("done", "error"))
        return {
            "status": job["status"],
            "playlist_title": job["playlist_title"],
            "items": items,
            "done": done,
            "total": len(items),
            "error": job.get("error"),
        }


@app.get("/api/playlist/result/{job_id}")
def playlist_result(job_id: str):
    job = JOBS.get(job_id)
    if not job or job["status"] != "completed" or not job["zip_path"]:
        raise HTTPException(status_code=404, detail="Resultado ainda não disponível.")

    zip_path = job["zip_path"]
    tmpdir = job["tmpdir"]
    filename = f"{sanitize_filename(job['playlist_title'])}.zip"

    def cleanup():
        shutil.rmtree(tmpdir, ignore_errors=True)
        Path(zip_path).unlink(missing_ok=True)
        JOBS.pop(job_id, None)

    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(cleanup),
    )


@app.get("/api/playlist/tracklist")
def playlist_tracklist(url: str = Query(..., min_length=1)):
    list_id = get_playlist_id(url)
    if not list_id:
        raise HTTPException(status_code=400, detail="O link informado não é uma playlist.")

    # A API do YouTube Music também lê playlists comuns do YouTube e separa
    # título/artista; se falhar, cai para os títulos de vídeo do yt-dlp.
    try:
        playlist = extract_music_playlist(list_id)
    except HTTPException:
        playlist = extract_playlist(list_id)

    lines = [playlist["title"], f"{len(playlist['entries'])} músicas", ""]
    for idx, entry in enumerate(playlist["entries"], start=1):
        lines.append(f"{idx}. {entry['title']}")
    content = "\n".join(lines) + "\n"

    filename = f"{sanitize_filename(playlist['title'])}.txt"
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


class FrontendFiles(StaticFiles):
    """Sem Cache-Control o navegador pode guardar um app.js antigo por dias após
    uma atualização; "no-cache" força revalidar (via ETag) a cada carregamento."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", FrontendFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
