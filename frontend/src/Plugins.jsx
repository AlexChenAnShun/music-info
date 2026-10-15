import React, { useEffect, useState } from "react";
import { request } from "./api";
import { Alert, Button, Field, Modal } from "./common";
import { useApp } from "./AppContext";
const KIND = {
  tag_source: "标签源",
  fingerprint: "声纹识别",
  lyric: "歌词服务",
};
function Editor({ item, onClose, onSaved }) {
  const custom = !item?.builtin,
    [form, setForm] = useState({
      id: item?.id,
      display_name: item?.display_name || "",
      description: item?.description || "",
      order: item?.order ?? 100,
      params: { ...item?.params },
      search_url: item?.custom?.search_url || "",
      method: item?.custom?.method || "GET",
      timeout: item?.custom?.timeout ?? 15,
      limit: item?.custom?.limit ?? 8,
      result_path: item?.custom?.result_path || "",
      fields: { ...item?.custom?.fields },
      headers: JSON.stringify(item?.custom?.headers || {}),
    }),
    [q, setQ] = useState("Shape of My Heart"),
    [error, setError] = useState(""),
    [test, setTest] = useState(null),
    [busy, setBusy] = useState(false);
  const update = (key, value) => setForm((f) => ({ ...f, [key]: value }));
  function payload() {
    const value = {
      ...(form.id ? { id: form.id } : {}),
      display_name: form.display_name.trim(),
      description: form.description.trim(),
      order: Number(form.order),
    };
    if (!Number.isInteger(value.order) || value.order < 0 || value.order > 999)
      throw new Error("排序必须是 0–999 的整数");
    if (!value.display_name) throw new Error("请填写显示名");
    if (custom) {
      let headers;
      try {
        headers = JSON.parse(form.headers || "{}");
      } catch {
        throw new Error("请求头必须是合法 JSON");
      }
      if (!headers || typeof headers !== "object" || Array.isArray(headers))
        throw new Error("请求头必须是 JSON 对象");
      if (!form.search_url.includes("{q}"))
        throw new Error("搜索 URL 必须包含 {q}");
      if (!form.fields.title) throw new Error("请填写标题字段映射");
      return {
        ...value,
        search_url: form.search_url.trim(),
        method: form.method,
        timeout: Number(form.timeout),
        limit: Number(form.limit),
        result_path: form.result_path.trim(),
        headers,
        fields: form.fields,
      };
    }
    const params = {};
    for (const spec of item.params_schema || []) {
      const v = form.params[spec.key];
      if (v !== "" && v != null)
        params[spec.key] = ["int", "float"].includes(spec.type) ? Number(v) : v;
    }
    return { ...value, params };
  }
  async function run(testOnly) {
    setBusy(true);
    setError("");
    setTest(null);
    try {
      const p = payload();
      if (testOnly) {
        const d = await request("/api/plugins/test", {
          body: { config: p, q },
        });
        setTest(d);
      } else {
        await request("/api/plugins/save", { body: p });
        onSaved();
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  const input = (key, label, type = "text") => (
    <Field label={label}>
      <input
        type={type}
        value={form[key]}
        onChange={(e) => update(key, e.target.value)}
      />
    </Field>
  );
  return (
    <Modal
      title={item ? "编辑插件：" + item.display_name : "添加自定义插件"}
      onClose={onClose}
      busy={busy}
      actions={
        <Button primary disabled={busy} onClick={() => run(false)}>
          保存插件
        </Button>
      }
    >
      {error && <Alert error>{error}</Alert>}
      <div className="field-grid">
        {input("display_name", "显示名")}
        {input("order", "排序（0–999）", "number")}
      </div>
      {input("description", "描述")}
      {custom ? (
        <>
          {input("search_url", "搜索 URL 模板（必须包含 {q}）")}
          <div className="field-grid">
            <Field label="请求方法">
              <select
                value={form.method}
                onChange={(e) => update("method", e.target.value)}
              >
                <option>GET</option>
                <option>POST</option>
              </select>
            </Field>
            {input("timeout", "超时（秒）", "number")}
            {input("limit", "融合搜索条数", "number")}
            {input("result_path", "结果数组路径")}
          </div>
          <Field label="请求头（JSON 对象）">
            <textarea
              rows="3"
              value={form.headers}
              onChange={(e) => update("headers", e.target.value)}
              spellCheck={false}
            />
          </Field>
          <h4>响应字段映射</h4>
          <div className="field-grid">
            {[
              "title",
              "artist",
              "album",
              "date",
              "track",
              "genre",
              "cover",
              "id",
            ].map((k) => (
              <Field label={k + (k === "title" ? " *" : "")} key={k}>
                <input
                  value={form.fields[k] || ""}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      fields: { ...f.fields, [k]: e.target.value },
                    }))
                  }
                />
              </Field>
            ))}
          </div>
          <div className="test-row">
            <input
              aria-label="插件测试检索词"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <Button disabled={busy} onClick={() => run(true)}>
              测试连接
            </Button>
          </div>
          {test && (
            <Alert>
              {test.ok === false ? "测试失败" : "测试已返回"} ·{" "}
              {test.error || test.message || "请核对返回样本"}
              {test.results && (
                <pre className="plugin-test-result">
                  {JSON.stringify(test.results.slice(0, 3), null, 2)}
                </pre>
              )}
            </Alert>
          )}
        </>
      ) : (
        <>
          {(item.params_schema || []).map((spec) => (
            <Field label={spec.label || spec.key} key={spec.key}>
              {spec.options ? (
                <select
                  value={form.params[spec.key] ?? spec.default ?? ""}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      params: { ...f.params, [spec.key]: e.target.value },
                    }))
                  }
                >
                  {spec.options.map((o) => (
                    <option key={o.value ?? o} value={o.value ?? o}>
                      {o.label ?? o}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type={
                    ["int", "float"].includes(spec.type)
                      ? "number"
                      : /secret|token|cookie|key/i.test(spec.key)
                        ? "password"
                        : "text"
                  }
                  min={spec.min}
                  max={spec.max}
                  step={spec.type === "float" ? "any" : "1"}
                  value={form.params[spec.key] ?? spec.default ?? ""}
                  onChange={(e) =>
                    setForm((f) => ({
                      ...f,
                      params: { ...f.params, [spec.key]: e.target.value },
                    }))
                  }
                />
              )}
            </Field>
          ))}
        </>
      )}
    </Modal>
  );
}
export default function Plugins({ active }) {
  const app = useApp(),
    [plugins, setPlugins] = useState([]),
    [removed, setRemoved] = useState(false),
    [editing, setEditing] = useState(undefined),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  async function load(signal) {
    try {
      const d = await request(
        "/api/plugins" + (removed ? "?include_removed=1" : ""),
        { signal },
      );
      setPlugins(d.plugins || []);
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
      await load(ctl.signal);
      if (!ctl.signal.aborted) timer = setTimeout(tick, 5000);
    }
    tick();
    return () => {
      ctl.abort();
      clearTimeout(timer);
    };
  }, [active, removed]);
  async function mutate(path, body) {
    setBusy(true);
    try {
      await request("/api/plugins/" + path, { body });
      await load();
      app.notify("插件状态已更新");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  function remove(p) {
    app.confirm({
      title: "删除插件「" + p.display_name + "」？",
      description: p.builtin
        ? "内置插件将移到已删除列表，可恢复。"
        : "自定义插件将永久删除；请确认已保存必要配置。",
      label: "确认删除",
      run: () => mutate("delete", { id: p.id }),
    });
  }
  return (
    <section
      className={"surface list-panel" + (!active ? " hidden" : "")}
      data-setting="plugins"
    >
      <div className="panel-title">
        <div>
          <h3>插件库</h3>
          <span className="small-label">
            {plugins.filter((p) => p.enabled && !p.removed).length} /{" "}
            {plugins.filter((p) => !p.removed).length} 启用
          </span>
        </div>
        <div className="plugin-actions">
          <label className="checkline">
            <input
              type="checkbox"
              checked={removed}
              onChange={(e) => setRemoved(e.target.checked)}
            />
            显示已删除
          </label>
          <Button onClick={() => load()}>刷新插件</Button>
          <Button primary onClick={() => setEditing(null)}>
            添加插件
          </Button>
        </div>
      </div>
      {error && <Alert error>{error}</Alert>}
      <p className="subtle">停用的源不参与检索；改动保存到 NAS，重启后保留。</p>
      {plugins.length ? (
        <div className="table-wrap">
          <table className="data-table">
            <thead>
              <tr>
                <th>插件</th>
                <th>调用统计</th>
                <th>启用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {plugins.map((p) => (
                <tr key={p.id}>
                  <td>
                    <b>{p.display_name}</b>
                    <span className="pill purple">
                      {KIND[p.kind] || p.kind}
                    </span>
                    <p className="small-label">{p.description}</p>
                    <small>
                      {p.id} · v{p.version || "—"}
                      {p.removed ? " · 已删除" : ""}
                    </small>
                  </td>
                  <td className="small-label">
                    调用 {p.stats?.calls || 0} 次 · 成功 {p.stats?.ok || 0}
                    <br />
                    失败 {p.stats?.fail || 0} · 平均 {p.stats?.avg_ms || 0}ms
                  </td>
                  <td>
                    {!p.removed && (
                      <input
                        type="checkbox"
                        aria-label={"启用 " + p.display_name}
                        checked={!!p.enabled}
                        disabled={busy}
                        onChange={(e) =>
                          mutate("toggle", {
                            id: p.id,
                            enabled: e.target.checked,
                          })
                        }
                      />
                    )}
                  </td>
                  <td>
                    <div className="btn-row">
                      {p.removed ? (
                        <Button
                          disabled={busy}
                          onClick={() => mutate("restore", { id: p.id })}
                        >
                          恢复
                        </Button>
                      ) : (
                        <>
                          <Button disabled={busy} onClick={() => setEditing(p)}>
                            编辑
                          </Button>
                          <Button disabled={busy} onClick={() => remove(p)}>
                            删除
                          </Button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="detail-empty">没有可用插件</p>
      )}
      {editing !== undefined && (
        <Editor
          item={editing}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined);
            load();
            app.notify("插件已保存");
          }}
        />
      )}
    </section>
  );
}
