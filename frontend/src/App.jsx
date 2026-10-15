import React, { useEffect, useState } from "react";
import { useApp } from "./AppContext";
import { request } from "./api";
import Workbench from "./Workbench";
import Jobs from "./Jobs";
import History from "./History";
import Logs from "./Logs";
import Preferences from "./Preferences";
import Network from "./Network";
import Plugins from "./Plugins";
import OrganizerFeature from "./OrganizerFeature";
const NAV = [
  ["work", "工作台"],
  ["tasks", "任务中心"],
  ["history", "修改记录"],
  ["logs", "实时日志"],
  ["organize", "自动整理"],
  ["settings", "设置"],
];
const SETTING_TABS = [
  ["prefs", "工作台偏好", "保存在本浏览器"],
  ["plugins", "插件库", "数据源启停与统计"],
  ["net", "网络代理", "服务端出网设置"],
  ["organize", "自动整理", "本地曲库整理规则"],
];
export default function App() {
  const app = useApp(),
    { route } = app,
    [system, setSystem] = useState(null),
    [clock, setClock] = useState(new Date());
  useEffect(() => {
    let timer;
    const ctl = new AbortController();
    async function tick() {
      try {
        setSystem(await request("/api/system", { signal: ctl.signal }));
      } catch {}
      if (!ctl.signal.aborted) timer = setTimeout(tick, 10000);
    }
    tick();
    return () => {
      ctl.abort();
      clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    const timer = setInterval(() => setClock(new Date()), 30000);
    return () => clearInterval(timer);
  }, []);
  return (
    <>
      <header className="appbar">
        <div className="brand">
          <div className="brand-icon">♫</div>
          <div>
            <h1>Music Info</h1>
            <small>YOUR LOCAL MUSIC WORKBENCH</small>
          </div>
        </div>
        <nav className="bar-nav" aria-label="主导航">
          {NAV.map(([key, label]) => (
            <button
              key={key}
              className={"nav-btn" + (route.view === key ? " active" : "")}
              data-view={key}
              aria-current={route.view === key ? "page" : undefined}
              onClick={() => app.navigate(key, route.tab)}
            >
              {label}
              {key === "tasks" && system?.storage?.active_jobs
                ? " (" + system.storage.active_jobs + ")"
                : ""}
            </button>
          ))}
        </nav>
        <div className="bar-right">
          <span className="db-pill">
            <i className="status-dot" />
            {system?.storage?.database || "SQLite"} ·{" "}
            {system?.storage?.journal_mode?.toUpperCase() || "WAL"}
          </span>
          <button
            className="btn icon-btn"
            aria-label="打开设置"
            onClick={() => app.navigate("settings")}
          >
            ⚙
          </button>
          <small>
            {new Intl.DateTimeFormat("zh-CN", {
              timeZone: "Asia/Tokyo",
              hour: "2-digit",
              minute: "2-digit",
              hour12: false,
            }).format(clock)}{" "}
            JST
          </small>
        </div>
      </header>
      <Workbench active={route.view === "work"} />
      <main
        className={"page app-pages" + (route.view === "work" ? " hidden" : "")}
      >
        {route.view === "settings" && (
          <div className="page-heading">
            <div>
              <span className="eyebrow">PREFERENCES &amp; PLUGINS</span>
              <h2>设置与插件库</h2>
              <p>工作台偏好保存在浏览器；插件、代理与整理配置保存到 NAS。</p>
            </div>
          </div>
        )}
        <div
          className={
            route.view === "settings" ? "settings-shell" : "route-pages"
          }
        >
          {route.view === "settings" && (
            <nav className="settings-tabs" aria-label="设置分区">
              {SETTING_TABS.map(([key, label, description]) => (
                <button
                  className={
                    "settings-tab" + (route.tab === key ? " active" : "")
                  }
                  key={key}
                  data-tab={key}
                  onClick={() => app.navigate("settings", key)}
                >
                  <b>{label}</b>
                  <span>{description}</span>
                </button>
              ))}
            </nav>
          )}
          <div className="route-content">
            <Jobs active={route.view === "tasks"} />
            <History active={route.view === "history"} />
            <Logs active={route.view === "logs"} />
            <Preferences
              active={route.view === "settings" && route.tab === "prefs"}
            />
            <Plugins
              active={route.view === "settings" && route.tab === "plugins"}
            />
            <Network
              active={route.view === "settings" && route.tab === "net"}
            />
            <div
              className={
                route.view === "settings" && route.tab === "organize"
                  ? "surface settings-card"
                  : ""
              }
            >
              <OrganizerFeature
                active={
                  route.view === "organize" ||
                  (route.view === "settings" && route.tab === "organize")
                }
                settingsMode={route.view === "settings"}
              />
            </div>
          </div>
        </div>
      </main>
    </>
  );
}
