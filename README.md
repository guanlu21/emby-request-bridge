# emby-request-bridge

独立的求片站（不依赖 Seerr，内存占用小）：
Emby 用户登录 → 搜 TMDB → 选片/选季 → 自动搜资源（PanSou 的 115 分享 + Prowlarr 的磁力）→ 115 转存/离线 → LitePan 整理并生成 strm → 通知 Emby 刷新。

## 使用流程
1. 用户打开站点（映射端口，默认容器 8686），用 **Emby 用户名和密码** 登录（停用/到期的账号登不进来）
2. 搜索影片或剧集名称（走 TMDB），点「求片」；剧集可选要哪几季
3. 普通用户的请求默认进「待审批」；管理员（Emby 管理员账号）在「管理」页勾选 → **批量通过**，自动处理；管理员自己求片直接处理。`BRIDGE_APPROVAL=auto` 则所有人直接处理
4. 「我的请求」里能看到自己每部片的进度；管理页可按用户/状态筛选，支持批量通过/拒绝/重试/删除，失败的"批量重试"会继续换下一个候选
5. 管理员在「设置」页配置 TMDB API Key、语言、API/图片主机名和**代理**（与 LitePan 的设置项一致，含「测试连通性」）。海报由服务端通过代理转发，浏览器不用直连 TMDB

## 选源规则（.env 可调）
- 只要视频文件，nfo/海报/字幕/样片等下载后自动删除
- 标题明确写了低于 720p 的丢弃；没写分辨率的不丢，靠体积兜底，排序靠后；CAM/TS/枪版丢弃；**带水印的丢弃**（标题含「水印」「watermark」「带logo」等，「无水印」加分）
- 硬性范围：单个视频 500MB–5GB（`MIN_FILE_GB`/`MAX_FILE_GB`）
- 排序偏好：**1080p 最优先**（2160p、720p 靠后）；单文件/每集在 **1–3GB**（`PREFER_MIN_GB`/`PREFER_MAX_GB`）内加分；再看片源、中字、做种数，115 分享略加分
- 电影的分享里有多个版本时只保留一个（优先 1080p、落在 1–3GB 内的）
- 任何一步失败自动换下一个候选（最多 MAX_ATTEMPTS 个）；每次先下载到暂存目录，合格后才移入正式目录

## 部署
1. `cp .env.example .env` 并填写（至少 BRIDGE_TOKEN、EMBY_URL、EMBY_API_KEY、115 相关）；容器的 `/data` 要挂载到可写目录
2. `docker compose up -d`（含 Prowlarr、PanSou、桥接服务）；或只单独跑桥接镜像
3. Prowlarr（:9696）里添加种子站，把 API Key 填进 .env
4. 打开桥接站点，用 Emby 管理员账号登录 → 设置 → 填 TMDB Key、代理 → 测试连通性
5. LitePan 里对"正式目录"建 STRM/整理任务

## 代理说明
- 代理地址要写 NAS 的局域网 IP（如 `http://192.168.1.10:7891`），不要写 127.0.0.1（容器里指向容器自己）
- 设置保存在 `/data/settings.json`（明文），别把 /data 暴露出去

## 与 emby-manager 关联
- 求片人就是 Emby 账号；`GET /api/users`（X-Token 请求头，值为 BRIDGE_TOKEN）返回每个用户的求片统计和 Emby 账号状态，`GET /api/requests?user=用户名` 返回某人的记录
- `USER_QUOTA_WEEKLY` 限制每人每 7 天求片数（管理员不限；剧集每季算一部）
- Seerr 的 Webhook 入口 `/webhook/seerr` 仍保留，不用可忽略

## 未联调 / 待确认（开发环境无网络，只测了纯逻辑和路由）
- P115Drive 里的 115 调用基于 p115client，**没有用真实账号跑过**；先 `/api/selftest`，再手动求一部片
- Emby 登录（AuthenticateByName）、「库里是否已有」的判断、PanSou 返回字段、Prowlarr 接口按公开资料写成，首次使用请实测
- LitePan 是否有触发整理的接口未确认，默认靠它自己的定时任务 + `LITEPAN_WAIT_SECONDS` 后刷新 Emby
- 水印只能靠标题/文件名关键词识别，看不到画面，无法保证 100% 没水印
- 剧集只收整季包，不做单集追更；磁力只用带 magnet 链接的结果

## 测试
`python -m unittest tests.test_all`
