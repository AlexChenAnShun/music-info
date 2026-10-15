/* Full React regression: every endpoint is mocked; no real NAS or music is accessed. */
const assert = require("node:assert/strict"),
  fs = require("node:fs"),
  path = require("node:path");
const { chromium } = require("../frontend/node_modules/playwright");
const root = path.resolve(__dirname, "../backend/static"),
  base = "http://music-info.test";
(async () => {
  const { DEFAULTS } = await import("../frontend/src/model.js");
  const browser = await chromium.launch({
    headless: true,
    channel: process.env.UI_BROWSER_CHANNEL || undefined,
  });
  const results = [];
  async function scenario(name, fn, opts = {}) {
    const ctx = await browser.newContext({
        viewport: { width: 1568, height: 900 },
      }),
      page = await ctx.newPage();
    page.setDefaultTimeout(12000);
    const errors = [],
      writes = [],
      gets = [];
    let cfg = { ...DEFAULTS },
      proxy = { url: "", enabled: false },
      statusFailed = !!opts.failedStatus,
      taskKind = "preview",
      jobPhase = "completed";
    const version = "a".repeat(64),
      track = {
        path: "fixture.flac",
        name: "测试歌曲 - 艺术家.flac",
        title: "测试歌曲",
        artist: "艺术家",
        album: "原专辑",
        has_cover: false,
        lyrics: "原歌词",
        lrc: "[00:01.00]原歌词",
        file_version: version,
        size: 15400000,
        tech: {
          duration: 180,
          bitrate: 1411,
          sample_rate: 44100,
          bits: 16,
          channels: 2,
        },
        capabilities: {
          fields: [
            "title",
            "artist",
            "album",
            "albumartist",
            "date",
            "track",
            "genre",
            "lyrics",
            "lrc",
            "tracktotal",
            "disc",
            "disctotal",
            "language",
            "albumtype",
            "comment",
            "description",
            "version",
          ],
          cover: true,
        },
      };
    const candidate = {
      id: "c1",
      source: "migu",
      title: "候选歌曲",
      artist: "候选艺术家",
      album: "候选专辑",
      genre: "Pop",
      match_percent: 92,
    };
    const preview = {
      path: track.path,
      name: track.name,
      original: track,
      candidate,
      candidates: [candidate, { ...candidate, id: "c2", album: "第二版本" }],
      score: 92,
      lyric: "新歌词",
    };
    const jobs = [
      {
        id: "history-job",
        kind: opts.jobKind || "preview",
        status: "completed",
        created: 1792070000,
        total: 1,
        counts: { completed: 1 },
        items: [
          {
            path: track.path,
            status: "completed",
            result: opts.noMatch
              ? { path: track.path, original: track, candidate: null }
              : preview,
          },
        ],
        logs: [{ at: 1792070000, level: "INFO", message: "完成" }],
      },
    ];
    let plan = {
      plan_id: "fixture-plan",
      created: 1792070000,
      status: opts.planStatus || "ready",
      cfg: { ...DEFAULTS },
      counts: {
        move: 105,
        wash: 1,
        duplicate: 1,
        junk: 1,
        error: 1,
        sidecar: 1,
      },
      applied: {},
    };
    let plugins = [
      {
        id: "migu",
        display_name: "咪咕",
        kind: "tag_source",
        builtin: true,
        enabled: true,
        order: 1,
        description: "标签源",
        version: "1",
        params: { limit: 8 },
        params_schema: [
          {
            key: "limit",
            label: "检索条数",
            type: "int",
            default: 8,
            min: 1,
            max: 50,
          },
        ],
        stats: { calls: 1, ok: 1, fail: 0, avg_ms: 30 },
      },
    ];
    await page.addInitScript(() => {
      window.__stream = { opened: 0, closed: 0 };
      window.EventSource = class {
        constructor() {
          window.__stream.opened++;
          this.timer = setTimeout(() => {
            this.onopen?.();
            this.onmessage?.({
              data: JSON.stringify({
                id: 2,
                ts: Date.now(),
                level: "ERROR",
                src: "test",
                msg: "离线错误日志",
              }),
            });
          }, 60);
        }
        close() {
          clearTimeout(this.timer);
          window.__stream.closed++;
        }
      };
    });
    page.on("pageerror", (e) => errors.push(e.message));
    await ctx.route("**/*", async (route) => {
      const r = route.request(),
        u = new URL(r.url());
      if (u.origin !== base) return route.abort();
      const respond = (d, status = 200) =>
        route.fulfill({
          status,
          contentType: "application/json",
          body: JSON.stringify(d),
        });
      if (!u.pathname.startsWith("/api/")) {
        const file = path.join(
          root,
          u.pathname === "/" ? "index.html" : u.pathname,
        );
        return route.fulfill({
          status: 200,
          contentType: file.endsWith(".js")
            ? "text/javascript"
            : file.endsWith(".css")
              ? "text/css"
              : "text/html",
          body: fs.readFileSync(file),
        });
      }
      if (!["GET", "HEAD"].includes(r.method())) {
        let body;
        try {
          body = r.postDataJSON();
        } catch {}
        writes.push({ path: u.pathname, body });
        if (u.pathname === "/api/commit") {
          if (opts.conflict)
            return respond({ detail: "版本冲突，文件已变化" }, 409);
          Object.assign(track, body);
          track.file_version = "b".repeat(64);
          if (body.filename) track.name = body.filename;
          return respond({ path: track.path });
        }
        if (u.pathname === "/api/tasks") {
          taskKind = body.kind;
          return respond({ job_id: "created-job" });
        }
        if (u.pathname.endsWith("/cancel")) {
          jobPhase = "cancelled";
          return respond({ ok: true });
        }
        if (u.pathname.endsWith("/restore")) return respond({ ok: true });
        if (u.pathname === "/api/lrc_file")
          return respond({ path: track.path });
        if (u.pathname === "/api/settings") {
          proxy = body.proxy;
          return respond({ settings: { proxy } });
        }
        if (u.pathname === "/api/settings/test_proxy")
          return respond({
            ok: true,
            results: [{ ok: true, ms: 12, target: "https://example.com" }],
          });
        if (u.pathname === "/api/plugins/toggle") {
          plugins[0].enabled = body.enabled;
          return respond({ ok: true });
        }
        if (u.pathname === "/api/plugins/save") return respond({ ok: true });
        if (
          u.pathname === "/api/plugins/delete" ||
          u.pathname === "/api/plugins/restore"
        )
          return respond({ ok: true });
        if (u.pathname === "/api/plugins/test")
          return respond({ ok: true, results: [] });
        if (u.pathname === "/api/organize/preview")
          return respond(
            (body.classify_rules || "").includes("INVALID")
              ? { ok: false, error: "YAML 格式错误" }
              : {
                  ok: true,
                  rules: 0,
                  paths: [
                    {
                      sample: "示例",
                      path: "music/已整理/歌手/专辑/歌名.flac",
                    },
                  ],
                },
          );
        if (u.pathname === "/api/organize/settings") {
          cfg = { ...cfg, ...body };
          return respond({ ok: true, created: [], settings: cfg });
        }
        if (u.pathname === "/api/organize/apply")
          return respond({
            job_id: "org-job",
            plan_id: "fixture-plan",
            total: 109,
            remaining: 0,
          });
        if (u.pathname === "/api/organize/scan")
          return respond({ job_id: "org-job", plan_id: "fixture-plan" });
        if (u.pathname.endsWith("/discard")) return respond({ ok: true });
        if (u.pathname === "/api/organize/mkdir")
          return respond({ created: true, path: "music/new" });
        return respond({ detail: "Unexpected write" }, 500);
      }
      gets.push(u.pathname);
      if (u.pathname === "/api/list")
        return respond({
          dirs: ["music"],
          files: Array.from({ length: opts.many ? 51 : 1 }, (_, i) => ({
            name: opts.many ? "曲目 " + i + ".flac" : track.name,
            path: opts.many ? "file" + i + ".flac" : track.path,
            size: 123456,
          })),
        });
      if (u.pathname === "/api/track")
        return respond({ ...track, path: u.searchParams.get("path") });
      if (u.pathname === "/api/cover")
        return route.fulfill({ status: 404, body: "" });
      if (u.pathname === "/api/lrc_file")
        return respond({ content: "[00:02.00]外置歌词" });
      if (u.pathname.endsWith("_search") || u.pathname === "/api/fp_search")
        return respond({ results: [candidate] });
      if (u.pathname === "/api/lyric")
        return respond({ lyric: "[00:03.00]候选歌词" });
      if (u.pathname === "/api/lrclib_lyric")
        return respond({ lyric: "新歌词" });
      if (u.pathname === "/api/system")
        return respond({
          app_version: "2026.10-workflow.4",
          storage: {
            database: "SQLite",
            journal_mode: "wal",
            jobs: 1,
            active_jobs: 0,
          },
        });
      if (u.pathname === "/api/tasks") return respond({ jobs });
      if (u.pathname === "/api/tasks/created-job")
        return respond({
          id: "created-job",
          kind: taskKind,
          status: opts.running
            ? jobPhase === "cancelled"
              ? "cancelled"
              : "running"
            : "completed",
          total: 1,
          counts: { completed: 1 },
          items: [
            {
              path: track.path,
              status: "completed",
              result: taskKind === "preview" ? preview : {},
            },
          ],
          logs: [],
        });
      if (u.pathname === "/api/tasks/history-job") return respond(jobs[0]);
      if (u.pathname === "/api/tasks/org-job")
        return respond({ status: "running" });
      if (u.pathname === "/api/history")
        return respond({
          versions: [
            {
              id: "history1",
              before_path: track.path,
              after_path: track.path,
              created: 1792070000,
              kind: "manual",
              status: "completed",
              before_tags: { title: "旧标题" },
              after_tags: { title: "新标题" },
            },
          ],
        });
      if (u.pathname === "/api/history/history1/preview")
        return respond({
          path: track.path,
          target_path: track.path,
          file_version: version,
          changes: [{ key: "title", before: "新标题", after: "旧标题" }],
        });
      if (u.pathname === "/api/plugins") return respond({ plugins });
      if (u.pathname === "/api/settings")
        return respond({ settings: { proxy } });
      if (u.pathname === "/api/logs")
        return respond({
          logs: [
            {
              id: 1,
              ts: Date.now(),
              level: "INFO",
              src: "test",
              msg: "离线初始日志",
            },
          ],
          total: 1,
        });
      if (u.pathname === "/api/organize/status") {
        if (statusFailed) return respond({ detail: "测试读取失败" }, 503);
        return respond({
          settings: cfg,
          defaults: DEFAULTS,
          media_root: "/media",
          media_writable: opts.writable !== false,
          paths: Object.fromEntries(
            ["source", "library", "duplicates", "junk"].map((k) => [
              k,
              {
                rel: cfg[k],
                abs: "/media/" + cfg[k],
                host: cfg.host_root + "/" + cfg[k],
                files: 5,
                bytes: 1234,
                exists: true,
              },
            ]),
          ),
          problems: [],
          running_plans: [],
          effective_name_template: cfg.name_template || "{title}",
        });
      }
      if (u.pathname === "/api/organize/plans")
        return respond({ plans: [plan] });
      if (u.pathname === "/api/organize/plan/fixture-plan") {
        const kind = u.searchParams.get("kind"),
          offset = Number(u.searchParams.get("offset") || 0);
        const items = Array.from({ length: 105 }, (_, i) => ({
          ord: i,
          name: "计划曲目 " + i,
          src: "source/" + i,
          target: "library/" + i,
          kind: "move",
          status: "pending",
        }));
        return respond({
          ...plan,
          ...(u.searchParams.has("view")
            ? {
                total: kind === "move" ? 105 : 0,
                items: kind === "move" ? items.slice(offset, offset + 100) : [],
              }
            : {}),
        });
      }
      if (u.pathname === "/api/organize/browse") {
        const rel = u.searchParams.get("path") || "";
        return respond({
          path: rel,
          abs: "/media" + (rel ? "/" + rel : ""),
          parent: rel ? "" : null,
          dirs: rel ? [] : ["music", "new"],
          dirs_total: rel ? 0 : 2,
          files: 0,
          roles_exact: [],
          roles_below: ["source", "library"],
        });
      }
      return respond({});
    });
    const nav = (view) => page.locator(`.nav-btn[data-view="${view}"]`).click();
    const open = async () => {
      await page.locator(".song-open").first().click();
      await page.locator("#tag-title").waitFor();
    };
    const setting = async (tab) => {
      await nav("settings");
      await page.locator(`.settings-tab[data-tab="${tab}"]`).click();
    };
    try {
      await page.goto(base);
      await page.locator('[data-page="work"] .song-open').first().waitFor();
      await fn({
        page,
        writes,
        gets,
        nav,
        open,
        setting,
        track,
        version,
        recover: () => (statusFailed = false),
      });
      assert.deepEqual(errors, [], "browser errors");
      results.push(name);
    } catch (e) {
      await page.screenshot({
        path: path.join(__dirname, "react-failure.png"),
        fullPage: true,
      });
      throw new Error(
        name + ": " + e.stack + "\nBrowser errors: " + errors.join(";"),
      );
    } finally {
      await ctx.close();
    }
  }
  try {
    await scenario(
      "single React entry and read-only initialization",
      async ({ page, writes }) => {
        assert.equal(await page.locator("#root > header").count(), 1);
        assert.equal(
          await page.evaluate(() => typeof window.showView),
          "undefined",
        );
        assert.equal(
          fs
            .readFileSync(path.join(root, "index.html"), "utf8")
            .includes("onclick="),
          false,
        );
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "17 tag fields and legacy layout at desktop sizes",
      async ({ page, open, writes }) => {
        await open();
        assert.equal(
          await page.locator(".tag-grid textarea,.tag-grid input").count(),
          18,
        );
        for (const [width, height] of [
          [1920, 1080],
          [1568, 787],
          [1440, 900],
          [1280, 720],
          [1080, 700],
        ]) {
          await page.setViewportSize({ width, height });
          const m = await page.evaluate(() => {
            const r = document
              .querySelector(".form-actions")
              .getBoundingClientRect();
            const e = document
              .querySelector(".react-editor")
              .getBoundingClientRect();
            return {
              width: document.documentElement.scrollWidth,
              height: document.documentElement.scrollHeight,
              save: r.bottom,
              bottom: e.bottom,
              fields: [
                ...document.querySelectorAll(
                  ".tag-grid input,.tag-grid textarea",
                ),
              ].every((n) => n.getBoundingClientRect().bottom < r.top),
            };
          });
          assert(m.width <= width + 1, "horizontal overflow " + width);
          assert(m.height <= height + 1, "vertical overflow " + height);
          assert(m.save <= height && m.fields, "fields/save outside viewport");
        }
        assert.equal(writes.length, 0);
        await page.screenshot({
          path: path.join(__dirname, "react-workbench.png"),
          fullPage: true,
        });
      },
    );
    await scenario(
      "manual edit cancelled confirmation performs no write",
      async ({ page, open, writes }) => {
        await open();
        await page.getByLabel("歌曲标题", { exact: true }).fill("修改标题");
        await page
          .getByRole("button", { name: "核对并保存修改", exact: true })
          .click();
        await page.getByRole("dialog").waitFor();
        assert.equal(writes.length, 0);
        await page.keyboard.press("Escape");
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "manual commit sends only differences and current file_version",
      async ({ page, open, writes, version }) => {
        await open();
        await page.getByLabel("歌曲标题", { exact: true }).fill("修改标题");
        await page
          .getByRole("button", { name: "核对并保存修改", exact: true })
          .click();
        await page
          .getByRole("button", { name: "确认提交", exact: true })
          .click();
        await page.waitForFunction(() => !document.querySelector("dialog"));
        assert.deepEqual(writes.find((w) => w.path === "/api/commit").body, {
          path: "fixture.flac",
          expected_version: version,
          title: "修改标题",
        });
      },
    );
    await scenario(
      "409 conflict preserved and never automatically retried",
      async ({ page, open, writes }) => {
        await open();
        await page.getByLabel("歌曲标题", { exact: true }).fill("冲突标题");
        await page
          .getByRole("button", { name: "核对并保存修改", exact: true })
          .click();
        await page
          .getByRole("button", { name: "确认提交", exact: true })
          .click();
        await page
          .getByRole("dialog")
          .getByText("版本冲突，文件已变化", { exact: true })
          .waitFor();
        assert.equal(writes.filter((w) => w.path === "/api/commit").length, 1);
        assert.equal(
          await page.getByLabel("歌曲标题", { exact: true }).inputValue(),
          "冲突标题",
        );
      },
      { conflict: true },
    );
    await scenario(
      "cover import stages bytes without server write",
      async ({ page, open, writes }) => {
        await open();
        await page
          .locator("input[type=file]")
          .setInputFiles({
            name: "cover.png",
            mimeType: "image/png",
            buffer: Buffer.from(
              "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==",
              "base64",
            ),
          });
        await page.getByText("封面已暂存，未写入", { exact: true }).waitFor();
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "external lyrics read only then explicit export confirmation",
      async ({ page, open, writes, version }) => {
        await open();
        await page
          .getByRole("button", { name: "读取同名 .lrc", exact: true })
          .click();
        await page.waitForFunction(() =>
          document.querySelector("#tag-lrc").value.includes("外置歌词"),
        );
        assert.equal(writes.length, 0);
        await page
          .getByRole("button", { name: "确认导出 .lrc", exact: true })
          .click();
        await page.getByRole("button", { name: "取消", exact: true }).click();
        assert.equal(writes.length, 0);
        await page
          .getByRole("button", { name: "确认导出 .lrc", exact: true })
          .click();
        await page
          .getByRole("button", { name: "确认提交", exact: true })
          .click();
        await page.waitForFunction(() => !document.querySelector("dialog"));
        assert.equal(
          writes.find((w) => w.path === "/api/lrc_file").body.expected_version,
          version,
        );
      },
    );
    await scenario(
      "candidate preview only supplements empty fields by default",
      async ({ page, open, writes }) => {
        await open();
        await page
          .getByRole("button", { name: "查找候选", exact: true })
          .click();
        await page
          .getByRole("button", { name: "生成此候选预览", exact: true })
          .click();
        await page
          .getByRole("button", { name: "核对所选差异并确认", exact: true })
          .click();
        await page.getByRole("dialog").waitFor();
        assert.equal(writes.length, 0);
        await page
          .getByRole("button", { name: "确认提交", exact: true })
          .click();
        await page.waitForFunction(() => !document.querySelector("dialog"));
        const b = writes.find((w) => w.path === "/api/tasks").body;
        assert.equal(b.kind, "apply");
        assert.equal(b.confirmed, true);
        assert.equal(b.items[0].fields.title, undefined);
        assert.equal(b.items[0].fields.genre, "Pop");
      },
    );
    await scenario(
      "batch preview import and explicit selection",
      async ({ page, writes }) => {
        await page
          .getByRole("checkbox", {
            name: "选择 测试歌曲 - 艺术家.flac",
            exact: true,
          })
          .check();
        await page
          .getByRole("button", { name: "生成所选曲目预览 →", exact: true })
          .click();
        await page
          .getByRole("checkbox", {
            name: "写入 测试歌曲 - 艺术家.flac",
            exact: true,
          })
          .waitFor();
        assert.equal(
          await page
            .getByRole("checkbox", {
              name: "写入 测试歌曲 - 艺术家.flac",
              exact: true,
            })
            .isChecked(),
          false,
        );
        assert.deepEqual(
          writes.map((w) => w.body.kind),
          ["preview"],
        );
      },
    );
    await scenario(
      "oversize batches reject rather than silently truncate",
      async ({ page, writes }) => {
        await page.getByRole("checkbox", { name: "全选显示项" }).check();
        await page
          .getByRole("button", { name: "生成所选曲目预览 →", exact: true })
          .click();
        await page.getByText("单批最多 50 首，请减少选择后再试").waitFor();
        assert.equal(writes.length, 0);
      },
      { many: true },
    );
    await scenario(
      "unsaved workbench navigation cancelled",
      async ({ page, open, nav, writes }) => {
        await open();
        await page.getByLabel("歌曲标题", { exact: true }).fill("草稿");
        page.once("dialog", (d) => d.dismiss());
        await nav("tasks");
        assert.equal(await page.locator("[data-page=work]").isVisible(), true);
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "task details, import preview and unmatched is not failure",
      async ({ page, nav, writes }) => {
        await nav("tasks");
        await page.getByRole("button", { name: "查看", exact: true }).click();
        await page
          .getByRole("button", { name: "读取整理预览", exact: true })
          .click();
        await page.getByText("未匹配", { exact: true }).waitFor();
        assert.equal(writes.length, 0);
      },
      { noMatch: true },
    );
    await scenario(
      "retry prior write task only creates preview",
      async ({ page, nav, writes }) => {
        await nav("tasks");
        await page.getByRole("button", { name: "查看", exact: true }).click();
        await page
          .getByRole("button", { name: "重新生成预览", exact: true })
          .click();
        await page
          .getByRole("button", { name: "创建只读预览", exact: true })
          .click();
        await page.waitForFunction(() => !document.querySelector("dialog"));
        assert.equal(
          writes.find((w) => w.path === "/api/tasks").body.kind,
          "preview",
        );
      },
      { jobKind: "apply" },
    );
    await scenario(
      "history details and cancel restore never writes",
      async ({ page, nav, writes }) => {
        await nav("history");
        await page
          .getByRole("button", { name: "查看差异", exact: true })
          .click();
        await page
          .getByRole("dialog")
          .getByText("旧标题", { exact: true })
          .waitFor();
        await page.getByRole("button", { name: "关闭", exact: true }).click();
        await page.getByRole("button", { name: "回退", exact: true }).click();
        await page.getByRole("button", { name: "取消", exact: true }).click();
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "history restore has explicit confirmation and expected version",
      async ({ page, nav, writes, version }) => {
        await nav("history");
        await page.getByRole("button", { name: "回退", exact: true }).click();
        await page
          .getByRole("button", { name: "确认回退", exact: true })
          .click();
        await page.waitForFunction(() => !document.querySelector("dialog"));
        assert.deepEqual(writes.find((w) => w.path.endsWith("/restore")).body, {
          expected_version: version,
          confirmed: true,
        });
      },
    );
    await scenario(
      "preferences preserve old storage key and update workbench",
      async ({ page, setting, nav, writes }) => {
        await setting("prefs");
        await page
          .getByLabel("默认匹配来源", { exact: true })
          .selectOption("qq");
        await page
          .getByRole("button", { name: "保存设置", exact: true })
          .click();
        assert.equal(
          await page.evaluate(
            () => JSON.parse(localStorage.getItem("mtl_settings")).source,
          ),
          "qq",
        );
        await nav("work");
        assert.equal(
          await page.getByLabel("元数据来源", { exact: true }).inputValue(),
          "qq",
        );
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "plugin edit and delete cancellation",
      async ({ page, setting, writes }) => {
        await setting("plugins");
        await page.getByRole("button", { name: "编辑", exact: true }).click();
        await page.getByLabel("显示名", { exact: true }).fill("新显示名");
        await page
          .getByRole("button", { name: "保存插件", exact: true })
          .click();
        await page.waitForFunction(() => !document.querySelector("dialog"));
        assert.equal(writes[0].body.display_name, "新显示名");
        await page.getByRole("button", { name: "删除", exact: true }).click();
        await page.getByRole("button", { name: "取消", exact: true }).click();
        assert.equal(writes.length, 1);
      },
    );
    await scenario(
      "custom plugin invalid schema blocks save",
      async ({ page, setting, writes }) => {
        await setting("plugins");
        await page
          .getByRole("button", { name: "添加插件", exact: true })
          .click();
        await page
          .getByRole("button", { name: "保存插件", exact: true })
          .click();
        await page.getByText("请填写显示名", { exact: true }).waitFor();
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "proxy test/save explicit and no hidden initial writes",
      async ({ page, setting, writes }) => {
        await setting("net");
        assert.equal(writes.length, 0);
        await page
          .getByLabel("HTTP 代理", { exact: true })
          .fill("http://proxy.test:7890");
        await page
          .getByRole("button", { name: "测试连接", exact: true })
          .click();
        await page.getByText("✓ 连接成功 · 12ms").waitFor();
        await page
          .getByRole("button", { name: "保存代理", exact: true })
          .click();
        await page.getByText("代理配置已保存并生效").waitFor();
        assert.equal(
          writes.find((w) => w.path === "/api/settings").body.proxy.url,
          "http://proxy.test:7890",
        );
      },
    );
    await scenario(
      "SSE logs start/close, filter and pause",
      async ({ page, nav, writes }) => {
        await nav("logs");
        await page.getByText("离线错误日志", { exact: true }).waitFor();
        await page.getByLabel("过滤日志", { exact: true }).fill("错误");
        assert.equal(
          await page.getByText("离线初始日志", { exact: true }).count(),
          0,
        );
        await page.getByRole("button", { name: "暂停", exact: true }).click();
        await nav("work");
        assert.equal(await page.evaluate(() => window.__stream.closed), 1);
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "organize plans pagination and cancel execution",
      async ({ page, nav, writes }) => {
        await nav("organize");
        await page.getByRole("button", { name: "查看", exact: true }).click();
        await page.getByText("计划曲目 99", { exact: true }).waitFor();
        await page
          .getByRole("button", { name: "加载更多（100 / 105）", exact: true })
          .click();
        await page.getByText("计划曲目 104", { exact: true }).waitFor();
        await page
          .getByRole("button", { name: "执行整理（109 项）", exact: true })
          .click();
        await page.getByRole("button", { name: "取消", exact: true }).click();
        assert.equal(writes.length, 0);
      },
    );
    await scenario(
      "organize settings drafts, picker and save",
      async ({ page, setting, writes }) => {
        await setting("organize");
        await page
          .getByRole("tab", { name: "重命名规则", exact: true })
          .click();
        await page
          .getByLabel("文件命名模板", { exact: true })
          .fill("{artist} - {title}");
        await page.getByRole("tab", { name: "基础配置", exact: true }).click();
        await page
          .getByRole("button", { name: "点击选择", exact: true })
          .first()
          .click();
        await page.getByRole("button", { name: "音乐库", exact: true }).click();
        await page.getByRole("button", { name: "▱ new", exact: true }).click();
        await page
          .getByRole("button", { name: "用这个目录", exact: true })
          .click();
        await page
          .getByRole("tab", { name: "重命名规则", exact: true })
          .click();
        assert.equal(
          await page.getByLabel("文件命名模板", { exact: true }).inputValue(),
          "{artist} - {title}",
        );
        await page
          .getByRole("button", { name: "保存配置", exact: true })
          .click();
        await page.getByText("整理配置已保存，不会移动音乐文件。").waitFor();
        assert.equal(writes[0].body.source, "new");
      },
    );
    await scenario(
      "YAML preview error never saves config",
      async ({ page, setting, writes }) => {
        await setting("organize");
        await page
          .getByRole("tab", { name: "二级分类策略", exact: true })
          .click();
        await page.getByLabel("二级分类 YAML", { exact: true }).fill("INVALID");
        await page
          .getByRole("button", { name: "预览示例路径", exact: true })
          .click();
        await page.getByText("YAML 格式错误", { exact: true }).waitFor();
        assert.deepEqual(
          writes.map((w) => w.path),
          ["/api/organize/preview"],
        );
      },
    );
    for (const planStatus of ["done", "failed", "cancelled", "applying"])
      await scenario(
        "non-ready organize cannot execute " + planStatus,
        async ({ page, nav, writes }) => {
          await nav("organize");
          await page.getByRole("button", { name: "查看", exact: true }).click();
          assert.equal(
            await page
              .getByRole("button", { name: "执行整理（109 项）", exact: true })
              .isDisabled(),
            true,
          );
          assert.equal(writes.length, 0);
        },
        { planStatus },
      );
    await scenario(
      "read-only media blocks organization",
      async ({ page, nav, writes }) => {
        await nav("organize");
        await page.getByRole("button", { name: "查看", exact: true }).click();
        assert.equal(
          await page
            .getByRole("button", { name: "执行整理（109 项）", exact: true })
            .isDisabled(),
          true,
        );
        assert.equal(writes.length, 0);
      },
      { writable: false },
    );
    await scenario(
      "all routes responsive on mobile/tablet",
      async ({ page, nav, setting, open, writes }) => {
        await open();
        for (const width of [768, 390]) {
          await page.setViewportSize({ width, height: 844 });
          for (const route of [
            "work",
            "tasks",
            "history",
            "logs",
            "organize",
            "settings",
          ]) {
            await nav(route);
            assert(
              await page.evaluate(
                () => document.documentElement.scrollWidth <= innerWidth + 1,
              ),
              route + " overflow " + width,
            );
          }
          for (const tab of ["prefs", "plugins", "net", "organize"]) {
            await setting(tab);
            assert(
              await page.evaluate(
                () => document.documentElement.scrollWidth <= innerWidth + 1,
              ),
              tab + " overflow " + width,
            );
          }
        }
        assert.equal(writes.length, 0);
        await page.screenshot({
          path: path.join(__dirname, "react-mobile.png"),
          fullPage: true,
        });
      },
    );
    await scenario(
      "organizer failed status retry",
      async ({ page, nav, writes, recover }) => {
        await nav("organize");
        await page
          .getByRole("alert")
          .filter({ hasText: "读取失败：测试读取失败" })
          .waitFor();
        recover();
        await page
          .getByRole("button", { name: "重新读取", exact: true })
          .click();
        await page
          .getByRole("heading", { name: "整理规则", exact: true })
          .waitFor();
        assert.equal(writes.length, 0);
      },
      { failedStatus: true },
    );
    console.log(
      JSON.stringify({ passed: results.length, scenarios: results }, null, 2),
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
