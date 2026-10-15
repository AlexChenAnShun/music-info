/* Real-server smoke: GET/HEAD only. Never run mocked write tests against this URL. */
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { chromium } = require("../frontend/node_modules/playwright");
const base = process.env.MUSIC_INFO_URL;
if (!base || !/^https?:\/\//.test(base))
  throw new Error("Set MUSIC_INFO_URL explicitly");
const hash = (x) =>
  crypto.createHash("sha256").update(JSON.stringify(x)).digest("hex");
const done = [],
  forbidden = [],
  errors = [],
  failures = [],
  external = [];
async function get(url) {
  const r = await fetch(new URL(url, base), {
    signal: AbortSignal.timeout(30000),
  });
  assert(r.ok, "GET failed: " + url + " " + r.status);
  return r.json();
}
async function inventory() {
  const files = [],
    queue = [""];
  let n = 0;
  while (queue.length) {
    assert(++n < 250, "Inventory too large; review the smoke scope first");
    const dir = queue.shift(),
      d = await get("/api/list?path=" + encodeURIComponent(dir));
    files.push(...d.files.map((f) => [f.path, f.size, f.mtime]));
    queue.push(...d.dirs.map((x) => (dir ? dir + "/" + x : x)));
  }
  return files.sort((a, b) => a[0].localeCompare(b[0]));
}
(async () => {
  const before = await inventory();
  const statusBefore = await get("/api/organize/status");
  const plansBefore = await get("/api/organize/plans");
  const proxyBefore = await get("/api/settings");
  const pluginsBefore = await get("/api/plugins?include_removed=1");
  const sample = before[0]?.[0];
  const trackBefore = sample
    ? await get("/api/track?path=" + encodeURIComponent(sample))
    : null;
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.UI_BROWSER_CHANNEL
      ? { channel: process.env.UI_BROWSER_CHANNEL }
      : {}),
  });
  const ctx = await browser.newContext({
    viewport: { width: 1568, height: 900 },
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(20000);
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("response", (r) => {
    if (r.url().includes("/api/") && r.status() >= 400)
      failures.push([r.status(), r.url()]);
  });
  await ctx.route("**/*", async (r) => {
    const q = r.request();
    if (!["GET", "HEAD"].includes(q.method())) {
      forbidden.push([q.method(), q.url()]);
      return r.abort();
    }
    if (new URL(q.url()).origin !== new URL(base).origin) {
      external.push(q.url());
      return r.abort();
    }
    return r.continue();
  });
  try {
    await page.goto(base, { waitUntil: "domcontentloaded" });
    await page.locator("#view-work .library").waitFor();
    assert.equal(await page.locator("#root").count(), 1);
    assert.equal(await page.locator("script:not([src])").count(), 0);
    done.push("single React root and built assets");
    if (sample) {
      const dir = sample.includes("/")
        ? sample.slice(0, sample.lastIndexOf("/"))
        : "";
      for (const part of dir.split("/").filter(Boolean))
        await page
          .getByRole("button", { name: "▱ " + part, exact: true })
          .click();
      await page
        .locator(".song-open")
        .filter({
          hasText: path.posix.basename(sample).replace(/\.[^.]+$/, ""),
        })
        .first()
        .click();
      await page.locator("#tag-title").waitFor();
      assert.equal(
        await page.locator('.rx-tag-field [id^="tag-"]').count(),
        17,
      );
      await page.screenshot({
        path: path.join(__dirname, "react-live-workbench.png"),
        fullPage: true,
      });
      done.push("real library/track, 17 fields and file_version read");
    }
    for (const view of ["tasks", "history", "logs", "organize", "settings"]) {
      await page.locator('nav [data-view="' + view + '"]').click();
      await page.waitForTimeout(600);
      assert.equal(
        await page.locator(".rx-error:visible").count(),
        0,
        view + " error banner",
      );
      done.push(view + " GET-only page");
      if (view === "organize" && plansBefore.plans?.length) {
        const row = page
          .locator('[data-react-section="organizer"] tr')
          .filter({ hasText: plansBefore.plans[0].plan_id });
        await row.getByRole("button", { name: "查看", exact: true }).click();
        await page.getByTestId("plan-detail").waitFor();
        for (const name of [
          "待移动",
          "洗版替换",
          "重复",
          "冗余/失败",
          "附属文件",
        ]) {
          await page.getByRole("tab", { name: new RegExp(name) }).click();
          await page.waitForTimeout(120);
        }
        assert.equal(
          await page.locator(".rx-error:visible").count(),
          0,
          "real plan details",
        );
        await page.screenshot({
          path: path.join(__dirname, "react-live-organizer.png"),
          fullPage: true,
        });
        done.push(
          "existing plan and all five categories read without scan/apply",
        );
      }
    }
    for (const tab of ["prefs", "plugins", "net", "organize"]) {
      await page
        .locator(".settings-tabs button")
        .filter({
          hasText: {
            prefs: "工作台偏好",
            plugins: "插件库",
            net: "网络代理",
            organize: "自动整理",
          }[tab],
        })
        .click();
      await page.waitForTimeout(500);
      assert.equal(
        await page.locator(".rx-error:visible").count(),
        0,
        tab + " settings error",
      );
      done.push(tab + " GET-only settings");
    }
    for (const title of [
      "基础配置",
      "重命名规则",
      "二级分类策略",
      "洗版策略",
      "变量与语法",
    ]) {
      const b = page.getByRole("tab", { name: title, exact: true });
      assert.equal(
        await b.count(),
        1,
        "missing organizer settings tab " + title,
      );
      await b.click();
      await page.waitForTimeout(100);
    }
    done.push("five organizer settings subpages");
    await page.getByRole("tab", { name: "基础配置", exact: true }).click();
    await page
      .getByRole("button", { name: "点击选择", exact: true })
      .first()
      .click();
    await page.locator("dialog .org-pick-role").waitFor();
    await page
      .locator("dialog")
      .getByRole("button", { name: "取消", exact: true })
      .click();
    done.push("real directory picker GET then cancel");
    await page.setViewportSize({ width: 390, height: 844 });
    for (const view of [
      "work",
      "tasks",
      "history",
      "logs",
      "organize",
      "settings",
    ]) {
      await page.locator('nav [data-view="' + view + '"]').click();
      await page.waitForTimeout(150);
      const geometry = await page.evaluate(() => ({
        width: innerWidth,
        scroll: document.documentElement.scrollWidth,
      }));
      assert(
        geometry.scroll <= geometry.width + 2,
        view + " mobile overflow " + JSON.stringify(geometry),
      );
    }
    await page.screenshot({
      path: path.join(__dirname, "react-live-mobile.png"),
      fullPage: true,
    });
    done.push("all six views on 390px mobile");
    assert.deepEqual(errors, [], "browser runtime errors");
    assert.deepEqual(failures, [], "API failures");
    assert.deepEqual(forbidden, [], "attempted non-read requests");
    assert.deepEqual(external, [], "external/CDN requests");
  } catch (e) {
    await page.screenshot({
      path: path.join(__dirname, "react-live-failure.png"),
      fullPage: true,
    });
    throw e;
  } finally {
    await browser.close();
  }
  assert.deepEqual(await inventory(), before, "media inventory changed");
  assert.deepEqual(
    (await get("/api/organize/status")).settings,
    statusBefore.settings,
    "organizer settings changed",
  );
  assert.deepEqual(
    await get("/api/settings"),
    proxyBefore,
    "proxy settings changed",
  );
  // Plugin metrics may change with unrelated external activity; only configuration is compared.
  const cfg = (x) =>
    (x.plugins || []).map((p) => ({
      id: p.id,
      enabled: p.enabled,
      removed: p.removed,
      params: p.params,
      config: p.config,
    }));
  assert.deepEqual(
    cfg(await get("/api/plugins?include_removed=1")),
    cfg(pluginsBefore),
    "plugin configuration changed",
  );
  if (sample)
    assert.equal(
      (await get("/api/track?path=" + encodeURIComponent(sample))).file_version,
      trackBefore.file_version,
      "sample file version changed",
    );
  console.log(
    JSON.stringify(
      {
        passed: done.length,
        checks: done,
        files: before.length,
        inventory_sha256: hash(before),
        forbidden,
        errors,
        failures,
        external,
      },
      null,
      2,
    ),
  );
})().catch((e) => {
  console.error(e.stack);
  process.exitCode = 1;
});
