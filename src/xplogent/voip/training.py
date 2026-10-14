"""Training material for the phone agent: recordings, scripts/FAQs and Q&A sheets.

Uploads land in ``data/voip/training`` and are processed one at a time in the
background (the server is often CPU only):

* **recordings** are transcribed with the built-in faster-whisper. A stereo call
  recording is split per channel so the transcript says who spoke (agent on the
  left channel, customer on the right, or the other way round);
* **documents** (txt/md/pdf…) are read as text;
* **Q&A CSV** rows become one "Q: … A: …" knowledge chunk each.

Everything ends up in the knowledge base the live agent searches during a call.
``build_playbook`` then asks the call model to distil the transcripts into the
playbook (FAQ, objections, call stages, tone) that is added to every call's prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from xplogent.core.config import Config, data_dir, load_config
from xplogent.core.logging import get_logger
from xplogent.memory.store import Store

_log = get_logger("voip.training")

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".webm", ".aac", ".wma",
             ".mp4"}
DOC_EXT = {".txt", ".md", ".markdown", ".pdf", ".rst", ".html", ".json"}
QA_EXT = {".csv", ".tsv"}
LAYOUTS = ("agent-left", "agent-right")

_Q_NAMES = ("question", "questions", "q", "query", "prompt", "customer", "ask")
_A_NAMES = ("answer", "answers", "a", "response", "reply", "agent")


def training_dir() -> Path:
    path = data_dir() / "voip" / "training"
    path.mkdir(parents=True, exist_ok=True)
    return path


def playbook_path() -> Path:
    return data_dir() / "voip" / "playbook.md"


def classify(filename: str) -> str:
    """``recording`` | ``qa`` | ``document`` | "" (not supported)."""
    ext = Path(filename).suffix.lower()
    if ext in AUDIO_EXT:
        return "recording"
    if ext in QA_EXT:
        return "qa"
    if ext in DOC_EXT:
        return "document"
    return ""


def safe_name(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .") or "upload"
    return name[:120]


# ── Q&A spreadsheets ──────────────────────────────────────────────────────────
def parse_qa_csv(text: str) -> list[tuple[str, str]]:
    """Question/answer pairs from a CSV (or TSV). Column names are flexible; without a
    recognisable header the first two columns are used."""
    text = text.lstrip("﻿")
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    qi = next((i for i, h in enumerate(header) if h in _Q_NAMES), None)
    ai = next((i for i, h in enumerate(header) if h in _A_NAMES), None)
    if qi is not None and ai is not None and qi != ai:
        body, width = rows[1:], len(header)
    else:
        qi, ai, body, width = 0, 1, rows, 2
    sep = getattr(dialect, "delimiter", ",")
    pairs = []
    for r in body:
        if len(r) > max(qi, ai):
            q = r[qi].strip()
            # An unquoted comma inside a last-column answer splits it: put it back.
            a = (sep.join(r[ai:]) if ai == width - 1 else r[ai]).strip()
            if q and a:
                pairs.append((q, a))
    return pairs


# ── recordings ───────────────────────────────────────────────────────────────
_whisper_cache: dict[str, Any] = {}


def _whisper_model(name: str):
    """faster-whisper model; shares the live-call model when it's the same one."""
    from xplogent.voip.providers import WhisperLocalSTT

    live = WhisperLocalSTT._model
    if live is not None and getattr(live, "_xpl_name", "") == name:
        return live
    if name not in _whisper_cache:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("transcribing recordings needs faster-whisper: "
                               "pip install 'xplogent[voip]'") from exc
        model = WhisperModel(name, device="auto", compute_type="auto")
        model._xpl_name = name
        _whisper_cache.clear()          # keep at most one extra model in memory
        _whisper_cache[name] = model
    return _whisper_cache[name]


def whisper_settings(cfg: Config) -> tuple[str, str]:
    """(model, language) for transcribing training recordings."""
    v = cfg.voip or {}
    stt = v.get("stt") or {}
    tr = v.get("training") or {}
    model = str(tr.get("whisper_model") or "")
    if not model:
        model = str(stt.get("model") or "small.en") if stt.get("provider", "whisper-local") \
            == "whisper-local" else "small.en"
    lang = str(tr.get("language") or stt.get("language") or "en")
    return model, lang


def _channels(path: Path) -> int:
    import av

    with av.open(str(path)) as container:
        streams = container.streams.audio
        if not streams:
            raise ValueError("the file has no audio track")
        return int(streams[0].codec_context.channels or 1)


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def merge_turns(segments: list[tuple[float, str, str]]) -> list[tuple[float, str, str]]:
    """Sort (start, speaker, text) segments by time and join consecutive same-speaker ones."""
    out: list[tuple[float, str, str]] = []
    for start, who, text in sorted(segments, key=lambda s: s[0]):
        if out and out[-1][1] == who:
            out[-1] = (out[-1][0], who, f"{out[-1][2]} {text}")
        else:
            out.append((start, who, text))
    return out


def format_transcript(turns: list[tuple[float, str, str]]) -> str:
    return "\n".join(f"[{_fmt_ts(t)}] {who}: {text}" for t, who, text in turns)


def transcribe_recording(path: Path, *, layout: str = "agent-left", model: str = "small.en",
                         language: str = "en",
                         progress: Callable[[float], None] | None = None) -> str:
    """Speaker-labelled transcript of a call recording (blocking; run in a thread)."""
    import numpy as np
    from faster_whisper import decode_audio

    whisper = _whisper_model(model)
    tracks: list[tuple[str, Any]]
    if _channels(path) >= 2:
        left, right = decode_audio(str(path), sampling_rate=16000, split_stereo=True)
        if np.abs(left - right).mean() < 1e-3:          # "stereo" that is really mono
            tracks = [("Speaker", left)]
        else:
            agent, customer = (left, right) if layout != "agent-right" else (right, left)
            tracks = [("Agent", agent), ("Customer", customer)]
    else:
        tracks = [("Speaker", decode_audio(str(path), sampling_rate=16000))]

    segments: list[tuple[float, str, str]] = []
    for n, (who, audio) in enumerate(tracks):
        duration = max(len(audio) / 16000.0, 0.01)
        parts, _info = whisper.transcribe(audio, language=language or None, beam_size=1,
                                          vad_filter=True, condition_on_previous_text=False)
        for seg in parts:
            text = seg.text.strip()
            if text:
                segments.append((float(seg.start), who, text))
            if progress:
                progress(min(0.99, (n + min(seg.end / duration, 1.0)) / len(tracks)))
    return format_transcript(merge_turns(segments))


# ── knowledge base ───────────────────────────────────────────────────────────
async def _add_document(store: Store, embedder, title: str, source: str,
                        pieces: list[str]) -> tuple[int | None, int]:
    pieces = [p for p in pieces if p.strip()]
    if not pieces:
        return None, 0
    digest = hashlib.sha256("\n\x00".join(pieces).encode("utf-8", "replace")).hexdigest()
    if store.doc_exists(digest):
        return None, 0                              # same content already in the knowledge base
    doc_id = store.add_document(source, title, digest)
    vecs = await embedder.embed(pieces)
    for i, piece in enumerate(pieces):
        store.add_chunk(doc_id, i, piece, vecs[i] if i < len(vecs) else [])
    return doc_id, len(pieces)


async def process_item(item_id: int, *, cfg: Config | None = None,
                       make_provider: Callable[[str], Any] | None = None,
                       transcribe: Callable[..., str] | None = None) -> dict[str, Any]:
    """Turn one uploaded item into knowledge (and a transcript for recordings)."""
    from xplogent.core.rag import chunk_text, read_text
    from xplogent.memory.vector import Embedder

    cfg = cfg or load_config()
    if make_provider is None:
        from xplogent.providers.registry import build_provider as make_provider
    store = Store(cfg.db_path)
    provider = None
    try:
        item = store.get_training_item(item_id)
        if not item:
            return {"ok": False, "error": "no such item"}
        store.update_training_item(item_id, status="processing", progress=0.02, error="")
        path = Path(item["path"])
        title = f"Training: {item['filename']}"
        kind = item["kind"]
        transcript = ""
        if kind == "recording":
            model, lang = whisper_settings(cfg)
            loop = asyncio.get_running_loop()
            last = [0.0]

            def report(p: float) -> None:
                if p - last[0] >= 0.02:
                    last[0] = p
                    loop.call_soon_threadsafe(
                        lambda: store.update_training_item(item_id, progress=round(p, 3)))

            run = transcribe or transcribe_recording
            transcript = await asyncio.to_thread(
                run, path, layout=(item["options"] or {}).get("layout", "agent-left"),
                model=model, language=lang, progress=report)
            if not transcript.strip():
                raise ValueError("no speech found in the recording")
            pieces = chunk_text(f"Call recording {item['filename']}\n\n{transcript}")
        elif kind == "qa":
            text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
            pairs = parse_qa_csv(text)
            if not pairs:
                raise ValueError("no question/answer rows found (use columns 'question' and 'answer')")
            pieces = [f"Q: {q}\nA: {a}" for q, a in pairs]
            transcript = "\n\n".join(pieces)
        else:
            text = await asyncio.to_thread(read_text, path)
            if not text.strip():
                raise ValueError("no text found in the file (a scanned PDF needs OCR first)")
            pieces = chunk_text(text)
            transcript = text[:200_000]
        provider = make_provider(cfg.embedding_model)
        doc_id, chunks = await _add_document(store, Embedder(provider), title,
                                             f"voip-training:{item_id}", pieces)
        store.update_training_item(item_id, status="done", progress=1.0, transcript=transcript,
                                   doc_id=doc_id, chunks=chunks,
                                   error="" if doc_id else "already in the knowledge base")
        return {"ok": True, "chunks": chunks}
    except Exception as exc:  # noqa: BLE001 - reported on the item
        _log.warning("training item %s failed: %s", item_id, exc)
        store.update_training_item(item_id, status="error", error=str(exc)[:500])
        return {"ok": False, "error": str(exc)}
    finally:
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.aclose()
        store.close()


def delete_item(store: Store, item_id: int) -> bool:
    item = store.get_training_item(item_id)
    if not item:
        return False
    if item.get("doc_id"):
        store.delete_document(int(item["doc_id"]))
    path = Path(item.get("path") or "")
    with contextlib.suppress(OSError):
        if path.is_file() and training_dir() in path.resolve().parents:
            path.unlink()
    store.delete_training_item(item_id)
    return True


# ── background worker (one item at a time) ───────────────────────────────────
class _Worker:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.recovered = False
        self.process: Callable[..., Any] = process_item

    def kick(self, cfg: Config | None = None) -> None:
        if self.task is not None and not self.task.done():
            return
        self.task = asyncio.create_task(self._run(cfg))

    async def _run(self, cfg: Config | None) -> None:
        cfg = cfg or load_config()
        with Store(cfg.db_path) as store:
            if not self.recovered:      # items cut off by a restart
                for it in store.list_training_items("processing"):
                    store.update_training_item(it["id"], status="queued", progress=0)
                self.recovered = True
        while True:
            with Store(cfg.db_path) as store:
                queued = store.list_training_items("queued")
            if not queued:
                return
            await self.process(queued[-1]["id"], cfg=cfg)     # oldest first


worker = _Worker()


# ── playbook ─────────────────────────────────────────────────────────────────
_EXTRACT = """You are studying a real sales/support phone call to train an AI phone agent \
for {company}. From the transcript below, write short bullet notes under these headings \
(skip a heading if nothing fits; never invent facts):
## Questions customers ask (with the answer given)
## Objections and good responses
## Call flow (opening, discovery, offer, closing)
## Tone and phrases that worked

Transcript:
{text}"""

_REDUCE = """You are writing the playbook an AI phone agent for {company} reads before \
every call. Merge the notes below (from {n} real calls and the FAQ sheet) into one \
concise playbook in Markdown with exactly these sections:
# Playbook
## Call flow
## Frequently asked questions
## Objections and responses
## Tone and wording
Keep it under 900 words, use bullets, keep concrete facts (prices, times, policies) \
exactly as written, drop duplicates, and never invent anything.

Notes:
{notes}"""


class PlaybookJob:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.status, self.progress, self.draft, self.error = "idle", 0.0, "", ""
        self.task: asyncio.Task | None = None

    def snapshot(self) -> dict[str, Any]:
        return {"status": self.status, "progress": round(self.progress, 3),
                "draft": self.draft, "error": self.error}


playbook_job = PlaybookJob()


def _windows(text: str, size: int) -> list[str]:
    lines, out, buf = text.splitlines(), [], ""
    for line in lines:
        if len(buf) + len(line) + 1 > size and buf:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n{line}" if buf else line
    if buf.strip():
        out.append(buf)
    return out


async def build_playbook(cfg: Config, provider, *, job: PlaybookJob | None = None,
                         window: int = 6000, max_windows: int = 40) -> str:
    """Distil the transcripts (and Q&A) into a playbook draft with the call model."""
    from xplogent.providers.base import Message, Role

    v = cfg.voip or {}
    company = str(v.get("company") or "the company")
    with Store(cfg.db_path) as store:
        calls = store.training_transcripts(("recording",))
        qa = store.training_transcripts(("qa",))
    if not calls and not qa:
        raise ValueError("upload and process some call recordings or a Q&A sheet first")
    parts = [w for c in calls for w in _windows(c["transcript"], window)][:max_windows]
    notes: list[str] = []
    total = len(parts) + 1
    for i, part in enumerate(parts):
        msg = await provider.complete([Message(role=Role.USER, content=_EXTRACT.format(
            company=company, text=part))], temperature=0.2)
        if msg.content.strip():
            notes.append(msg.content.strip())
        if job:
            job.progress = (i + 1) / total
    for item in qa:
        notes.append("## FAQ sheet\n" + item["transcript"][:8000])
    joined = "\n\n---\n\n".join(notes)
    if len(joined) > window * 3:     # keep the final prompt within a small model's context
        joined = joined[: window * 3]
    msg = await provider.complete([Message(role=Role.USER, content=_REDUCE.format(
        company=company, n=len(calls), notes=joined))], temperature=0.2)
    draft = msg.content.strip()
    if not draft:
        raise ValueError("the model returned an empty playbook")
    if job:
        job.progress = 1.0
    return draft


def save_playbook(text: str) -> None:
    path = playbook_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n" if text.strip() else "", encoding="utf-8")
