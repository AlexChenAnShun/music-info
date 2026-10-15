import React, { useCallback, useEffect, useRef, useState } from "react";
import { request } from "./api";
import {
  DEFAULTS,
  canApply,
  pendingCount,
  settingsBody,
  TERMINAL,
} from "./model";
import { Alert, Button, Modal } from "./common";
import Organizer from "./Organizer";
import Settings from "./Settings";
import { useApp } from "./AppContext";
export default function OrganizerFeature({ active, settingsMode }) {
  const app = useApp();
  const [status, setStatus] = useState(null),
    [plans, setPlans] = useState([]),
    [plan, setPlan] = useState(null),
    [form, setForm] = useState(null),
    [saved, setSaved] = useState(null),
    [activeJob, setActive] = useState(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [modal, setModal] = useState(null);
  const dirty = !!form && JSON.stringify(form) !== JSON.stringify(saved),
    dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const planRef = useRef(plan);
  planRef.current = plan;
  const refreshId = useRef(0),
    openId = useRef(0),
    mounted = useRef(true),
    operation = useRef(false);
  const refresh = useCallback(async (force = false, signal) => {
    const n = ++refreshId.current;
    try {
      const [s, p] = await Promise.all([
        request("/api/organize/status", { signal }),
        request("/api/organize/plans", { signal }),
      ]);
      if (!mounted.current || n !== refreshId.current) return;
      setStatus(s);
      setPlans(p.plans || []);
      if (force || !dirtyRef.current) {
        const cfg = { ...DEFAULTS, ...s.settings };
        setSaved(cfg);
        setForm(cfg);
      }
      const running = s.running_plans?.[0];
      setActive(running || null);
      setError("");
    } catch (e) {
      if (e.name !== "AbortError" && mounted.current && n === refreshId.current)
        setError("读取失败：" + e.message);
    }
  }, []);
  async function open(id) {
    const n = ++openId.current;
    try {
      const d = await request("/api/organize/plan/" + encodeURIComponent(id));
      if (n === openId.current && mounted.current) {
        setPlan(d);
        if (d.scan_job)
          setActive({ job_id: d.scan_job, plan_id: id, kind: "organize_scan" });
      }
    } catch (e) {
      setError(e.message);
    }
  }
  useEffect(() => {
    mounted.current = true;
    const ctl = new AbortController();
    refresh(false, ctl.signal);
    return () => {
      mounted.current = false;
      refreshId.current++;
      openId.current++;
      ctl.abort();
    };
  }, [refresh]);
  useEffect(() => {
    let timer;
    const ctl = new AbortController();
    if (!activeJob?.job_id) return;
    async function tick() {
      try {
        const [job, p] = await Promise.all([
          request("/api/tasks/" + encodeURIComponent(activeJob.job_id), {
            signal: ctl.signal,
          }),
          request(
            "/api/organize/plan/" + encodeURIComponent(activeJob.plan_id),
            { signal: ctl.signal },
          ),
        ]);
        if (ctl.signal.aborted) return;
        if (!planRef.current || planRef.current.plan_id === activeJob.plan_id)
          setPlan(p);
        setError("");
        if (TERMINAL.has(job.status)) {
          setActive(null);
          setMessage(
            "后台任务已结束（" + job.status + "），请核对计划明细及任务日志。",
          );
          await refresh(false, ctl.signal);
          return;
        }
      } catch (e) {
        if (!ctl.signal.aborted)
          setError("任务状态暂时无法读取，后台任务可能仍在运行：" + e.message);
      }
      if (!ctl.signal.aborted) timer = setTimeout(tick, 1500);
    }
    tick();
    return () => {
      ctl.abort();
      clearTimeout(timer);
    };
  }, [activeJob?.job_id, refresh]);
  useEffect(
    () =>
      app.registerGuard("organizer", (next) => {
        if (
          dirtyRef.current &&
          (next.view !== "settings" || next.tab !== "organize")
        ) {
          if (!window.confirm("自动整理配置有未保存的修改，舍弃后离开？"))
            return false;
          setForm(saved);
        }
        return true;
      }),
    [saved],
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
  useEffect(() => {
    if (active) refresh();
  }, [active, refresh]);
  async function action(fn) {
    if (operation.current) return;
    operation.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      operation.current = false;
      setBusy(false);
    }
  }
  const post = (path, body) => request(path, { method: "POST", body });
  const save = () =>
    action(async () => {
      const d = await post("/api/organize/settings", settingsBody(form));
      await refresh(true);
      setMessage(
        d.created?.length
          ? "配置已保存；创建目录：" + d.created.join("、")
          : "整理配置已保存，不会移动音乐文件。",
      );
    });
  const scan = () =>
    action(async () => {
      const d = await post("/api/organize/scan");
      await open(d.plan_id);
      setActive({
        job_id: d.job_id,
        plan_id: d.plan_id,
        kind: "organize_scan",
      });
      setMessage("只读扫描已创建，不会移动文件。");
    });
  const stop = () =>
    action(async () => {
      if (activeJob) {
        await post(
          "/api/tasks/" + encodeURIComponent(activeJob.job_id) + "/cancel",
        );
        setMessage(
          "已请求安全停止；当前操作完成后停止剩余项。已完成的移动不会自动撤销。",
        );
      }
    });
  const confirmApply = () => {
    if (!canApply(plan, status?.media_writable, activeJob)) return;
    setModal({
      type: "apply",
      planId: plan.plan_id,
      count: pendingCount(plan),
    });
  };
  async function submitModal() {
    const current = modal;
    await action(async () => {
      if (current.type === "apply") {
        const latest = await request(
          "/api/organize/plan/" + encodeURIComponent(current.planId),
        );
        if (!canApply(latest, status.media_writable, activeJob))
          throw new Error("计划状态已变化，请刷新后重新核对。");
        if (pendingCount(latest) !== current.count)
          throw new Error("计划条目数已变化，请关闭确认框并重新核对。");
        const d = await post("/api/organize/apply", {
          plan_id: current.planId,
          confirmed: true,
        });
        setActive({
          job_id: d.job_id,
          plan_id: d.plan_id,
          kind: "organize_apply",
        });
        setMessage(
          "整理任务已创建，本次 " +
            d.total +
            " 项" +
            (d.remaining
              ? "；剩余 " + d.remaining + " 项需再次确认执行。"
              : "。"),
        );
      }
      if (current.type === "discard") {
        await post(
          "/api/organize/plan/" +
            encodeURIComponent(current.planId) +
            "/discard",
        );
        if (planRef.current?.plan_id === current.planId) setPlan(null);
        await refresh();
        setMessage("计划已丢弃，音乐文件未改动。");
      }
      if (current.type === "reset") {
        await post("/api/organize/settings", status.defaults || DEFAULTS);
        await refresh(true);
        setMessage("已恢复默认整理配置，音乐文件未改动。");
      }
      setModal(null);
    });
  }
  const notices = (
    <>
      {error && (
        <Alert error>
          {error}
          <Button disabled={busy} onClick={() => refresh()}>
            重新读取
          </Button>
        </Alert>
      )}
      {message && <Alert>{message}</Alert>}
    </>
  );
  return (
    <section
      className={!active ? "hidden" : ""}
      data-page={settingsMode ? "organize-settings" : "organize"}
    >
      {notices}
      {status && form ? (
        settingsMode ? (
          <Settings
            status={status}
            form={form}
            setForm={setForm}
            dirty={dirty}
            busy={busy}
            onSave={save}
            onReset={() => setModal({ type: "reset" })}
            onReload={() => {
              if (!dirty || window.confirm("重新读取会舍弃未保存修改，继续？"))
                refresh(true);
            }}
          />
        ) : (
          <Organizer
            status={status}
            plans={plans}
            plan={plan}
            setPlan={setPlan}
            active={activeJob}
            busy={busy}
            onRefresh={() => refresh()}
            onSettings={() => app.navigate("settings", "organize")}
            onScan={scan}
            onApply={confirmApply}
            onDiscard={() =>
              setModal({ type: "discard", planId: plan.plan_id })
            }
            onStop={stop}
            onOpen={open}
          />
        )
      ) : (
        <p role="status">正在读取整理状态…</p>
      )}
      {modal && (
        <Modal
          busy={busy}
          title={
            modal.type === "apply"
              ? `确认执行整理（${modal.count} 项）？`
              : modal.type === "discard"
                ? "丢弃这个整理计划？"
                : "恢复默认整理配置？"
          }
          onClose={() => setModal(null)}
          actions={
            <Button primary disabled={busy} onClick={submitModal}>
              {modal.type === "apply"
                ? "确认执行整理"
                : modal.type === "discard"
                  ? "丢弃计划"
                  : "恢复默认"}
            </Button>
          }
        >
          {modal.type === "apply" ? (
            <>
              <p className="rx-path">计划：{modal.planId}</p>
              {modal.count > 500 && (
                <Alert error>本次超过 500 项，建议先使用小批次核验。</Alert>
              )}
              <p>
                按清单移动歌曲和附属文件；重复与旧版进「已存在」，无用文件进「冗余数据」。
              </p>
              <Alert error>
                这一步会真正移动文件，不删除、不覆盖，版本冲突停止对应项目。
              </Alert>
            </>
          ) : modal.type === "discard" ? (
            <p>只删除计划，不删除或移动音乐文件。</p>
          ) : (
            <p>
              恢复目录、命名、分类与洗版默认设置，可能创建缺失目录，但不会移动音乐。
            </p>
          )}
          {error && <Alert error>{error}</Alert>}
        </Modal>
      )}
    </section>
  );
}
