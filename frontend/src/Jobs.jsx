import React, { useEffect, useState } from "react";
import { request } from "./api";
import { useApp } from "./AppContext";
import { Alert, Button } from "./common";
import { date } from "./model";
import { JOB_KINDS, JOB_STATES, terminal } from "./workflow";
export default function Jobs({ active }) {
  const app = useApp(),
    [jobs, setJobs] = useState([]),
    [id, setId] = useState(null),
    [job, setJob] = useState(null),
    [error, setError] = useState("");
  async function refresh(signal) {
    try {
      const d = await request("/api/tasks", { signal });
      setJobs(d.jobs || []);
      if (id) {
        const j = await request("/api/tasks/" + encodeURIComponent(id), {
          signal,
        });
        setJob(j);
      }
      setError("");
    } catch (e) {
      if (e.name !== "AbortError") setError(e.message);
    }
  }
  useEffect(() => {
    if (!active) return;
    const ctl = new AbortController();
    let timer;
    async function tick() {
      await refresh(ctl.signal);
      if (!ctl.signal.aborted) timer = setTimeout(tick, 3000);
    }
    tick();
    return () => {
      ctl.abort();
      clearTimeout(timer);
    };
  }, [active, id]);
  async function stop() {
    try {
      await request("/api/tasks/" + encodeURIComponent(job.id) + "/cancel", {
        method: "POST",
      });
      app.notify("已请求安全停止；已完成操作不会自动撤销");
      refresh();
    } catch (e) {
      setError(e.message);
    }
  }
  function retry() {
    if (job.kind.startsWith("organize")) return app.navigate("organize");
    app.confirm({
      title: "重新生成匹配预览？",
      description:
        "只重新匹配这些曲目，不重放之前的写入。最多 50 首，不会修改音乐。",
      content: <p>{job.items?.length || 0} 首曲目</p>,
      label: "创建只读预览",
      run: async () => {
        const paths = (job.items || []).map((i) => i.path);
        if (paths.length > 50) throw new Error("单批最多 50 首");
        const d = await request("/api/tasks", {
          body: {
            kind: "preview",
            paths,
            options: {
              source: app.prefs.source,
              include_lyrics: app.prefs.includeLyrics,
              only_empty: app.prefs.onlyEmpty,
              include_cover: app.prefs.includeCover,
            },
          },
        });
        setId(d.job_id);
        app.notify("已重新创建匹配预览，可在任务中心查看");
      },
    });
  }
  return (
    <section className={!active ? "hidden" : ""} data-page="tasks">
      <div className="page-heading">
        <div>
          <span className="eyebrow">PERSISTENT JOBS</span>
          <h2>后台任务中心</h2>
          <p>任务与日志保存在 NAS；写入失败不会自动重放。</p>
        </div>
        <Button onClick={() => refresh()}>刷新任务</Button>
      </div>
      {error && <Alert error>{error}</Alert>}
      <div className="jobs-layout">
        <section className="surface list-panel">
          <h3>最近任务</h3>
          {jobs.length ? (
            <div className="table-wrap">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>任务 / 时间 · JST</th>
                    <th>状态</th>
                    <th>已处理</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((j) => (
                    <tr key={j.id}>
                      <td>
                        <b>{JOB_KINDS[j.kind] || j.kind}</b>
                        <small className="block-text">{date(j.created)}</small>
                      </td>
                      <td>
                        <span className="pill purple">
                          {JOB_STATES[j.status] || j.status}
                        </span>
                      </td>
                      <td>
                        {(j.counts?.completed || 0) + (j.counts?.failed || 0)} /{" "}
                        {j.total}
                      </td>
                      <td>
                        <Button onClick={() => setId(j.id)}>查看</Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="detail-empty">暂无后台任务</p>
          )}
        </section>
        <aside className="surface job-detail">
          <h3>任务详情</h3>
          {job ? (
            <>
              <div className="head-line">
                <b>{JOB_KINDS[job.kind] || job.kind}</b>
                <span className="pill">
                  {JOB_STATES[job.status] || job.status}
                </span>
              </div>
              <p>
                {date(job.created)} · {job.total} 个项目
              </p>
              <div className="btn-row">
                {job.kind === "preview" && terminal(job.status) && (
                  <Button
                    onClick={() => {
                      app.setImportJob(job);
                      app.navigate("work");
                    }}
                  >
                    读取整理预览
                  </Button>
                )}
                {!terminal(job.status) ? (
                  <Button onClick={stop}>安全停止</Button>
                ) : (
                  <Button onClick={retry}>
                    {job.kind.startsWith("organize")
                      ? "到自动整理查看"
                      : "重新生成预览"}
                  </Button>
                )}
              </div>
              <div className="job-items">
                {job.items?.map((item, i) => (
                  <p key={i}>
                    <b className="rx-path">{item.path}</b>
                    <br />
                    {JOB_STATES[item.status] || item.status}
                    {item.error ? " · " + item.error : ""}
                    {item.result &&
                    !item.result.candidate &&
                    job.kind === "preview"
                      ? " · 无候选"
                      : ""}
                  </p>
                ))}
              </div>
              <h4>操作日志</h4>
              <div className="job-log">
                {job.logs?.map((l, i) => (
                  <p key={i}>
                    {date(l.at)} [{l.level}] {l.message}
                  </p>
                ))}
              </div>
            </>
          ) : (
            <p className="detail-empty">选择任务查看进度与日志</p>
          )}
        </aside>
      </div>
    </section>
  );
}
