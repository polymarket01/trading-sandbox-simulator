"""独立 FLOW 进程：节奏、随机金额、方向不再依赖 Maker 循环。"""
import asyncio
import os
import random
import time
from uuid import uuid4

import httpx


async def main():
    token = os.environ['FLOW_INTERNAL_TOKEN']
    parent = int(os.environ['FLOW_PARENT_PID'])
    due = {}
    async with httpx.AsyncClient(base_url=os.environ['FLOW_API_URL'], headers={'X-Flow-Token': token}, timeout=10) as client:
        while os.getppid() == parent:
            try:
                response = await client.get('/api/v1/internal/flow/config')
                response.raise_for_status()
                for symbol, doc in response.json()['items'].items():
                    config = doc['config']
                    if not config['enabled'] or time.monotonic() < due.get(symbol, 0):
                        continue
                    due[symbol] = time.monotonic() + random.uniform(config['interval_min_seconds'], config['interval_max_seconds'])
                    event_id = uuid4().hex
                    # A transport timeout is never blindly retried: real IOC may
                    # already have settled. Backend also deduplicates event IDs.
                    try:
                        await client.post('/api/v1/internal/flow/'+symbol+'/tick', json={
                            'version': doc['version'], 'event_id': event_id,
                            'side': 'buy' if random.random() < 0.5 else 'sell',
                            'quote': str(random.uniform(float(config['min_quote']), float(config['max_quote'])))})
                    except httpx.HTTPError:
                        pass
            except httpx.HTTPError:
                await asyncio.sleep(1)
            await asyncio.sleep(.2)


if __name__ == '__main__':
    asyncio.run(main())
