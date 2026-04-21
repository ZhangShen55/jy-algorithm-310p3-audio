#!/usr/bin/env python3
"""测试并发控制功能"""
import asyncio
import aiohttp
import time
from pathlib import Path


async def send_request(session: aiohttp.ClientSession, request_id: int, audio_file: Path) -> dict:
    """发送单个ASR请求"""
    url = "http://localhost:8081/v1.1.8/seacraft_asr"

    try:
        data = aiohttp.FormData()
        with open(audio_file, 'rb') as f:
            file_content = f.read()
        data.add_field('audioFile', file_content, filename=audio_file.name, content_type='audio/wav')
        data.add_field('showEmotion', 'false')

        start_time = time.time()
        async with session.post(url, data=data) as response:
            elapsed = time.time() - start_time
            status = response.status

            if status == 200:
                result = await response.json()
                return {
                    'id': request_id,
                    'status': status,
                    'elapsed': f"{elapsed:.2f}s",
                    'segments': len(result.get('segments', []))
                }
            else:
                text = await response.text()
                return {
                    'id': request_id,
                    'status': status,
                    'elapsed': f"{elapsed:.2f}s",
                    'error': text[:100]
                }
    except Exception as e:
        return {
            'id': request_id,
            'status': 'ERROR',
            'error': str(e)
        }


async def test_concurrency(num_requests: int, audio_file: Path):
    """测试并发请求"""
    print(f"\n{'='*60}")
    print(f"测试并发控制: 发送 {num_requests} 个并发请求")
    print(f"音频文件: {audio_file.name}")
    print(f"{'='*60}\n")

    async with aiohttp.ClientSession() as session:
        tasks = [
            send_request(session, i+1, audio_file)
            for i in range(num_requests)
        ]

        start_time = time.time()
        results = await asyncio.gather(*tasks)
        total_time = time.time() - start_time

        # 统计结果
        success_count = sum(1 for r in results if r['status'] == 200)
        rejected_count = sum(1 for r in results if r['status'] == 429)
        error_count = sum(1 for r in results if r['status'] not in [200, 429])

        print(f"\n{'='*60}")
        print(f"测试结果汇总:")
        print(f"{'='*60}")
        print(f"总请求数: {num_requests}")
        print(f"成功处理: {success_count}")
        print(f"并发限制拒绝 (429): {rejected_count}")
        print(f"其他错误: {error_count}")
        print(f"总耗时: {total_time:.2f}s")
        print(f"{'='*60}\n")

        # 显示详细结果
        print("详细结果:")
        for result in results:
            status_str = f"HTTP {result['status']}" if isinstance(result['status'], int) else result['status']
            if result['status'] == 200:
                print(f"  请求 #{result['id']:2d}: {status_str} - {result['elapsed']} - {result['segments']} segments")
            elif result['status'] == 429:
                print(f"  请求 #{result['id']:2d}: {status_str} - {result['elapsed']} - 并发限制")
            else:
                print(f"  请求 #{result['id']:2d}: {status_str} - {result.get('error', 'Unknown error')}")


async def main():
    audio_file = Path("教师1.wav")

    if not audio_file.exists():
        print(f"错误: 音频文件不存在: {audio_file}")
        return

    # 测试1: 发送20个并发请求（超过16的限制）
    await test_concurrency(60, audio_file)


if __name__ == "__main__":
    asyncio.run(main())
