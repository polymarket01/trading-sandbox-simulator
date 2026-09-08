import { useEffect, useState } from 'react';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';

type Field = {path:string;label:string;kind:string;help?:string;options?:string[]};
type Document = {strategy_key:string;expected_version:number;fields:Field[];config:Record<string,unknown>};
const at=(value:unknown,path:string):unknown=>path.split('.').reduce<unknown>((v,k)=>(v as Record<string,unknown>)?.[k],value);
const fieldsToDraft=(d:Document)=>Object.fromEntries(d.fields.map(f=>[f.path,["array","object"].includes(f.kind)?JSON.stringify(at(d.config,f.path),null,2):at(d.config,f.path)]));
const encodeDraft=(d:Document,draft:Record<string,unknown>)=>Object.fromEntries(d.fields.map(f=>[f.path,["array","object"].includes(f.kind)?JSON.parse(String(draft[f.path])):draft[f.path]]));
export function PluginParameters({symbol,strategy,saved}:{symbol:string;strategy:string;saved:boolean}) {
 const key=useAppStore(s=>s.authSession?.api_key);
 const [doc,setDoc]=useState<Document|null>(null),[draft,setDraft]=useState<Record<string,unknown>>({}),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
 useEffect(()=>{let active=true;setDoc(null);setMessage('');if(saved)api.get<Document>(`/admin/liquidity/makers/${symbol}/parameters`,key).then(d=>{if(active){if(d.strategy_key!==strategy)throw Error('策略已切换，请刷新');setDoc(d);setDraft(fieldsToDraft(d));}}).catch(e=>{if(active)setMessage(e.message);});return()=>{active=false;};},[symbol,strategy,saved,key]);
 if(!saved)return <p>请先保存策略选择，再配置参数。</p>;
 return <section className="space-y-3 rounded border border-white/10 p-4"><h2>策略参数</h2>
 <div className="grid gap-4 md:grid-cols-2">{doc?.fields.map(f=><label className="grid gap-1 text-sm" key={f.path}>{f.label}
 {['array','object'].includes(f.kind)?<textarea rows={6} className="rounded border border-white/15 bg-slate-950 p-2 font-mono" value={String(draft[f.path]??'')} onChange={e=>setDraft({...draft,[f.path]:e.target.value})}/>:f.kind==='boolean'?<input type="checkbox" checked={draft[f.path]===true} onChange={e=>setDraft({...draft,[f.path]:e.target.checked})}/>:f.options?<select className="rounded bg-slate-950 p-2" value={String(draft[f.path]??'')} onChange={e=>setDraft({...draft,[f.path]:e.target.value})}>{f.options.map(v=><option key={v}>{v}</option>)}</select>:<input className="rounded border border-white/15 bg-slate-950 p-2" inputMode={['decimal','number','integer'].includes(f.kind)?'decimal':'text'} value={String(draft[f.path]??'')} onChange={e=>setDraft({...draft,[f.path]:['number','integer'].includes(f.kind)?Number(e.target.value):e.target.value})}/>}
 {f.help&&<span className="text-slate-400">{f.help}</span>}</label>)}</div>
 <button disabled={busy||!doc} className="rounded bg-emerald-400/20 px-4 py-2" onClick={async()=>{if(!doc)return;setBusy(true);try{await api.put(`/admin/liquidity/makers/${symbol}/parameters`,{strategy_key:strategy,expected_version:doc.expected_version,parameters:encodeDraft(doc,draft)},key);const next=await api.get<Document>(`/admin/liquidity/makers/${symbol}/parameters`,key);setDoc(next);setDraft(fieldsToDraft(next));setMessage('参数已保存');}catch(e){setMessage(e instanceof Error?e.message:String(e));}finally{setBusy(false);}}}>保存参数</button>
 {message&&<p role="status">{message}</p>}</section>;
}
