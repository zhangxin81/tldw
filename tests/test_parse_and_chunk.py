"""Tests for VTT/SRT parsing and chunking logic."""

import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tldw import parse_vtt, chunk_segments, Segment, Source, parse_source


# ── parse_vtt ──────────────────────────────────────────────────────────

VTT_SAMPLE = """WEBVTT

00:00:01.000 --> 00:00:03.500
Hello world

00:00:03.500 --> 00:00:06.000
Hello world this is a test

00:00:06.000 --> 00:00:09.000
<c.bg>Another</c> segment here
"""


def test_parse_vtt_basic():
    """VTT with timestamps should produce segments."""
    segs = parse_vtt(VTT_SAMPLE)
    assert len(segs) >= 2
    assert segs[0].start == 1.0
    assert "Hello" in segs[0].text


def test_parse_vtt_strips_inline_tags():
    """<c.bg> and similar inline tags should be removed."""
    segs = parse_vtt(VTT_SAMPLE)
    for s in segs:
        assert "<" not in s.text


def test_parse_vtt_scroll_repetition():
    """YouTube auto-subs repeat text in rolling cues. The prefix should be trimmed."""
    vtt = """WEBVTT

00:00:00.500 --> 00:00:02.000
the quick brown fox

00:00:02.000 --> 00:00:04.000
the quick brown fox jumps over
"""
    segs = parse_vtt(vtt)
    full = " ".join(s.text for s in segs)
    assert full.count("the quick brown fox") == 1, "repeated prefix should be trimmed"


def test_parse_vtt_empty():
    """Empty input should produce no segments."""
    assert parse_vtt("") == []
    assert parse_vtt("WEBVTT\n") == []


# ── chunk_segments ─────────────────────────────────────────────────────

def _make_segs(n, text_len=100):
    return [Segment(start=i * 5, end=i * 5 + 4, text="x" * text_len) for i in range(n)]


def test_chunk_single():
    """Short input should produce exactly one chunk."""
    segs = _make_segs(3, 50)
    chunks = chunk_segments(segs, max_chars=6000, overlap_chars=100)
    assert len(chunks) == 1


def test_chunk_multiple():
    """Long input should split into multiple chunks with overlap."""
    segs = _make_segs(100, 100)
    chunks = chunk_segments(segs, max_chars=1000, overlap_chars=200)
    assert len(chunks) >= 2
    for c in chunks:
        assert "text" in c
        assert "start" in c
        assert "end" in c


def test_chunk_overlap():
    """Adjacent chunks should share overlapping content."""
    segs = _make_segs(50, 200)
    chunks = chunk_segments(segs, max_chars=2000, overlap_chars=400)
    if len(chunks) >= 2:
        # The end of chunk[0] should be >= start of chunk[1] minus some overlap
        assert chunks[0]["end"] >= chunks[1]["start"]


# ── Source / parse_source ─────────────────────────────────────────────

def test_parse_source_youtube():
    src = parse_source("https://www.youtube.com/watch?v=abc12345678")
    assert src.platform == "youtube"
    assert src.vid == "abc12345678"
    assert "youtube.com" in src.url


def test_parse_source_youtube_short():
    src = parse_source("https://youtu.be/abc12345678")
    assert src.platform == "youtube"
    assert src.vid == "abc12345678"


def test_parse_source_bilibili():
    src = parse_source("https://www.bilibili.com/video/BV1xx411c7mu")
    assert src.platform == "bilibili"
    assert src.vid == "BV1xx411c7mu"


def test_parse_source_bare_id():
    src = parse_source("abc12345678")
    assert src.platform == "youtube"
    assert src.vid == "abc12345678"


def test_source_ts_link_youtube():
    src = parse_source("https://www.youtube.com/watch?v=abc12345678")
    link = src.ts_link(120)
    assert "t=120s" in link


def test_source_ts_link_bilibili():
    src = parse_source("https://www.bilibili.com/video/BV1xx411c7mu")
    link = src.ts_link(60)
    assert "t=60" in link


def test_source_key_youtube():
    src = parse_source("https://www.youtube.com/watch?v=abc12345678")
    assert src.key == "abc12345678"


def test_source_key_bilibili():
    src = parse_source("https://www.bilibili.com/video/BV1xx411c7mu")
    assert src.key == "bilibili_BV1xx411c7mu"


# ── Segment.hhmmss ────────────────────────────────────────────────────

def test_hhmmss_short():
    s = Segment(start=65, end=70, text="test")
    assert s.hhmmss == "01:05"


def test_hhmmss_long():
    s = Segment(start=3725, end=3730, text="test")
    assert s.hhmmss == "01:02:05"


# ── Transcript round-trip ────────────────────────────────────────────

def test_transcript_roundtrip():
    from tldw import Transcript
    tr = Transcript(
        video_id="test123",
        url="https://www.youtube.com/watch?v=test123",
        platform="youtube",
        title="Test Video",
        uploader="Test Channel",
        duration=120.0,
        language="en",
        source="ytdlp-subs",
        segments=[Segment(start=0, end=5, text="hello"), Segment(start=5, end=10, text="world")],
    )
    raw = tr.to_json()
    tr2 = Transcript.from_json(raw)
    assert tr2.video_id == "test123"
    assert tr2.title == "Test Video"
    assert len(tr2.segments) == 2
    assert tr2.segments[0].text == "hello"
