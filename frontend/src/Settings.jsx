import React, { useEffect, useRef, useState } from "react";
import { request } from "./api";
import { absolute, DIRS, EXAMPLES, WASH } from "./model";
import { Alert, Button, Field, Modal } from "./common";
const SUBS = [
  ["basic", "基础配置"],
  ["naming", "重命名规则"],
  ["classify", "二级分类策略"],
  ["wash", "洗版策略"],
  ["help", "变量与语法"],
];
function Picker({ role, form, status, onUse, onClose }) {
  const [data, setData] = useState(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [name, setName] = useState("");
  const seq = useRef(0);
  useEffect(
    () => () => {
      seq.current++;
    },
    [],
  );
  async function browse(path, fallback = false) {
    const n = ++seq.current;
    setLoading(true);
    setError("");
    setData(null);
    try {
      const d = await request(
        "/api/organize/browse?path=" + encodeURIComponent(path),
      );
      if (n === seq.current) setData(d);
    } catch (e) {
      if (n !== seq.current) return;
      if (fallback && path) return browse("");
      setError(e.message);
    } finally {
      if (n === seq.current) setLoading(false);
    }
  }
  useEffect(() => {
    browse(form[role] || "", true);
  }, [role]);
  async function create() {
    if (!name.trim()) return setError("请先输入文件夹名称");
    setLoading(true);
    setError("");
    try {
      const d = await request("/api/organize/mkdir", {
        body: { path: data.path, name: name.trim() },
      });
      setName("");
      await browse(d.path);
    } catch (e) {
      setError(e.message);
      setLoading(false);
    }
  }
  const crumbs = [{ name: "音乐库", path: "" }];
  let p = "";
  for (const part of (data?.path || "").split("/").filter(Boolean)) {
    p = p ? p + "/" + part : part;
    crumbs.push({ name: part, path: p });
  }
  const labels = (keys) =>
    (keys || [])
      .map((k) => DIRS.find(([key]) => key === k)?.[1] || k)
      .join("、");
  return (
    <Modal
      title={"选择「" + DIRS.find(([k]) => k === role)[1] + "」"}
      onClose={onClose}
      busy={loading}
      actions={
        <Button
          primary
          disabled={loading || !data}
          onClick={() => onUse(data.path)}
        >
          用这个目录
        </Button>
      }
    >
      <p>仅浏览音乐库内的目录，不会移动文件。选择后还需保存配置。</p>
      {error && <Alert error>{error}</Alert>}
      <div className="org-pick-crumbs">
        {crumbs.map((c) => (
          <Button
            key={c.path}
            disabled={loading}
            onClick={() => browse(c.path)}
          >
            {c.name}
          </Button>
        ))}
      </div>
      {loading ? (
        <p role="status">读取中…</p>
      ) : (
        data && (
          <>
            <div className="org-pick-role">
              {data.roles_exact?.length
                ? "这一层就是：" + labels(data.roles_exact)
                : data.roles_below?.length
                  ? "这一层包含：" + labels(data.roles_below)
                  : "当前目录未指定用途"}
            </div>
            <div className="org-pick-list">
              {data.parent !== null && data.parent !== undefined && (
                <button
                  className="org-pick-row"
                  onClick={() => browse(data.parent)}
                >
                  ↑ 上一级
                </button>
              )}
              {data.dirs.map((dir) => (
                <button
                  className="org-pick-row"
                  key={dir}
                  onClick={() =>
                    browse(data.path ? data.path + "/" + dir : dir)
                  }
                >
                  ▱ {dir}
                </button>
              ))}
              {!data.dirs.length && (
                <p className="detail-empty">没有子文件夹</p>
              )}
            </div>
            <p className="rx-path">
              当前选择：{data.abs}
              <br />
              <small>
                子目录 {data.dirs_total} · 直接文件 {data.files}
              </small>
            </p>
            <div className="org-pick-new">
              <input
                aria-label="新文件夹名称"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="新文件夹名称"
              />
              <Button onClick={create}>新建文件夹</Button>
            </div>
          </>
        )
      )}
    </Modal>
  );
}
export default function Settings({
  status,
  form,
  setForm,
  dirty,
  busy,
  onSave,
  onReset,
  onReload,
}) {
  const [tab, setTab] = useState("basic"),
    [picker, setPicker] = useState(null),
    [preview, setPreview] = useState(null),
    [previewBusy, setPreviewBusy] = useState(false),
    [previewError, setPreviewError] = useState("");
  const previewSeq = useRef(0);
  useEffect(
    () => () => {
      previewSeq.current++;
    },
    [],
  );
  const update = (key, value) => {
    setForm((f) => ({ ...f, [key]: value }));
    setPreview(null);
    setPreviewError("");
    previewSeq.current++;
    setPreviewBusy(false);
  };
  const text = (key, label, extra = {}) => (
    <Field label={label}>
      <input
        value={form[key] ?? ""}
        onChange={(e) => update(key, e.target.value)}
        {...extra}
      />
    </Field>
  );
  async function showPreview(value = form) {
    const n = ++previewSeq.current;
    setPreviewBusy(true);
    setPreviewError("");
    setPreview(null);
    try {
      const d = await request("/api/organize/preview", {
        body: Object.fromEntries(
          [
            "name_template",
            "dir_template",
            "classify_rules",
            "track_prefix",
            "track_sep",
            "unknown_artist",
            "unknown_album",
            "host_root",
          ].map((k) => [k, value[k]]),
        ),
      });
      if (n === previewSeq.current) {
        if (!d.ok) throw new Error(d.error);
        setPreview(d);
      }
    } catch (e) {
      if (n === previewSeq.current) setPreviewError(e.message);
    } finally {
      if (n === previewSeq.current) setPreviewBusy(false);
    }
  }
  function example(key, value) {
    const next = { ...form, [key]: value };
    update(key, value);
    showPreview(next);
  }
  function track(on, sep = form.track_sep) {
    setForm((f) => ({
      ...f,
      track_prefix: on,
      track_sep: sep,
      name_template: on ? `<{track:02d}${sep}>{title}` : "{title}",
    }));
    setPreview(null);
    previewSeq.current++;
    setPreviewBusy(false);
  }
  const previewBox = (
    <div className="org-preview" aria-live="polite">
      {previewError ? (
        <Alert error>{previewError}</Alert>
      ) : previewBusy ? (
        <p>正在预览…</p>
      ) : preview ? (
        <>
          {preview.paths.map((r, i) => (
            <div className="org-preview-row" key={i}>
              <span>{r.sample}</span>
              <code>{r.path}</code>
              {r.rule && <span className="pill purple">规则：{r.rule}</span>}
            </div>
          ))}
          <p className="small-label">
            共 {preview.rules || 0} 条分类规则 · 示例预览，不扫描、不改文件
          </p>
        </>
      ) : (
        <p className="detail-empty">
          点击「预览示例路径」检查效果，不会改动文件。
        </p>
      )}
    </div>
  );
  return (
    <div className="rx-scope" data-react-section="organize-settings">
      <div className="panel-title">
        <div>
          <h3>自动整理</h3>
          <span className="small-label">
            {dirty
              ? "有未保存的修改"
              : status.media_writable
                ? "音乐库可写"
                : "音乐库只读"}
          </span>
        </div>
        <Button disabled={busy} onClick={onReload}>
          重新读取
        </Button>
      </div>
      <div className="org-subtabs" role="tablist" aria-label="自动整理设置">
        {SUBS.map(([k, label]) => (
          <button
            role="tab"
            aria-selected={tab === k}
            aria-controls={"rx-panel-" + k}
            id={"rx-tab-" + k}
            className={"org-subtab" + (tab === k ? " active" : "")}
            key={k}
            onClick={() => setTab(k)}
          >
            {label}
          </button>
        ))}
      </div>
      <fieldset disabled={busy} className="rx-fieldset">
        <div
          role="tabpanel"
          id={"rx-panel-" + tab}
          aria-labelledby={"rx-tab-" + tab}
        >
          {tab === "basic" && (
            <>
              <Alert>
                目录框显示容器内路径，下方显示 NAS
                实际路径。只能通过「点击选择」修改目录；四个目录不能相同或互相包含。
              </Alert>
              <div className="org-dir-list">
                {DIRS.map(([key, label]) => (
                  <label className="org-dir" key={key}>
                    <span className="org-dir-label">{label}</span>
                    <span className="filename-row">
                      <input
                        className="org-ro"
                        readOnly
                        value={absolute(status.media_root, form[key])}
                        aria-label={label}
                      />
                      <Button onClick={() => setPicker(key)}>点击选择</Button>
                    </span>
                    <span className="org-abs">
                      服务器实际路径：
                      {form.host_root
                        ? absolute(form.host_root, form[key])
                        : "未设置映射"}
                    </span>
                  </label>
                ))}
              </div>
              <label className="checkline">
                <input
                  type="checkbox"
                  checked={!!form.follow_sidecars}
                  onChange={(e) => update("follow_sidecars", e.target.checked)}
                />
                同名 .lrc 歌词与图片跟随歌曲移动并同步改名
              </label>
              <label className="checkline">
                <input
                  type="checkbox"
                  checked={!!form.track_prefix}
                  onChange={(e) => track(e.target.checked)}
                />
                归档文件名带音轨号前缀
              </label>
              <div className="field-grid">
                <Field label="音轨号格式">
                  <select
                    value={form.track_sep}
                    onChange={(e) => track(form.track_prefix, e.target.value)}
                  >
                    {[". ", " - ", " "].map((v) => (
                      <option key={v} value={v}>
                        {"07" + v + "歌名"}
                      </option>
                    ))}
                  </select>
                </Field>
                {text("unknown_artist", "未知歌手占位名")}
                {text("unknown_album", "未知专辑占位名")}
              </div>
              {text(
                "host_root",
                "服务器实际路径（容器 /media 对应的 NAS 目录）",
              )}
              <p className="helper">
                此映射只用于路径显示，不会修改 Docker
                挂载。默认音乐库是部署目录下的 ./music。
              </p>
            </>
          )}
          {tab === "naming" && (
            <>
              <Alert>
                默认文件名 {"{title}"}，目录 {"{artist}/{album}"}
                。可选块在变量缺失时整块省略，不留下孤立分隔符。
              </Alert>
              {text("name_template", "文件命名模板", {
                placeholder: "{title}（留空即默认）",
              })}
              {text("dir_template", "目录模板")}
              <div className="btn-row">
                <Button disabled={previewBusy} onClick={() => showPreview()}>
                  预览示例路径
                </Button>
                <Button
                  onClick={() =>
                    example("name_template", "<{track:02d}. >{title}")
                  }
                >
                  示例：带音轨号
                </Button>
                <Button
                  onClick={() =>
                    example("name_template", "<{artist} - >{title}")
                  }
                >
                  示例：歌手 - 歌名
                </Button>
              </div>
              {previewBox}
            </>
          )}
          {tab === "classify" && (
            <>
              <Alert>
                YAML 规则从上到下匹配，命中即用；留空关闭。支持
                genre、year_min、year_max、decade、artist_initial。
              </Alert>
              <Field label="二级分类 YAML">
                <textarea
                  rows="12"
                  spellCheck={false}
                  value={form.classify_rules || ""}
                  onChange={(e) => update("classify_rules", e.target.value)}
                />
              </Field>
              <div className="btn-row">
                {[
                  ["genre", "流派"],
                  ["decade", "年代"],
                  ["letter", "首字母"],
                ].map(([k, label]) => (
                  <Button
                    key={k}
                    onClick={() => example("classify_rules", EXAMPLES[k])}
                  >
                    填入示例：{label}
                  </Button>
                ))}
                <Button onClick={() => example("classify_rules", "")}>
                  清空（关闭二级分类）
                </Button>
                <Button disabled={previewBusy} onClick={() => showPreview()}>
                  预览示例路径
                </Button>
              </div>
              {previewBox}
            </>
          )}
          {tab === "wash" && (
            <>
              <Alert>
                洗版只处理同歌手、同歌名。旧版本移入「已存在」，不直接删除。执行前仍需核对计划并确认。
              </Alert>
              <div className="field-grid">
                <Field label="洗版模式">
                  <select
                    value={form.wash_mode}
                    onChange={(e) => update("wash_mode", e.target.value)}
                  >
                    {Object.entries(WASH).map(([k, label]) => (
                      <option value={k} key={k}>
                        {label}
                      </option>
                    ))}
                  </select>
                </Field>
                {text("wash_max_mb", "体积上限（MB，0 = 不限制）", {
                  type: "number",
                  min: 0,
                  step: 1,
                })}
              </div>
              <p className="helper">
                音质比较顺序：码率 → 采样率 → 位深 →
                体积。替换先保存旧版，再归档新版。
              </p>
            </>
          )}
          {tab === "help" && (
            <>
              <h4>变量与语法</h4>
              <div className="org-help-grid">
                {[
                  ["{title}", "歌名"],
                  ["{artist}", "歌手"],
                  ["{album}", "专辑"],
                  ["{albumartist}", "专辑艺术家"],
                  ["{track:02d}", "音轨号，补零成 07"],
                  ["{year}", "年份"],
                  ["{decade}", "年代，例如 1990s"],
                  ["{genre}", "流派"],
                  ["{artist_initial}", "歌手首字母 / 首字"],
                  ["<...>", "可选块：任一变量为空就整块省略"],
                  ["/", "目录层级分隔符"],
                ].map(([key, label]) => (
                  <React.Fragment key={key}>
                    <b>{key}</b>
                    <span>{label}</span>
                  </React.Fragment>
                ))}
              </div>
              <p>
                非法路径字符自动替换。YAML path
                必填，错误规则会在预览或保存时显示原因。
              </p>
            </>
          )}
        </div>
        <div className="dialog-actions">
          <span className="small-label">
            {dirty ? "尚未保存；切换子页不会丢失修改" : "配置已同步"}
          </span>
          <Button onClick={onReset}>恢复默认</Button>
          <Button primary disabled={!dirty || busy} onClick={onSave}>
            保存配置
          </Button>
        </div>
      </fieldset>
      {picker && (
        <Picker
          role={picker}
          form={form}
          status={status}
          onClose={() => setPicker(null)}
          onUse={(rel) => {
            update(picker, rel);
            setPicker(null);
          }}
        />
      )}
    </div>
  );
}
