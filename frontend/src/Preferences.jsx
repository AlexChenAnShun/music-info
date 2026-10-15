import React, { useEffect, useState } from "react";
import { Button, Field } from "./common";
import { useApp } from "./AppContext";
import { PREFS, SOURCES } from "./workflow";
export default function Preferences({ active }) {
  const app = useApp(),
    [value, setValue] = useState(app.prefs);
  useEffect(() => setValue(app.prefs), [app.prefs]);
  const update = (k, v) => setValue((p) => ({ ...p, [k]: v }));
  return (
    <section
      className={"surface settings-card" + (!active ? " hidden" : "")}
      data-setting="prefs"
    >
      <h3>工作台偏好</h3>
      <p className="subtle">保存在此浏览器，保存后同步到工作台。</p>
      <Field label="默认匹配来源">
        <select
          value={value.source}
          onChange={(e) => update("source", e.target.value)}
        >
          {Object.entries(SOURCES).map(([k, v]) => (
            <option value={k} key={k}>
              {v}
            </option>
          ))}
        </select>
      </Field>
      {[
        ["onlyEmpty", "默认只补空白"],
        ["includeLyrics", "默认尝试获取歌词"],
        ["includeCover", "默认预览封面"],
      ].map(([k, label]) => (
        <label className="checkline" key={k}>
          <input
            type="checkbox"
            checked={!!value[k]}
            onChange={(e) => update(k, e.target.checked)}
          />
          {label}
        </label>
      ))}
      <Field label="文件重命名模板">
        <input
          value={value.template}
          onChange={(e) => update("template", e.target.value)}
        />
      </Field>
      <p className="helper">
        支持 {"{artist}、{title}、{album}、{track}、{date}、{year}"}。
      </p>
      <div className="dialog-actions">
        <Button
          onClick={() => {
            setValue(PREFS);
            app.savePrefs(PREFS);
          }}
        >
          恢复默认
        </Button>
        <Button
          primary
          onClick={() =>
            app.savePrefs({
              ...value,
              template: value.template.trim() || PREFS.template,
            })
          }
        >
          保存设置
        </Button>
      </div>
    </section>
  );
}
