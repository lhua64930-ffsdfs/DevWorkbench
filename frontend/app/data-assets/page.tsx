"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { apiGet, apiPost } from "@/lib/api";
import type { Repository } from "@/lib/types";

const kinds = [
  { id: "documents", label: "上传文档", href: "/rag" },
  { id: "issues", label: "Issue", href: "/issues" },
  { id: "pull_requests", label: "PR", href: "/pull-requests" },
  { id: "workflow_runs", label: "CI 运行", href: "/workflow-runs" },
] as const;
const PAGE_SIZE = 25;
const statuses: Record<string, string> = {
  not_started: "未同步", missing: "未索引", completed: "已完成", ready: "就绪", success: "成功",
  queued: "排队中", parsing: "解析中", splitting: "分块中", embedding: "生成向量中",
  failed: "失败", failure: "失败", partial: "部分完成", building: "构建中", open: "开放", closed: "已关闭",
};

type Catalog = {
  counts: Record<string, number>;
  document_status: Record<string, number>;
  matched: number;
  source: { name: string; provider: string; status: string; last_sync_at?: string };
  project_index: { status: string; docs_indexed: number; docs_total: number; last_scan_at?: string } | null;
  items: Array<{
    id: string; title: string; status: string; updated_at?: string;
    checksum?: string; chunk_count?: number; commit?: string; number?: number;
  }>;
};

function date(value?: string) {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

export default function DataAssetsPage() {
  const [repos, setRepos] = useState<Repository[]>([]);
  const [repoId, setRepoId] = useState("");
  const [kind, setKind] = useState<(typeof kinds)[number]>(kinds[0]);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [loadingRepos, setLoadingRepos] = useState(true);

  useEffect(() => {
    let current = true;
    apiGet<Repository[]>("/api/repos").then((items) => {
      if (!current) return;
      setRepos(items);
      setRepoId(items[0]?.id || "");
    }).catch((reason) => {
      if (current) setError(reason.message);
    }).finally(() => {
      if (current) setLoadingRepos(false);
    });
    return () => { current = false; };
  }, []);

  useEffect(() => {
    if (!repoId) return;
    let current = true;
    setCatalog(null);
    setError("");
    const params = new URLSearchParams({ kind: kind.id, query, offset: String(offset), limit: String(PAGE_SIZE) });
    apiGet<Catalog>(`/api/data-assets/${encodeURIComponent(repoId)}?${params}`).then((value) => {
      if (current) setCatalog(value);
    }).catch((reason) => {
      if (current) setError(reason.message);
    });
    return () => { current = false; };
  }, [repoId, kind, query, offset, refresh]);

  const field = "rounded-md border border-line bg-white px-3 py-2 text-sm";
  return (
    <div className="mx-auto max-w-6xl space-y-5 px-3 py-5">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink">数据中台</h1>
          <p className="mt-1 text-sm text-slate-600">查看仓库的数据资产、同步时间和索引进度。</p>
        </div>
        <label className="flex items-center gap-2 text-sm">
          仓库
          <select className={field} value={repoId} onChange={(event) => {
            setCatalog(null); setRepoId(event.target.value); setOffset(0);
          }}>
            <option value="" disabled>选择仓库</option>
            {repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.full_name}</option>)}
          </select>
        </label>
      </header>

      {error && <p role="alert" className="rounded-md bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
      {loadingRepos && <p role="status" className="text-sm text-slate-600">正在读取仓库…</p>}
      {!loadingRepos && !repoId && !error && <Link className="text-sm text-teal-800 underline" href="/repos">先连接一个仓库</Link>}
      {repoId && (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="rounded-md border border-line bg-white p-4">
              <h2 className="text-sm font-semibold text-ink">仓库同步</h2>
              <p className="mt-2 text-sm text-slate-600">
                {catalog ? statuses[catalog.source.status] || catalog.source.status : "—"} · 最近同步 {date(catalog?.source.last_sync_at)}
              </p>
            </div>
            <div className="rounded-md border border-line bg-white p-4">
              <h2 className="text-sm font-semibold text-ink">项目索引</h2>
              <p className="mt-2 text-sm text-slate-600">
                {catalog?.project_index ? `${statuses[catalog.project_index.status] || catalog.project_index.status} · 已索引 ${catalog.project_index.docs_indexed}/${catalog.project_index.docs_total} · ${date(catalog.project_index.last_scan_at)}` : catalog ? "暂无索引记录" : "—"}
              </p>
            </div>
          </div>
          <SnapshotPanel key={repoId} repoId={repoId} />
          <section className="rounded-md border border-line bg-white p-4">
            <div className="flex flex-wrap items-center gap-2">
              {kinds.map((item) => (
                <button key={item.id} type="button" aria-pressed={kind.id === item.id}
                  className={`rounded-md border px-3 py-2 text-sm ${kind.id === item.id ? "border-teal-300 bg-teal-50 text-teal-900" : "border-line text-slate-600"}`}
                  onClick={() => { if (kind.id !== item.id) { setCatalog(null); setKind(item); setOffset(0); } }}>
                  {item.label} {catalog?.counts[item.id] ?? "—"}
                </button>
              ))}
              <Link className="ml-auto text-sm text-teal-800 underline" href={kind.href}>打开{kind.label}页面</Link>
            </div>
            {kind.id === "documents" && catalog && (
              <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600">
                {Object.entries(catalog.document_status).map(([key, count]) => <span key={key}>{statuses[key] || key} {count}</span>)}
              </div>
            )}
            <form className="my-4 flex flex-wrap gap-2" onSubmit={(event) => {
              event.preventDefault(); setQuery(search.trim()); setOffset(0); setRefresh((value) => value + 1);
            }}>
              <input aria-label="按标题搜索资产" placeholder="按标题搜索" className={field} value={search} maxLength={200}
                onChange={(event) => setSearch(event.target.value)} />
              <button className={field} type="submit">搜索</button>
              <button className={field} type="button" onClick={() => setRefresh((value) => value + 1)}>刷新</button>
            </form>
            {!catalog && !error && <p role="status" className="py-4 text-sm text-slate-600">正在读取数据资产…</p>}
            {catalog && catalog.items.length === 0 && <p className="py-4 text-sm text-slate-600">没有匹配的数据资产。</p>}
            {catalog && catalog.items.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="bg-slate-50 text-slate-600">
                    <tr>{["标题", "状态", "最近更新", "来源记录"].map((name) => <th className="px-3 py-2" key={name}>{name}</th>)}</tr>
                  </thead>
                  <tbody>
                    {catalog.items.map((item) => (
                      <tr key={item.id} className="border-t border-line align-top">
                        <td className="px-3 py-2 font-medium text-ink">{item.number ? `#${item.number} ` : ""}{item.title}</td>
                        <td className="px-3 py-2">{statuses[item.status] || item.status}</td>
                        <td className="whitespace-nowrap px-3 py-2 text-slate-600">{date(item.updated_at)}</td>
                        <td className="px-3 py-2 text-xs text-slate-600">
                          {item.checksum ? <span title={item.checksum}>SHA-256 {item.checksum.slice(0, 12)} · {item.chunk_count} 个分块</span> : item.commit?.slice(0, 12) || "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {catalog && (offset > 0 || catalog.matched > PAGE_SIZE) && (
              <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
                <span>共 {catalog.matched} 条 · 第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
                <button className={`${field} disabled:opacity-40`} disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>上一页</button>
                <button className={`${field} disabled:opacity-40`} disabled={offset + PAGE_SIZE >= catalog.matched} onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

type SnapshotList = {
  active_id: string | null;
  items: { id: string; label: string; sha256: string; created_at: string; chunk_count: number; vector_count: number }[];
};

function SnapshotPanel({ repoId }: { repoId: string }) {
  const [snapshots, setSnapshots] = useState<SnapshotList | null>(null);
  const [label, setLabel] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [evalId, setEvalId] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const path = `/api/data-assets/${encodeURIComponent(repoId)}/snapshots`;
  const field = "rounded-md border border-line bg-white px-3 py-2 text-sm disabled:opacity-50";

  useEffect(() => {
    apiGet<SnapshotList>(path).then(setSnapshots).catch((reason) => setError(reason.message));
  }, [path]);

  async function create(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      const row = await apiPost<SnapshotList["items"][number]>(path, { label: label.trim() });
      setSnapshots((current) => ({ active_id: current?.active_id ?? null, items: [row, ...(current?.items ?? [])] }));
      setSelectedId(row.id);
      setLabel("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "创建快照失败");
    } finally {
      setPending(false);
    }
  }

  async function activate() {
    if (!snapshots) return;
    setPending(true);
    setError("");
    try {
      const result = await apiPost<{ active_id: string | null }>(`${path}/select`, {
        snapshot_id: selectedId || null, eval_id: evalId.trim() || null,
      });
      setSnapshots({ ...snapshots, active_id: result.active_id });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "切换索引失败");
    } finally {
      setPending(false);
    }
  }

  const selected = snapshots?.items.find((row) => row.id === selectedId);
  const active = snapshots?.items.find((row) => row.id === snapshots.active_id);
  return (
    <section className="space-y-3 rounded-md border border-line bg-white p-4">
      <div>
        <h2 className="text-sm font-semibold text-ink">知识快照与索引切换</h2>
        <p className="mt-1 text-sm text-slate-600">当前索引 {active?.label ?? (snapshots ? "实时索引" : "正在读取…")}</p>
      </div>
      <form className="flex flex-wrap gap-2" onSubmit={create}>
        <input className={field} required maxLength={160} aria-label="快照名称" placeholder="快照名称" value={label} onChange={(event) => setLabel(event.target.value)} />
        <button className={field} disabled={pending || !snapshots || !label.trim()}>创建快照</button>
      </form>
      {snapshots && (
        <div className="flex flex-wrap gap-2">
          <select className={field} value={selectedId} aria-label="选择索引版本" onChange={(event) => setSelectedId(event.target.value)}>
            <option value="">实时索引</option>
            {snapshots.items.map((row) => <option key={row.id} value={row.id}>{row.label} · {date(row.created_at)}</option>)}
          </select>
          <input className={field} aria-label="发布门禁评测 ID" placeholder="发布门禁评测 ID" value={evalId} onChange={(event) => setEvalId(event.target.value)} />
          <button type="button" className={field} onClick={activate} disabled={pending || (selectedId || null) === snapshots.active_id}>切换索引</button>
        </div>
      )}
      {selected && <p className="text-xs text-slate-600" title={selected.sha256}>{selected.chunk_count} 个分块 · {selected.vector_count} 个向量 · SHA-256 {selected.sha256.slice(0, 12)}</p>}
      {error && <p role="alert" className="text-sm text-rose-800">{error}</p>}
    </section>
  );
}
