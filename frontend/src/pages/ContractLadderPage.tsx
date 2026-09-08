import { lazy, Suspense } from 'react';
const modules = import.meta.glob('../strategy_plugins/*/ContractLadderPage.tsx');
const load = modules['../strategy_plugins/contract_ladder/ContractLadderPage.tsx'];
const Page = load ? lazy(async () => ({default: (await load() as {ContractLadderPage: React.ComponentType<{initialSymbol?:string;embedded?:boolean}>}).ContractLadderPage})) : null;
export function ContractLadderPage(props:{initialSymbol?:string;embedded?:boolean}) {
 return Page ? <Suspense fallback={<p>加载中…</p>}><Page {...props}/></Suspense> : <p>此分享包未安装该策略。</p>;
}
