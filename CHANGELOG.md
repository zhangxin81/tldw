# Changelog

## [1.0.0] - 2026-09-06

### Added
- 三层 pipeline 架构：取素材层（yt-dlp 字幕 / youtube-transcript-api / 第三方 API / ASR）→ 转写层（faster-whisper）→ 总结层（map-reduce）
- 四个子命令：`digest`（端到端）、`asr`（只跑转写）、`summarize`（只跑总结）、`doctor`（环境自检）
- 多平台支持：YouTube、Bilibili、任意 yt-dlp 支持的站点（generic）
- VTT/SRT 解析：自动字幕滚动重复裁剪、`<c>` 内联标签清除、残留标点清理
- faster-whisper 转写：ffmpeg 归一化 16k 单声道、CPU int8 / CUDA float16 自动选择、中文简体归一（initial_prompt + opencc 兜底）
- map-reduce 总结：按字符数分块带重叠、OpenAI 兼容 API（纯标准库）、无 key 时导出结构化 request
- 带时间戳深链的 Markdown 输出（YouTube `&t=Ns`、Bilibili `?t=N`）
- 缓存键包含转写参数指纹（模型 + initial_prompt 的 sha1）
- 18 个单元测试覆盖 VTT 解析、分块、URL 解析、Transcript 序列化
