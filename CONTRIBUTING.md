# Contributing

## 开发环境

```bash
git clone https://github.com/your-org/tldw.git
cd tldw
pip install -e ".[dev]"
pip install -r requirements.txt
```

系统依赖：`ffmpeg` / `ffprobe`（用于音频归一化与时长探测）。

## 跑测试

```bash
pytest tests/ -v
```

测试覆盖纯逻辑层（VTT 解析、分块、URL 解析、Transcript 序列化），不依赖网络或模型。

## 代码结构

```
tldw/
├── __init__.py       # 导出公共 API
├── core.py           # 全部 pipeline 逻辑（单文件，分块清晰）
└── cli.py            # CLI 入口
tests/
└── test_parse_and_chunk.py
```

`core.py` 是单文件，按 `# ----` 注释分块：基础设施 → 数据结构 → URL/元信息 → 取素材层（3 个后端）→ 转写层 → 分段 → 总结层 → 渲染 → doctor → 子命令。

## 新增取素材后端

1. 写一个 `transcript_from_xxx(...) -> list[Segment]` 函数
2. 在 `build_transcript` 的 `attempts` 列表里按优先级插入
3. 失败抛 `PipelineError`，上层会自动 fallback

## 新增 LLM 后端

`call_llm` 是唯一的 LLM 调用点。换供应商只需改这个函数或设 `OPENAI_BASE_URL`。

## 提交规范

- 保持 `core.py` 单文件结构；超过 1000 行时再考虑拆分
- 新增功能补对应测试
- 不要引入不必要的依赖——pipeline 目前只硬依赖 `yt-dlp`
