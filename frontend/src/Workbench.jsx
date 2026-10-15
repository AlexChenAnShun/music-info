import React, { useEffect, useMemo, useRef, useState } from "react";
import { request } from "./api";
import { useApp } from "./AppContext";
import { Alert, Button, Field } from "./common";
import { bytes } from "./model";
import {
  applyItem,
  changes,
  FIELD_LABEL,
  FIELDS,
  filename,
  importRows,
  proposal,
  safeUrl,
  SOURCES,
  splitLyrics,
  terminal,
  JOB_STATES,
} from "./workflow";
export function Diff({ rows }) {
  return (
    <div className="diff-list">
      {rows.map((r) => (
        <div key={r.key}>
          <b>{FIELD_LABEL[r.key] || r.key}</b>
          <span className="diff-before">{r.before || "（空白）"}</span>
          <span>→</span>
          <span className="diff-after">{r.after || "（清空）"}</span>
        </div>
      ))}
    </div>
  );
}
function Cover({ path, hasCover = true, src = "", className = "" }) {
  const [bad, setBad] = useState(false);
  useEffect(() => setBad(false), [path, src]);
  return (
    <span className={"react-cover " + className}>
      {hasCover && !bad ? (
        <img
          src={src || "/api/cover?path=" + encodeURIComponent(path)}
          loading="lazy"
          alt="歌曲封面"
          onError={() => setBad(true)}
        />
      ) : (
        <span aria-hidden="true">♫</span>
      )}
    </span>
  );
}
async function lyricFor(c) {
  if (c._lrc || c.lyrics) return c._lrc || c.lyrics;
  let lyric = "";
  if (["netease", "migu", "qq", "kugou", "kuwo"].includes(c.source) && c.id) {
    try {
      lyric =
        (
          await request(
            "/api/lyric?" + new URLSearchParams({ source: c.source, id: c.id }),
          )
        ).lyric || "";
    } catch {}
  }
  if (!lyric && c.artist && c.title) {
    try {
      lyric =
        (
          await request(
            "/api/lrclib_lyric?" +
              new URLSearchParams({
                artist: c.artist,
                title: c.title,
                album: c.album || "",
              }),
          )
        ).lyric || "";
    } catch {}
  }
  return lyric;
}
export default function Workbench({ active }) {
  const app = useApp(),
    { prefs, notify, confirm } = app;
  const [cwd, setCwd] = useState(""),
    [directory, setDirectory] = useState({ dirs: [], files: [] }),
    [query, setQuery] = useState(""),
    [sort, setSort] = useState("name"),
    [selected, setSelected] = useState(new Set()),
    [track, setTrack] = useState(null),
    [draft, setDraft] = useState({}),
    [pendingCover, setPendingCover] = useState(null),
    [center, setCenter] = useState("edit"),
    [queue, setQueue] = useState([]),
    [job, setJob] = useState(null),
    [options, setOptions] = useState(prefs),
    [queryMode, setQueryMode] = useState("auto"),
    [searchQ, setSearchQ] = useState(""),
    [candidates, setCandidates] = useState([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [progress, setProgress] = useState(null),
    [loadingLibrary, setLoadingLibrary] = useState(false);
  const trackSeq = useRef(0),
    dirSeq = useRef(0),
    searchSeq = useRef(0),
    fileInput = useRef(null),
    dirtyRef = useRef(false),
    jobRef = useRef(job);
  jobRef.current = job;
  const dirty =
    !!track &&
    (changes(track, draft).length > 0 ||
      (draft.filename ?? track.name) !== track.name ||
      !!pendingCover);
  dirtyRef.current = dirty;
  useEffect(() => setOptions(prefs), [prefs]);
  useEffect(
    () =>
      app.registerGuard("work", (next) => {
        if (next.view !== "work" && dirtyRef.current) {
          if (!window.confirm("当前标签有未保存的修改，舍弃后离开？"))
            return false;
          setDraft({ ...track, filename: track.name });
          setPendingCover(null);
        }
        return true;
      }),
    [track],
  );
  useEffect(() => {
    const fn = (e) => {
      if (dirtyRef.current) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", fn);
    return () => window.removeEventListener("beforeunload", fn);
  }, []);
  async function loadDir(path) {
    const n = ++dirSeq.current;
    setLoadingLibrary(true);
    setError("");
    try {
      const d = await request("/api/list?path=" + encodeURIComponent(path));
      if (n !== dirSeq.current) return;
      setCwd(path);
      setDirectory({ dirs: d.dirs || [], files: d.files || [] });
      setSelected(new Set());
      setQuery("");
    } catch (e) {
      if (n === dirSeq.current) setError(e.message);
    } finally {
      if (n === dirSeq.current) setLoadingLibrary(false);
    }
  }
  useEffect(() => {
    loadDir("");
    return () => {
      dirSeq.current++;
      trackSeq.current++;
      searchSeq.current++;
    };
  }, []);
  async function openTrack(path, force = false) {
    if (
      !force &&
      dirtyRef.current &&
      !window.confirm("舍弃未保存修改并打开歌曲？")
    )
      return;
    const n = ++trackSeq.current;
    setError("");
    try {
      const t = await request("/api/track?path=" + encodeURIComponent(path));
      if (n !== trackSeq.current) return;
      setTrack(t);
      setDraft({ ...t, filename: t.name });
      setPendingCover(null);
      setCandidates([]);
      setSearchQ(
        (t.title || t.name.replace(/\.[^.]+$/, "")) +
          (t.artist ? " - " + t.artist : ""),
      );
      setCenter("edit");
    } catch (e) {
      if (n === trackSeq.current) setError(e.message);
    }
  }
  const shown = useMemo(
    () =>
      [...directory.files]
        .filter((f) => f.name.toLowerCase().includes(query.toLowerCase()))
        .sort((a, b) =>
          sort === "size"
            ? b.size - a.size
            : sort === "mtime"
              ? (b.mtime || 0) - (a.mtime || 0)
              : sort === "name-desc"
                ? b.name.localeCompare(a.name, "zh-CN")
                : a.name.localeCompare(b.name, "zh-CN"),
        ),
    [directory, query, sort],
  );
  function select(path, on) {
    setSelected((s) => {
      const next = new Set(s);
      on ? next.add(path) : next.delete(path);
      return next;
    });
  }
  function allowed() {
    if (jobRef.current) {
      notify("已有后台任务，请等待完成或在任务中心安全停止", true);
      return false;
    }
    return true;
  }
  async function startPreview(paths) {
    if (!allowed()) return;
    if (!paths.length) return notify("请先选择曲目", true);
    if (paths.length > 50)
      return notify("单批最多 50 首，请减少选择后再试", true);
    setBusy(true);
    setError("");
    try {
      const d = await request("/api/tasks", {
        body: {
          kind: "preview",
          paths: [...new Set(paths)],
          options: {
            source: options.source,
            query_mode: queryMode,
            include_lyrics: options.includeLyrics,
            include_cover: options.includeCover,
            only_empty: options.onlyEmpty,
          },
        },
      });
      setQueue([]);
      setCenter("preview");
      setJob({ id: d.job_id, kind: "preview" });
      notify("后台匹配预览已创建，不会写入音乐");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    if (!job) return;
    const ctl = new AbortController();
    let timer;
    async function tick() {
      try {
        const d = await request("/api/tasks/" + encodeURIComponent(job.id), {
          signal: ctl.signal,
        });
        if (ctl.signal.aborted) return;
        setProgress(d);
        if (job.kind === "preview")
          setQueue((old) =>
            importRows(d).map((r) => ({
              ...r,
              selected: old.find((x) => x.path === r.path)?.selected || false,
            })),
          );
        if (terminal(d.status)) {
          setJob(null);
          setProgress(d);
          if (job.kind === "apply") {
            setQueue((old) =>
              old.map((row) => {
                const item = d.items?.find((x) => x.path === row.path);
                return item
                  ? {
                      ...row,
                      done: item.status === "completed",
                      selected: false,
                      error:
                        item.status === "completed"
                          ? null
                          : item.error || "未执行，请重新生成预览",
                    }
                  : row;
              }),
            );
            await loadDir(cwd);
            if (track && d.items?.some((x) => x.path === track.path))
              await openTrack(track.path, true);
          }
          notify(
            "后台任务" + (JOB_STATES[d.status] || d.status) + "，请核对结果",
            !["completed", "cancelled"].includes(d.status),
          );
          return;
        }
      } catch (e) {
        if (!ctl.signal.aborted)
          setError("任务查询中断，后台任务可能仍在运行：" + e.message);
      }
      if (!ctl.signal.aborted) timer = setTimeout(tick, 1200);
    }
    tick();
    return () => {
      ctl.abort();
      clearTimeout(timer);
    };
  }, [job?.id]);
  useEffect(() => {
    if (!app.importJob) return;
    setQueue(importRows(app.importJob));
    setCenter("preview");
    setProgress(app.importJob);
    app.setImportJob(null);
  }, [app.importJob]);
  async function saveManual() {
    if (!track || !allowed()) return;
    const diff = changes(track, draft),
      rename = draft.filename?.trim() && draft.filename.trim() !== track.name;
    if (!diff.length && !rename && !pendingCover)
      return notify("没有需要保存的修改");
    if (!track.file_version)
      return notify("文件缺少版本信息，请重新读取", true);
    const body = {
      path: track.path,
      expected_version: track.file_version,
      ...Object.fromEntries(diff.map((d) => [d.key, d.after])),
      ...(rename ? { filename: draft.filename.trim() } : {}),
      ...(pendingCover
        ? { cover_b64: pendingCover.b64, cover_mime: pendingCover.mime }
        : {}),
    };
    confirm({
      title: "确认保存手动修改？",
      description:
        "以下改动会写入音乐文件。提交前核验当前版本，成功后保留元数据历史。",
      content: (
        <>
          <Diff rows={diff} />
          {rename && (
            <p>
              文件名：{track.name} → {body.filename}
            </p>
          )}
          {pendingCover && <p>封面：替换为暂存图片</p>}
        </>
      ),
      run: async () => {
        const d = await request("/api/commit", { body });
        await loadDir(cwd);
        await openTrack(d.path || track.path, true);
        notify("已提交，修改历史已保存");
      },
    });
  }
  async function importCover(file) {
    if (!file || !track) return;
    if (
      file.size > 8 * 1024 * 1024 ||
      !["image/jpeg", "image/png", "image/webp", "image/gif"].includes(
        file.type,
      )
    )
      return notify("请选择不超过 8 MiB 的 JPEG/PNG/WebP/GIF 图片", true);
    const path = track.path,
      n = trackSeq.current;
    try {
      const data = await new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result));
        r.onerror = reject;
        r.readAsDataURL(file);
      });
      if (n === trackSeq.current)
        setPendingCover({
          path,
          url: data,
          b64: data.split(",")[1],
          mime: file.type,
        });
    } catch {
      notify("读取封面失败", true);
    }
  }
  async function exportCover() {
    if (!track?.has_cover) return;
    try {
      const res = await fetch(
        "/api/cover?path=" + encodeURIComponent(track.path),
      );
      if (!res.ok) throw new Error("封面读取失败");
      const blob = await res.blob(),
        url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download =
        track.name.replace(/\.[^.]+$/, "") +
        "." +
        ({ "image/png": "png", "image/webp": "webp", "image/gif": "gif" }[
          blob.type
        ] || "jpg");
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      setError(e.message);
    }
  }
  async function readLrc() {
    try {
      const d = await request(
        "/api/lrc_file?path=" + encodeURIComponent(track.path),
      );
      setDraft((old) => ({ ...old, ...splitLyrics(d.content) }));
      notify("外置歌词已读取到表单，尚未保存");
    } catch (e) {
      setError(e.message);
    }
  }
  function exportLrc() {
    const content = String(draft.lrc || draft.lyrics || "").trim();
    if (!content) return notify("没有可导出的歌词", true);
    confirm({
      title: "确认导出同名外置歌词？",
      description: "将在 NAS 创建或替换同名 .lrc 文件，并保留历史快照。",
      content: <p>{track.name.replace(/\.[^.]+$/, "")}.lrc</p>,
      run: async () => {
        await request("/api/lrc_file", {
          body: {
            path: track.path,
            content,
            expected_version: track.file_version,
          },
        });
        await openTrack(track.path, true);
        notify("外置歌词已导出");
      },
    });
  }
  async function search(fingerprint = false) {
    if (fingerprint && !track) return;
    const n = ++searchSeq.current;
    setBusy(true);
    setError("");
    try {
      let d;
      if (fingerprint)
        d = await request(
          "/api/fp_search?path=" + encodeURIComponent(track.path),
          { timeout: 210000 },
        );
      else {
        const source = options.source === "netease" ? "ncm" : options.source;
        try {
          d = await request(
            "/api/" +
              source +
              "_search?" +
              new URLSearchParams({ q: searchQ, limit: 15 }),
          );
        } catch (e) {
          if (!["smart", "ncm"].includes(source)) throw e;
          d = await request("/api/mb_search?q=" + encodeURIComponent(searchQ));
        }
      }
      if (n === searchSeq.current) {
        setCandidates(d.results || []);
        if (!d.results?.length) notify("未找到候选，可修改检索词或来源后重试");
      }
    } catch (e) {
      if (n === searchSeq.current) setError(e.message);
    } finally {
      if (n === searchSeq.current) setBusy(false);
    }
  }
  async function previewCandidate(c) {
    if (!track || !allowed()) return;
    setBusy(true);
    try {
      const lyric = options.includeLyrics ? await lyricFor(c) : "";
      const row = {
        path: track.path,
        name: track.name,
        original: track,
        candidate: {
          ...c,
          ...(splitLyrics(lyric).lrc ? { _lrc: splitLyrics(lyric).lrc } : {}),
        },
        candidates: [c],
        lyric,
        selected: true,
      };
      setQueue((old) => [...old.filter((r) => r.path !== track.path), row]);
      setCenter("preview");
      notify("已生成此候选预览，尚未写入");
    } finally {
      setBusy(false);
    }
  }
  async function changeCandidate(path, c) {
    setQueue((old) =>
      old.map((r) =>
        r.path === path
          ? { ...r, candidate: c, lyric: "", loading: true, selected: false }
          : r,
      ),
    );
    try {
      const lyric = options.includeLyrics ? await lyricFor(c) : "";
      setQueue((old) =>
        old.map((r) =>
          r.path === path
            ? {
                ...r,
                candidate: {
                  ...c,
                  ...(splitLyrics(lyric).lrc
                    ? { _lrc: splitLyrics(lyric).lrc }
                    : {}),
                },
                lyric,
                loading: false,
              }
            : r,
        ),
      );
    } catch (e) {
      setQueue((old) =>
        old.map((r) =>
          r.path === path ? { ...r, loading: false, error: e.message } : r,
        ),
      );
    }
  }
  function commitBatch() {
    if (!allowed()) return;
    const chosen = queue.filter(
      (q) => q.selected && !q.done && !q.error && !q.loading && q.candidate,
    );
    if (!chosen.length) return notify("请先勾选需要写入的候选预览", true);
    try {
      const items = chosen
        .map((row) => applyItem(row, options))
        .filter((row) => Object.keys(row.fields).length || row.cover_url);
      if (!items.length) return notify("所选候选没有可写入的差异");
      confirm({
        title: `确认写入 ${items.length} 首曲目？`,
        description:
          "仅提交已勾选的差异与封面。逐首检查预览时的文件版本；冲突不覆盖，不自动重试。",
        content: (
          <>
            {chosen.map((row) => (
              <div key={row.path}>
                <b>{row.name}</b>
                <Diff
                  rows={Object.entries(
                    proposal(row.original, row.candidate, row.lyric, options)
                      .fields,
                  ).map(([key, after]) => ({
                    key,
                    before: row.original[key] || "",
                    after,
                  }))}
                />
                {proposal(row.original, row.candidate, row.lyric, options)
                  .cover_url && <p>封面：写入候选封面</p>}
              </div>
            ))}
          </>
        ),
        run: async () => {
          const d = await request("/api/tasks", {
            body: { kind: "apply", confirmed: true, items, options: {} },
          });
          setJob({ id: d.job_id, kind: "apply" });
          notify("已创建后台写入任务");
        },
      });
    } catch (e) {
      setError(e.message);
    }
  }
  const update = (key, value) => setDraft((d) => ({ ...d, [key]: value }));
  const toggleOption = (k, v) => setOptions((o) => ({ ...o, [k]: v }));
  const tech = track?.tech || {};
  return (
    <section
      id="view-work"
      className={"page work-page" + (!active ? " hidden" : "")}
      data-page="work"
    >
      <div className="work-layout">
        <aside className="surface library">
          <div className="panel-head">
            <div className="head-line">
              <h3>我的曲库</h3>
              <Button
                aria-label="刷新曲库"
                disabled={loadingLibrary}
                onClick={() => loadDir(cwd)}
              >
                ↻
              </Button>
            </div>
            <div className="crumb">
              <button onClick={() => loadDir("")}>⌂ 根目录</button>
              {cwd
                .split("/")
                .filter(Boolean)
                .map((p, i, parts) => (
                  <button
                    key={i}
                    onClick={() => loadDir(parts.slice(0, i + 1).join("/"))}
                  >
                    › {p}
                  </button>
                ))}
            </div>
            <div className="search">
              <input
                aria-label="搜索当前文件夹"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索当前文件夹…"
              />
            </div>
          </div>
          <div className="library-toolbar">
            <label>
              <input
                type="checkbox"
                checked={
                  !!shown.length && shown.every((f) => selected.has(f.path))
                }
                onChange={(e) =>
                  setSelected((old) => {
                    const s = new Set(old);
                    shown.forEach((f) =>
                      e.target.checked ? s.add(f.path) : s.delete(f.path),
                    );
                    return s;
                  })
                }
              />{" "}
              全选显示项
            </label>
            <span>已选 {selected.size} 首</span>
          </div>
          <div className="sort-line">
            <select
              aria-label="曲库排序"
              value={sort}
              onChange={(e) => setSort(e.target.value)}
            >
              <option value="name">文件名 A–Z</option>
              <option value="name-desc">文件名 Z–A</option>
              <option value="size">文件大小</option>
              <option value="mtime">修改时间</option>
            </select>
            <Button onClick={() => setSelected(new Set())}>清选</Button>
          </div>
          <div className="song-list" role="listbox" aria-label="曲库文件">
            {loadingLibrary && <p role="status">读取中…</p>}
            {cwd && (
              <button
                className="dir-row"
                onClick={() => loadDir(cwd.split("/").slice(0, -1).join("/"))}
              >
                ↑ 上一级
              </button>
            )}
            {directory.dirs
              .filter((d) => d.toLowerCase().includes(query.toLowerCase()))
              .map((dir) => (
                <button
                  className="dir-row"
                  key={dir}
                  onClick={() => loadDir(cwd ? cwd + "/" + dir : dir)}
                >
                  ▱ {dir}
                </button>
              ))}
            {shown.map((f) => (
              <div
                className={
                  "file-row" + (track?.path === f.path ? " active" : "")
                }
                key={f.path}
              >
                <input
                  type="checkbox"
                  aria-label={"选择 " + f.name}
                  checked={selected.has(f.path)}
                  onChange={(e) => select(f.path, e.target.checked)}
                />
                <Cover path={f.path} />
                <button
                  className="song-body song-open"
                  onClick={() => openTrack(f.path)}
                >
                  <b className="song-title">{f.name.replace(/\.[^.]+$/, "")}</b>
                  <span className="song-meta">
                    {f.name.split(".").at(-1).toUpperCase()} · {bytes(f.size)}
                  </span>
                </button>
              </div>
            ))}
            {!shown.length && !directory.dirs.length && !loadingLibrary && (
              <p className="empty-list">此目录暂无音乐文件</p>
            )}
          </div>
          <div className="library-footer">
            <span>
              {directory.dirs.length} 个目录 · {directory.files.length} 首
            </span>
          </div>
        </aside>
        <main className="surface react-editor">
          <div className="workbar">
            <div className="center-tabs">
              <button
                className={center === "edit" ? "active" : ""}
                onClick={() => setCenter("edit")}
              >
                标签信息
              </button>
              <button
                className={center === "preview" ? "active" : ""}
                onClick={() => setCenter("preview")}
              >
                整理预览 {queue.length ? `(${queue.length})` : ""}
              </button>
              <span className="pill purple">
                {options.onlyEmpty ? "仅补空白" : "允许覆盖"}
              </span>
            </div>
            <div className="btn-row">
              <Button
                disabled={busy || !!job || !selected.size}
                onClick={() => startPreview([...selected])}
              >
                批量匹配预览
              </Button>
              <Button disabled={!track || !dirty || !!job} onClick={saveManual}>
                保存手动修改
              </Button>
            </div>
          </div>
          {error && (
            <Alert error>
              {error}
              <Button onClick={() => setError("")}>关闭</Button>
            </Alert>
          )}
          {center === "edit" ? (
            track ? (
              <>
                <div className="track-header">
                  <div className="track-art">
                    <Cover
                      className="large"
                      path={track.path + "#" + track.file_version}
                      hasCover={!!pendingCover || track.has_cover}
                      src={
                        pendingCover?.url ||
                        "/api/cover?path=" +
                          encodeURIComponent(track.path) +
                          "&v=" +
                          track.file_version
                      }
                    />
                    <div className="btn-row">
                      <Button
                        disabled={track.capabilities?.cover === false || !!job}
                        onClick={() => fileInput.current.click()}
                      >
                        导入
                      </Button>
                      <Button disabled={!track.has_cover} onClick={exportCover}>
                        导出
                      </Button>
                    </div>
                    <input
                      ref={fileInput}
                      type="file"
                      accept="image/jpeg,image/png,image/webp,image/gif"
                      hidden
                      onChange={(e) => {
                        importCover(e.target.files[0]);
                        e.target.value = "";
                      }}
                    />
                  </div>
                  <div className="track-title">
                    <h2>{track.title || track.name}</h2>
                    <p>{track.artist || "艺术家待补充"}</p>
                    <small>{track.name}</small>
                    <div className="track-status">
                      <span className="pill green">
                        {track.has_cover ? "已有封面" : "暂无封面"}
                      </span>
                      <span className="pill purple">
                        {track.lrc
                          ? "同步歌词"
                          : track.lyrics
                            ? "纯文本歌词"
                            : "暂无歌词"}
                      </span>
                      <span className="pill">
                        {track.name.split(".").at(-1).toUpperCase()}
                      </span>
                      {pendingCover && (
                        <span className="pill amber">封面已暂存，未写入</span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="react-tag-form">
                  <div className="tag-grid">
                    {FIELDS.map((key) => (
                      <React.Fragment key={key}>
                        <label
                          className={
                            "rx-tag-field " +
                            ([
                              "title",
                              "comment",
                              "description",
                              "lyrics",
                              "lrc",
                            ].includes(key)
                              ? "span2"
                              : "")
                          }
                        >
                          <span>{FIELD_LABEL[key]}</span>
                          {["comment", "description", "lyrics", "lrc"].includes(
                            key,
                          ) ? (
                            <textarea
                              id={"tag-" + key}
                              aria-label={FIELD_LABEL[key]}
                              value={draft[key] ?? ""}
                              disabled={
                                !!job ||
                                !(
                                  track.capabilities?.fields ?? FIELDS
                                ).includes(key)
                              }
                              onChange={(e) => update(key, e.target.value)}
                            />
                          ) : (
                            <input
                              id={"tag-" + key}
                              aria-label={FIELD_LABEL[key]}
                              value={draft[key] ?? ""}
                              disabled={
                                !!job ||
                                !(
                                  track.capabilities?.fields ?? FIELDS
                                ).includes(key)
                              }
                              onChange={(e) => update(key, e.target.value)}
                            />
                          )}
                        </label>
                        {key === "title" && (
                          <label className="rx-tag-field span2">
                            <span>文件名</span>
                            <span className="filename-row">
                              <input
                                aria-label="文件名"
                                value={draft.filename || ""}
                                disabled={!!job}
                                onChange={(e) =>
                                  update("filename", e.target.value)
                                }
                              />
                              <Button
                                disabled={!!job}
                                onClick={() => {
                                  try {
                                    update(
                                      "filename",
                                      filename(prefs.template, draft),
                                    );
                                  } catch (e) {
                                    notify(e.message, true);
                                  }
                                }}
                              >
                                按模板生成
                              </Button>
                            </span>
                          </label>
                        )}
                      </React.Fragment>
                    ))}
                  </div>
                  <div className="react-tech">
                    <span>
                      时长 {tech.duration || "—"}s · 码率 {tech.bitrate || "—"}
                      kbps · 采样率 {tech.sample_rate || "—"}Hz · 位深{" "}
                      {tech.bits || "—"}bit · 声道 {tech.channels || "—"}
                    </span>
                    <div className="btn-row">
                      <Button onClick={readLrc}>读取同名 .lrc</Button>
                      <Button disabled={!!job} onClick={exportLrc}>
                        确认导出 .lrc
                      </Button>
                    </div>
                  </div>
                  <div className="form-actions">
                    <Button
                      disabled={busy || !!job}
                      onClick={() => startPreview([track.path])}
                    >
                      ✧ 单曲匹配预览
                    </Button>
                    <Button
                      primary
                      disabled={!!job || !dirty}
                      onClick={saveManual}
                    >
                      核对并保存修改
                    </Button>
                    <Button
                      onClick={() => {
                        app.setHistoryPath(track.path);
                        app.navigate("history");
                      }}
                    >
                      查看历史
                    </Button>
                  </div>
                </div>
              </>
            ) : (
              <div className="detail-empty react-empty">
                <span>♫</span>
                <h3>选择一首歌曲</h3>
                <p>从左侧曲库打开音乐，查看和编辑标签。所有写入需再次确认。</p>
              </div>
            )
          ) : (
            <div className="react-preview">
              <div className="head-line">
                <h3>待确认的整理预览</h3>
                <Button disabled={!!job} onClick={() => setQueue([])}>
                  清空预览
                </Button>
              </div>
              <p className="helper">
                相似度不代表版本一定正确。未匹配不等于失败；请人工核对。
              </p>
              {progress && (
                <p role="status">
                  {JOB_STATES[progress.status] || progress.status} ·{" "}
                  {(progress.counts?.completed || 0) +
                    (progress.counts?.failed || 0)}{" "}
                  / {progress.total || 0}
                </p>
              )}
              <div className="react-queue">
                {queue.map((row) => {
                  const p =
                    row.candidate && row.original
                      ? proposal(
                          row.original,
                          row.candidate,
                          row.lyric,
                          options,
                        )
                      : { fields: {} };
                  const actionable =
                    !row.done &&
                    !row.error &&
                    !row.loading &&
                    !!row.candidate &&
                    (Object.keys(p.fields).length || p.cover_url);
                  return (
                    <article className="queue-item" key={row.path}>
                      <div className="queue-item-top">
                        <label>
                          <input
                            type="checkbox"
                            aria-label={"写入 " + row.name}
                            checked={!!row.selected}
                            disabled={!actionable || !!job}
                            onChange={(e) =>
                              setQueue((q) =>
                                q.map((x) =>
                                  x.path === row.path
                                    ? { ...x, selected: e.target.checked }
                                    : x,
                                ),
                              )
                            }
                          />
                          <b>{row.name}</b>
                        </label>
                        <span
                          className={
                            "pill " +
                            (row.error
                              ? "red"
                              : row.noMatch
                                ? "amber"
                                : "purple")
                          }
                        >
                          {row.error
                            ? "失败"
                            : row.done
                              ? "已完成"
                              : row.noMatch
                                ? "未匹配"
                                : row.candidate?.match_percent != null
                                  ? row.candidate.match_percent + "%"
                                  : "待核对"}
                        </span>
                      </div>
                      {row.error ? (
                        <Alert error>{row.error}</Alert>
                      ) : row.noMatch ? (
                        <p>{row.note}</p>
                      ) : (
                        <>
                          <div className="candidate-title">
                            {row.candidate?.title} · {row.candidate?.artist} ·{" "}
                            {row.candidate?.album}
                          </div>
                          {row.candidates?.length > 1 && (
                            <Field label="候选版本">
                              <select
                                disabled={!!job || row.loading || row.done}
                                value={row.candidates.findIndex(
                                  (c) =>
                                    c.id === row.candidate?.id &&
                                    c.source === row.candidate?.source,
                                )}
                                onChange={(e) =>
                                  changeCandidate(
                                    row.path,
                                    row.candidates[Number(e.target.value)],
                                  )
                                }
                              >
                                {row.candidates.map((c, i) => (
                                  <option key={i} value={i}>
                                    {c.title} · {c.artist} · {c.album} ·{" "}
                                    {c.source}
                                  </option>
                                ))}
                              </select>
                            </Field>
                          )}
                          {row.loading ? (
                            <p>正在读取歌词…</p>
                          ) : (
                            <>
                              <Diff
                                rows={Object.entries(p.fields).map(
                                  ([key, after]) => ({
                                    key,
                                    before: row.original[key] || "",
                                    after,
                                  }),
                                )}
                              />
                              {p.cover_url && <p>将写入候选封面</p>}
                              {!Object.keys(p.fields).length &&
                                !p.cover_url && (
                                  <p className="small-label">
                                    当前选项下无可写差异
                                  </p>
                                )}
                            </>
                          )}
                        </>
                      )}
                    </article>
                  );
                })}
                {!queue.length && (
                  <div className="detail-empty">
                    还没有候选预览。可选择曲目匹配，或在右侧查找候选。
                  </div>
                )}
              </div>
              <div className="queue-action">
                <Button
                  primary
                  disabled={
                    !!job ||
                    !queue.some(
                      (r) => r.selected && !r.done && !r.error && !r.loading,
                    )
                  }
                  onClick={commitBatch}
                >
                  核对所选差异并确认
                </Button>
                {job && (
                  <Button
                    onClick={async () => {
                      try {
                        await request("/api/tasks/" + job.id + "/cancel", {
                          method: "POST",
                        });
                        notify("已请求安全停止；已完成的修改不会撤销");
                      } catch (e) {
                        setError(e.message);
                      }
                    }}
                  >
                    安全停止任务
                  </Button>
                )}
              </div>
            </div>
          )}
        </main>
        <aside className="surface automation">
          <span className="eyebrow">MATCHING OPTIONS</span>
          <h3>生成整理预览</h3>
          <p className="subtle">检索在后台执行；离开页面不会停止任务。</p>
          <Field label="元数据来源">
            <select
              value={options.source}
              onChange={(e) => toggleOption("source", e.target.value)}
            >
              {Object.entries(SOURCES).map(([k, v]) => (
                <option value={k} key={k}>
                  {v}
                </option>
              ))}
            </select>
          </Field>
          <Field label="检索依据">
            <select
              value={queryMode}
              onChange={(e) => setQueryMode(e.target.value)}
            >
              <option value="auto">自动 · 检查标签与文件名</option>
              <option value="filename">按文件名检索</option>
              <option value="tags">按现有标签检索</option>
            </select>
          </Field>
          {[
            ["onlyEmpty", "只补空白，不覆盖现有标签"],
            ["includeLyrics", "尝试匹配歌词"],
            ["includeCover", "预览候选封面"],
          ].map(([k, label]) => (
            <label className="checkline" key={k}>
              <input
                type="checkbox"
                checked={!!options[k]}
                disabled={!!job}
                onChange={(e) => toggleOption(k, e.target.checked)}
              />
              {label}
            </label>
          ))}
          <Button
            primary
            className="btn primary block"
            disabled={busy || !!job || !selected.size}
            onClick={() => startPreview([...selected])}
          >
            生成所选曲目预览 →
          </Button>
          <Button
            className="btn outline block"
            disabled={!track || busy}
            onClick={() => search(true)}
          >
            声纹识别当前歌曲
          </Button>
          <div className="notice">
            {options.onlyEmpty
              ? "当前仅补空白，不覆盖已有标签和封面。"
              : "当前允许覆盖，请逐项核对差异！"}
            所有操作先预览，确认后才写入。
          </div>
          <div className="manual-search">
            <h4>手动选择更合适的版本</h4>
            <Field label="歌名 / 艺术家">
              <input
                value={searchQ}
                onChange={(e) => setSearchQ(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && search()}
                placeholder="歌名 - 艺术家"
              />
            </Field>
            <Button disabled={busy || !searchQ.trim()} onClick={() => search()}>
              查找候选
            </Button>
            <div className="manual-results">
              {candidates.map((c, i) => (
                <article className="candidate-card" key={i}>
                  <Cover
                    path="candidate"
                    hasCover={!!safeUrl(c.cover || c.album_img)}
                    src={safeUrl(c.cover || c.album_img)}
                  />
                  <div>
                    <b>{c.title}</b>
                    <p>
                      {c.artist} · {c.album}
                    </p>
                    <small>
                      {c.source}{" "}
                      {c.match_percent != null ? c.match_percent + "%" : ""}
                    </small>
                    <Button
                      disabled={!track || busy || !!job}
                      onClick={() => previewCandidate(c)}
                    >
                      生成此候选预览
                    </Button>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </section>
  );
}
