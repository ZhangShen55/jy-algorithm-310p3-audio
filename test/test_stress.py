#!/usr/bin/env python3
"""压测脚本 - 持续24小时，20并发"""
import asyncio
import aiohttp
import time
import signal
import sys
from pathlib import Path
from datetime import datetime, timedelta

# ── 配置 ──
CONCURRENCY = 25          # 并发数
DURATION_HOURS = 24       # 持续时间（小时）
AUDIO_FILE = Path("教师1.wav")
URL = "http://localhost:8081/v1.1.8/seacraft_asr"
REQUEST_TIMEOUT = 600     # 单请求超时（秒）

# ── 统计 ──
stats = {
    "total": 0,
    "success": 0,
    "rejected": 0,
    "error": 0,
    "elapsed_sum": 0.0,
}
stop_flag = False


def handle_signal(sig, frame):
    global stop_flag
    print(f"\n[{datetime.now():%H:%M:%S}] 收到停止信号，等待当前请求完成后退出...")
    stop_flag = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


async def send_request(session: aiohttp.ClientSession, rid: int, audio_file: Path) -> dict:
    start = time.time()
    try:
        with open(audio_file, "rb") as f:
            data = aiohttp.FormData()
            data.add_field("audioFile", f, filename=audio_file.name)
            data.add_field("showEmotion", "false")
            async with session.post(URL, data=data) as resp:
                elapsed = time.time() - start
                return {"id": rid, "status": resp.status, "elapsed": round(elapsed, 2)}
    except Exception as e:
        elapsed = time.time() - start
        return {"id": rid, "status": "error", "elapsed": round(elapsed, 2), "error": str(e)}


async def check_status(session: aiohttp.ClientSession) -> dict | None:
    try:
        async with session.get("http://localhost:8081/get_status") as resp:
            return await resp.json()
    except Exception:
        return None


async def worker(worker_id: int, session: aiohttp.ClientSession, audio_file: Path):
    """每个worker循环发请求，直到stop_flag或超时"""
    global stats
    while not stop_flag:
        rid = stats["total"] + 1
        result = await send_request(session, rid, audio_file)

        stats["total"] += 1
        status = result["status"]
        if status == 200:
            stats["success"] += 1
            stats["elapsed_sum"] += result["elapsed"]
        elif status == 429:
            stats["rejected"] += 1
        else:
            stats["error"] += 1

        ts = datetime.now().strftime("%H:%M:%S")
        if status == 200:
            print(f"[{ts}] W{worker_id} #{rid} 200 {result['elapsed']}s")
        elif status == 429:
            print(f"[{ts}] W{worker_id} #{rid} 429 REJECTED")
        elif status == "error":
            print(f"[{ts}] W{worker_id} #{rid} ERROR {result.get('error', '')[:80]}")
        else:
            print(f"[{ts}] W{worker_id} #{rid} {status}")

        # 429时稍等再重试
        if status == 429:
            await asyncio.sleep(2)


async def print_status_loop(session: aiohttp.ClientSession, end_time: float):
    """每30秒打印一次汇总状态"""
    while not stop_flag and time.time() < end_time:
        await asyncio.sleep(30)
        if stop_flag:
            break
        remaining = max(0, end_time - time.time())
        s = stats
        avg = s["elapsed_sum"] / s["success"] if s["success"] > 0 else 0
        status = await check_status(session)
        queue_info = ""
        if status:
            queue_info = f" | processing={status['processing_count']} queued={status['queued_count']}"

        print(f"\n{'─' * 60}")
        print(f"[{datetime.now():%H:%M:%S}] 剩余 {remaining/3600:.1f}h | "
              f"总计={s['total']} 成功={s['success']} 拒绝={s['rejected']} 错误={s['error']} | "
              f"平均耗时={avg:.1f}s{queue_info}")
        print(f"{'─' * 60}\n")


async def main():
    audio = AUDIO_FILE
    if not audio.exists():
        print(f"错误: 音频文件不存在 {audio}")
        sys.exit(1)

    end_time = time.time() + DURATION_HOURS * 3600

    print(f"压测开始: {CONCURRENCY} 并发, 持续 {DURATION_HOURS} 小时")
    print(f"预计结束: {datetime.fromtimestamp(end_time):%Y-%m-%d %H:%M:%S}")
    print(f"Ctrl+C 可随时停止")
    print("=" * 60)

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 启动状态打印协程
        status_task = asyncio.create_task(print_status_loop(session, end_time))

        # 启动worker
        workers = [worker(i + 1, session, audio) for i in range(CONCURRENCY)]

        # 等待所有worker或超时
        done, pending = await asyncio.wait(
            [asyncio.create_task(w) for w in workers] + [status_task],
            timeout=DURATION_HOURS * 3600 + 60,
        )

        # 超时或信号后取消剩余任务
        global stop_flag
        stop_flag = True
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # 最终汇总
    s = stats
    avg = s["elapsed_sum"] / s["success"] if s["success"] > 0 else 0
    print(f"\n{'=' * 60}")
    print(f"压测结束")
    print(f"总请求数:   {s['total']}")
    print(f"成功 (200): {s['success']}")
    print(f"拒绝 (429): {s['rejected']}")
    print(f"错误:       {s['error']}")
    print(f"平均耗时:   {avg:.1f}s")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
