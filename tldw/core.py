#!/usr/bin/env python3
"""
tldw — 自建 YouTube 视频理解 / 总结 pipeline

架构分三层，每层可独立替换与测试：

    取素材层  transcript_from_* : yt-dlp 字幕 -> youtube-transcript-api -> 第三方 API -> ASR
    转写层    asr_transcribe    : faster-whisper（本地，带时间戳）
    总结层    summarize         : 分段 map-reduce，输出带时间戳引用的 Markdown

设计要点
  - 字幕优先。有字幕就不下载音频，省钱省时间（几秒 vs 几分钟）。
  - 每一层都有明确的失败边界，失败原因往上抛，不静默降级到"编一个总结"。
  - 所有中间产物落盘并缓存，重跑不重复拉取。
  - 无 LLM key 时不报错，转而输出结构化的 summarize request，交给外部 agent 处理。

用法
    python3 tldw.py doctor
    python3 tldw.py digest "https://www.youtube.com/watch?v=VIDEO_ID"
    python3 tldw.py digest "<url>" --cookies cookies.txt --proxy http://host:port
    python3 tldw.py digest "<url>" --force-asr --whisper-model small
    python3 tldw.py asr ./local.flac            # 只测转写层
    python3 tldw.py summarize ./transcript.json  # 只测总结层
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------- 基础设施

CACHE_DIR = Path(os.environ.get("YDIGEST_CACHE", "./.tldw-cache"))
LOG_PREFIX = "[tldw]"


def log(msg: str, *, level: str = "info") -> None:
    mark = {"info": "·", "ok": "✓", "warn": "!", "err": "✗"}.get(level, "·")
    print(f"{LOG_PREFIX} {mark} {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], *, timeout: int = 600, check: bool = False) -> subprocess.CompletedProcess:
    """薄封装。故意不用 shell=True，参数直接传数组，避免 URL 里的 & 被吃掉。"""
    log(" ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise PipelineError(f"命令失败 ({proc.returncode}): {proc.stderr.strip()[-600:]}")
    return proc


class PipelineError(RuntimeError):
    """预期内的失败：缺依赖、拿不到字幕、被风控拦。带可读的原因。"""


# ---------------------------------------------------------------- 数据结构


@dataclass
class Segment:
    """转写的最小单位。start/end 单位为秒。"""

    start: float
    end: float
    text: str
    speaker: Optional[str] = None

    @property
    def hhmmss(self) -> str:
        s = int(self.start)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


@dataclass
class Transcript:
    video_id: str
    url: str
    platform: str = "youtube"
    title: str = ""
    uploader: str = ""
    duration: Optional[float] = None
    language: str = ""
    source: str = ""  # 素材来自哪一层，用于判断可信度
    segments: list[Segment] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return "\n".join(s.text for s in self.segments)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Transcript":
        d = json.loads(raw)
        segs = [Segment(**s) for s in d.pop("segments", [])]
        return cls(segments=segs, **d)


# ---------------------------------------------------------------- URL / 元信息


YOUTUBE_ID_RE = re.compile(
    r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})"
)
BILIBILI_ID_RE = re.compile(r"(BV[A-Za-z0-9]{10})")


@dataclass
class Source:
    """把平台差异收在这一个小结构里，上层逻辑不用再判断是哪家。"""

    platform: str
    vid: str
    url: str

    @property
    def key(self) -> str:
        """缓存与文件名用的唯一键。加平台前缀防止跳 id 撞车。"""
        return self.vid if self.platform == "youtube" else f"{self.platform}_{self.vid}"

    def ts_link(self, seconds: float) -> str:
        t = int(seconds)
        if self.platform == "youtube":
            return f"{self.url}&t={t}s"
        if self.platform == "bilibili":
            return f"{self.url}?t={t}"
        return self.url


def parse_source(url: str) -> Source:
    """识别平台并归一化为标准链接。yt-dlp 支持的其他站点走 generic 分支。"""
    m = YOUTUBE_ID_RE.search(url)
    if m:
        vid = m.group(1)
        return Source("youtube", vid, f"https://www.youtube.com/watch?v={vid}")
    m = BILIBILI_ID_RE.search(url)
    if m:
        vid = m.group(1)
        return Source("bilibili", vid, f"https://www.bilibili.com/video/{vid}")
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return Source("youtube", url, f"https://www.youtube.com/watch?v={url}")
    if url.startswith(("http://", "https://")):
        # 交给 yt-dlp 自己试，id 用 URL 指纹，保证缓存键稳定
        import hashlib
        return Source("generic", hashlib.sha1(url.encode()).hexdigest()[:11], url)
    raise PipelineError(f"无法识别的视频来源: {url}")


def ytdlp_base(cookies: Optional[str], proxy: Optional[str]) -> list[str]:
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--sleep-requests", "1"]
    if cookies:
        cmd += ["--cookies", cookies]
    if proxy:
        cmd += ["--proxy", proxy]
    return cmd


def fetch_metadata(url: str, cookies=None, proxy=None, retries: int = 2) -> dict:
    """
    单独取元信息。失败不致命 —— 标题拿不到也能继续总结，
    但标题是总结时很有用的上下文，而且各平台都会偶发风控，所以重试几次。
    """
    fmt = "%(id)s\t%(title)s\t%(uploader)s\t%(duration)s\t%(upload_date)s"
    last_err = ""
    for attempt in range(1, retries + 2):
        proc = run(ytdlp_base(cookies, proxy) + ["--skip-download", "--print", fmt, url], timeout=180)
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.strip().split("\t")
            meta = dict(zip(["id", "title", "uploader", "duration", "upload_date"], parts))
            try:
                meta["duration"] = float(meta.get("duration") or 0) or None
            except ValueError:
                meta["duration"] = None
            return meta
        last_err = proc.stderr.strip()[-200:]
        if attempt <= retries:
            log(f"元信息第 {attempt} 次失败，{attempt * 2}s 后重试", level="warn")
            time.sleep(attempt * 2)
    log(f"元信息获取失败，继续（不影响转写）: {last_err}", level="warn")
    return {}


# ------------------------------------------------------ 取素材层：后端 1 yt-dlp


VTT_TS = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)


def _vtt_seconds(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_vtt(text: str) -> list[Segment]:
    """
    解析 VTT/SRT。YouTube 自动字幕有个坑：为了做滚动效果，会把同一句话
    在连续 cue 里重复输出，并夹带 <c> 内联标签。这里做去重和去标签。
    """
    segments: list[Segment] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = VTT_TS.search(lines[i])
        if not m:
            i += 1
            continue
        start = _vtt_seconds(*m.groups()[:4])
        end = _vtt_seconds(*m.groups()[4:])
        i += 1
        buf: list[str] = []
        while i < len(lines) and lines[i].strip() and not VTT_TS.search(lines[i]):
            buf.append(lines[i])
            i += 1
        raw = " ".join(buf)
        raw = re.sub(r"<[^>]+>", "", raw)          # 去 <c>/<00:00:01.000> 标签
        raw = re.sub(r"\s+", " ", raw).strip()
        if not raw:
            continue
        # 自动字幕的滚动重复：新 cue 往往以上一条的尾部开头
        if segments and (raw == segments[-1].text or raw in segments[-1].text):
            segments[-1].end = end
            continue
        if segments and segments[-1].text and raw.startswith(segments[-1].text):
            raw = raw[len(segments[-1].text):].strip()
            # 裁掉重复前缀后常剩下孤立的标点，比如 "，成交量放大"
            raw = raw.lstrip("，,。.、；;！!？?　 ").strip()
            if not raw:
                segments[-1].end = end
                continue
        segments.append(Segment(start=start, end=end, text=raw))
    return segments


def transcript_from_ytdlp(
    src: Source, langs: str, cookies=None, proxy=None, workdir: Path = Path(".")
) -> list[Segment]:
    """后端 1：优先人工字幕，回退自动字幕。最快最省，几秒出结果。"""
    workdir.mkdir(parents=True, exist_ok=True)
    out_tpl = str(workdir / f"{src.key}.%(ext)s")
    cmd = ytdlp_base(cookies, proxy) + [
        "--skip-download",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", langs,
        "--sub-format", "vtt/srt/best",
        "-o", out_tpl,
        src.url,
    ]
    proc = run(cmd, timeout=300)
    files = sorted(workdir.glob(f"{src.key}*.vtt")) + sorted(workdir.glob(f"{src.key}*.srt"))
    if not files:
        raise PipelineError(
            "yt-dlp 未取到字幕。"
            + (f" stderr: {proc.stderr.strip()[-300:]}" if proc.stderr else "")
        )
    # 人工字幕文件名不带 .auto，优先它
    files.sort(key=lambda p: (".auto" in p.name, len(p.name)))
    picked = files[0]
    log(f"字幕文件: {picked.name}", level="ok")
    return parse_vtt(picked.read_text(encoding="utf-8", errors="ignore"))


# --------------------------------------------- 取素材层：后端 2 transcript-api


def transcript_from_api_lib(video_id: str, langs: list[str], proxy=None) -> list[Segment]:
    """
    后端 2：youtube-transcript-api。比 yt-dlp 轻，但用的是未公开接口，
    云机房 IP 大概率被封，生产环境要配住宅代理。
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
    except ImportError as e:
        raise PipelineError("未安装 youtube-transcript-api（pip install youtube-transcript-api）") from e

    kwargs = {}
    if proxy:
        try:
            from youtube_transcript_api.proxies import GenericProxyConfig  # type: ignore
            kwargs["proxy_config"] = GenericProxyConfig(http_url=proxy, https_url=proxy)
        except ImportError:
            log("当前版本不支持 proxy_config，忽略代理", level="warn")

    api = YouTubeTranscriptApi(**kwargs)
    fetched = api.fetch(video_id, languages=langs)
    return [
        Segment(start=float(x.start), end=float(x.start) + float(x.duration), text=x.text.strip())
        for x in fetched
        if x.text.strip()
    ]


# ------------------------------------------------- 取素材层：后端 3 第三方 API


def transcript_from_supadata(video_id: str) -> list[Segment]:
    """后端 3：托管服务兜底。稳定性外包，代价是钱和数据出境。"""
    key = os.environ.get("SUPADATA_API_KEY")
    if not key:
        raise PipelineError("未设置 SUPADATA_API_KEY")
    url = f"https://api.supadata.ai/v1/youtube/transcript?videoId={video_id}&text=false"
    req = urllib.request.Request(url, headers={"x-api-key": key})
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    out = []
    for c in data.get("content", []):
        start = float(c.get("offset", 0)) / 1000.0
        dur = float(c.get("duration", 0)) / 1000.0
        txt = (c.get("text") or "").strip()
        if txt:
            out.append(Segment(start=start, end=start + dur, text=txt))
    if not out:
        raise PipelineError("Supadata 返回空字幕")
    return out


# ---------------------------------------------------------------- 转写层 ASR


AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".webm", ".wav", ".aac", ".flac"}
NORMALIZED_SUFFIX = ".16k.wav"  # to_wav16k 的产物标记


def _is_intermediate(p: Path) -> bool:
    """区分原始下载与归一化产物。不分的后果是反复转码成 x.16k.16k.16k.wav。"""
    return p.name.endswith(NORMALIZED_SUFFIX)


def download_audio(src: Source, cookies=None, proxy=None, workdir: Path = Path(".")) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    existing = [
        p for p in sorted(workdir.glob(f"{src.key}.*"))
        if p.suffix.lower() in AUDIO_EXTS and p.stat().st_size > 0 and not _is_intermediate(p)
    ]
    if existing:
        log(f"复用已下载音频 {existing[0].name}", level="ok")
        return existing[0]
    cmd = ytdlp_base(cookies, proxy) + [
        "-f", "bestaudio/best",
        "-x", "--audio-format", "m4a", "--audio-quality", "5",
        "-o", str(workdir / f"{src.key}.%(ext)s"),
        src.url,
    ]
    run(cmd, timeout=1800, check=True)
    audio = [
        p for p in sorted(workdir.glob(f"{src.key}.*"))
        if p.suffix.lower() in AUDIO_EXTS and not _is_intermediate(p)
    ]
    if not audio:
        raise PipelineError("音频下载失败")
    return audio[0]


def to_wav16k(src: Path, workdir: Optional[Path] = None) -> Path:
    """
    统一转成 16kHz 单声道 PCM 再喂给 whisper。

    别跳过这一步。whisper 内部解码某些容器（实测 flac、部分 m4a/webm）会提前
    截断，表现是"11 秒音频只转写出 0.8 秒"，而且不报错，静默丢内容。交给
    ffmpeg 解码最稳，代价只有几秒。
    """
    if not src.exists():
        raise PipelineError(f"音频文件不存在: {src}")
    if _is_intermediate(src):  # 已经是归一化产物，直接用，不要再转一道
        return src
    workdir = workdir or src.parent
    workdir.mkdir(parents=True, exist_ok=True)
    dst = workdir / f"{src.stem}{NORMALIZED_SUFFIX}"
    if dst.exists() and dst.stat().st_size > 0:
        log(f"复用已归一化音频 {dst.name}", level="ok")
        return dst
    run(["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)], timeout=1800, check=True)
    src_dur, dst_dur = _probe_duration(src), _probe_duration(dst)
    log(f"归一化 {src.name} -> {dst.name}（{src_dur:.1f}s -> {dst_dur:.1f}s）", level="ok")
    if src_dur and dst_dur and abs(src_dur - dst_dur) > max(2.0, src_dur * 0.05):
        log("转换前后时长偏差偏大，原始文件可能损坏", level="warn")
    return dst


def _probe_duration(p: Path) -> float:
    proc = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "csv=p=0", str(p)], timeout=60)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


# whisper 对中文默认容易吐繁体（训练语料使然）。给一段简体 initial_prompt
# 可以把输出拉回简体，同时顺便把标点风格带对。这是比转换后处理更干净的做法。
ZH_INITIAL_PROMPT = "以下是普通话的句子，请用简体中文转写，并保留必要的标点符号。"


def asr_transcribe(
    audio: Path,
    model_size: str = "base",
    language: Optional[str] = None,
    vad: bool = True,
    initial_prompt: Optional[str] = None,
) -> tuple[list[Segment], str]:
    """
    本地转写。用 faster-whisper 而非原版 whisper：同精度快数倍且更省内存。
    国内环境要先 export HF_ENDPOINT=https://hf-mirror.com。

    中文注意两件事：base 模型同音错字很多（实测会把"叠伞"弄成"跌散"），
    正式跑建议 small 以上；且默认会吐繁体，靠 initial_prompt 拉回简体。
    """
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")  # xet CAS 端点常不可达，退回普通下载
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError as e:
        raise PipelineError("未安装 faster-whisper（pip install faster-whisper）") from e

    wav = to_wav16k(Path(audio))

    device, compute = ("cuda", "float16") if _has_cuda() else ("cpu", "int8")
    log(f"加载 whisper {model_size} on {device}/{compute}")
    model = WhisperModel(model_size, device=device, compute_type=compute)

    if initial_prompt is None and (language is None or language.startswith("zh")):
        initial_prompt = ZH_INITIAL_PROMPT

    t0 = time.time()
    segs_iter, info = model.transcribe(
        str(wav), language=language, vad_filter=vad, beam_size=5,
        initial_prompt=initial_prompt,
    )
    segments = [
        Segment(start=float(s.start), end=float(s.end), text=s.text.strip())
        for s in segs_iter
        if s.text.strip()
    ]
    segments = _to_simplified(segments)
    log(
        f"转写完成 {len(segments)} 段，语言={info.language}(p={info.language_probability:.2f})，"
        f"音频 {info.duration:.1f}s，耗时 {time.time() - t0:.1f}s",
        level="ok",
    )
    return segments, info.language


def _to_simplified(segments: list[Segment]) -> list[Segment]:
    """initial_prompt 压不住的漏网繁体，用 opencc 再过一道。没装就跳过。"""
    try:
        from opencc import OpenCC  # type: ignore
    except ImportError:
        return segments
    cc = OpenCC("t2s")
    for s in segments:
        s.text = cc.convert(s.text)
    return segments


def _has_cuda() -> bool:
    try:
        import torch  # type: ignore
        return bool(torch.cuda.is_available())
    except Exception:
        return shutil.which("nvidia-smi") is not None and \
            subprocess.run(["nvidia-smi"], capture_output=True).returncode == 0


# ------------------------------------------------------------ 取素材层：编排


def build_transcript(
    url: str,
    *,
    langs: str = "zh-Hans,zh-Hant,zh,en",
    cookies: Optional[str] = None,
    proxy: Optional[str] = None,
    force_asr: bool = False,
    whisper_model: str = "base",
    initial_prompt: Optional[str] = None,
    workdir: Path = Path("./.tldw-work"),
    use_cache: bool = True,
) -> Transcript:
    """按成本从低到高依次尝试后端，全失败则抛出汇总错误。"""
    src = parse_source(url)
    # 缓存键要包含影响转写结果的参数。否则换了模型或术语提示词重跑，
    # 新旧结果会互相覆盖，调参时完全看不出差异。
    fp = "subs" if not force_asr else f"asr-{whisper_model}"
    if force_asr and initial_prompt:
        import hashlib
        fp += "-" + hashlib.sha1(initial_prompt.encode()).hexdigest()[:8]
    cache = CACHE_DIR / f"{src.key}.{fp}.transcript.json"
    if use_cache and cache.exists():
        log(f"命中缓存 {cache}", level="ok")
        return Transcript.from_json(cache.read_text(encoding="utf-8"))

    meta = fetch_metadata(src.url, cookies, proxy)
    tr = Transcript(
        video_id=src.key,
        url=src.url,
        platform=src.platform,
        title=meta.get("title", ""),
        uploader=meta.get("uploader", ""),
        duration=meta.get("duration"),
    )

    attempts: list[tuple[str, object]] = []
    if not force_asr:
        attempts += [
            ("ytdlp-subs", lambda: transcript_from_ytdlp(src, langs, cookies, proxy, workdir)),
        ]
        if src.platform == "youtube":  # 这两个后端只懂 YouTube
            attempts += [
                ("transcript-api", lambda: transcript_from_api_lib(src.vid, langs.split(","), proxy)),
                ("supadata", lambda: transcript_from_supadata(src.vid)),
            ]
    attempts.append(("asr", lambda: _asr_route(src, cookies, proxy, whisper_model, workdir, tr,
                                               initial_prompt)))

    errors: list[str] = []
    for name, fn in attempts:
        try:
            log(f"尝试后端: {name}")
            segs = fn()
            if segs:
                tr.segments = segs
                tr.source = name
                tr.language = tr.language or ("zh" if _looks_chinese(tr.full_text) else "en")
                log(f"后端 {name} 成功，{len(segs)} 段", level="ok")
                break
        except Exception as e:  # 每个后端失败都记账，继续下一个
            log(f"后端 {name} 失败: {e}", level="warn")
            errors.append(f"{name}: {e}")

    if not tr.segments:
        raise PipelineError(
            "所有取素材后端均失败。常见原因：数据中心 IP 被风控（需住宅代理或 "
            "--cookies），或该视频无字幕且音频下载被拦。明细：\n  - " + "\n  - ".join(errors)
        )

    # 字幕路线的缓存键用 subs，ASR 路线带上模型名，两者不互相覆盖
    if tr.source == "asr" and not force_asr:
        cache = cache.with_name(cache.name.replace(".subs.", f".asr-{whisper_model}."))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(tr.to_json(), encoding="utf-8")
    log(f"转写已缓存 {cache}", level="ok")
    return tr


def _asr_route(src, cookies, proxy, whisper_model, workdir, tr, initial_prompt=None) -> list[Segment]:
    audio = download_audio(src, cookies, proxy, workdir)
    segs, lang = asr_transcribe(audio, whisper_model, initial_prompt=initial_prompt)
    tr.language = lang
    return segs


def _looks_chinese(text: str) -> bool:
    han = len(re.findall(r"[\u4e00-\u9fff]", text[:2000]))
    return han > len(text[:2000]) * 0.1


# ---------------------------------------------------------------- 分段


def chunk_segments(
    segments: Iterable[Segment], *, max_chars: int = 6000, overlap_chars: int = 400
) -> list[dict]:
    """
    按字符数切块并保留重叠。用字符不用 token：跨模型稳定，且中文下 token
    换算差异大，字符更好预估。重叠是为了让跨块的论证不被切断。
    """
    chunks: list[dict] = []
    cur: list[Segment] = []
    cur_len = 0
    for seg in segments:
        cur.append(seg)
        cur_len += len(seg.text) + 1
        if cur_len >= max_chars:
            chunks.append(_pack(cur))
            keep, kept_len = [], 0
            for s in reversed(cur):
                if kept_len >= overlap_chars:
                    break
                keep.insert(0, s)
                kept_len += len(s.text)
            cur, cur_len = keep, kept_len
    if cur and (not chunks or cur_len > overlap_chars * 1.2):
        chunks.append(_pack(cur))
    return chunks


def _pack(segs: list[Segment]) -> dict:
    return {
        "start": segs[0].start,
        "end": segs[-1].end,
        "text": "\n".join(f"[{s.hhmmss}] {s.text}" for s in segs),
    }


# ---------------------------------------------------------------- 总结层


MAP_PROMPT = """你在处理一个长视频转写稿的第 {idx}/{total} 段（{start}–{end}）。
只依据下面的原文，提取这一段真正讲了什么。

要求：
1. 列出 3-6 条要点，每条前面标注该要点出现的时间戳（用原文里的 [mm:ss]）。
2. 保留具体的数字、公司名、代码、结论；不要写"讲述了相关内容"这类空话。
3. 原文没有的信息一律不要补。字幕本身可能有错字，遇到明显同音错误可标注推测。

原文：
{text}
"""

REDUCE_PROMPT = """下面是同一个视频各段的要点汇总（按时间顺序）。请合成一份最终总结。

视频标题：{title}
频道：{uploader}
时长：{duration}

输出结构：
## 一句话结论
## 核心内容（3-6 条，每条带时间戳）
## 关键数据与事实
## 值得跳看的时间点（时间戳 + 一句话说明）
## 存疑或未覆盖的地方（字幕/转写不确定处，没有就写"无"）

要求：合并重复项，保留数字与专有名词，时间戳必须来自输入，不要编造。

各段要点：
{parts}
"""


def call_llm(prompt: str, *, model: Optional[str] = None, max_tokens: int = 2000) -> str:
    """
    OpenAI 兼容接口。只用标准库，避免为一个 HTTP POST 引入 sdk 依赖。
    没有 key 时抛错，由上层决定是导出 request 还是终止。
    """
    key = os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    if not key:
        raise PipelineError("未设置 OPENAI_API_KEY")
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


def summarize(tr: Transcript, *, outdir: Path, model: Optional[str] = None) -> dict:
    """
    map-reduce。逐块出要点，再合成全局总结。
    无 LLM key 时把 prompt 全部导出成 request 包，交给外部 agent 跑。
    """
    chunks = chunk_segments(tr.segments)
    log(f"切分为 {len(chunks)} 块（共 {len(tr.full_text)} 字）")
    dur = f"{int((tr.duration or 0) // 60)} 分钟" if tr.duration else "未知"

    map_prompts = [
        MAP_PROMPT.format(
            idx=i + 1, total=len(chunks),
            start=Segment(c["start"], c["start"], "").hhmmss,
            end=Segment(c["end"], c["end"], "").hhmmss,
            text=c["text"],
        )
        for i, c in enumerate(chunks)
    ]

    try:
        call_llm("ping", max_tokens=5)
        has_llm = True
    except PipelineError:
        has_llm = False
    except Exception as e:
        log(f"LLM 连通性检查异常，按无 key 处理: {e}", level="warn")
        has_llm = False

    if not has_llm:
        req = {
            "task": "video_summary_map_reduce",
            "video": {"id": tr.video_id, "title": tr.title, "uploader": tr.uploader,
                      "duration": tr.duration, "url": tr.url, "source": tr.source},
            "map_prompts": map_prompts,
            "reduce_prompt_template": REDUCE_PROMPT,
        }
        p = outdir / f"{tr.video_id}.summarize_request.json"
        p.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"无 LLM key，已导出 request 供 agent 处理: {p}", level="warn")
        return {"status": "request_exported", "path": str(p), "chunks": len(chunks)}

    parts = []
    for i, prompt in enumerate(map_prompts, 1):
        log(f"map {i}/{len(map_prompts)}")
        parts.append(call_llm(prompt, model=model))
    final = call_llm(
        REDUCE_PROMPT.format(
            title=tr.title or tr.video_id, uploader=tr.uploader or "未知",
            duration=dur, parts="\n\n---\n\n".join(parts),
        ),
        model=model, max_tokens=3000,
    )
    return {"status": "ok", "map_parts": parts, "summary": final, "chunks": len(chunks)}


# ---------------------------------------------------------------- 渲染


def render_markdown(tr: Transcript, result: dict) -> str:
    dur = f"{int((tr.duration or 0) // 60)}:{int((tr.duration or 0) % 60):02d}" if tr.duration else "未知"
    src = Source(tr.platform, tr.video_id.split("_")[-1], tr.url)
    head = [
        f"# {tr.title or tr.video_id}",
        "",
        f"- 频道：{tr.uploader or '未知'}",
        f"- 时长：{dur}",
        f"- 链接：{tr.url}",
        f"- 转写来源：`{tr.source}`（语言 {tr.language or '未知'}，{len(tr.segments)} 段）",
        "",
    ]
    if result.get("status") == "ok":
        head += [result["summary"], ""]
    else:
        head += [f"> 总结请求已导出至 `{result.get('path')}`，待 LLM 处理。", ""]
    head += ["## 时间轴索引", ""]
    step = max(1, len(tr.segments) // 40)
    # tr.url 可能是本地文件路径（asr 子命令未传 --url 时），这种情况下生成深链没有意义，
    # 退化成纯时间戳，不要拼出一个点不开的假链接。
    linkable = tr.url.startswith(("http://", "https://"))
    for s in tr.segments[::step]:
        stamp = f"[{s.hhmmss}]({src.ts_link(s.start)})" if linkable else f"`{s.hhmmss}`"
        head.append(f"- {stamp} {s.text[:90]}")
    return "\n".join(head)


# ---------------------------------------------------------------- doctor


def cmd_doctor(args) -> int:
    print("== 依赖 ==")
    ok = True
    for b in ["yt-dlp", "ffmpeg", "ffprobe"]:
        p = shutil.which(b)
        print(f"  {b:<10} {'OK ' + p if p else 'MISSING'}")
        ok &= bool(p)
    for mod in ["faster_whisper", "youtube_transcript_api"]:
        try:
            __import__(mod)
            print(f"  {mod:<24} OK")
        except ImportError:
            print(f"  {mod:<24} MISSING (可选)")
    print("== 环境变量 ==")
    for k in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
              "SUPADATA_API_KEY", "HTTPS_PROXY", "HF_ENDPOINT"]:
        v = os.environ.get(k)
        shown = "***" if k.endswith("KEY") and v else (v or "-")
        print(f"  {k:<18} {shown}")
    print("== 出网 ==")
    proxy = args.proxy or os.environ.get("HTTPS_PROXY")
    for name, url in [("youtube", "https://www.youtube.com/"),
                      ("hf-mirror", "https://hf-mirror.com/")]:
        print(f"  {name:<10} {_probe(url, proxy)}")
    print(f"  proxy      {proxy or '未配置'}")
    return 0 if ok else 1


def _probe(url: str, proxy: Optional[str]) -> str:
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy
            else urllib.request.ProxyHandler({})
        )
        with opener.open(url, timeout=12) as r:
            return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return f"FAIL ({type(e).__name__})"


# ---------------------------------------------------------------- 子命令


def cmd_digest(args) -> int:
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    tr = build_transcript(
        args.url, langs=args.langs, cookies=args.cookies, proxy=args.proxy,
        force_asr=args.force_asr, whisper_model=args.whisper_model,
        initial_prompt=args.initial_prompt,
        workdir=Path(args.workdir), use_cache=not args.no_cache,
    )
    (outdir / f"{tr.video_id}.transcript.json").write_text(tr.to_json(), encoding="utf-8")
    result = summarize(tr, outdir=outdir, model=args.model)
    md = render_markdown(tr, result)
    p = outdir / f"{tr.video_id}.summary.md"
    p.write_text(md, encoding="utf-8")
    log(f"完成 -> {p}", level="ok")
    print(md[:1500])
    return 0


def cmd_asr(args) -> int:
    """只测转写层，输入本地音视频文件。

    可选 --url：音频是从哪条视频抽出来的。给了才能在总结里生成可点的时间戳深链，
    否则时间戳只是文本。
    """
    segs, lang = asr_transcribe(Path(args.audio), args.whisper_model, args.language,
                                initial_prompt=args.initial_prompt)
    if args.url:
        src = parse_source(args.url)
        vid, url, platform = src.key, src.url, src.platform
    else:
        vid, url, platform = Path(args.audio).stem, str(args.audio), "local"
    tr = Transcript(video_id=vid, url=url, platform=platform,
                    title=Path(args.audio).name, language=lang, source="asr-local",
                    segments=segs)
    # 给了 --url 就顺手取一次元信息。标题和频道名是总结时的重要上下文，
    # 少了它们，reduce 阶段容易把内容理解偏。取不到也不影响主流程。
    if args.url:
        meta = fetch_metadata(args.url, proxy=args.proxy)
        tr.title = meta.get("title") or tr.title
        tr.uploader = meta.get("uploader") or tr.uploader
        tr.duration = meta.get("duration") or tr.duration
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    p = out / f"{tr.video_id}.transcript.json"
    p.write_text(tr.to_json(), encoding="utf-8")
    for s in segs[:20]:
        print(f"[{s.hhmmss}] {s.text}")
    log(f"共 {len(segs)} 段 -> {p}", level="ok")
    return 0


def cmd_summarize(args) -> int:
    """只测总结层，输入已有 transcript.json。

    --url 用于给一份来自本地文件的 transcript 补上源链接，补完时间轴就能点了。
    """
    tr = Transcript.from_json(Path(args.transcript).read_text(encoding="utf-8"))
    if args.url:
        src = parse_source(args.url)
        tr.url, tr.platform, tr.video_id = src.url, src.platform, src.key
    out = Path(args.outdir); out.mkdir(parents=True, exist_ok=True)
    result = summarize(tr, outdir=out, model=args.model)
    md = render_markdown(tr, result)
    (out / f"{tr.video_id}.summary.md").write_text(md, encoding="utf-8")
    print(md[:1200])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="tldw", description="自建 YouTube 视频总结 pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = dict(outdir="./output", workdir="./.tldw-work")

    d = sub.add_parser("digest", help="端到端：取素材 -> 转写 -> 总结")
    d.add_argument("url")
    d.add_argument("--langs", default="zh-Hans,zh-Hant,zh,en")
    d.add_argument("--cookies")
    d.add_argument("--proxy")
    d.add_argument("--force-asr", action="store_true")
    d.add_argument("--whisper-model", default="base")
    d.add_argument("--initial-prompt",
                   help="喂给 whisper 的引导词。把人名、产品名、术语写进去能明显减少同音错字")
    d.add_argument("--model", help="LLM 模型名")
    d.add_argument("--outdir", default=common["outdir"])
    d.add_argument("--workdir", default=common["workdir"])
    d.add_argument("--no-cache", action="store_true")
    d.set_defaults(func=cmd_digest)

    a = sub.add_parser("asr", help="只跑转写层（本地文件）")
    a.add_argument("audio")
    a.add_argument("--url", help="音频对应的源视频 URL，用于生成可点的时间戳深链并补全元信息")
    a.add_argument("--proxy", help="取元信息用的代理")
    a.add_argument("--whisper-model", default="base")
    a.add_argument("--language")
    a.add_argument("--initial-prompt")
    a.add_argument("--outdir", default=common["outdir"])
    a.set_defaults(func=cmd_asr)

    s = sub.add_parser("summarize", help="只跑总结层")
    s.add_argument("transcript")
    s.add_argument("--url", help="补上源视频 URL，让时间轴可点")
    s.add_argument("--model")
    s.add_argument("--outdir", default=common["outdir"])
    s.set_defaults(func=cmd_summarize)

    doc = sub.add_parser("doctor", help="环境自检")
    doc.add_argument("--proxy")
    doc.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    try:
        return args.func(args)
    except PipelineError as e:
        log(str(e), level="err")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
