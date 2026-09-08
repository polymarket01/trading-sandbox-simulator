import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';

type Record = {desired_version:number; config:{second_level_notional:string;second_level_distance_bps:string}; runtime:{enabled:boolean;state:string;open_order_count:number;pending_reason?:string;last_error?:string}};
export function SimpleBBOParameters({symbol,saved}:{symbol:string;saved:boolean}) {
 const key=useAppStore(s=>s.authSession?.api_key);
 const [record,setRecord]=useState<Record>(),[buy,setBuy]=useState('1000000'),[sell,setSell]=useState('5'),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
 useEffect(()=>{let alive=true;if(saved)api.get<Record>(`/admin/liquidity/simple-bbo/${symbol}`,key).then(r=>{if(alive){if(r.config.second_level_notional===undefined){setMessage('请重载后端以使用四档铺单');return;}setRecord(r);setBuy(r.config.second_level_notional);setSell(r.config.second_level_distance_bps);}}).catch(e=>alive&&setMessage(e.message));return()=>{alive=false;};},[symbol,saved,key]);
 useEffect(()=>{if(!saved)return;let alive=true;const timer=window.setInterval(()=>{api.get<Record>(`/admin/liquidity/simple-bbo/${symbol}`,key).then(r=>{if(alive)setRecord(old=>old?{...old,runtime:r.runtime}:old);}).catch(e=>alive&&setMessage(e.message));},2000);return()=>{alive=false;window.clearInterval(timer);};},[symbol,saved,key]);
 async function action(kind:'save'|'start'|'stop'){
  if(!record)return;setBusy(true);setMessage('');
  try{
   if(kind==='save')await api.put(`/admin/liquidity/simple-bbo/${symbol}`,{expected_version:record.desired_version,second_level_notional:buy,second_level_distance_bps:sell},key);
   else await api.post(`/admin/liquidity/makers/${symbol}/control`,{action:kind},key);
   const r=await api.get<Record>(`/admin/liquidity/simple-bbo/${symbol}`,key);setRecord(r);setBuy(r.config.second_level_notional);setSell(r.config.second_level_distance_bps);
   setMessage(kind==='save'?'二档参数已保存':kind==='start'?'已启用，收到有效买卖价和数量后开始铺单':'已停用，正在确认自有订单撤净');
  }catch(e){setMessage(e instanceof Error?e.message:String(e));}finally{setBusy(false);}
 }
 return <div className="space-y-4 border-t border-white/10 pt-4">
  <h2 className="text-lg">极简四档铺单</h2>
  <p className="text-sm text-slate-400">买一卖一的价格和数量原样复制上游 bookTicker；买二卖二各挂固定金额，默认每侧 100 万 USDT，分别向外距离对应一档万分之五（5 bps = 0.05%）。</p>
  {!saved?<p>先保存上方策略选择，即可编辑两个二档参数并启动。</p>:<>
   <div className="grid gap-4 md:grid-cols-2">
    <label className="grid gap-1">二档每侧金额（USDT）<input aria-label="二档每侧金额（USDT）" className="rounded border border-white/15 bg-slate-950 p-2" type="number" min="0.00000001" step="any" value={buy} onChange={e=>setBuy(e.target.value)}/></label>
    <label className="grid gap-1">二档距离（万分之一 / bps）<input aria-label="二档距离（万分之一 / bps）" className="rounded border border-white/15 bg-slate-950 p-2" type="number" min="0.00000001" max="9999.99999999" step="any" value={sell} onChange={e=>setSell(e.target.value)}/></label>
   </div>
   <p className="text-sm">二档买价向下、卖价向上按 tick 对齐，至少距离一档一个 tick；二档数量按步长向下取整。一档精度不兼容时暂停并提示，不偷偷修改上游价格或数量。行情不变不重复挂撤，成交后补齐对应档位。</p>
   <div className="flex gap-3">{(['save','start','stop'] as const).map(k=><button key={k} disabled={busy||!record} onClick={()=>action(k)} className="rounded border border-white/15 px-4 py-2">{k==='save'?'保存二档参数':k==='start'?'启动已保存配置':'停止并撤单'}</button>)}</div>
   {record&&<p>配置：{record.runtime.enabled?'已启用':'已停用'} · 状态：{record.runtime.state} · 自有挂单：{record.runtime.open_order_count} · {record.runtime.pending_reason||record.runtime.last_error||'—'}</p>}
  </>}
  {message&&<p role="status">{message}</p>}
 </div>;
}
