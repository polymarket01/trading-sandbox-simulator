import { Component, type ReactNode } from 'react';

export class PageLoadBoundary extends Component<{children: ReactNode}, {failed: boolean}> {
  state = {failed: false};
  static getDerivedStateFromError() { return {failed: true}; }
  componentDidCatch(error: Error) {
    if (!/dynamically imported module|module script|Importing a module|Loading chunk/i.test(error.message)) return;
    const key = 'page-module-recovery:' + location.pathname;
    try {
      const last = Number(sessionStorage.getItem(key) || 0);
      if (Date.now() - last < 60000) return;
      sessionStorage.setItem(key, String(Date.now()));
    } catch { return; }
    // Only reload when the page server is reachable. Never loop while offline.
    void fetch(location.href, {cache:'no-store', signal:AbortSignal.timeout(5000)}).then(r => {
      if (r.ok) location.reload();
    }).catch(() => undefined);
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return <div role="alert" className="m-6 rounded-xl border border-white/10 bg-slate-900 p-6 text-slate-200"><h1 className="text-lg">页面暂时无法加载</h1><p className="my-3 text-sm text-slate-400">可能是页面版本已更新，或服务暂时不可连接。服务恢复后可重试。</p><button className="rounded-lg bg-cyan-400/15 px-4 py-2" onClick={() => location.reload()}>重新加载页面</button></div>;
  }
}
