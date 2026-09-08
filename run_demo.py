#!/usr/bin/env python3
"""Portable local demo. Fresh instance only: BTC spot/perpetual, installed maker, virtual FLOW."""
from pathlib import Path
import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5174)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--data-dir', default='demo_runtime')
    parser.add_argument('--skip-install', action='store_true', help='使用已有 Python 依赖与前端制品')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须在 1–65535 之间')
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', args.port)) == 0:
            raise SystemExit('端口已占用，请指定其他 --port；不会接管已有服务。')
    data = (ROOT / args.data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    if not args.skip_install:
        venv = ROOT / 'backend/.venv'
        python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.exists():
            subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT/'backend/requirements.txt')], check=True)
        npm = shutil.which('npm')
        if not npm:
            raise SystemExit('请安装 Node.js 22（含 npm），然后重新运行。')
        subprocess.run([npm, 'ci'], cwd=ROOT/'frontend', check=True)
        subprocess.run([npm, 'run', 'build'], cwd=ROOT/'frontend', check=True)
    elif not (ROOT/'frontend/dist/index.html').is_file():
        raise SystemExit('缺少前端制品；先运行 npm ci 和 npm run build，或省略 --skip-install。')
    marker = data / 'demo_initialized.json'
    fresh = not (data/'control.db').exists()
    key_file = data / '.admin_api_key'
    if not key_file.exists():
        if not fresh:
            raise SystemExit('已有数据库但缺少本入口的身份文件，拒绝接管；请使用独立数据目录。')
        with key_file.open('x') as handle:
            handle.write(secrets.token_urlsafe(32))
        key_file.chmod(0o600)
    key = key_file.read_text().strip()
    env = os.environ.copy()
    env.update(DATABASE_URL=f'sqlite+aiosqlite:///{data}/control.db', SANDBOX_DATA_DIR=str(data),
        SANDBOX_PERSISTENCE_MODE='durable', HISTORY_DB_PATH=str(data/'market_history.db'),
        EXCHANGE_SNAPSHOT_DIR=str(data/'snapshots'), FRONTEND_DIST_DIR=str(ROOT/'frontend/dist'),
        PAPER_EXCHANGE_ADMIN_USERNAME='admin', PAPER_EXCHANGE_ADMIN_PASSWORD=os.getenv('PAPER_EXCHANGE_ADMIN_PASSWORD','12345'),
        PAPER_EXCHANGE_ADMIN_API_KEY=key, PAPER_EXCHANGE_DEFAULT_SPOT_USDT='100000000',
        PAPER_EXCHANGE_DEFAULT_PERP_USDT='100000000', SANDBOX_FLOW_API_URL=f'http://127.0.0.1:{args.port}',
        PYTHONPATH=os.pathsep.join([str(ROOT), str(ROOT/'backend')]), SANDBOX_RUNNER_WATCHDOG_ENABLED='1',
        SANDBOX_RUNNER_PID=str(os.getpid()))
    subprocess.run([str(python), '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT/'backend', env=env, check=True)
    process = subprocess.Popen([str(python), '-m', 'uvicorn', 'app.main:app', '--host', args.host,
        '--port', str(args.port), '--log-level', 'warning', '--no-access-log'], cwd=ROOT/'backend', env=env)
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    def api(method, path, body=None):
        request = urllib.request.Request(f'http://127.0.0.1:{args.port}/api/v1'+path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'X-API-Key':key, 'Content-Type':'application/json'}, method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    try:
        deadline = time.monotonic()+120
        while True:
            if process.poll() is not None:
                raise RuntimeError('后端启动失败，请查看上方日志')
            try:
                plugins = api('GET','/admin/liquidity/plugins')['items']; break
            except (OSError, ValueError):
                if time.monotonic()>deadline:raise RuntimeError('后端启动超时')
                time.sleep(.5)
        if fresh:
            # Safe to retry our partially initialized new instance; no pre-existing data is imported.
            for symbol,product in [('BTCUSDT','SPOT'),('BTCUSDT-PERP','PERP')]:
                choices=[p['strategy_key'] for p in plugins if product in p['products']]
                strategy='SIMPLE_BBO' if 'SIMPLE_BBO' in choices else next(iter(choices),None)
                if strategy:
                    source = api('POST', f'/admin/liquidity/{symbol}/source-test', {'exchange':'binance','symbol':'BTCUSDT'})
                    rules = source.get('rules') or {}
                    if rules:
                        api('PUT', f'/admin/markets/{symbol}', {k:rules[k] for k in ('price_tick','qty_step','price_precision','qty_precision','min_qty')})
                    else:
                        print(f'{symbol} 暂未取得上游精度；请在管理页面测试上游并核对规格。', flush=True)
                    api('PUT',f'/admin/liquidity/makers/{symbol}',{'strategy_key':strategy,'source':{'exchange':'binance','symbol':'BTCUSDT'}})
                    api('POST',f'/admin/liquidity/makers/{symbol}/control',{'action':'start'})
                    api('PUT',f'/admin/liquidity/flow/{symbol}',{'expected_version':0,'config':{'enabled':True,'mode':'virtual_volume','virtual_allow_touch':True}})
            marker.write_text(json.dumps({'version':1,'markets':['BTCUSDT','BTCUSDT-PERP']}))
        if not marker.exists():
            print('上次初始化未完成；保留已有配置，请在管理页面检查并启动策略。', flush=True)
        print(f'交易页：http://{args.host}:{args.port}/paper/trade/BTCUSDT\n登录：admin；默认密码 12345（如已设置环境变量，则使用自定义密码）。', flush=True)
        print('模拟资金：现货与合约各 100,000,000 USDT。Ctrl+C 停止。', flush=True)
        process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()

if __name__ == '__main__':
    try:main()
    except KeyboardInterrupt:pass
