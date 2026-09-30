# emby-request-bridge

Seerr 求片 → PanSou（115 分享/磁力）+ Prowlarr（磁力）搜资源 → 115 转存/离线 → LitePan 整理并生成 strm → Emby 刷新。

## 选源规则（可在 .env 调整）
- 只要视频文件（mkv/mp4/ts/m2ts/avi/mov/wmv/flv/rmvb/webm），其余（nfo、海报、字幕、样片文件夹等）下载后自动删除
- 标题明确写了分辨率且低于 720p 的丢弃；没写分辨率的不丢，靠体积区间兜底，但排序靠后；CAM/TS/枪版丢弃
- 单个视频 500MB–5GB；磁力按总大小（剧集按每集平均）预筛，下载完再按实际文件二次检查
- 候选打分：优先 1080p、其次 2160p，再看片源/中字/做种数，115 分享略加分；**任何一步失败自动换下一个**（最多 MAX_ATTEMPTS 个）
- 每次尝试都下载到暂存目录，合格后才移动到正式目录，失败不会在媒体库留垃圾

## 使用流程
1. 用户打开 **Seerr**（:5055，用 Emby 账号登录）→ 搜索影片 → 点"请求"
2. 桥接页（:8686）里能看到**谁求的什么片**，状态为"待审批"
3. 你勾选（或全选）→ **批量通过** → 自动搜资源、转存/离线、过滤、移入正式目录、通知 Emby；失败的可"批量重试"（继续换下一个候选）
4. 想省事：`BRIDGE_APPROVAL=auto` 则新请求直接处理；或在 Seerr 里给可信用户开"自动批准"

## 与 Emby / emby-manager 用户关联
- Seerr 用 Emby 账号登录，求片人 = Emby 用户名；桥接服务用 Emby API（EMBY_URL/EMBY_API_KEY）按用户名校验
- Emby 里不存在、或已停用/到期（emby-manager 到期会停用账号）的用户，请求自动记为"已拒绝"并写明原因；管理员可在桥接页"批量重试"强制放行
- `USER_QUOTA_WEEKLY` 可限制每人每 7 天求片数（剧集每季算一部）
- 桥接页可按用户筛选；`GET /api/users`（X-Token）返回每个用户的求片统计和 Emby 账号状态，`GET /api/requests?user=用户名` 返回某人的全部记录，emby-manager 可直接调用
- 注意：Seerr 传过来的是用户的"显示名"，别在 Seerr 里改成和 Emby 用户名不一致的名字

## 部署
1. `cp .env.example .env` 并填写；`mkdir -p data/seerr && chown 1000:1000 data/seerr`
2. `docker compose up -d --build`
3. Prowlarr（:9696）里添加你要用的种子站，把 API Key 填进 .env，`docker compose up -d` 重启 bridge
4. Seerr（:5055）里连接 Emby；Settings → Notifications → Webhook：
   - URL：`http://NAS内网IP:桥接映射端口/webhook/seerr?token=你的BRIDGE_TOKEN（用 compose 部署在同一网络时可写 http://bridge:8686/...）`
   - 勾选 Request Pending Approval、Request Approved、Request Automatically Approved
   - JSON payload 用默认即可（需包含 media.media_type、media.tmdbId、subject、extra）
5. LitePan 里对"正式目录"建 STRM/整理任务；Seerr 的 Emby 扫描任务会把新片标记为可用
6. 打开 `http://NAS:8686/` 看请求状态；`/api/selftest`（带 X-Token 头）检查 115 是否连通

## 未联调 / 待确认（开发环境无网络，只测了纯逻辑）
- P115Drive 里的 115 调用（建目录、转存、离线、列目录、移动、删除）基于 p115client，**没有用真实账号跑过**，第一次请先 selftest 再手动求一部片
- PanSou 镜像名与返回字段、Seerr Webhook 字段按公开资料写成，如有出入改 sources.py / main.py 里对应几行
- LitePan 是否有触发整理的接口未确认，目前默认靠 LitePan 自己的定时任务 + LITEPAN_WAIT_SECONDS 之后刷新 Emby
- v1 剧集只收"整季包"，不做单集追更；磁力只用带 magnet 链接的结果

## 测试
`python -m unittest tests.test_all`
