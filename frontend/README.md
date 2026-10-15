# Music Info · 完整 React 前端

整个应用已由 React 19 + Vite 接管：工作台、任务中心、修改记录、实时日志、自动整理和全部设置。入口为 `frontend/index.html`，根组件 `src/App.jsx`；生产 `backend/static/index.html` 仅是 Vite 生成的轻量入口，不再包含旧原生页面脚本或事件桥接。

## 构建与开发

```bash
cd frontend
npm ci
npm run dev
npm run build
npm test
npm run test:ui
# Windows 本机已安装 Edge：
UI_BROWSER_CHANNEL=msedge npm run test:ui
```

Node.js 20.19+ 或 22.12+。开发服务器代理 `/api` 到当前内网 NAS；可使用 `MUSIC_INFO_API=http://你的服务器:8003 npm run dev` 覆盖。开发环境同样可能真实写文件，不要把真实接口当测试夹具。

生产构建直接输出到 `backend/static/`：Vite 生成 HTML、哈希 JS/CSS 和 manifest。Dockerfile 原有 COPY static 自动包含资源；生产无需 Node、不依赖 CDN。为了保留已打开页面的资源，构建不自动清空旧哈希包，维护者确认无引用后再清理。

## 架构

- `main.jsx`：唯一 createRoot、错误边界与全局样式。
- `App.jsx`：React 应用壳与页面导航；哈希路由支持刷新和前进/后退，无需修改后端路由。
- `AppContext.jsx`：偏好、跨页预览、提示、写入确认与离开保护。
- `Workbench.jsx` / `workflow.js`：曲库、17 字段、封面/歌词暂存、候选、批量任务与安全差异。
- `Jobs.jsx`、`History.jsx`、`Logs.jsx`：持久任务、回退与生命周期正确的 SSE 连接。
- `Preferences.jsx`、`Plugins.jsx`、`Network.jsx`：全部设置分区的 React 实现。
- `OrganizerFeature.jsx`、`Organizer.jsx`、`Settings.jsx`：自动整理共享状态、计划明细及五个设置子页。
- `api.js`、`common.jsx`、`model.js`：接口、受控表单与对话框、规则/状态。
- `base.css` 仅保留原视觉样式；`app.css` 负责 React 布局，不加载旧脚本。

## 验证与安全

`npm test` 是纯逻辑测试；`npm run test:ui` 全量拦截网络，仅用模拟 API，覆盖确认/取消、版本冲突、只补空白、批量选择、任务、回退、插件、代理、SSE、自动整理和响应式布局。默认使用 Playwright Chromium，首次需 `npx playwright install chromium`；Windows 可使用 Edge。

真实只读验收：`MUSIC_INFO_URL=http://你的内网服务器:8003 UI_BROWSER_CHANNEL=msedge npm run test:live`。必须显式提供地址；测试阻断全部非 GET/HEAD 请求，绝不扫描、保存配置、写标签、执行整理或回退。截图在 `tests/react-*.png`，不入 Git。上一阶段 `organizer-react.cjs` 等测试只适用于旧页面，整站迁移后以 `react-app.cjs` / `react-app-live.cjs` 为准。

写标签只提交变化字段及预览时的 `expected_version`；图片先暂存，确认后与标签一起提交。批量预览不写音乐，批量写入必须人工勾选并再次确认。失败不自动重试；任务重做只创建预览，不重放写入。只读/非就绪整理计划禁执行；恢复历史先预览、再版本核验。偏好继续兼容 `mtl_settings` 存储键。

部署前比对线上哈希、确认无运行任务、备份静态目录与旧镜像 ID；先上传新资源再替换入口，仅重建 music-tag-lite。回滚恢复备份入口并重建同一服务，保留 data/music；前端回滚不能撤销已执行的文件操作。
