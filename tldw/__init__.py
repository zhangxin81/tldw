"""tldw — self-hosted video understanding & summarization pipeline."""

from .core import (
    Segment, Transcript, Source, parse_source, PipelineError,
    parse_vtt, chunk_segments, asr_transcribe, build_transcript,
    summarize, render_markdown, fetch_metadata, log,
    cmd_digest, cmd_asr, cmd_summarize, cmd_doctor, main,
)

__version__ = "1.0.0"
__all__ = [
    "Segment", "Transcript", "Source", "parse_source", "PipelineError",
    "parse_vtt", "chunk_segments", "asr_transcribe", "build_transcript",
    "summarize", "render_markdown", "fetch_metadata", "log",
    "cmd_digest", "cmd_asr", "cmd_summarize", "cmd_doctor", "main",
]
