import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';
type Field={path:string;label?:string;description?:string;kind?:string;type?:string};
type Config={strategy_key:string;effective_config:Record<string,unknown>;schema?:{sections?:{key:string;label:string;fields:Field[]}[]}};
const read=(obj:Record<string,unknown>,path:string):unknown=>path.split('.').reduce<unknown>((v,k)=>v&&typeof v==='object'?(v as Record<string,unknown>)[k]:undefined,obj);
function write(obj:Record<string,unknown>,path:string,value:unknown){const copy=structuredClone(obj);const keys=path.split('.');let node=copy;for(const k of keys.slice(0,-1)){if(!node[k]||typeof node[k]!=='object')node[k]={};node=node[k] as Record<string,unknown>;}node[keys[keys.length-1]]=value;return copy;}
export function MakerParameters({symbol,strategy,savedStrategy}:{symbol:string;strategy:string;savedStrategy:string}){
 const key=useAppStore(s=>s.authSession?.api_key),[item,setItem]=useState<Config>(),[draft,setDraft]=useState<Record<string,unknown>>({}),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
 useEffect(()=>{let alive=true;api.get<{items:Config[];selected:Config}>(`/admin/markets/${symbol}/strategy`,key).then(r=>{if(!alive)return;const value=r.items.find(x=>x.strategy_key===strategy)||r.selected;setItem(value);setDraft(value.effective_config||{});}).catch(e=>alive&&setMessage(e.message));return()=>{alive=false;};},[key,symbol,strategy]);
 async function save(){setBusy(true);setMessage('');try{await api.put(`/admin/markets/${symbol}/strategy`,{strategy_key:strategy,config:draft},key);setMessage('铺单参数已保存');}catch(e){setMessage(e instanceof Error?e.message:String(e));}finally{setBusy(false);}}
 return <div className="space-y-4 border-t border-white/10 pt-4"><h2 className="text-lg">{strategy} 参数</h2>
 {(item?.schema?.sections||[]).map(section=>{const fields=section.fields.filter(f=>!/(^|\.)(flow|idle)/.test(f.path)&&['string','number','boolean'].includes(typeof read(draft,f.path)));if(!fields.length)return null;return <fieldset key={section.key} className="rounded border border-white/10 p-3"><legend className="px-2">{section.label}</legend><div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{fields.map(f=>{const value=read(draft,f.path);return <label className="grid gap-1 text-sm" key={f.path} title={f.description}>{f.label||f.path}{typeof value==='boolean'?<select className="rounded border border-white/15 bg-slate-950 p-2" value={String(value)} onChange={e=>setDraft(write(draft,f.path,e.target.value==='true'))}><option value="true">启用</option><option value="false">关闭</option></select>:<input className="rounded border border-white/15 bg-slate-950 p-2" value={String(value??'')} onChange={e=>setDraft(write(draft,f.path,typeof value==='number'?Number(e.target.value):e.target.value))}/>}</label>;})}</div></fieldset>;})}
 {savedStrategy!==strategy?<p>请先保存上方策略选择，再保存该策略的参数。</p>:<button disabled={busy} className="rounded bg-emerald-400/20 px-4 py-2 text-emerald-200" onClick={save}>保存铺单参数</button>}
 {message&&<p role="status">{message}</p>}
 </div>;
}
