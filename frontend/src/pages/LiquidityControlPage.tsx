import { SimpleBBOParameters } from "./SimpleBBOParameters";
import { PluginParameters } from "./PluginParameters";
import { ContractLadderPage } from "./ContractLadderPage";
import { MakerParameters } from "./MakerParameters";
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import { useAppStore } from '../store/useAppStore';

type Market = {symbol:string; product_type:string; strategy_key:string; choices:string[]; parameter_editors?:Record<string,string>; source_exchange:string; source_symbol:string; readiness:{ok:boolean; blockers:{code?:string;label:string;detail:string}[]}; ladder?:{state:string}; account_slots:Record<string,number>; maker_accounts:{uid:number;username:string}[]; flow_accounts:{uid:number;username:string}[]};
const readinessText=(m:Market)=>m.readiness.ok?'通过':m.readiness.blockers.length>0&&m.readiness.blockers.every(b=>b.code==='no_enabled_maker'||b.code==='missing_api_credentials')?'启动时自动初始化':'阻断';
type Flow = {virtual_allow_touch?:boolean;enabled:boolean; mode:string; uid:number|null; interval_min_seconds:number; interval_max_seconds:number; min_quote:string; max_quote:string; turnover_quote_per_min:string; max_level_take_ratio:string; volatility_enabled:boolean; real_ioc_volatility_enabled:boolean; volatility_window_seconds:number; volatility_return_bps:number; volatility_range_bps:number; volatility_max_multiplier:number};
type FlowDoc = {version:number;config:Flow};
const defaults:Flow = {enabled:false,mode:'virtual_volume',uid:null,interval_min_seconds:1.5,interval_max_seconds:2.5,min_quote:'100',max_quote:'1000',turnover_quote_per_min:'30000',max_level_take_ratio:'.25',volatility_enabled:true,real_ioc_volatility_enabled:false,volatility_window_seconds:10,volatility_return_bps:5,volatility_range_bps:8,volatility_max_multiplier:100};
const statusText:Record<string,string>={virtual_print:'虚拟成交已生成',filled:'真实成交已完成',partially_filled:'部分成交',canceled:'IOC 未成交',spread_le_one_tick:'价差不超过 1 tick，跳过',empty_book:'盘口为空，等待',blocked:'执行被阻断，修正后重新保存启动',market_paused:'市场暂停',turnover_limit:'已达每分钟金额上限',waiting_interval:'等待下一轮',below_minimum_or_thin_book:'最优档深度不足或低于最小成交量'};
type SwitchState={status:string;stage:string;error?:string};
const inputClass='rounded border border-white/15 bg-slate-950 p-2 text-slate-100 w-full';

function LiquidityEditor({kind, initialSymbol, onBack}:{kind:'maker'|'flow';initialSymbol:string;onBack:()=>void}) {
 const key=useAppStore(s=>s.authSession?.api_key);
 const [markets,setMarkets]=useState<Market[]>([]); const [symbol,setSymbol]=useState(initialSymbol);
 const [uids,setUids]=useState<string[]>([]);
 const [strategy,setStrategy]=useState(''); const [source,setSource]=useState('');
 const [docs,setDocs]=useState<Record<string,FlowDoc>>({}); const [draft,setDraft]=useState<Flow>(defaults);
 const [activitySupported,setActivitySupported]=useState(false);
 const [switchSupported,setSwitchSupported]=useState(false),[switchState,setSwitchState]=useState<SwitchState>();
 const [activity,setActivity]=useState<Record<string,{multiplier:number;status:string;return_bps:number;range_bps:number}>>({});
 const [process,setProcess]=useState<{pid?:number;running?:boolean;heartbeat_age_seconds?:number;error?:string}>({});
 const [metrics,setMetrics]=useState<Record<string,{status?:string;reason?:string}>>({});
 const [busy,setBusy]=useState(false); const [message,setMessage]=useState('');
 const [sourceResult,setSourceResult]=useState('');
 useEffect(()=>setSourceResult(''),[source,symbol]);
 const market=markets.find(m=>m.symbol===symbol);
 const refresh=useCallback(async()=>{
  const [m,f]=await Promise.all([api.get<{items:Market[];switch_supported?:boolean;switches?:Record<string,SwitchState>}>('/admin/liquidity/makers',key),api.get<{items:Record<string,FlowDoc>;process:typeof process;metrics:typeof metrics;activity:typeof activity;activity_version?:number}>('/admin/liquidity/flow',key)]);
  setSwitchSupported(m.switch_supported===true);setSwitchState(m.switches?.[initialSymbol]);
  setMarkets(m.items);setDocs(f.items);setProcess(f.process);setMetrics(f.metrics);setActivity(f.activity||{});setActivitySupported(f.activity_version===1);setSymbol(s=>s||m.items[0]?.symbol||'');
 },[key,initialSymbol]);
 useEffect(()=>{refresh().catch(e=>setMessage(e.message));},[refresh]);
 useEffect(()=>{if(market){setStrategy(market.strategy_key);setUids(market.maker_accounts.map(a=>String(a.uid)));setSource(market.source_symbol);setDraft({...defaults,...docs[symbol]?.config});}},[symbol,market?.strategy_key,market?.source_symbol,docs]);
 async function act(fn:()=>Promise<unknown>, success:string, refreshAfter=true){setBusy(true);setMessage('');try{await fn();if(success)setMessage(success);if(refreshAfter)await refresh();}catch(e){setMessage(e instanceof Error?e.message:String(e));}finally{setBusy(false);}}
 const field=(label:string,name:keyof Flow,type:'number'|'text'='number')=><label className="grid gap-1 text-sm">{label}<input className={inputClass} type={type} step="any" value={String(draft[name]??'')} onChange={e=>setDraft({...draft,[name]:['interval_min_seconds','interval_max_seconds','volatility_window_seconds','volatility_return_bps','volatility_range_bps','volatility_max_multiplier'].includes(name)?Number(e.target.value):e.target.value})}/></label>;
 return <div className="mx-auto max-w-6xl p-5 text-slate-200 space-y-5">
  <button onClick={onBack} className="text-emerald-300">← 返回币对列表</button>
  <h1 className="text-xl">{symbol} · {kind==='maker'?'铺单策略参数':'刷量策略参数'}</h1>
  {kind==='maker'&&market?<>
   <div className="grid md:grid-cols-3 gap-4"><label>铺单策略<select aria-label="铺单策略" className={inputClass} value={strategy} onChange={e=>setStrategy(e.target.value)}>{market.choices.map(c=><option key={c} value={c}>{c==='SIMPLE_BBO'?'极简四档铺单（SIMPLE_BBO）':c}</option>)}</select></label>
    <label>上游交易所<select className={inputClass} value="binance" disabled><option value="binance">Binance（已支持）</option></select></label>
    <label>上游币对<input disabled={busy} aria-label="上游币对" className={inputClass} value={source} onChange={e=>setSource(e.target.value.toUpperCase())}/></label></div>
   <details><summary className="cursor-pointer text-sm text-slate-400">执行账户（自动配置，高级设置）</summary>
   <div className="grid md:grid-cols-2 gap-4">{Array.from({length:market.account_slots[strategy]||1},(_,i)=><label key={i}>执行 UID {i+1}<input aria-label={`执行 UID ${i+1}`} className={inputClass} type="number" min="1" placeholder="留空自动分配，保存后固定" value={uids[i]||''} onChange={e=>setUids(old=>{const next=[...old];next[i]=e.target.value;return next;})}/></label>)}</div>
   <p className="text-sm text-slate-400">UID 由本币对独占；可指定已有机器人 UID。留空优先复用本币对账户，否则按顺序创建。策略切换保留账户身份。</p></details>
   <p className="text-sm">{['CONTRACT_LADDER','SIMPLE_BBO'].includes(strategy)?'内部免资产 Maker：专用身份与市场绑定，不需要 API 密钥、钱包或双账户；用户成交仍正常结算。':'账户型 Maker：凭据与资产检查正常执行。'} 单独更改上游映射前需停止并撤单；更换策略使用“切换并启动”。</p>
   <div className="flex flex-wrap gap-3">
    <div className="flex flex-wrap items-center gap-3">
    <button className={inputClass+' !w-auto'} disabled={busy} onClick={async()=>{setBusy(true);setSourceResult('测试中…');try{const r=await api.post<{ok:boolean;reason?:string;bid:string;ask:string;bid_qty?:string;ask_qty?:string;latency_ms:number;rules?:{price_tick:string;qty_step:string;price_precision:number;qty_precision:number;min_qty:string;min_notional?:string;quote_asset?:string};rules_error?:string}>(`/admin/liquidity/${symbol}/source-test`,{exchange:'binance',symbol:source},key);if(!r.ok)throw Error(r.reason||'上游不可用');setSourceResult(`测试成功：买一 ${r.bid} × ${r.bid_qty ?? "未知"} / 卖一 ${r.ask} × ${r.ask_qty ?? "未知"} · ${r.latency_ms}ms${r.rules?.price_tick ? `；价格步长 ${r.rules.price_tick}（${r.rules.price_precision}位），数量步长 ${r.rules.qty_step}（${r.rules.qty_precision}位）；最小数量 ${r.rules.min_qty}，最小金额 ${r.rules.min_notional ?? "未知"} ${r.rules.quote_asset ?? ""}` : `；${r.rules_error || "未取得上游交易精度"}`}`);}catch(e){setSourceResult(`测试失败：${e instanceof Error?e.message:String(e)}`);}finally{setBusy(false);}}}>测试上游数据</button>
    <span role="status" aria-live="polite" className="text-sm text-slate-300">{sourceResult}</span>
    </div>
    <button className={inputClass+' !w-auto'} disabled={busy||(strategy!==market.strategy_key&&!switchSupported)} onClick={()=>act(async()=>{
     const payload={strategy_key:strategy,uids:Array.from({length:market.account_slots[strategy]||1},(_,i)=>uids[i]?Number(uids[i]):null),source:{exchange:'binance',symbol:source}};
     if(strategy===market.strategy_key)await api.put(`/admin/liquidity/makers/${symbol}`,payload,key);
     else {try{await api.post(`/admin/liquidity/makers/${symbol}/switch`,{...payload,expected_strategy_key:market.strategy_key},key);}catch(e){await refresh();throw e;}}
    },strategy===market.strategy_key?'策略与上游映射已保存':'旧策略已停并撤净，新策略已启用，请查看运行状态')}>{busy?'处理中…':strategy===market.strategy_key?'保存策略与映射':'切换并启动'}</button>
    {strategy!==market.strategy_key&&<p className="w-full text-sm text-amber-200">{switchSupported?'切换会先停旧策略、撤净自有挂单，再按新策略已保存参数（首次使用默认参数）启动，期间盘口会短暂空缺；失败不会自动恢复旧策略。':'当前后端尚不支持自动切换，请重载后端。'}</p>}
    {switchState&&['running','failed','interrupted'].includes(switchState.status)&&<p className="w-full text-sm text-amber-200">上次切换：{switchState.status==='running'?'进行中':'未完成'} · {switchState.error||switchState.stage}</p>}

   </div>
   <p>当前配置：{market.strategy_key} · 预检：{readinessText(market)}{market.ladder&&market.strategy_key==='CONTRACT_LADDER'?` · ${market.ladder.state}`:''}</p>
   {market.readiness.blockers.map((b,i)=><p className="text-rose-300" key={i}>{b.label}：{b.detail}</p>)}
   {market.parameter_editors?.[strategy]==='schema' ? <PluginParameters key={symbol+strategy} symbol={symbol} strategy={strategy} saved={market.strategy_key===strategy} /> : !market.parameter_editors && strategy==='SIMPLE_BBO' ? <SimpleBBOParameters key={symbol} symbol={symbol} saved={market.strategy_key===strategy}/> : strategy==='CONTRACT_LADDER' ? market.strategy_key==='CONTRACT_LADDER' ? <ContractLadderPage key={symbol} initialSymbol={symbol} embedded /> : <p>保存策略选择后，在此编辑 LADDER 的分区深度、档位、时段和保护参数。</p> : <MakerParameters key={symbol+strategy} symbol={symbol} strategy={strategy} savedStrategy={market.strategy_key} />}

  </>:null}
  {kind==='flow'?<>
   <p>本币对 FLOW：{docs[symbol]?.config.enabled?'已启用':'已停止'} · 独立进程：{process.running?'运行中':'未运行'} · 最近一轮：{statusText[metrics[symbol]?.status||'']||metrics[symbol]?.status||'暂无'} {metrics[symbol]?.reason||''}</p>
   <div className="grid md:grid-cols-3 gap-4">
    <label>成交模式<select aria-label="成交模式" className={inputClass} value={draft.mode} onChange={e=>setDraft({...draft,mode:e.target.value,real_ioc_volatility_enabled:false})}><option value="virtual_volume">虚拟成交 · 不进撮合、不落库</option><option value="real_ioc_sandbox">真实 IOC · 正常撮合与结算</option></select></label>
    {draft.mode==='real_ioc_sandbox'?<label>FLOW UID<input aria-label="FLOW UID" className={inputClass} type="number" min="1" placeholder="留空自动分配独立 UID" value={draft.uid||''} onChange={e=>setDraft({...draft,uid:Number(e.target.value)||null})}/></label>:<p>虚拟模式无需账户，不会与用户订单成交。</p>}
    {field('最短间隔（秒）','interval_min_seconds')}{field('最长间隔（秒）','interval_max_seconds')}{field('最小单笔金额（Quote）','min_quote')}{field('最大单笔金额（Quote）','max_quote')}{field(draft.mode==='virtual_volume'?'每分钟基础金额上限（放量前）':'每分钟硬金额上限（含放量）','turnover_quote_per_min')}
    {draft.mode==='real_ioc_sandbox'?field('最多吃掉最优档数量比例','max_level_take_ratio'):null}
   </div>
   {draft.mode==='virtual_volume'&&<label className="flex gap-2 text-sm"><input type="checkbox" checked={draft.virtual_allow_touch===true} onChange={e=>setDraft({...draft,virtual_allow_touch:e.target.checked})}/>价差只有一个 tick 时允许在买一/卖一生成虚拟成交（不撮合、不改资产）</label>}
   <p className="text-xs text-slate-400">方向自动跟随行情：无趋势时买卖各50%，窗口上涨/下跌时80%顺势；波动放量单固定跟随窗口内最近一次价格变动方向。无需设置买卖概率。</p>
   <section className="rounded-xl border border-white/10 p-4 space-y-3">
    <h2 className="text-base">急涨急跌放量</h2>
    {!activitySupported&&<p role="status" className="text-xs text-amber-200">当前后端尚未加载放量功能，需要重启后端后使用；现在保存并启动不会提交，以免参数被旧版本忽略。</p>}
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={draft.volatility_enabled} onChange={e=>setDraft({...draft,volatility_enabled:e.target.checked})}/>启用波动放量（虚拟模式默认开启）</label>
    {draft.mode==='real_ioc_sandbox'&&<label className="flex items-center gap-2 text-sm text-amber-200"><input type="checkbox" checked={draft.real_ioc_volatility_enabled} onChange={e=>setDraft({...draft,real_ioc_volatility_enabled:e.target.checked})}/>也允许真实 IOC 放量（默认关闭，可能增加与用户订单的成交）</label>}
    <p className="text-xs leading-5 text-slate-400">每秒读取现有行情，取窗口涨跌幅绝对值与高低价振幅的较强信号。只扩大单笔金额，不提高请求频率；沿用上方随机间隔。窗口不再满足门槛时，下一次采样直接恢复1倍。1 bps = 0.01%。</p>
    <div className="grid gap-3 md:grid-cols-3">
      {field('观察窗口（秒，2–60）','volatility_window_seconds')}
      {field('涨跌幅触发门槛（bps）','volatility_return_bps')}
      {field('高低价振幅门槛（bps）','volatility_range_bps')}
      {field('最大金额倍数（1–100）','volatility_max_multiplier')}
    </div>
    <p className="text-xs text-slate-400">{draft.mode==='virtual_volume'?`虚拟单笔上限 ${Number(draft.max_quote)*draft.volatility_max_multiplier} Quote；每分钟理论上限 ${Number(draft.turnover_quote_per_min)*draft.volatility_max_multiplier} Quote。关闭放量时使用基础上限。`:'真实 IOC 的每分钟金额硬上限、最优档吃单比例和账户校验始终保留。'}</p>
    <p className="text-xs text-cyan-200">最近采样：{activity[symbol]?`${({active:'放量中',normal:'平稳',warming_up:'积累窗口',disabled:'已关闭',unavailable:'行情不可用'} as Record<string,string>)[activity[symbol].status]||activity[symbol].status} · ${activity[symbol].multiplier.toFixed(2)} 倍 · 涨跌 ${activity[symbol].return_bps} bps / 振幅 ${activity[symbol].range_bps} bps`:'等待后端采样'}（点击下方刷新运行状态查看）</p>
   </section>
   <p className="text-sm text-slate-400">默认买卖价差 ≤ 1 tick 时跳过；勾选上方选项后仅虚拟模式可在最优报价展示成交。虚拟价格优先取价差内合法中间档，金额随机；重启后虚拟历史消失。真实模式受账户资产与市场规则约束。</p>
   <div className="flex gap-3">{[false,true].map(enabled=><button className={inputClass+' !w-auto'} key={String(enabled)} disabled={busy || (enabled && !activitySupported)} onClick={()=>act(()=>api.put(`/admin/liquidity/flow/${symbol}`,{config:{...draft,enabled},expected_version:docs[symbol]?.version||0},key),enabled?'FLOW 配置已保存并启动':'FLOW 已保存并停止')}>{enabled?'保存并启动 FLOW':'保存并停止 FLOW'}</button>)}
   <Link to="/admin?section=bots">管理 / 创建 FLOW 账户</Link></div>
  </>:null}
  <button onClick={()=>refresh().catch(e=>setMessage(e.message))}>刷新运行状态</button>
  {message?<p role="status" className="rounded border border-white/20 p-3 whitespace-pre-wrap">{message}</p>:null}
 </div>;
}

export function LiquidityControlPage({kind}:{kind:'maker'|'flow'}) {
 const key=useAppStore(s=>s.authSession?.api_key);
 const [markets,setMarkets]=useState<Market[]>([]),[docs,setDocs]=useState<Record<string,FlowDoc>>({});
 const [states,setStates]=useState<Record<string,string>>({}),[edit,setEdit]=useState<string | undefined>();
 const [message,setMessage]=useState(''),[busy,setBusy]=useState(''),[filter,setFilter]=useState('');
 const refresh=useCallback(async()=>{
  const [m,f,i]=await Promise.all([
   api.get<{items:Market[];switch_supported?:boolean;switches?:Record<string,SwitchState>}>('/admin/liquidity/makers',key),
   api.get<{items:Record<string,FlowDoc>;metrics:Record<string,{status?:string;reason?:string}>}>('/admin/liquidity/flow',key),
   api.get<{items:{symbol:string;running:boolean;status:string}[]}>('/admin/maker-instances',key),
  ]);
  setMarkets(m.items);setDocs(f.items);
  setStates(Object.fromEntries(m.items.map(row=>{
   const instance=i.items.find(x=>x.symbol===row.symbol),doc=f.items[row.symbol],metric=f.metrics[row.symbol];
   const transition=m.switches?.[row.symbol];
   if(kind==='maker'&&transition&&['running','failed','interrupted'].includes(transition.status))return [row.symbol,transition.status==='running'?'切换中':`切换未完成：${transition.error||transition.stage}`];
   return [row.symbol,kind==='maker'?(instance?.running?(row.ladder?.state==='NORMAL'?'运行中':row.ladder?.state==='REST_BACKUP'||instance.status==='REST_BACKUP'?'REST备用':row.ladder?.state==='INDEX_FALLBACK'||instance.status==='INDEX_FALLBACK'?'指数应急铺单':instance.status):'已停止'):
    !doc?.config.enabled?'已停止':metric?.status==='blocked'?`阻断：${metric.reason||''}`:'已启用'];
  })));
 },[key,kind]);
 useEffect(()=>{if(edit)return;let disposed=false;let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{await refresh();}catch(e){if(!disposed)setMessage(e instanceof Error?e.message:String(e));}if(!disposed)timer=setTimeout(poll,5000);};void poll();return()=>{disposed=true;clearTimeout(timer);};},[refresh,edit]);
 async function control(symbol:string,enabled:boolean){setBusy(symbol);setMessage('');try{
  if(kind==='maker')await api.post(`/admin/liquidity/makers/${symbol}/control`,{action:enabled?'start':'stop'},key);
  else {const doc=docs[symbol];await api.put(`/admin/liquidity/flow/${symbol}`,{config:{...(doc?.config||defaults),enabled},expected_version:doc?.version||0},key);}
  setMessage(`${symbol}：${enabled?'启动请求已提交':'停止请求已提交'}`);await refresh();
 }catch(e){setMessage(e instanceof Error?e.message:String(e));}finally{setBusy('');}}
 if(edit)return <LiquidityEditor key={kind+edit} kind={kind} initialSymbol={edit} onBack={()=>setEdit(undefined)} />;
 return <section className="panel rounded-2xl p-4 space-y-4">
  <div className="flex items-center justify-between gap-3"><div><h1 className="text-xl">{kind==='maker'?'铺单策略':'刷量策略'}</h1><p className="mt-1 text-sm text-slate-400">按币对查看状态，在右侧编辑参数或独立启停。</p></div><input aria-label="筛选币对" placeholder="搜索币对" className={inputClass+' !w-56'} value={filter} onChange={e=>setFilter(e.target.value)}/></div>
  {message&&<p role="status" className="text-amber-200">{message}</p>}
  <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="text-slate-400"><tr><th className="p-3">币对</th><th className="p-3">{kind==='maker'?'铺单策略':'成交模式'}</th><th className="p-3">状态</th><th className="p-3">{kind==='maker'?'上游 / 预检':'间隔 / 单笔金额'}</th><th className="p-3 text-right">操作</th></tr></thead><tbody>
  {markets.filter(m=>m.symbol.toLowerCase().includes(filter.toLowerCase())).map(m=>{const doc=docs[m.symbol],fc=doc?.config||defaults;return <tr key={m.symbol} className="border-t border-white/10" data-market-row={m.symbol}>
   <td className="p-3 font-mono">{m.symbol}<span className="ml-2 text-xs text-slate-500">{m.product_type==='PERP'?'合约':'现货'}</span></td>
   <td className="p-3">{kind==='maker'?`${m.strategy_key} · UID ${m.maker_accounts.map(a=>a.uid).join(' / ')||'待分配'}`:fc.mode==='virtual_volume'?'虚拟成交':`真实 IOC · UID ${fc.uid||'未配置'}`}</td>
   <td className="p-3">{states[m.symbol]||'读取中'}</td>
   <td className="p-3 text-slate-400">{kind==='maker'?`Binance ${m.source_symbol} · ${'预检：'+readinessText(m)}`:`${fc.interval_min_seconds}–${fc.interval_max_seconds} 秒 · ${fc.min_quote}–${fc.max_quote} Quote`}</td>
   <td className="p-3"><div className="flex justify-end gap-3 whitespace-nowrap"><button className="text-emerald-300" onClick={()=>setEdit(m.symbol)}>编辑</button><button disabled={!!busy} onClick={()=>control(m.symbol,true)}>启动</button><button disabled={!!busy} onClick={()=>control(m.symbol,false)}>{kind==='maker'?'停止并撤单':'停止'}</button></div></td>
  </tr>;})}
  {!markets.length&&<tr><td className="p-5 text-slate-400" colSpan={5}>正在读取币对列表…</td></tr>}
  </tbody></table></div>
 </section>;
}
