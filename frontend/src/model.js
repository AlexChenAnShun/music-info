export const DIRS = [
  ["source", "待整理文件夹（源）"],
  ["library", "已整理归档目录"],
  ["duplicates", "已存在（重复）文件夹"],
  ["junk", "冗余数据文件夹"],
];
export const TABS = [
  ["move", "待移动"],
  ["wash", "洗版替换"],
  ["duplicate", "重复"],
  ["junk", "冗余/失败"],
  ["sidecar", "附属文件"],
];
export const STATES = {
  scanning: "扫描中",
  ready: "待执行",
  applying: "执行中",
  done: "已完成",
  cancelled: "已停止",
  failed: "失败",
};
export const WASH = {
  coexist: "并存（默认，不比较音质）",
  prefer_better: "音质更优时替换",
  always_replace: "总是替换",
  skip: "从不替换（新文件进「已存在」）",
};
export const TERMINAL = new Set([
  "completed",
  "failed",
  "cancelled",
  "partial",
  "interrupted",
]);
export const DEFAULTS = {
  source: "music/未整理/待整理",
  duplicates: "music/未整理/已存在",
  junk: "music/未整理/冗余数据",
  library: "music/已整理",
  track_prefix: false,
  track_sep: ". ",
  follow_sidecars: true,
  unknown_artist: "未知歌手",
  unknown_album: "未知专辑",
  name_template: "",
  dir_template: "{artist}/{album}",
  classify_rules: "",
  wash_mode: "coexist",
  wash_max_mb: 0,
  host_root: "/vol1/1000/Docker/music-tag-lite",
};
export const EXAMPLES = {
  genre:
    '- name: 爵士\n  match:\n    genre: [jazz, 爵士]\n  path: "爵士/{artist}/{album}"\n- name: 华语\n  match:\n    genre: [mandopop, c-pop, 华语]\n  path: "华语/{artist}/{album}"\n',
  decade:
    '- name: 2020s\n  match:\n    year_min: 2020\n  path: "{decade}/{artist}/{album}"\n- name: 2010s\n  match:\n    year_min: 2010\n    year_max: 2019\n  path: "{decade}/{artist}/{album}"\n- name: 九十年代\n  match:\n    year_min: 1990\n    year_max: 1999\n  path: "{decade}/{artist}/{album}"\n',
  letter:
    '- name: 字母 A-Z\n  match:\n    artist_initial: [a, b, c, d, e, f, g, h, i, j, k, l, m, n, o, p, q, r, s, t, u, v, w, x, y, z]\n  path: "{artist_initial}/{artist}/{album}"\n- name: 其它\n  match: {}\n  path: "其它/{artist}/{album}"\n',
};
export function counts(plan) {
  const c = plan?.counts ?? {};
  return {
    move: c.move || 0,
    wash: c.wash || 0,
    duplicate: c.duplicate || 0,
    junk: (c.junk || 0) + (c.error || 0),
    sidecar: c.sidecar || 0,
  };
}
export function pendingCount(plan) {
  const c = counts(plan);
  return c.move + c.wash + c.duplicate + c.junk;
}
export function canApply(plan, writable, active) {
  return (
    !!plan &&
    plan.status === "ready" &&
    writable &&
    !active &&
    pendingCount(plan) > 0
  );
}
export function bytes(value) {
  const n = Number(value) || 0;
  return n >= 2 ** 30
    ? (n / 2 ** 30).toFixed(2) + " GB"
    : n >= 2 ** 20
      ? (n / 2 ** 20).toFixed(1) + " MB"
      : n >= 1024
        ? (n / 1024).toFixed(1) + " KB"
        : n + " B";
}
export function absolute(root, rel) {
  return root ? root.replace(/\/+$/, "") + (rel ? "/" + rel : "") : rel || "/";
}
export function date(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Tokyo",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value * 1000));
}
export function settingsBody(form) {
  const value = { ...form, wash_max_mb: Number(form.wash_max_mb) };
  if (!Number.isInteger(value.wash_max_mb) || value.wash_max_mb < 0)
    throw new Error("体积上限必须是大于等于 0 的整数");
  return value;
}
