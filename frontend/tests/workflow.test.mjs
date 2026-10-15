import { test } from "node:test";
import assert from "node:assert/strict";
import {
  applyItem,
  changes,
  FIELDS,
  filename,
  proposal,
  safeUrl,
  splitLyrics,
} from "../src/workflow.js";
test("exactly 17 metadata fields", () => assert.equal(FIELDS.length, 17));
test("manual diff respects format capabilities", () =>
  assert.deepEqual(
    changes(
      { title: "x", capabilities: { fields: ["title"] } },
      { title: "y", genre: "Pop" },
    ),
    [{ key: "title", before: "x", after: "y" }],
  ));
test("only empty and cover safety gates", () => {
  const p = proposal(
    { title: "old", has_cover: true },
    { title: "new", genre: "Pop", cover: "https://example.com/a.jpg" },
    "",
    { onlyEmpty: true, includeCover: true },
  );
  assert.deepEqual(p, { fields: { genre: "Pop" }, cover_url: null });
});
test("batch write requires strong snapshot version", () => {
  assert.throws(() =>
    applyItem(
      { path: "a", original: {}, candidate: { title: "x" } },
      { onlyEmpty: true },
    ),
  );
  assert.equal(
    applyItem(
      {
        path: "a",
        original: { file_version: "a".repeat(64) },
        candidate: { title: "x" },
      },
      { onlyEmpty: true },
    ).expected_version,
    "a".repeat(64),
  );
});
test("URLs reject executable schemes", () => {
  assert.equal(safeUrl("javascript:alert(1)"), "");
  assert.equal(safeUrl("data:image/png;base64,x"), "");
});
test("LRC split preserves timeline and strips plain lyric", () =>
  assert.deepEqual(splitLyrics("[00:02.00]hello"), {
    lyrics: "hello",
    lrc: "[00:02.00]hello",
  }));
test("filename keeps extension and sanitizes unsafe chars", () =>
  assert.equal(
    filename("{artist} - {title}", {
      artist: "A/B",
      title: "Song",
      name: "old.flac",
    }),
    "A_B - Song.flac",
  ));
