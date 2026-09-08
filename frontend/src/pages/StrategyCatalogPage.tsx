import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';

export function StrategyCatalogPage() {
 const key=useAppStore(s=>s.authSession?.api_key);
 const [notional,setNotional]=useState('1000000'),[distance,setDistance]=useState('5');
 const [message,setMessage]=useState(''),[busy,setBusy]=useState(false),[loaded,setLoaded]=useState(false);
 useEffect(()=>{api.get<{second_level_notional:string;second_level_distance_bps:string}>('/admin/liquidity/templates/SIMPLE_BBO',key).then(r=>{setNotional(r.second_level_notional);setDistance(r.second_level_distance_bps);setLoaded(true);}).catch(e=>setMessage(e.message));},[key]);
 return <section className="panel rounded-2xl p-5 space-y-5">
  <h2 className="text-xl">策略模板</h2><p className="text-sm text-slate-400">这里只管理新币对的默认参数。已有币对的配置和启停统一在铺单策略、刷量策略页面操作。</p>
  <table className="w-full text-left text-sm"><thead><tr><th>算法</th><th>产品</th><th>执行账户</th></tr></thead><tbody>
  {[
   ['SIMPLE_BBO','合约','内部铺单，无钱包；一档照抄上游，二档固定金额'],
   ['CONTRACT_LADDER','合约','内部铺单，无钱包；多档梯度报价'],
   ['LITE','现货','普通账户；校验可用资产'],
   ['PERP_MM','合约','普通账户；校验保证金和持仓'],
  ].map(row=><tr key={row[0]} className="border-t border-white/10">{row.map(cell=><td key={cell} className="py-3">{cell}</td>)}</tr>)}
  </tbody></table>
  <h3>极简四档默认参数</h3><div className="grid gap-4 md:grid-cols-2">
  <label>买二 / 卖二每侧金额（USDT）<input className="block w-full rounded bg-slate-950 p-2" value={notional} onChange={e=>setNotional(e.target.value)} /></label>
  <label>二档距一档（bps，5 = 万分之 5）<input className="block w-full rounded bg-slate-950 p-2" value={distance} onChange={e=>setDistance(e.target.value)} /></label>
  </div><button disabled={!loaded||busy} className="rounded bg-cyan-400/15 px-4 py-2 disabled:opacity-50" onClick={async()=>{setBusy(true);try{await api.put('/admin/liquidity/templates/SIMPLE_BBO',{second_level_notional:notional,second_level_distance_bps:distance},key);setMessage('默认模板已保存，仅用于新建策略配置。');}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}}}>保存默认参数</button>
  {message&&<p role="status">{message}</p>}
 </section>;
}
