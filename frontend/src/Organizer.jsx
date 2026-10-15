import React, { useEffect, useRef, useState } from "react";
import { request } from "./api";
import {
  bytes,
  canApply,
  counts,
  date,
  DIRS,
  pendingCount,
  STATES,
  TABS,
  WASH,
} from "./model";
import { Alert, Button } from "./common";
export default function Organizer({
  status,
  plans,
  plan,
  setPlan,
  active,
  busy,
  onRefresh,
  onSettings,
  onScan,
  onApply,
  onDiscard,
  onStop,
  onOpen,
}) {
  const [kind, setKind] = useState("move"),
    [items, setItems] = useState([]),
    [total, setTotal] = useState(0),
    [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const generation = useRef(0),
    inFlight = useRef(false),
    abort = useRef(null);
  useEffect(() => {
    setKind("move");
  }, [plan?.plan_id]);
  const id = plan?.plan_id,
    version = JSON.stringify([plan?.status, plan?.applied, plan?.counts]);
  async function loadItems(append = false) {
    if (!id || plan.status === "scanning" || (append && inFlight.current))
      return;
    const n = append ? generation.current : ++generation.current;
    inFlight.current = true;
    if (!append) {
      abort.current?.abort();
      abort.current = new AbortController();
      setItems([]);
    }
    setLoading(true);
    setError("");
    try {
      const query = new URLSearchParams({
        view: "full",
        kind: kind === "junk" ? "junk,error" : kind,
        offset: String(append ? items.length : 0),
        limit: "100",
      });
      const d = await request(
        "/api/organize/plan/" + encodeURIComponent(id) + "?" + query,
        { signal: abort.current.signal },
      );
      if (n !== generation.current) return;
      setItems((previous) =>
        append ? [...previous, ...(d.items || [])] : d.items || [],
      );
      setTotal(d.total || 0);
    } catch (e) {
      if (n === generation.current && e.name !== "AbortError")
        setError(e.message);
    } finally {
      if (n === generation.current) {
        inFlight.current = false;
        setLoading(false);
      }
    }
  }
  useEffect(() => {
    loadItems();
    return () => {
      generation.current++;
      abort.current?.abort();
      inFlight.current = false;
    };
  }, [id, kind, version]);
  const c = counts(plan),
    paths = status.paths || {},
    cfg = status.settings || {};
  return (
    <div className="rx-scope" data-react-section="organizer">
      <div className="page-heading">
        <div>
          <span className="eyebrow">LOCAL LIBRARY ORGANIZER · REACT</span>
          <h2>自动整理</h2>
          <p>先扫描、核对清单，再确认执行。只移动，不删除、不覆盖。</p>
        </div>
        <div className="btn-row">
          <Button disabled={busy} onClick={onRefresh}>
            刷新
          </Button>
          <Button onClick={onSettings}>整理设置</Button>
          <Button
            primary
            disabled={!!active || busy || !!status.problems?.length}
            onClick={onScan}
          >
            扫描待整理
          </Button>
        </div>
      </div>
      <div className="stat-strip">
        {DIRS.map(([key, label]) => (
          <div className="stat-box" key={key}>
            {label}
            <strong>{paths[key]?.files ?? "—"}</strong>
            <span className="org-stat-line">{bytes(paths[key]?.bytes)}</span>
          </div>
        ))}
      </div>
      {status.problems?.length > 0 && (
        <Alert error>
          {status.problems.map((r, i) => (
            <div key={i}>{r.reason}</div>
          ))}
        </Alert>
      )}
      {!status.media_writable && (
        <Alert error>
          音乐库只读：可以查看计划，但不能执行整理。请先核对 Docker 可写挂载。
        </Alert>
      )}
      {active && (
        <Alert>
          后台任务进行中：
          {active.kind === "organize_apply" ? "整理执行" : "只读扫描"}
          。离开页面不会停止任务。
          <Button disabled={busy} onClick={onStop}>
            安全停止
          </Button>
        </Alert>
      )}
      <div className="org-grid">
        <section className="surface org-card">
          <div className="panel-title">
            <div>
              <h3>整理规则</h3>
              <span className="small-label">
                {status.media_writable ? "音乐库可写" : "音乐库只读"} ·
                配置只影响之后的扫描
              </span>
            </div>
            <Button onClick={onSettings}>在设置中修改</Button>
          </div>
          <div className="org-rule-list">
            {DIRS.map(([k, label]) => (
              <div className="org-rule" key={k}>
                <span>{label}</span>
                <b>
                  {paths[k]?.abs || cfg[k] || "—"}
                  {paths[k]?.host && <i>NAS：{paths[k].host}</i>}
                </b>
              </div>
            ))}
            {[
              [
                "命名模板",
                (status.effective_name_template || "{title}") +
                  " · " +
                  (cfg.dir_template || "{artist}/{album}"),
              ],
              ["二级分类", cfg.classify_rules ? "已启用（YAML）" : "未启用"],
              ["洗版策略", WASH[cfg.wash_mode] || "—"],
              [
                "附属文件",
                cfg.follow_sidecars
                  ? "跟随歌曲并同步改名"
                  : "不跟随（进冗余数据）",
              ],
              [
                "未知占位名",
                (cfg.unknown_artist || "未知歌手") +
                  " / " +
                  (cfg.unknown_album || "未知专辑"),
              ],
            ].map(([label, value]) => (
              <div className="org-rule" key={label}>
                <span>{label}</span>
                <b>{value}</b>
              </div>
            ))}
          </div>
          <p className="helper">
            逐字节相同（SHA-256）才视为重复。无法识别的文件进入冗余目录；不自动删除。所有文件操作必须经过计划确认。
          </p>
        </section>
        <section className="surface list-panel org-card">
          <div className="panel-title">
            <h3>整理计划</h3>
            <span className="small-label">最近 {plans.length} 个计划</span>
          </div>
          {plans.length ? (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>计划 / 创建时间 · JST</th>
                    <th>状态 / 内容</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {plans.map((p) => (
                    <tr key={p.plan_id}>
                      <td>
                        <b className="rx-path">{p.plan_id}</b>
                        <div className="small-label">{date(p.created)}</div>
                      </td>
                      <td>
                        <span className="pill purple">
                          {STATES[p.status] || p.status}
                        </span>
                        <div className="small-label">
                          移动 {counts(p).move} · 洗版 {counts(p).wash} · 重复{" "}
                          {counts(p).duplicate} · 冗余 {counts(p).junk}
                        </div>
                      </td>
                      <td>
                        <Button
                          disabled={busy}
                          aria-pressed={id === p.plan_id}
                          onClick={() => onOpen(p.plan_id)}
                        >
                          查看
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="detail-empty">
              暂无计划。扫描待整理目录可生成只读预览。
            </p>
          )}
        </section>
      </div>
      {plan && (
        <section className="surface org-card" data-testid="plan-detail">
          <div className="org-plan-head">
            <div>
              <span className="eyebrow rx-path">PLAN · {id}</span>
              <h3 className="rx-path">源目录：{plan.cfg?.source || "—"}</h3>
              <div className="org-count-pills">
                <span className="pill purple">
                  {STATES[plan.status] || plan.status}
                </span>
                {TABS.map(([k, label]) => (
                  <span className="pill" key={k}>
                    {label} {c[k]}
                  </span>
                ))}
              </div>
            </div>
            <div className="org-actions">
              <Button
                primary
                disabled={
                  busy || !canApply(plan, status.media_writable, active)
                }
                onClick={onApply}
              >
                执行整理（{pendingCount(plan)} 项）
              </Button>
              <Button
                disabled={
                  busy ||
                  !!active ||
                  plan.status === "scanning" ||
                  plan.status === "applying"
                }
                onClick={onDiscard}
              >
                丢弃计划
              </Button>
            </div>
          </div>
          {plan.status === "scanning" || plan.status === "applying" ? (
            <Alert>
              <div className="rx-indeterminate" aria-label="任务进行中" />
              <p>
                {plan.status === "scanning"
                  ? "扫描只读，不会移动文件。已扫描 " +
                    (plan.scanned || 0) +
                    " 个文件。"
                  : "正在执行已确认的计划，可安全停止剩余项。"}
              </p>
            </Alert>
          ) : (
            <Alert>
              {plan.status === "done"
                ? "本计划已完成；以下为实际去向。"
                : plan.status === "failed" || plan.status === "cancelled"
                  ? plan.note || "计划未完成，请检查日志后重新扫描。"
                  : "核对每个文件去向后再执行；版本冲突或失败会保留原文件并显示原因。"}
            </Alert>
          )}
          {!!plan.applied && (
            <p className="small-label">
              已执行：移动 {plan.applied.move || 0} · 洗版{" "}
              {plan.applied.wash || 0} · 重复 {plan.applied.duplicate || 0} ·
              冗余 {plan.applied.junk || 0} · 失败 {plan.applied.failed || 0}
            </p>
          )}
          <div className="org-tabs" role="tablist" aria-label="计划分类">
            {TABS.map(([k, label]) => (
              <button
                className={"org-tab" + (kind === k ? " active" : "")}
                role="tab"
                aria-selected={kind === k}
                key={k}
                onClick={() => setKind(k)}
              >
                {label}
                <span className="n">{c[k]}</span>
              </button>
            ))}
          </div>
          {error && (
            <Alert error>
              {error}
              <Button onClick={() => loadItems()}>重试</Button>
            </Alert>
          )}
          <div aria-live="polite">
            {items.map((r, i) => (
              <div className="queue-item rx-item" key={r.ord ?? r.src ?? i}>
                <div className="queue-item-top">
                  <b className="org-name">{r.name}</b>
                  <span className="pill">
                    {{
                      pending: "待执行",
                      done: "已完成",
                      failed: "失败",
                      skipped: "已跳过",
                      attached: "随歌曲移动",
                    }[r.status] || r.status}
                  </span>
                </div>
                <div className="org-src">{r.src}</div>
                {(r.actual || r.target) && (
                  <div className="org-dest">→ {r.actual || r.target}</div>
                )}
                <div className="org-reason">
                  {r.reason}
                  {r.error ? " · " + r.error : ""}
                </div>
                {r.sidecars?.length > 0 && (
                  <div className="org-reason">
                    附属：
                    {r.sidecars
                      .map(
                        (s) => s.name + (s.error ? "（" + s.error + "）" : ""),
                      )
                      .join("、")}
                  </div>
                )}
              </div>
            ))}
            {loading ? (
              <p role="status">读取明细中…</p>
            ) : (
              !items.length &&
              !error && <p className="detail-empty">这一分类暂无项目。</p>
            )}
            {items.length < total && (
              <Button disabled={loading} onClick={() => loadItems(true)}>
                加载更多（{items.length} / {total}）
              </Button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
