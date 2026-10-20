# ai_note_backend

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

## 部署：nginx 必须放宽请求体大小限制

语音笔记、图片、附件上传接口默认走 nginx 反代，而 nginx 的 `client_max_body_size`
默认只有 **1MB**，稍长一点的录音就会触发 HTTP 413（Request Entity Too Large），
客户端表现为保存语音笔记失败。

在 `http {}` 或 `server {}` 块里加上大小限制（建议 50MB，能覆盖大多数语音/图片场景）：

```nginx
http {
    # 全局生效，所有 location 都继承
    client_max_body_size 50M;

    server {
        # 也可以只在特定 server 里生效
        client_max_body_size 50M;

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

建议的大小：语音 50MB、图片 10MB、通用附件 50-100MB，按业务需求调整。

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

审核员在登录页连续点副标题 5 次后输入该访问码。访问码只放在 Play Console
的 App access 说明里，不要写进客户端代码。

## 部署：AdMob app-ads.txt

文件位于 `backend/app/static/app-ads.txt`，FastAPI 在站点根路径
`https://kuliqi.us/app-ads.txt` 提供。部署后确认该地址返回 200 且内容与
AdMob 后台登记的发布商 ID 一致。
