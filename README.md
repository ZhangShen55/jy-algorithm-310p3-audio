# Seacraft ASR FastAPI Service

基于 FastAPI 的音频转写后端服务，面向昇腾 **Ascend 310P3 NPU**。服务本身不加载任何深度学习模型，而是以子进程形式调度三个外部 CLI 引擎，专注于请求编排、并发控制、结果合并与接口化。

## 1. 涉及模型

| 模型 | 载体 | 用途 |
|---|---|---|
| **Paraformer**（中文 ASR） | `sherpa-onnx-vad-with-offline-asr` + encoder/predictor/decoder 三个 `.om` | 纯识别 + 时间戳 |
| **SenseVoice**（多语 ASR，zh/en/ja/ko/yue） | `sherpa-onnx-vad-with-offline-asr` + 单个 `.om` | 识别 + 情绪标签 |
| **CT-Transformer**（标点恢复） | `sherpa-onnx-offline-punctuation` + `model.int8.onnx` | 对无标点文本做标点恢复 |

VAD 使用 `silero_vad.onnx`，被 Paraformer / SenseVoice 两条命令分别带参数调用。

## 2. 目录结构

```text
.
├── app
│   ├── api
│   │   ├── deps.py
│   │   ├── router.py
│   │   └── routes
│   │       ├── asr.py          # 主接口 & /get_status
│   │       └── health.py       # /healthz
│   ├── core
│   │   ├── config.py           # Pydantic + tomllib 配置模型
│   │   ├── errors.py           # ASRProcessingError / ConcurrencyLimitError
│   │   └── logging.py          # 控制台 + 文件滚动日志
│   ├── schemas
│   │   └── asr.py              # Segment / AsrResponse
│   ├── services
│   │   └── asr_service.py      # 核心编排、并发控制、CLI 调度
│   └── main.py                 # FastAPI app、lifespan、中间件
├── run.py
├── config.toml
├── requirements.txt
└── README.md
```

## 3. 安装依赖

```bash
pip install -r requirements.txt
```

服务端运行环境不需要 PyTorch / ONNX Runtime —— 模型执行完全由 `sherpa-onnx-*` CLI 完成，Python 侧只调度和解析 JSON。

## 4. 启动服务

```bash
python run.py
```

或直接用 uvicorn：

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8081
```

指定自定义配置文件：

```bash
APP_CONFIG_FILE=/path/to/config.toml python run.py
```

## 5. 处理流程

当前版本是 **"二选一 ASR + 可选标点恢复"** 的流水线，不再是旧文档中的两阶段级联：

1. 接收 `multipart/form-data` 上传，先过应用级并发控制（信号量 + 等待队列）。
2. 落盘到 `tmp_audio/<uuid>.<ext>`，用 `wave` 校验必须是 **PCM WAV** 且时长 ≥ 1.0 秒。
3. 根据 `showEmotion` 选择一种 ASR：
   - `showEmotion=true` → 跑 **SenseVoice**，一次性拿到 `text + start + end + emotion`；
   - `showEmotion=false` → 跑 **Paraformer**，只拿 `text + start + end`，所有 segment 的 `emotion` 为 `null`。
4. 计算每段语速：`speed = round(len(text) * 60 / duration_sec * 0.6)`（字/分钟，`0.6` 为经验校准系数，用于贴近真实感知语速）。
5. 若 `openPunc=true`（默认），把所有 segment 文本拼接后丢给 **CT-Transformer** 标点模型，再用字符对齐算法把标点回填到各个 segment；标点 CLI 失败时自动回退为无标点文本，不影响整体结果。
6. 情绪标签映射（见第 8 节）。
7. 删除临时文件，释放信号量，返回响应。

## 6. 接口定义

### 6.1 `POST /v1.1.8/seacraft_asr`

请求（`form-data`）：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `audioFile` | File | 必填 | PCM WAV，时长 ≥ 1 秒 |
| `showSpk` | bool | `false` | 说话人分离开关（**保留字段，未实现**，仅记录告警） |
| `showEmotion` | bool | `true` | `true` 走 SenseVoice，`false` 走 Paraformer |
| `language` | string | 不传 | 仅对 SenseVoice 生效，透传为 `--sense-voice-language=…`，如 `zh`/`en`/`auto` |
| `openPunc` | bool | `true` | 是否对文本做 CT-Transformer 标点恢复 |

响应（`AsrResponse`，Pydantic `extra="forbid"`）：

```json
{
  "language": "auto",
  "segments": [
    {
      "segment_text": "如果与中文相比，",
      "bg": "0.17",
      "ed": "1.13",
      "speed": 137,
      "role": null,
      "emotion": "平淡"
    }
  ],
  "text": "如果与中文相比，...",
  "load_audio_time_ms": "155.92",
  "gpu_time_ms": "879.39"
}
```

字段说明：

- `bg` / `ed`：开始/结束秒数，保留两位小数（字符串）
- `speed`：字/分钟（= `round(len(text) * 60 / duration_sec * 0.6)`，含 `0.6` 校准系数）
- `role`：恒为 `null`，为后续说话人分离预留
- `emotion`：教学场景中文标签或 `null`
- `text`：所有 segment 的拼接文本，开启 `openPunc` 时为带标点版本

错误码：

- `HTTP 400`：`audioFile` 为空或文件名缺失
- `HTTP 429`：并发槽和等待队列都已满（`ConcurrencyLimitError`）
- `HTTP 500`：`ASRProcessingError`（CLI 失败、超时、音频格式/时长不合法等）或任何未捕获异常

### 6.2 `GET /get_status`

返回运行时状态：成功/失败数、在处理/排队任务、信号量剩余槽位等，用于健康观察和压测。

### 6.3 `GET /healthz`

返回 `{"status": "ok"}`。

## 7. 示例调用

```bash
curl -X POST "http://127.0.0.1:8081/v1.1.8/seacraft_asr" \
  -F "audioFile=@./音频测试.wav" \
  -F "showSpk=false" \
  -F "showEmotion=true" \
  -F "openPunc=true"
```

## 8. 情绪标签映射

SenseVoice 原始输出为英文标签，服务内做了一次教学语义重写：

| SenseVoice 原始 | 对外 `emotion` |
|---|---|
| `HAPPY` | `积极` |
| `SAD` | `平淡` |
| `ANGRY` | `强调` |
| `NEUTRAL` | `平淡` |
| `FEARFUL` | `思考` |
| `DISGUSTED` | `疑问` |
| `SURPRISED` | `兴奋` |

未覆盖的标签被置为 `null`。

## 9. 并发与队列

`ASRService` 维护应用级两层闸门：

- `asyncio.Semaphore(max_concurrent_requests)`：最大并发处理数（配置默认 20）
- `asyncio.Queue(max_queue_size)`：等待队列（配置默认 10）
- 两层都满 → `HTTP 429`，响应体为 `{"detail": "Server is busy and queue is full. Please try again later."}`

每个请求带一个 UUID `task_id`，生命周期状态 `queued → processing → completed / failed` 通过 `/get_status` 对外可见。

## 10. 配置说明（`config.toml`）

关键配置块：

- `[server]`：`host` / `port` / `reload` / `workers`
- `[storage]`：`tmp_dir`（上传落盘目录）、`chunk_size`（流式写盘块大小）
- `[logging]`：等级、日志目录、滚动大小、保留份数
- `[asr]`：`max_concurrent_requests`、`max_queue_size`
- `[asr.paraformer]`：`executable`、`working_dir`、`provider`（如 `"ascend"`）、`num_threads`（透传为 `--num-threads=…`，默认 2）、VAD 参数、`paraformer`（逗号分隔的 encoder/predictor/decoder `.om`）、`tokens`、`command_timeout_seconds`
- `[asr.sensevoice]`：同上运行参数（`provider`、`num_threads`、VAD），加 `sense_voice_model`（单个 `.om`）、`tokens`
- `[punctuation_cli]`：`executable`、`working_dir`、`ct_transformer`（int8 ONNX 模型路径）

`working_dir` 让 CLI 子进程在指定目录下启动，因此 `.om` / `.onnx` 路径可以写相对路径。

## 11. 日志与追踪

- 控制台 + `./logs/app.log` 双写，文件按 `max_bytes` / `backup_count` 滚动。
- 中间件给每个请求生成 UUID 并写回响应头 `X-Request-ID`，同一个 ID 会出现在全部相关日志行中，便于串联排查。
- 关键节点（落盘、ASR 调用、标点、任务入队/出队、成功/失败计数）都有结构化日志。
