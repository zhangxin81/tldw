# TLDW — YouTube 视频总结 / AI 视频转写工具

> **TLDW** = **T**oo **L**ong **D**idn't **W**atch。视频太长不想看？给它一条链接，还你一份带时间戳的结构化总结。

自建视频理解 pipeline：输入 YouTube / Bilibili 链接，输出带可点时间戳深链的 Markdown 总结。三层架构全部本地运行，不把你的视频内容传到第三方服务器。

## 为什么不用现成工具

| 需求 | ChatGPT / Gemini | NotebookLM | 浏览器插件 | **TLDW** |
| --- | --- | --- | --- | --- |
| YouTube 视频总结 | ✅ 但内容传到第三方 | ✅ 但要手动上传 | ✅ 但功能受限 | ✅ **全本地** |
| Bilibili 支持 | ❌ | ❌ | ❌ | ✅ |
| 中文 ASR（语音转文字） | 取决于平台 | 不支持 | 不支持 | ✅ faster-whisper |
| 可点时间戳深链 | ❌ | ❌ | 部分 | ✅ |
| 术语引导（减少同音错字） | ❌ | ❌ | ❌ | ✅ `--initial-prompt` |
| 无 API key 也能用 | ❌ | ❌ | ✅ | ✅ 导出 request 交任意模型 |
| 开源自部署 | ❌ | ❌ | ❌ | ✅ MIT |

## 它能做什么

- **YouTube 视频总结** — 粘贴链接，自动拉字幕或转写音频，生成结构化总结
- **Bilibili 视频总结** — 同样支持，时间戳深链直接跳转 B 站
- **任意视频转文字** — 只要 yt-dlp 能下载，就能 ASR 转写
- **播客 / 录音转写** — 本地音频文件直接喂 `asr` 子命令
- **AI 视频摘要** — map-reduce 分块总结，超长视频也能处理
- **视频内容提取** — 不只给摘要，保留具体数字、公司名、代码和结论

## 快速开始

```bash
# 1. 安装
pip install -e ".[all]"

# 2. 系统依赖
# macOS:  brew install ffmpeg
# Ubuntu: sudo apt install ffmpeg

# 3. 环境自检
python -m tldw.cli doctor

# 4. 跑一条 YouTube 视频
python -m tldw.cli digest "https://www.youtube.com/watch?v=VIDEO_ID"

# 5. 中文视频建议用 small 模型 + 术语引导词
python -m tldw.cli digest "https://www.youtube.com/watch?v=VIDEO_ID" \
  --whisper-model small \
  --initial-prompt "以下是普通话的句子，请用简体中文转写。内容涉及半导体、甲骨文、SOXX。"
```

## 架构

```
取素材层  yt-dlp 字幕 → youtube-transcript-api → 第三方 API → 下载音频
                                                      ↓
转写层    ffmpeg 归一化 16k → faster-whisper → 简体归一
                                                      ↓
总结层    按字符分块(带重叠) → map → reduce → Markdown(带时间戳深链)
```

字幕能拿到就不跑 ASR — 几秒 vs 几分钟。ASR 是兜底，不是默认路径。

## 子命令

| 命令 | 用途 | 典型场景 |
| --- | --- | --- |
| `digest` | 端到端：取素材 → 转写 → 总结 | 给个链接出总结 |
| `asr` | 只跑转写层，接本地音视频文件 | 播客转文字、录音转文字 |
| `summarize` | 只跑总结层，接已有 transcript.json | 改 prompt 不重跑 ASR |
| `doctor` | 环境自检：依赖、出网、代理 | 部署前排查 |

## 实测数据

CPU + int8，模型已缓存：

| 素材 | 模型 | 耗时 | 结果 |
| --- | --- | --- | --- |
| 11s 英文 | base | 1.2s | 全句准确 |
| 143s 中文 | base | 11.4s / 13 段 | 同音错字多 |
| 143s 中文 | small | 12.1s / 19 段 | 专名全部正确 |
| 1323s 中文财经（22 分钟） | small | 649 段 | 数字准确，中文专名有错字 |

**结论：中文直接上 small。** `--initial-prompt` 把领域术语写进去，同音错字率显著下降。

## 环境变量

| 变量 | 必需 | 作用 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 否 | 总结层 LLM key。不配就导出 summarize request |
| `OPENAI_BASE_URL` | 否 | 换供应商（DeepSeek / 智谱 / Moonshot） |
| `OPENAI_MODEL` | 否 | 默认 `gpt-4o-mini` |
| `SUPADATA_API_KEY` | 否 | 第三方 transcript API |
| `HF_ENDPOINT` | 否 | HuggingFace 镜像，代码内已设默认 |
| `HF_HUB_DISABLE_XET` | 否 | 禁用 xet CAS，代码内已设默认 |

无 `OPENAI_API_KEY` 不报错。总结层会把 map-reduce 的 prompt 全部导出成 `*.summarize_request.json`，拿去喂任意模型即可。

## 支持平台

| 平台 | 字幕提取 | ASR 兜底 | 时间戳深链 |
| --- | --- | --- | --- |
| YouTube | ✅ | ✅ | `&t=Ns` |
| Bilibili | ✅ | ✅ | `?t=N` |
| 其他 yt-dlp 支持的 | ❌ | ✅ | 无 |

## 常见问题

<details>
<summary>YouTube 报 <code>Video unavailable</code></summary>

数据中心 IP 被风控。先升级 yt-dlp 重试，仍不行就导出浏览器 cookies 或配住宅代理：

```bash
python -m tldw.cli digest "<url>" --cookies cookies.txt --whisper-model small
# 或
python -m tldw.cli digest "<url>" --proxy http://user:pass@residential:port
```
</details>

<details>
<summary>转写结果莫名短了一截</summary>

pipeline 里的 ffmpeg 归一化步骤就是为这个存在的 — whisper 直接解某些容器会静默截断且不报错。归一化会比对转换前后时长，偏差超 5% 打警告。
</details>

<details>
<summary>中文转写同音错字多</summary>

两件事：模型用 `small` 不要用 `base`；把领域术语写进 `--initial-prompt`。实测把「折叠雨伞、伞骨、收伞」写进引导词后，全篇误听的 `散` 全部修正为 `伞`。
</details>

<details>
<summary>没有 LLM key 怎么用</summary>

不配 `OPENAI_API_KEY` 不报错。总结层导出 `*.summarize_request.json`，包含分好块的 map prompt 和 reduce 模板，拿去喂任意模型（DeepSeek、智谱 GLM、Kimi 都行）。
</details>

## 已知限制

- YouTube 取素材路径依赖 IP 质量，机房环境不稳定，报错先重试
- ASR 会出同音错字：数字基本可信，中文专名不可信，涉及决策请回看原片
- 抓取字幕/音频请自行确认符合平台服务条款和当地法规
- 超长视频（>2h）建议先按 chapter 切再总结，效果比无脑分块好

## 开发

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

18 个单元测试覆盖 VTT 解析、分块逻辑、URL 解析、Transcript 序列化，不依赖网络或模型，1.2s 跑完。

贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

MIT
