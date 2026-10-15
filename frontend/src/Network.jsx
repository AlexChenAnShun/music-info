import React, { useEffect, useState } from "react";
import { request } from "./api";
import { Alert, Button, Field } from "./common";
import { useApp } from "./AppContext";
export default function Network({ active }) {
  const app = useApp(),
    [proxy, setProxy] = useState({ url: "", enabled: false }),
    [error, setError] = useState(""),
    [result, setResult] = useState(""),
    [busy, setBusy] = useState(false),
    [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!active || loaded) return;
    const ctl = new AbortController();
    request("/api/settings", { signal: ctl.signal })
      .then((d) => {
        setProxy(d.settings?.proxy || { url: "", enabled: false });
        setLoaded(true);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => ctl.abort();
  }, [active, loaded]);
  async function execute(test) {
    setBusy(true);
    setError("");
    setResult("");
    try {
      if (test) {
        const d = await request("/api/settings/test_proxy", {
          body: { url: proxy.url.trim() },
        });
        const r = d.results?.find((x) => x.ok) || d.results?.[0] || {};
        setResult(
          d.ok
            ? "✓ 连接成功 · " + r.ms + "ms"
            : "✗ 连接失败：" + (r.error || r.status || "未知原因"),
        );
      } else {
        const d = await request("/api/settings", {
          body: { proxy: { ...proxy, url: proxy.url.trim() } },
        });
        setProxy(d.settings.proxy);
        app.notify("代理配置已保存并生效");
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      className={"surface settings-card" + (!active ? " hidden" : "")}
      data-setting="net"
    >
      <h3>网络代理</h3>
      <p className="subtle">服务端全部出网请求使用此代理，容器重启后保留。</p>
      {error && <Alert error>{error}</Alert>}
      <Field label="HTTP 代理">
        <input
          value={proxy.url}
          onChange={(e) => setProxy((p) => ({ ...p, url: e.target.value }))}
          placeholder="http://10.0.0.77:7890"
        />
      </Field>
      <label className="checkline">
        <input
          type="checkbox"
          checked={proxy.enabled}
          onChange={(e) =>
            setProxy((p) => ({ ...p, enabled: e.target.checked }))
          }
        />
        启用代理
      </label>
      {result && <Alert>{result}</Alert>}
      <div className="dialog-actions">
        <Button disabled={busy} onClick={() => execute(true)}>
          测试连接
        </Button>
        <Button
          primary
          disabled={busy || !loaded}
          onClick={() => execute(false)}
        >
          保存代理
        </Button>
      </div>
    </section>
  );
}
