# Seacraft ASR FastAPI Service

## 1. 项目说明

该项目提供一个 FastAPI 接口：

- `POST /v1.1.8/seacraft_asr`

接口使用 `form-data` 上传音频，采用两阶段识别：

- 阶段 1：`paraformer` 产出 `start/end/text`
- 阶段 2：按阶段 1 时间戳切分音频后，逐段用 `sensevoice` 提取 `emotion`

## 2. 目录结构

```text
.
├── app
│   ├── api
│   │   ├── deps.py
│   │   ├── router.py
│   │   └── routes
│   │       ├── asr.py
│   │       └── health.py
│   ├── core
│   │   ├── config.py
│   │   ├── errors.py
│   │   └── logging.py
│   ├── schemas
│   │   └── asr.py
│   ├── services
│   │   └── asr_service.py
│   └── main.py
├── config.toml
├── requirements.txt
└── README.md
```

## 3. 安装依赖

```bash
pip install -r requirements.txt
```

## 4. 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8081
```

或使用配置文件中的 host/port：

```bash
python run.py
```

如果需要指定配置文件路径：

```bash
APP_CONFIG_FILE=/path/to/config.toml uvicorn app.main:app --host 0.0.0.0 --port 8081
```

## 5. 接口定义

### 5.1 请求

- `audioFile` (`File`)：音频文件（必填）
- `showSpk` (`bool`)：说话人区分（参数保留，当前未实现）
- `showEmotion` (`bool`)：是否返回 `emotion` 字段
- `language` (`string`)：目标语言（可选，不传时默认 `auto`）

### 5.2 响应

```json
{
  "language": "auto",
  "segments": [
    {
      "segment_text": "如果与中文相比，",
      "bg": "0.17",
      "ed": "1.13",
      "speed": 137,
      "emotion": "<|NEUTRAL|>"
    }
  ],
  "text": "如果与中文相比，...",
  "load_audio_time_ms": "155.92",
  "gpu_time_ms": "879.39"
}
```

## 6. 关键行为

- 上传音频会先写入临时目录（默认 `./tmp_audio`）
- 识别流程：
  1) 先跑 paraformer，`text` 仅使用 paraformer 结果
  2) 再按 paraformer 时间戳切分音频
  3) 每个切片跑 sensevoice，只取 `emotion`
- 所有临时文件（上传音频、切片音频）在处理结束后都会清理
- 日志同时输出到控制台和文件（默认 `./logs/app.log`）
- 当 `showSpk=true` 时，仅记录日志告警，当前版本不做说话人区分
- 当前切分逻辑基于 `wav`（PCM）文件

## 7. 示例调用

```bash
curl -X POST "http://127.0.0.1:8081/v1.1.8/seacraft_asr" \
  -F "audioFile=@/root/workspaces/zhangs/jy-algorithm-app-asr-npu/教师1.wav" \
  -F "showSpk=false" \
  -F "showEmotion=true"
```

## 8. 健康检查

- `GET /healthz`

## 9. 配置说明

`config.toml` 中 ASR 必须包含两套命令配置：

- `[asr.paraformer]`：用于生成 `start/end/text`
- `[asr.sensevoice]`：用于切片情绪提取（`emotion`）

两套配置都支持 `working_dir`，命令会在该目录执行，因此模型路径可使用相对路径。
