"""Transcribe an Earnings-21 call and store it, with speakers from the dataset's RTTM."""

from pathlib import Path

import asyncpg

from app.calls import earnings21
from app.calls.stt import SpeechRequest, Transcriber
from app.calls.transcript import label_speakers, transcribe_audio
from app.store import CallRecord, save_call


async def ingest_earnings21(
    conn: asyncpg.Connection,
    transcriber: Transcriber,
    ticker: str,
    *,
    model: str,
    root: Path | None = None,
) -> int:
    """Returns the call ID. Re-running replaces the stored transcript."""
    file_id = earnings21.CALLS[ticker]
    info = earnings21.call_info(file_id, root)
    data = earnings21.audio_path(file_id, root).read_bytes()
    result = await transcribe_audio(
        transcriber, data, SpeechRequest(model=model), filename=f"{file_id}.mp3"
    )
    segments = label_speakers(
        result.segments,
        earnings21.speaker_turns(file_id, root),
        earnings21.speaker_names(file_id, root),
        result.words,
    )
    # Name the company as its filings do, when one is stored.
    company = await conn.fetchval(
        "select company from documents where ticker = $1 order by fiscal_year desc limit 1",
        ticker,
    )
    record = CallRecord(
        ticker=ticker,
        company=company or info.company,
        fiscal_year=earnings21.FISCAL_YEAR,
        quarter=info.quarter,
        source=f"earnings21:{file_id}",
        duration=result.duration,
        stt_provider=result.provider,
        stt_model=result.model,
        speakers="earnings21 reference diarization",
    )
    return await save_call(conn, record, segments)
