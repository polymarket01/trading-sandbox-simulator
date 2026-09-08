import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { ConfirmDialog, EmptyState, SkeletonBlock, StatusPill, TabBar } from "../components/ui";
import { bjDateTime, fmt, signedNumber, sideColor } from "../lib/format";
import { useAppStore } from "../store/useAppStore";

type Run = { run_id: string; account_epoch: number; status: string; reason: string; created_at: number; ended_at?: number | null };
type LedgerEntry = { entry_id: string; asset?: string; margin_asset?: string; change_type: string; amount: string; balance_after?: string; wallet_after?: string; available_after?: string; created_at: number };

const CHANGE_TYPE_LABEL: Record<string, string> = {
  freeze: "冻结",
  unfreeze: "解冻",
  trade_debit: "成交扣减",
  trade_credit: "成交入账",
  fee: "手续费",
  reset: "账户复位",
  order_margin_reserve: "订单保证金占用",
  order_margin_release: "订单保证金释放",
  position_margin_increase: "持仓保证金增加",
  position_margin_release: "持仓保证金释放",
  realized_pnl: "已实现盈亏",
  funding: "资金费率",
  liquidation: "强平结算",
  insurance_transfer: "保险基金",
  adl: "ADL 减仓",
};

export function PaperAccountPage() {
  const accountMeta = useAppStore((state) => state.paperAccountMeta);
  const authSession = useAppStore((state) => state.authSession);
  const pushToast = useAppStore((state) => state.pushToast);
  const [runs, setRuns] = useState<Run[]>([]);
  const [ledger, setLedger] = useState<{ spot: LedgerEntry[]; contract: LedgerEntry[] }>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [ledgerTab, setLedgerTab] = useState<"spot" | "contract">("spot");
  const [resetOpen, setResetOpen] = useState(false);
  const [resetBusy, setResetBusy] = useState(false);
  const [pwOpen, setPwOpen] = useState(false);
  const [pwBusy, setPwBusy] = useState(false);
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

  const refresh = useCallback(async () => {
    const [runResponse, ledgerResponse] = await Promise.all([
      api.get<{ items: Run[] }>("/paper/account/runs"),
      api.get<{ spot: LedgerEntry[]; contract: LedgerEntry[] }>("/paper/ledger"),
    ]);
    setRuns(runResponse.items);
    setLedger(ledgerResponse);
    setLoading(false);
    setError("");
  }, []);

  useEffect(() => {
    void refresh().catch((reason) => {
      setError(reason instanceof Error ? reason.message : "账户历史读取失败");
      setLoading(false);
    });
  }, [refresh]);

  const mergedLedger = useMemo(() => {
    const entries = [
      ...(ledger?.spot ?? []).map((entry) => ({ ...entry, product: "现货" as const, productAsset: entry.asset ?? "—" })),
      ...(ledger?.contract ?? []).map((entry) => ({ ...entry, product: "永续" as const, productAsset: entry.margin_asset ?? "—" })),
    ];
    return entries.sort((a, b) => Number(b.created_at ?? 0) - Number(a.created_at ?? 0));
  }, [ledger]);

  const activeRun = runs.find((item) => item.status === "active");
  const totalAmount = mergedLedger.slice(0, 200).reduce((sum, item) => sum + Number(item.amount ?? 0), 0);

  const doReset = async () => {
    setResetBusy(true);
    try {
      await api.post("/paper/account/reset", { reason: "user_account_reset" });
      pushToast("success", "模拟账户已复位，已开启新的账户运行批次");
      setResetOpen(false);
      await refresh();
    } catch (reason) {
      pushToast("error", `复位失败：${reason instanceof Error ? reason.message : "未知错误"}`);
      setResetOpen(false);
    } finally {
      setResetBusy(false);
    }
  };

  const changePassword = async () => {
    if (newPassword !== confirmPassword) {
      pushToast("error", "两次输入的新密码不一致");
      return;
    }
    if (!newPassword) {
      pushToast("error", "新密码不能为空");
      return;
    }
    setPwBusy(true);
    try {
      await api.post("/auth/change-password", { current_password: oldPassword, new_password: newPassword });
      pushToast("success", "密码已修改，请使用新密码登录");
      setPwOpen(false);
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
    } catch (reason) {
      pushToast("error", `修改失败：${reason instanceof Error ? reason.message : "未知错误"}`);
    } finally {
      setPwBusy(false);
    }
  };

  return (
    <>
      <div className="space-y-3">
        <section className="panel rounded-2xl p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h1 className="font-display text-xl text-white">账户</h1>
              <p className="mt-1 text-xs text-slate-500">Paper 模拟账户信息、运行批次与资金流水。</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" onClick={() => setPwOpen(true)} className="rounded-xl bg-white/6 px-3 py-2 text-xs text-slate-300 hover:bg-white/10">
                修改密码
              </button>
              <button type="button" onClick={() => setResetOpen(true)} className="rounded-xl border border-amber-300/20 bg-amber-400/10 px-3 py-2 text-xs text-amber-100 hover:bg-amber-400/20">
                复位模拟账户
              </button>
            </div>
          </div>
          {error ? <div className="mt-3 rounded-xl bg-rose-400/10 px-3 py-2 text-xs text-rose-200">{error}</div> : null}
          <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <InfoCard label="用户名" value={accountMeta?.username ?? authSession?.username ?? "-"} />
            <InfoCard label="角色" value={accountMeta?.role === "admin" ? "管理员" : "普通用户"} />
            <InfoCard label="当前账户 epoch" value={String(accountMeta?.account_epoch ?? "-")} />
            <InfoCard label="当前 run" value={activeRun?.run_id ?? accountMeta?.account_run_id ?? "-"} mono small />
          </div>
        </section>

        <div className="grid gap-3 xl:grid-cols-2">
          <section className="panel rounded-2xl p-4">
            <div className="flex items-center justify-between">
              <h2 className="font-display text-base text-slate-100">账户运行批次</h2>
              <span className="text-[11px] text-slate-500">{runs.length} 个</span>
            </div>
            <div className="mt-3 space-y-2">
              {loading ? (
                <div className="space-y-2">
                  {Array.from({ length: 4 }).map((_, index) => (
                    <SkeletonBlock key={index} className="h-14" />
                  ))}
                </div>
              ) : runs.length === 0 ? (
                <EmptyState compact title="暂无运行批次" />
              ) : (
                runs.slice(0, 20).map((run) => (
                  <div key={run.run_id} className={`rounded-xl border px-3 py-2.5 ${run.status === "active" ? "border-emerald-300/20 bg-emerald-400/5" : "border-white/8 bg-white/[0.03]"}`}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-mono text-sm text-slate-200">epoch {run.account_epoch}</span>
                      <StatusPill tone={run.status === "active" ? "buy" : "neutral"}>{run.status === "active" ? "当前运行" : "历史"}</StatusPill>
                    </div>
                    <div className="mt-1 break-all text-[11px] text-slate-500">{run.run_id}</div>
                    <div className="mt-1 text-[11px] text-slate-500">
                      {run.reason} · {bjDateTime(run.created_at)}
                      {run.ended_at ? ` → ${bjDateTime(run.ended_at)}` : ""}
                    </div>
                  </div>
                ))
              )}
            </div>
            <p className="mt-3 text-[11px] leading-5 text-slate-500">每次复位都会结束当前 run 并开启新的 epoch；历史批次记录保留，仅当前批次影响可用余额与仓位。</p>
          </section>

          <section className="panel rounded-2xl p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-display text-base text-slate-100">资金流水</h2>
              <TabBar<"spot" | "contract">
                items={[
                  { key: "spot", label: "现货" },
                  { key: "contract", label: "永续" },
                ]}
                value={ledgerTab}
                onChange={setLedgerTab}
              />
            </div>
            <div className="mt-3 scrollbar max-h-[420px] overflow-auto">
              {loading ? (
                <div className="space-y-2">
                  {Array.from({ length: 6 }).map((_, index) => (
                    <SkeletonBlock key={index} className="h-12" />
                  ))}
                </div>
              ) : mergedLedger.length === 0 ? (
                <EmptyState compact title="暂无资金流水" />
              ) : (
                <table className="w-full text-left text-xs">
                  <thead className="text-[11px] uppercase tracking-[0.12em] text-slate-500">
                    <tr>
                      <th className="pb-2">时间</th>
                      <th className="pb-2">类型</th>
                      <th className="pb-2">资产</th>
                      <th className="pb-2 text-right">变动</th>
                      <th className="pb-2 text-right">变动后余额</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono tabular-nums">
                    {(ledgerTab === "spot" ? mergedLedger.filter((item) => item.product === "现货") : mergedLedger.filter((item) => item.product === "永续"))
                      .slice(0, 120)
                      .map((entry) => (
                        <tr key={entry.entry_id} className="border-t border-white/5">
                          <td className="py-2 text-slate-500">{bjDateTime(entry.created_at)}</td>
                          <td className="py-2 text-slate-300">{CHANGE_TYPE_LABEL[entry.change_type] ?? entry.change_type}</td>
                          <td className="py-2 text-slate-400">{entry.productAsset}</td>
                          <td className={`py-2 text-right ${sideColor(Number(entry.amount) >= 0 ? "buy" : "sell")}`}>{signedNumber(entry.amount, 4)}</td>
                          <td className="py-2 text-right text-slate-300">{fmt(entry.balance_after ?? entry.wallet_after, 4)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="mt-3 flex items-center justify-between text-[11px] text-slate-500">
              <span>展示最近 120 条</span>
              <span>净变动（最近 200 条）{signedNumber(totalAmount, 2)}</span>
            </div>
          </section>
        </div>
      </div>

      <ConfirmDialog
        open={resetOpen}
        title="复位模拟账户"
        danger
        busy={resetBusy}
        confirmText={resetBusy ? "复位中..." : "确认复位"}
        message={<>当前挂单、仓位和本轮收益将清空；现货 USDT 与独立合约钱包各恢复为 100,000,000 USDT，历史记录仍保留在旧运行批次中。</>}
        onConfirm={() => void doReset()}
        onCancel={() => setResetOpen(false)}
      />
      <ConfirmDialog
        open={pwOpen}
        title="修改密码"
        busy={pwBusy}
        confirmText={pwBusy ? "提交中..." : "确认修改"}
        message={
          <div className="space-y-3">
            <label className="block">
              <span className="mb-1 block text-xs text-slate-400">当前密码</span>
              <input type="password" value={oldPassword} onChange={(event) => setOldPassword(event.target.value)} className="paper-input" autoComplete="current-password" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-slate-400">新密码</span>
              <input type="password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} className="paper-input" autoComplete="new-password" />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-slate-400">确认新密码</span>
              <input type="password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} className="paper-input" autoComplete="new-password" />
            </label>
            <p className="text-[11px] text-slate-500">新密码至少 8 位。修改成功后需要重新登录。</p>
          </div>
        }
        onConfirm={() => void changePassword()}
        onCancel={() => {
          setPwOpen(false);
          setOldPassword("");
          setNewPassword("");
          setConfirmPassword("");
        }}
      />
    
    </>
  );
}

function InfoCard({ label, value, mono = false, small = false }: { label: string; value: string; mono?: boolean; small?: boolean }) {
  return (
    <div className="ui-metric">
      <div className="ui-metric-label">{label}</div>
      <div className={`ui-metric-value truncate ${mono ? "" : "font-sans"} ${small ? "text-xs" : ""}`} title={value}>
        {value}
      </div>
    </div>
  );
}
