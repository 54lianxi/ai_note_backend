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
