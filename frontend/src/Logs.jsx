import React, { useEffect, useMemo, useRef, useState } from "react";
import { request } from "./api";
import { Button } from "./common";
const rank = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };
export default function Logs({ active }) {
  const [lines, setLines] = useState([]),
    [level, setLevel] = useState("ALL"),
    [q, setQ] = useState(""),
    [paused, setPaused] = useState(false),
    [follow, setFollow] = useState(true),
    [connection, setConnection] = useState("未连接"),
    [reconnect, setReconnect] = useState(0),
    [error, setError] = useState("");
  const box = useRef(null),
    maxId = useRef(0);
  useEffect(() => {
    if (!active) return;
    const ctl = new AbortController();
    let es,
      live = true;
    async function start() {
      setConnection("读取历史…");
      try {
        const d = await request("/api/logs?limit=400", { signal: ctl.signal });
        if (!live) return;
        setLines((old) => {
          const map = new Map(old.map((x) => [x.id, x]));
          (d.logs || []).forEach((x) => map.set(x.id, x));
          const rows = [...map.values()]
            .sort((a, b) => a.id - b.id)
            .slice(-5000);
          maxId.current = Math.max(
            maxId.current,
            ...rows.map((x) => x.id || 0),
          );
          return rows;
        });
      } catch (e) {
        if (!ctl.signal.aborted) setError(e.message);
      }
      if (!live) return;
      es = new EventSource("/api/logs/stream");
      es.onopen = () => setConnection("已连接");
      es.onerror = () => setConnection("重连中…");
      es.onmessage = (event) => {
        try {
          const line = JSON.parse(event.data);
          if (line.id <= maxId.current) return;
          maxId.current = line.id;
          setLines((old) => [...old, line].slice(-5000));
        } catch {}
      };
    }
    start();
    return () => {
      live = false;
      ctl.abort();
      es?.close();
      setConnection("未连接");
    };
  }, [active, reconnect]);
  const filtered = useMemo(
    () =>
      lines
        .filter(
          (e) =>
            (level === "ALL" || (rank[e.level] ?? 1) >= (rank[level] ?? 0)) &&
            (!q ||
              [e.msg, e.src, e.level]
                .join(" ")
                .toLowerCase()
                .includes(q.toLowerCase())),
        )
        .slice(-1500),
    [lines, level, q],
  );
  const frozen = useRef([]);
  if (!paused) frozen.current = filtered;
  const shown = paused ? frozen.current : filtered;
  useEffect(() => {
    if (follow && !paused && box.current)
      box.current.scrollTop = box.current.scrollHeight;
  }, [shown, follow, paused]);
  async function download() {
    try {
      const d = await request("/api/logs?limit=5000");
      const text = (d.logs || [])
        .map(
          (l) =>
            `${new Date(l.ts).toISOString()} [${l.level}] ${l.src}: ${l.msg}`,
        )
        .join("\n");
      const url = URL.createObjectURL(
        new Blob([text], { type: "text/plain;charset=utf-8" }),
      );
      const a = document.createElement("a");
      a.href = url;
      a.download = "music-info-log-" + Date.now() + ".txt";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      setError(e.message);
    }
  }
  return (
    <section className={!active ? "hidden" : ""} data-page="logs">
      <div className="page-heading">
        <div>
          <span className="eyebrow">LIVE CONSOLE</span>
          <h2>实时日志</h2>
          <p>服务端日志实时推送；离开页面关闭连接，返回时读取历史。</p>
        </div>
        <Button onClick={() => setReconnect((n) => n + 1)}>重新连接</Button>
      </div>
      {error && <p role="alert">{error}</p>}
      <section className="surface log-panel">
        <div className="log-toolbar">
          <div className="seg">
            {[
              ["ALL", "全部"],
              ["INFO", "信息"],
              ["WARNING", "警告"],
              ["ERROR", "错误"],
            ].map(([k, label]) => (
              <button
                className={level === k ? "on" : ""}
                key={k}
                onClick={() => setLevel(k)}
              >
                {label}
              </button>
            ))}
          </div>
          <input
            aria-label="过滤日志"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="过滤关键字"
          />
          <label className="checkline">
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
            />
            自动滚动
          </label>
          <Button onClick={() => setPaused((p) => !p)}>
            {paused ? "继续" : "暂停"}
          </Button>
          <Button
            onClick={() => {
              setLines([]);
              frozen.current = [];
            }}
          >
            清空
          </Button>
          <Button onClick={download}>下载</Button>
          <span className="pill">{connection}</span>
        </div>
        <div className="log-console" ref={box} aria-label="实时日志内容">
          {shown.length ? (
            shown.map((l, i) => (
              <div
                className={
                  "log-line" + ((rank[l.level] ?? 1) >= 3 ? " err" : "")
                }
                key={l.id ?? i}
              >
                <span className="log-t">
                  {new Intl.DateTimeFormat("zh-CN", {
                    timeZone: "Asia/Tokyo",
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                    hour12: false,
                  }).format(new Date(l.ts))}
                </span>
                <span className={"log-lv " + l.level?.toLowerCase()}>
                  {l.level}
                </span>
                <span className="log-src">{l.src}</span>
                <span className="log-msg">{l.msg}</span>
              </div>
            ))
          ) : (
            <p className="log-empty">等待日志…</p>
          )}
        </div>
        <div className="log-foot">
          显示 {shown.length} 行 · 缓冲 {lines.length} 行
        </div>
      </section>
    </section>
  );
}
