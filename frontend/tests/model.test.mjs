import { test } from "node:test";
import assert from "node:assert/strict";
import {
  absolute,
  bytes,
  canApply,
  counts,
  DEFAULTS,
  pendingCount,
  settingsBody,
} from "../src/model.js";
test("counts combine error and junk without including sidecars", () => {
  const p = { counts: { move: 3, wash: 1, junk: 2, error: 4, sidecar: 5 } };
  assert.equal(counts(p).junk, 6);
  assert.equal(pendingCount(p), 10);
});
test("only ready writable idle plan can execute", () => {
  const p = { status: "ready", counts: { move: 2 } };
  assert.equal(canApply(p, true, null), true);
  for (const status of ["scanning", "applying", "done", "cancelled", "failed"])
    assert.equal(canApply({ ...p, status }, true, null), false);
  assert.equal(canApply(p, false, null), false);
  assert.equal(canApply(p, true, { job_id: "job" }), false);
  assert.equal(canApply({ status: "ready" }, true, null), false);
});
test("path mapping removes trailing slash and handles root", () => {
  assert.equal(absolute("/media/", "music/待整理"), "/media/music/待整理");
  assert.equal(absolute("/media/", ""), "/media");
});
test("format bytes", () => {
  assert.equal(bytes(0), "0 B");
  assert.equal(bytes(1024), "1.0 KB");
  assert.equal(bytes(2 ** 20), "1.0 MB");
});
test("settings validation preserves whitespace and rejects invalid limits", () => {
  assert.equal(settingsBody(DEFAULTS).track_sep, ". ");
  for (const v of [-1, NaN, 1.5, "oops"])
    assert.throws(() => settingsBody({ ...DEFAULTS, wash_max_mb: v }));
  assert.equal(
    settingsBody({ ...DEFAULTS, wash_max_mb: "16" }).wash_max_mb,
    16,
  );
});
