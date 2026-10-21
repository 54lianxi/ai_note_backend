# ai_note_backend

## 部署：先安装完整依赖，再重启服务

更新代码后必须在服务器虚拟环境中重新安装 `requirements.txt`。文档解析依赖是按
文件格式懒加载的，缺依赖不会再拖垮整个 API，但对应格式也无法抽取正文：

```bash
cd /apps/ai-record
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -c "import app.main; print('app import ok')"
sudo systemctl restart ai-record-backend
sudo journalctl -u ai-record-backend -n 50 --no-pager
```

如果日志出现 `ModuleNotFoundError`，先用同一个 `venv/bin/python` 检查：

```bash
venv/bin/python -c "import docx, openpyxl, pptx, pypdf; print('document deps ok')"
```

## 数据边界：PostgreSQL 是唯一主数据源

Qdrant 只保存记录向量、`record_id` 和检索隔离所需的 `user_id`，不保存笔记
原文、摘要或附件信息。检索先由 Qdrant 返回记录 ID 和相似度，再从 PostgreSQL
读取完整内容。

切换后，存量 Qdrant payload 可用下面的脚本清理；脚本只覆盖 payload，不会重新
向量化，也不会修改 PostgreSQL：

```bash
uv run python scripts/compact_qdrant_payloads.py
```

## 部署：nginx 反代必须放行 WebSocket

实时语音转写走 `wss://<域名>/api/v1/records/voice/realtime`。nginx 默认不转发
`Upgrade` / `Connection` 头，会把 WebSocket 握手当普通 GET 交给后端，后端只会
回一个 404，客户端表现为一直卡在「正在连接实时转写...」。

API 的 location 需要这样配（`map` 放在 `http {}` 里，其余 location 按需保留）：

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    # 实时转写是长连接，别用默认 60s 读超时中途掐断
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

改完执行 `sudo nginx -t && sudo systemctl reload nginx`。

## 部署：图片/音频走 API 转发，不用暴露 MinIO

对象存储（MinIO）只在服务器本机监听 `9000`，公网连不上。所以 `file_url` /
`image_url` 里存的直连地址（`http://localhost:9000/...`）在客户端会被改写成
`https://<域名>/api/v1/media/...`，由后端 `GET /api/v1/media/{path}` 从 MinIO
取出来再转发，音频需要的 Range 请求也支持（否则进度条拖不动）。

也就是说 nginx 不需要给 MinIO 单独开端口或 location，`/api/` 反代配好就够了。
顺便别把 9000 直接暴露到公网——媒体接口是公开只读的，越少入口越好。

## 部署：代理变量必须留空

`AI_HTTP_PROXY` / `ASR_REALTIME_PROXY` 是给国内本机挂 Clash 用的
（`http://127.0.0.1:7897`）。服务器能直连 Google，这两个变量一旦照搬过去，
后端连上游会直接 `Connection refused`，前端表现为实时转写连不上、AI 请求全挂。

服务器上的 `.env` 保持：

```dotenv
AI_HTTP_PROXY=
ASR_REALTIME_PROXY=
```

## 免费额度与订阅

未订阅用户按类型单独计数：音频/录音合计 3 段、图片 3 张、文档 1 篇。
周订阅和月订阅的权益一致，商品 ID 分别为：

```dotenv
SUBSCRIPTION_WEEKLY_PRODUCT_ID=note_sub_week_0.99
SUBSCRIPTION_MONTHLY_PRODUCT_ID=note_sub_month_2.99
```

商店后台必须创建这两个自动续订商品，并让后端和客户端使用完全相同的 ID。

## 多语言语言包

App 内置英文和简体中文作为离线兜底，并支持西班牙语、法语、德语、葡萄牙语、
意大利语、日语、韩语、俄语、阿拉伯语、印地语、印尼语、土耳其语、越南语和泰语。
首次使用某语言时，客户端请求 `GET /api/v1/app/language-packs/{language}`，
服务端生成并缓存完整语言包；后续启动直接读本地缓存。

缓存目录默认是 `/tmp/ai-record-language-packs`，可通过
`LANGUAGE_PACK_CACHE_DIR` 改到持久卷。首次生成某种语言需要配置
`LLM_API_KEY`；也可以提前请求一次，把生成结果放进
`backend/app/static/language_packs/`，避免上线后首次请求等待。

## 部署：nginx 必须放宽请求体大小限制

语音笔记、图片、附件上传接口默认走 nginx 反代，而 nginx 的 `client_max_body_size`
默认只有 **1MB**，稍长一点的录音就会触发 HTTP 413（Request Entity Too Large），
客户端表现为保存语音笔记失败。

在 `http {}` 或 `server {}` 块里加上大小限制（建议 70MB；应用默认图片 8MB、
音频 64MB、文档 45MB，已经给 multipart 包装留出余量）：

```nginx
http {
    # 全局生效，所有 location 都继承
    client_max_body_size 70M;

    server {
        # 也可以只在特定 server 里生效
        client_max_body_size 70M;

        location /api/ {
            proxy_pass http://127.0.0.1:8000;
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }
    }
}
```

如果只想对 API 路径放开，也可以把 `client_max_body_size` 写在 `location /api/` 里。
改完执行 `sudo nginx -t && sudo systemctl reload nginx`。

上传限制由前后端同名配置控制，默认值如下：

```dotenv
MAX_IMAGE_UPLOAD_MB=8
MAX_AUDIO_UPLOAD_MB=64
MAX_FILE_UPLOAD_MB=45
```

前端可通过同名的 `--dart-define` 覆盖，后端通过 `.env` 覆盖。调整时要保证：
前端上限 ≤ 后端上限 < nginx `client_max_body_size`。后端对超限请求返回 HTTP 413，
前端也会在发起请求前直接提示，不会把超大文件放进离线队列。

## 文档正文检索

上传文档时会抽取正文并写入 Records/Qdrant，支持：

- 纯文本：TXT、Markdown、CSV、JSON、XML、HTML
- PDF：带文本层的 PDF；扫描图片型 PDF 暂不包含 OCR
- Word：DOCX
- Excel：XLSX
- PowerPoint：PPTX

长文档会按约 1800 字、180 字重叠分块，检索时只把命中的正文片段交给 AI。
单份文档默认最多索引 300000 字，可通过 `MAX_DOCUMENT_TEXT_CHARS` 调整。
旧版本上传的文档只保存了文件名，需要重新上传后才会建立正文索引。

## 部署：订阅必须配置 Google Play 服务端校验

后端不再相信客户端上报的 `expires_at`。`POST /subscription/verify` 会拿
`purchaseToken` 调 Google Play Developer API，只有商店返回的订阅状态有效
才会写库并授予权益。没配好服务账号时，订阅接口会直接返回 503，不会退回
“假 token 也能解锁”的老逻辑。

一次性配置：

1. 在 Google Cloud 项目里启用 **Android Publisher API**。
2. 创建服务账号，下载 JSON 私钥。
3. 在 Play Console 的「用户和权限」里邀请服务账号邮箱，授予
   **查看财务数据** 和 **管理订单和订阅** 权限（确认购买需要后者）。
4. 服务器 `.env` 填：

```dotenv
SUBSCRIPTION_VERIFY_WITH_STORE=true
GOOGLE_PLAY_PACKAGE_NAME=com.lx.note
GOOGLE_PLAY_SERVICE_ACCOUNT_FILE=/opt/ai-record/google-play-service-account.json
```

也可以把 JSON 内容放进 `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON`，适合容器 Secret。
服务账号文件不要提交到仓库。

本地用 `SKIP_IAP` 调试时，必须显式打开两个开关，否则后端仍会拒绝：

```dotenv
DEBUG=true
SUBSCRIPTION_VERIFY_WITH_STORE=false
SUBSCRIPTION_DEV_ALLOW_UNVERIFIED=true
```

iOS 目前还没接 App Store Server API，`platform=ios` 会返回 400。这是故意的：
在接好 Apple 服务端校验前，不接受 iOS 侧的任何客户端凭证。

## 部署：应用商店审核账号

审核账号不再使用 App 内置免密口令。服务器需要设置一段随机访问码：

```dotenv
REVIEW_LOGIN_ENABLED=true
REVIEW_ACCESS_CODE=换成足够长的随机字符串
```

配置默认从 `backend/.env` 读取；生产环境可以用 `ENV_FILE=.env.prod`
切换配置文件，环境变量本身仍优先于文件值。

审核员在登录页连续点副标题 5 次后输入该访问码。访问码只放在 Play Console
的 App access 说明里，不要写进客户端代码。

## 部署：AdMob app-ads.txt

文件位于 `backend/app/static/app-ads.txt`，FastAPI 在站点根路径
`https://kuliqi.us/app-ads.txt` 提供。部署后确认该地址返回 200 且内容与
AdMob 后台登记的发布商 ID 一致。
