export const FIELD_LABEL = {
  title: "歌曲标题",
  artist: "艺术家",
  album: "专辑",
  albumartist: "专辑艺术家",
  date: "发行日期",
  genre: "流派",
  language: "语言",
  albumtype: "专辑类型",
  version: "专辑版本",
  track: "音轨号",
  tracktotal: "总音轨数",
  disc: "光盘编号",
  disctotal: "总光盘数",
  comment: "简介 / 注释",
  description: "描述",
  lyrics: "纯文本歌词",
  lrc: "同步歌词 LRC",
};
export const FIELDS = Object.keys(FIELD_LABEL);
export const SOURCES = {
  smart: "智能匹配 · 多源融合",
  netease: "网易云优先 · MB 备选",
  migu: "咪咕音乐",
  qq: "QQ 音乐",
  kugou: "酷狗音乐",
  kuwo: "酷我音乐",
  itunes: "iTunes / Apple Music",
  mb: "MusicBrainz",
};
export const PREFS = {
  source: "smart",
  onlyEmpty: true,
  includeLyrics: true,
  includeCover: true,
  template: "{artist} - {title}",
};
export const JOB_STATES = {
  pending: "等待执行",
  running: "正在执行",
  completed: "已完成",
  partial: "部分失败",
  failed: "失败",
  cancelled: "已停止",
  interrupted: "已中断",
};
export const JOB_KINDS = {
  preview: "匹配预览",
  apply: "确认写入",
  organize_scan: "整理扫描",
  organize_apply: "整理执行",
};
export const terminal = (status) =>
  ["completed", "partial", "failed", "cancelled", "interrupted"].includes(
    status,
  );
export function safeUrl(value) {
  try {
    const u = new URL(value);
    return ["http:", "https:"].includes(u.protocol) ? u.href : "";
  } catch {
    return "";
  }
}
export function splitLyrics(text) {
  const lrc = String(text || "");
  return /\[\d{1,3}:\d{2}(?:[.:]\d+)?\]/.test(lrc)
    ? {
        lyrics: lrc
          .replace(/\[[^\]]*\]/g, "")
          .split("\n")
          .map((x) => x.trim())
          .filter(Boolean)
          .join("\n"),
        lrc,
      }
    : { lyrics: lrc, lrc: "" };
}
export function changes(original, draft) {
  const allowed = original.capabilities?.fields ?? FIELDS;
  return FIELDS.filter(
    (k) =>
      allowed.includes(k) &&
      String(original[k] ?? "") !== String(draft[k] ?? ""),
  ).map((key) => ({
    key,
    before: String(original[key] ?? ""),
    after: String(draft[key] ?? ""),
  }));
}
export function proposal(original, candidate, lyric, options) {
  const allowed = original?.capabilities?.fields ?? FIELDS;
  const values = {
    ...candidate,
    ...splitLyrics(candidate?._lrc || lyric || candidate?.lyrics || ""),
  };
  const fields = {};
  for (const key of FIELDS) {
    const value = String(values[key] ?? "");
    if (
      allowed.includes(key) &&
      value &&
      value !== String(original?.[key] ?? "") &&
      (!options.onlyEmpty || !String(original?.[key] ?? "").trim())
    )
      fields[key] = value;
  }
  const coverUrl = safeUrl(candidate?.cover || candidate?.album_img);
  return {
    fields,
    cover_url:
      options.includeCover &&
      original?.capabilities?.cover !== false &&
      (!options.onlyEmpty || !original?.has_cover) &&
      coverUrl
        ? coverUrl
        : null,
  };
}
export function applyItem(row, options) {
  if (!row.original || !row.candidate || row.error || row.done || row.loading)
    throw new Error("候选尚未就绪");
  if (!/^[a-f0-9]{64}$/.test(row.original.file_version || ""))
    throw new Error("预览缺少有效文件版本，请重新生成");
  return {
    path: row.path,
    expected_version: row.original.file_version,
    ...proposal(row.original, row.candidate, row.lyric, options),
  };
}
export function filename(template, t) {
  const ext = t.name.match(/\.[^.]+$/)?.[0] || "";
  const values = { ...t, year: String(t.date || "").slice(0, 4) };
  let stem = String(template || PREFS.template).replace(
    /\{([a-z]+)(?::0?(\d+)d)?\}/g,
    (_, k, width) =>
      width
        ? String(values[k] || "").padStart(Number(width), "0")
        : String(values[k] || ""),
  );
  stem = stem.replace(/[\\/:*?"<>|]/g, "_").trim();
  if (!stem) throw new Error("命名模板未生成有效文件名");
  return stem + ext;
}
export function importRows(job) {
  return (job.items || []).map((item) => {
    const r = item.result;
    return r?.candidate
      ? { ...r, selected: false, done: false, error: null }
      : {
          path: item.path,
          name: item.path.split("/").at(-1),
          original: r?.original,
          selected: false,
          error: item.status === "failed" ? item.error || "任务失败" : null,
          noMatch: item.status !== "failed",
          note: terminal(item.status)
            ? "未找到候选，可更换来源后重试。"
            : "尚未完成预览",
        };
  });
}
