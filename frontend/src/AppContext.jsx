import React, {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import { Alert, Button, Modal } from "./common";
import { PREFS } from "./workflow";
const Context = createContext();
export const useApp = () => useContext(Context);
export function AppProvider({ children }) {
  const [route, setRoute] = useState(() => readRoute()),
    [prefs, setPrefs] = useState(() => {
      try {
        return {
          ...PREFS,
          ...JSON.parse(localStorage.getItem("mtl_settings") || "{}"),
        };
      } catch {
        return { ...PREFS };
      }
    }),
    [notice, setNotice] = useState(null),
    [modal, setModal] = useState(null),
    [busy, setBusy] = useState(false),
    [confirmError, setConfirmError] = useState(""),
    [importJob, setImportJob] = useState(null),
    [historyPath, setHistoryPath] = useState(null);
  const guards = useRef(new Map()),
    lock = useRef(false),
    routeRef = useRef(route);
  routeRef.current = route;
  function notify(message, error = false) {
    setNotice({ message, error });
  }
  function registerGuard(key, guard) {
    guards.current.set(key, guard);
    return () => guards.current.delete(key);
  }
  function allowed(next) {
    return [...guards.current.values()].every((fn) => fn(next));
  }
  function navigate(view, tab) {
    const next = { view, tab: tab || "prefs" };
    if (!allowed(next)) return false;
    routeRef.current = next;
    setRoute(next);
    location.hash = hash(next);
    return true;
  }
  useEffect(() => {
    const fn = () => {
      const next = readRoute();
      if (hash(next) === hash(routeRef.current)) return;
      if (!allowed(next)) {
        history.replaceState(null, "", hash(routeRef.current));
        return;
      }
      setRoute(next);
    };
    window.addEventListener("hashchange", fn);
    return () => window.removeEventListener("hashchange", fn);
  }, []);
  function savePrefs(value) {
    localStorage.setItem("mtl_settings", JSON.stringify(value));
    setPrefs(value);
    notify("工作台偏好已保存到此浏览器");
  }
  function confirm(config) {
    setConfirmError("");
    setModal(config);
  }
  async function submit() {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setConfirmError("");
    try {
      await modal.run();
      setModal(null);
    } catch (e) {
      setConfirmError(
        e.message +
          (e.partial ? "；存在部分修改，请到修改记录核查，勿盲目重试。" : ""),
      );
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }
  return (
    <Context.Provider
      value={{
        route,
        navigate,
        prefs,
        savePrefs,
        notify,
        confirm,
        registerGuard,
        importJob,
        setImportJob,
        historyPath,
        setHistoryPath,
      }}
    >
      {children}
      {notice && (
        <div
          className={"app-toast" + (notice.error ? " bad" : "")}
          role={notice.error ? "alert" : "status"}
        >
          {notice.message}
          <button aria-label="关闭提示" onClick={() => setNotice(null)}>
            ×
          </button>
        </div>
      )}
      {modal && (
        <Modal
          title={modal.title}
          busy={busy}
          onClose={() => setModal(null)}
          actions={
            <Button primary disabled={busy} onClick={submit}>
              {busy ? "处理中…" : modal.label || "确认提交"}
            </Button>
          }
        >
          <p>{modal.description}</p>
          {modal.content}
          {confirmError && <Alert error>{confirmError}</Alert>}
        </Modal>
      )}
    </Context.Provider>
  );
}
function readRoute() {
  const [view, tab] = location.hash.slice(2).split("/");
  return {
    view: ["work", "tasks", "history", "logs", "organize", "settings"].includes(
      view,
    )
      ? view
      : "work",
    tab: ["prefs", "plugins", "net", "organize"].includes(tab) ? tab : "prefs",
  };
}
function hash(r) {
  return "#/" + r.view + (r.view === "settings" ? "/" + r.tab : "");
}
