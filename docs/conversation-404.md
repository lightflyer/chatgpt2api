# Conversation 404 compatibility

## 慢速图生图与超时补取（2026-10-21）

双参考图编辑可能需要 4～5 分钟。默认 `image_poll_timeout_secs` 现为 600 秒；已存在的挂载配置需要把原来的 120 显式改为 600。单账号并发 `image_account_concurrency` 保持 3。

“继续等待”通过原账号读取原会话，不再次提交生成请求。它会先检查已完成的结果，尚未完成时再等待，并将图片保存为与普通任务相同的 URL。任务的会话编号和账号标识在重启后继续保留；旧版 `proxy_url` 异常任务也可以再次补取。已完成或正在补取的任务重复点击不会启动另一个工作线程。

已知会话超过等待上限后，会保留会话供补取，不自动重新生成。补取不需要新的生图额度；账号限流但凭据仍有效时，仍可读取该账号之前生成的图片。

2026-10-15 的网页接口要求完成新的浏览器校验。旧 Python 求解器可能返回空值，旧图片请求还遗漏了 Turnstile 请求头；流式错误响应未读取时，界面只显示 `status=404, body=`。本分支补齐校验字段与请求头、保留真实错误内容，并提供可选的本地 Chromium 求解器。

同时更新了当前网页的 PoW 算法及 `~S` 答案格式。图片请求使用账号默认模型（无值时为 `auto`），新会话的 prepare 与 submit 使用相同的 `client-created-root`。

默认 TLS 指纹更新为 Chrome 146，并移除写死的 Edge 完整版本请求头。启用浏览器求解器时，会根据本地 Chromium 的实际主版本生成 User-Agent 和 Client Hints；浏览器校验复用同一个 User-Agent。账号显式配置的指纹仍优先，旧自定义指纹需与当前浏览器一致。

浏览器版本检测结果会在进程内缓存；更新 Chromium 后应重启应用。

## 本地运行

```bash
uv sync --frozen --group browser
.venv/bin/python -m playwright install --with-deps chromium
export CHATGPT2API_TURNSTILE_SOLVER=browser
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 3001
```

按原项目要求设置 `config.json` 或 `CHATGPT2API_AUTH_KEY`，并导入账号。直接使用 `.venv/bin/uvicorn`，或使用 `uv run --group browser`，以保留可选的 Playwright 依赖。

可选环境变量：

| 变量 | 用途 |
| --- | --- |
| `CHATGPT2API_TURNSTILE_SOLVER=browser` | 启用本地浏览器；默认 `legacy` 保留旧求解器 |
| `CHATGPT2API_BROWSER_EXECUTABLE` | 使用已安装的 Chromium 可执行文件；不设置时使用 Playwright 安装的浏览器 |
| `CHATGPT2API_BROWSER_TIMEZONE` | 浏览器时区，默认 `Asia/Shanghai`；可设 `Asia/Singapore` 做对照测试，两者均为 UTC+8 |
| `CHATGPT2API_BROWSER_NO_SANDBOX=1` | 仅在运行环境无法启用 Chromium sandbox 时显式设置 |

浏览器使用新的未登录 context，访问固定的 `https://chatgpt.com/gpts`。它仅接收校验 challenge、对应的 `p` 和客户端 User-Agent；账号访问令牌、Cookie、刷新令牌不会交给浏览器。每次求解都会关闭浏览器，超时会清理子进程组。每个并发生图请求可能需要一个 Chromium 进程，需为并发预留内存。

此版本的浏览器直接联网，不继承账号代理配置；当前验证范围是浏览器和后端使用相同的直连出口。浏览器时区变量不会改写会话 payload；新加坡与默认上海请求均使用 `timezone_offset_min=-480`。

## Docker 构建

原有预编译镜像不包含此分支代码，需要本地构建：

```bash
docker build --build-arg INSTALL_BROWSER_SOLVER=true -t chatgpt2api:conversation-404 .
```

创建单独的 compose override 文件（例如 `compose.browser.yml`）：

```yaml
services:
  app:
    image: chatgpt2api:conversation-404
    environment:
      CHATGPT2API_TURNSTILE_SOLVER: browser
      CHATGPT2API_BROWSER_NO_SANDBOX: "1"
```

基础镜像以 root 运行，因此示例显式关闭 Chromium sandbox；容器仍需保留自身的隔离。使用支持 sandbox 的非 root 环境时应去掉该变量。

完成原配置和数据备份后，使用以下命令切换镜像：

```bash
docker compose -f docker-compose.yml -f compose.browser.yml up -d --no-build --pull never app
```

SSH 隧道仍可指向原来的应用端口。关闭浏览器开关只会恢复旧求解器；上游要求新版挑战时，旧求解器仍可能无法生图。

## 验证

离线回归测试不使用真实账号：

```bash
CHATGPT2API_AUTH_KEY=test-auth .venv/bin/python -m unittest \
  test.test_upstream_http_errors \
  test.test_image_conversation_requirements \
  test.test_sentinel_pow \
  test.test_browser_turnstile
```

实测应同时检查会话 HTTP 状态、任务最终状态及可解码的图片文件；HTTP 200 本身不足以证明出图成功。遇到校验失败时，错误会明确中止请求，避免继续使用空校验令牌。

2026-10-15 已在 Linux 服务器以 Chromium 151 验证此分支：启用浏览器求解器、自动检测浏览器指纹、使用账号默认 `gpt-6-thinking`，将测试请求和浏览器时区设为 `Asia/Singapore`（偏移 `-480`），约 37 秒生成并下载可解码的 1254×1254 PNG。`gpt-6-pro` 也完成过出图验证。上海和新加坡同为 UTC+8，现有证据不支持时差是本次 404 的原因。

测试使用隔离进程，未替换线上容器。Docker 可选依赖安装参数已通过离线解析检查，但尚未完整构建和验证镜像。其他账号、代理出口和 Chromium 版本仍需各自验证。

扩展回归中的 `test_poll_waits_for_generated_asset_ids_to_settle` 在基线提交 `39f0930` 上也失败：当前默认配置关闭等待，而旧测试预期等待到第二张图片。这是已存在的测试/配置不一致，本分支未修改该行为。

协议参考：[当前 ChatGPT 网页脚本](https://chatgpt.com/cdn/assets/4813494d-e688mn57gf7k52mc.js)，以及 [MEIZU16 的浏览器 VM 修复](https://github.com/MEIZU16/chatgpt2api/commit/b4a08347e066c406cc341e6377f08c8e8dc524c2)。VM 的来源和许可证随源文件保留。
