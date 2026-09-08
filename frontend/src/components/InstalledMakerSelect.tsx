import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';
type Plugin={strategy_key:string;display_name:string;products:string[]};
export function InstalledMakerSelect({product,value,onChange}:{product:string;value:string;onChange:(v:string)=>void}) {
 const key=useAppStore(s=>s.authSession?.api_key);
 const [plugins,setPlugins]=useState<Plugin[]>([]),[error,setError]=useState('');
 useEffect(()=>{let alive=true;api.get<{items:Plugin[]}>('/admin/liquidity/plugins',key).then(r=>{if(alive)setPlugins(r.items);}).catch(()=>{if(alive)setError('策略清单加载失败');});return()=>{alive=false;};},[key]);
 const choices=plugins.filter(p=>p.products.includes(product));
 return <label className="grid gap-1 text-sm">默认铺单策略<select className="rounded border border-white/15 bg-slate-950 p-2" value={choices.some(p=>p.strategy_key===value)?value:''} onChange={e=>onChange(e.target.value)}><option value="">{error||(!choices.length?'无可用铺单策略':'使用已安装策略默认值')}</option>{choices.map(p=><option key={p.strategy_key} value={p.strategy_key}>{p.display_name}</option>)}</select></label>;
}
