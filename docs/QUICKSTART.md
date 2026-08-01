# Quick Start — Docker（推荐）

一条命令启动 **OpenAI 兼容 API**，由 pi agent 驱动。镜像里已包含 **acp-http-gateway + pi + pi-acp**，不需要安装 Node / Python / npm / uv，也不需要 clone 代码。

## 1. 准备模型配置

pi 从 `~/.pi/agent/models.json` 读取模型提供商和 API Key。**先创建这个文件**（示例）：

```bash
mkdir -p ~/.pi/agent && cat > ~/.pi/agent/models.json << 'EOF'
{
  "providers": {
    "huya": {
      "baseUrl": "https://copilot.huya.info/api/openai/v1",
      "api": "openai-completions",
      "apiKey": "sk-你的KEY",
      "models": [
        { "id": "deepseek/deepseek-v4-pro",   "name": "deepseek/deepseek-v4-pro",   "reasoning": true, "input": ["text"], "contextWindow": 1000000, "maxTokens": 32000 },
        { "id": "deepseek/deepseek-v4-flash", "name": "deepseek/deepseek-v4-flash", "reasoning": true, "input": ["text"], "contextWindow": 1000000, "maxTokens": 32000 }
      ]
    }
  }
}
EOF
```

> `apiKey` 只会被容器内的 pi 使用，不会进镜像。

## 2. 启动

### 方式 A：docker run（最简单）

```bash
docker run -d --name acp-http-gateway \
  -p 8766:8766 \
  -e ACP_BEARER_TOKEN=你的访问令牌 \
  -v ~/.pi/agent:/root/.pi/agent \
  acp-http-gateway:latest
```

### 方式 B：docker compose（推荐，方便管理）

```bash
# 只要一个 compose 文件 + .env（compose 会自动从 GitHub clone 构建）
curl -O https://raw.githubusercontent.com/panjianning/acp-http-gateway/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/panjianning/acp-http-gateway/main/.env.example
cp .env.example .env
# 编辑 .env，设置 ACP_BEARER_TOKEN=你的访问令牌

docker compose up -d --build
```

服务监听 `http://localhost:8766`。

## 3. 测试

```bash
curl -s http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 你的访问令牌" \
  -d '{
    "model": "deepseek/deepseek-v4-flash",
    "messages": [{"role": "user", "content": "你有哪些技能？"}]
  }'
```

### 流式

```bash
curl -s -N http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 你的访问令牌" \
  -d '{"model": "deepseek/deepseek-v4-flash", "messages": [{"role": "user", "content": "你好"}], "stream": true}'
```

## 4. 多轮对话（有状态）

```bash
# 第一次请求，从响应头拿 session id
curl -s -D - http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 你的访问令牌" \
  -d '{"messages":[{"role":"user","content":"记住数字 42"}]}'
# → X-ACP-Session-Id: 019fbb48-...

# 第二次带上 session_id，agent 记得上下文
curl -s http://localhost:8766/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 你的访问令牌" \
  -d '{"messages":[{"role":"user","content":"刚才的数字是？"}], "session_id": "019fbb48-..."}'
```

## 5. 常见操作

```bash
# 日志
docker logs -f acp-http-gateway        # docker run 方式
docker compose logs -f gateway         # compose 方式

# 停止
docker stop acp-http-gateway
docker compose down

# 换模型配置后重启（models.json 在宿主机，改完重启生效）
vim ~/.pi/agent/models.json
docker restart acp-http-gateway
docker compose restart gateway
```

## 6. 安全说明

- **Bearer Token**：所有请求必须带 `Authorization: Bearer <token>`，否则返回 401。
- **隔离**：pi 的 bash 工具运行在容器内，默认无法访问宿主机文件（除非你额外挂载目录）。
- **密钥**：模型 `apiKey` 只在宿主机 `~/.pi/agent/models.json`，不进镜像、不进 Git。
- **生产建议**：对外暴露请放在反向代理后并加 HTTPS；仅内网使用则限制防火墙端口。

---

完整 API 参考见 [openai-api.md](openai-api.md)。
