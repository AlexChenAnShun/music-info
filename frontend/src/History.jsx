import React, { useEffect, useState } from "react";
import { request } from "./api";
import { useApp } from "./AppContext";
import { Alert, Button } from "./common";
import { date } from "./model";
import { FIELDS } from "./workflow";
import { Diff } from "./Workbench";
export default function History({ active }) {
  const app = useApp(),
    [rows, setRows] = useState([]),
    [error, setError] = useState("");
  async function refresh(signal) {
    try {
      const d = await request(
        "/api/history" +
          (app.historyPath
            ? "?path=" + encodeURIComponent(app.historyPath)
            : ""),
        { signal },
      );
      setRows(d.versions || []);
      setError("");
    } catch (e) {
      if (e.name !== "AbortError") setError(e.message);
    }
  }
  useEffect(() => {
    if (active) {
      const ctl = new AbortController();
      refresh(ctl.signal);
      return () => ctl.abort();
    }
  }, [active, app.historyPath]);
  async function restore(row) {
    try {
      const p = await request(
        "/api/history/" + encodeURIComponent(row.id) + "/preview",
      );
      app.confirm({
        title: "确认回退到这次修改之前？",
        description:
          "恢复标签、封面与外置歌词，音频本体不变；回退本身也会创建新的历史记录。",
        content: (
          <>
            <p className="rx-path">
              {p.path} → {p.target_path}
            </p>
            <Diff rows={p.changes || []} />
          </>
        ),
        label: "确认回退",
        run: async () => {
          await request(
            "/api/history/" + encodeURIComponent(row.id) + "/restore",
            { body: { expected_version: p.file_version, confirmed: true } },
          );
          await refresh();
          app.notify(
            "回退完成，新的历史记录已保存。重新打开曲目读取最新标签。",
          );
        },
      });
    } catch (e) {
      setError(e.message);
    }
  }
  function details(row) {
    const diff = FIELDS.filter(
      (k) =>
        String(row.before_tags?.[k] ?? "") !==
        String(row.after_tags?.[k] ?? ""),
    ).map((key) => ({
      key,
      before: row.before_tags?.[key] || "",
      after: row.after_tags?.[key] || "",
    }));
    app.confirm({
      title: "修改记录详情",
      description: "仅查看，不会写入或回退。",
      content: (
        <>
          <Diff rows={diff} />
          <p className="rx-path">
            {row.before_path} → {row.after_path}
          </p>
          <p>
            封面：{row.before_tags?.has_cover ? "有" : "无"} →{" "}
            {row.after_tags?.has_cover ? "有" : "无"}
          </p>
          {row.error && <Alert error>{row.error}</Alert>}
        </>
      ),
      label: "关闭",
      run: async () => {},
    });
  }
  return (
    <section className={!active ? "hidden" : ""} data-page="history">
      <div className="page-heading">
        <div>
          <span className="eyebrow">METADATA VERSIONS</span>
          <h2>修改记录与回退</h2>
          <p>历史只备份元数据、封面、外置歌词，不复制音频本体。</p>
        </div>
        <div className="btn-row">
          <Button
            onClick={() => {
              app.historyPath ? app.setHistoryPath(null) : refresh();
            }}
          >
            查看全部记录
          </Button>
          <Button onClick={() => refresh()}>刷新</Button>
        </div>
      </div>
      {error && <Alert error>{error}</Alert>}
      {app.historyPath && (
        <p className="rx-path">当前曲目：{app.historyPath}</p>
      )}
      <section className="surface list-panel">
        {rows.length ? (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>文件</th>
                  <th>时间 · JST</th>
                  <th>类型 / 状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td className="rx-path">
                      {(r.after_path || r.before_path || "").split("/").at(-1)}
                    </td>
                    <td>{date(r.created)}</td>
                    <td>
                      {r.kind}
                      <br />
                      {r.status}
                    </td>
                    <td>
                      <div className="btn-row">
                        <Button onClick={() => details(r)}>查看差异</Button>
                        {["completed", "partial"].includes(r.status) && (
                          <Button onClick={() => restore(r)}>回退</Button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="detail-empty">
            暂无修改记录。只有实际提交才会创建历史。
          </p>
        )}
      </section>
    </section>
  );
}
