# Music Info

自托管的本地音乐标签工作台：**多源检索元数据 → 预览差异 → 人工确认后写入**，每次写入都有历史记录，可预览、可一键回退。

前端整站采用 **React 19 + Vite**：应用导航、标签工作台、任务中心、修改记录、实时日志、自动整理和全部设置均由 React 接管（源码 `frontend/`，本地打包，无 CDN 依赖）。后端 FastAPI + mutagen，元数据审计存 SQLite(WAL)，Docker 一条命令部署。

> 只改元数据：标签、封面、歌词。**不触碰音频内容本身**。

---

## ✨ 功能

- **多源融合检索**：咪咕 / QQ 音乐 / 网易云 / 酷狗 / 酷我 / iTunes / MusicBrainz 并行查询，按标题/艺术家/专辑相似度排序（`/api/smart_search`）
- **补充来源**：Deezer（`/api/deezer_search`）、AcoustID 声纹识别（本地 `fpcalc` 指纹，抗错误标签）、LRCLIB 歌词兜底
- **17 个字段**：标题、艺术家、专辑、专辑艺术家、发行日期、音轨号、总音轨数、光盘编号、总光盘数、流派、语言、专辑类型、简介、描述、专辑版本、歌词、同步 LRC
- **安全写入**：默认「只补空白，不覆盖现有标签」；写入前必须人工确认；每次提交校验 `file_version`，避免并发覆盖
- **历史与回退**：SQLite(WAL) 记录每次写入的元数据快照，可预览差异并一键回退
- **后台任务队列**：批量预览 / 批量写入，可取消、并发可配；未匹配（原文件尚未刮削）与真失败分开呈现
- **封面**：候选封面可预览，确认后与标签在同一次提交写入
- **界面**：整站 React 应用、曲库封面缩略图、匹配度徽标、进度条；响应式，桌面至 390px 手机布局
- **前端验证**：12 项逻辑单测 + 30 项全模拟浏览器回归；真实服务只读验收脚本强制阻断非 GET/HEAD 请求

---

## 🚀 快速开始（Docker）

```bash
git clone https://github.com/AlexChenAnShun/music-info.git
cd music-info
cp .env.example .env
# 编辑 docker-compose.yml，把 /path/to/your/music 换成你的音乐目录
docker compose up -d
```

浏览器打开 <http://localhost:8003>。

---

## ⚙️ 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEDIA_DIR` | `/media` | 音乐库根目录（容器内路径） |
| `DATA_DIR` | `/data` | 数据目录：SQLite 库 + 元数据版本快照 |
| `JOB_WORKERS` | `2` | 后台任务并发数 |
| `FANART_API_KEY` | 空 | Fanart.tv key，用于艺术家图片；留空则不启用 |
| `ACOUSTID_CLIENT_KEY` | 空 | AcoustID 客户端 key，用于声纹识别；留空则该功能不可用 |

---

## 🔌 接口一览

**曲库与标签**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/list` | 目录列表（曲库浏览） |
| GET | `/api/track` | 读取单个文件标签与音频技术信息 |
| POST | `/api/save` | 写入标签（需 `expected_version`，需确认） |
| GET/POST | `/api/cover` | 读取/设置封面 |
| GET/POST | `/api/lrc_file` | 读取/写入同名外置 `.lrc` |

**检索与歌词**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/smart_search` | 多源融合检索（推荐） |
| GET | `/api/sources` | 各源可用状态 |
| GET | `/api/{ncm,migu,qq,kugou,kuwo,itunes,deezer,mb}_search` | 单源检索 |
| GET | `/api/lyric` | 按源取歌词（含翻译） |
| GET | `/api/lrclib_lyric` | LRCLIB 歌词兜底 |
| GET | `/api/fp_search` | AcoustID 声纹识别 |

**任务与历史**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/commit` | 提交一批预览结果 |
| GET/POST | `/api/tasks` | 查询/创建后台任务 |
| GET | `/api/tasks/{job_id}` | 任务进度 |
| POST | `/api/tasks/{job_id}/cancel` | 取消任务 |
| GET | `/api/history` | 元数据修改历史 |
| GET | `/api/history/{version_id}/preview` | 预览某个历史版本 |
| POST | `/api/history/{version_id}/restore` | 回退到该版本 |

---

## 🧱 技术栈

| 层 | 实现 |
|---|---|
| 后端 | Python 3.12 · FastAPI · uvicorn · mutagen · requests |
| 存储 | SQLite（WAL 模式），元数据版本快照存 `data/versions/` |
| 前端 | React 19 + Vite，单根组件与模块化页面，静态资源随 Docker 分发 |
| 容器 | `python:3.12-slim` + `libchromaprint-tools`（fpcalc） |

---

## 🧪 开发与测试

完整 React 前端：`cd frontend && npm ci && npm run build && npm test`；浏览器交互回归 `npm run test:ui`（全模拟 API，不接触 NAS；Windows 可设置 `UI_BROWSER_CHANNEL=msedge`）。构建产物位于 `backend/static/assets/`，Vite 自动生成轻量 HTML 入口及资源引用。部署前须构建并保留对应哈希资源，详见 `frontend/README.md`。

```bash
# 后端接口测试
pip install -r backend/requirements.txt
python -m pytest tests/test_workflow.py

# React 前端单测与全模拟浏览器回归（需要 Node.js 与本地 Chromium/Edge）
cd frontend
npm ci && npm run build && npm test
npm run test:ui
```

---

## 🗂 本地自动整理（V4.2）

「自动整理」页把 `未整理/待整理` 里的音乐按 **歌手/专辑/歌名** 归档进 `已整理`：

- **只移动，不删除、不覆盖**：目标重名自动加 “ (2)”；执行前逐个复核大小与 mtime，变了就停。
- **重复判定严格**：与已整理库逐字节相同（SHA-256）才进 `未整理/已存在`；同歌不同音质不算重复。
- **冗余数据**：非音频、孤儿歌词/封面、隐藏文件、读不出标签的音频 → `未整理/冗余数据`。
- **附属文件**：同名 `.lrc` 与同名图片跟随歌曲一起改名为 `歌名.ext`。
- **设置页「自动整理」**：四个目录（点击选择）、命名模板 `{title}`、目录模板 `{artist}/{album}`、
  二级分类策略（YAML，支持 `artist_initial` 首字母分层）、洗版策略（并存 / 音质更优时替换 /
  总是替换 / 从不替换）、未知歌手与未知专辑占位名 —— 全部可改，保存即生效。
- **安全阀**：待整理必须在「已整理」库外（否则拒绝）；一次超过 500 条会提醒；
  任务可在「任务中心」安全停止；扫描只读，真正移动文件需要二次确认。

```yaml
# docker-compose.yml：自动整理要写入，挂载必须是 rw
volumes:
  - ./music:/media/music:rw
```

---

## ⚠️ 安全说明

- 服务**没有鉴权**，请只在本机或内网使用，不要直接暴露到公网
- 媒体目录默认**只读挂载**（`/media/music:ro`）即可；启用「本地自动整理」时挂载需为 **rw**（`/media/music:rw`），因为它要移动文件（只移动、不删除、不覆盖，且需二次确认）
- 只修改元数据、封面与歌词，不重编码音频；写入前会校验 `file_version`
- 请勿把 `.env`、`data/` 或任何密钥提交进仓库

---

## 📄 许可

[MIT](LICENSE)
