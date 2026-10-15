# Miho · Mihomo + Zashboard

[![ARM64 Docker verification](https://github.com/guaren40/miho/actions/workflows/docker.yml/badge.svg)](https://github.com/guaren40/miho/actions/workflows/docker.yml)

**为 ARM64 VPS 准备的单容器整合：管理登录、订阅导入、Zashboard 和 VLESS WebSocket 代理共用一个入口。**

固定打包 Mihomo **v1.19.32** 和 Zashboard **v3.29.1**，构建时校验上游资源 SHA256。无需准备 `config.yaml`，不需要特权容器、TUN 或 `NET_ADMIN`。

## 在 Zeabur 部署

1. 在 Zeabur 中选择你已经接入的 **ARM64 VPS**，新建服务，选择 GitHub 仓库 **`guaren40/miho`** 的 `main` 分支。根目录保持 `/`，自动识别根目录 `Dockerfile`。
2. 为服务添加持久化存储，挂载路径 **`/data`**。这样重新部署后，密码、节点配置、代理凭据和客户端 UUID 都会保留。
3. 在网络设置中绑定一个 HTTPS 域名。HTTP 服务端口为 **8000**；如果平台分配了 `PORT`，程序会自动跟随。
4. 打开域名登录：首次初始化优先使用 **`ADMIN_PASSWORD`**；未设置时，使用 Zeabur 服务变量中的 **`PASSWORD`**。两者都没有时，程序生成随机密码，在首次启动日志和 `/data/bootstrap-password.txt` 中提供。
5. 按需导入自己的 **Clash / Mihomo YAML 格式订阅**，然后打开 Zashboard，自动连接内核。返回概览页复制 VLESS 链接，或下载客户端 YAML，即可连接这台 VPS。

第一次不导入订阅也可以使用：默认通过 VPS 直连访问网络。程序不提供免费节点或机场订阅。

> 运行节点必须是 ARM64。Zeabur 的共享构建机器可以是 x86，镜像内的系统、Mihomo 二进制和运行时检查均限定 ARM64。构建不依赖共享构建器报告的 `TARGETARCH`，避免 `linux/amd64` 检查导致的误失败。

### 使用模板自动配置域名与存储

仓库提供 [`zeabur-template.yaml`](./zeabur-template.yaml)，包含本仓库 Git 源、HTTP 入口、随机初始密码与 `/data` 持久化卷。安装 Node.js 后在仓库目录执行：

```bash
npx zeabur@latest template deploy -f zeabur-template.yaml
```

按 CLI 提示登录、选择 ARM64 项目并设置域名。此文件也可通过你自己的 Zeabur 账户发布为一键部署模板。

Zeabur 的 Git 服务只提供一个 HTTP 公网入口，因此这里通过 **`/tunnel` 上的 VLESS-over-WebSocket** 提供客户端连接，HTTPS 由 Zeabur 终止。无需额外 TCP 端口。此方案不是操作系统 VPN，也不会修改 VPS 的系统路由。

## Docker Compose

在 **ARM64 Linux** 上：

```bash
git clone https://github.com/guaren40/miho.git
cd miho
docker compose up -d --build
docker compose logs miho
```

访问 `http://服务器IP:8000`。生产环境建议通过 HTTPS 反向代理访问，并确保代理转发 WebSocket。如果反向代理后导出的连接地址不正确，设置 `PUBLIC_URL=https://你的域名`。

可在 `.env` 设置首次初始化的 `ADMIN_PASSWORD`。不设置会生成随机密码。Compose 默认把混合代理 7890 映射到宿主机回环地址，访问时需使用管理页提供的独立代理用户名和密码。

## 环境变量

| 变量 | 用途 |
| --- | --- |
| `ADMIN_PASSWORD` | 首次创建数据目录时使用的管理密码，优先级最高 |
| `PASSWORD` | Zeabur 环境中的初始管理密码备用值 |
| `PUBLIC_URL` | 可选，公开的完整地址，例如 `https://miho.example.com` |
| `PORT` | HTTP 监听端口，默认 `8000` |
| `DATA_DIR` | 持久化目录，默认 `/data` |
| `MIHO_DEBUG` | 设为 `1` 时将 Mihomo 诊断日志写入容器日志，用于排查连接问题 |

已存在 `/data/state.json` 时不会因环境变量改变而重置密码或 UUID。需要重设管理密码时，可停机后备份并删除此文件，再使用新的 `ADMIN_PASSWORD` 启动；这也会更换代理密码、API 令牌和 VLESS UUID，需要重新导出客户端配置。

## 功能与边界

- **自动初始化**：配置文件与凭据自动创建；通过会话 Cookie 登录，Zashboard 自动连接 `/api`，无需手动填写控制器和密钥。
- **订阅管理**：URL 下载、粘贴或上传 YAML，支持手动刷新已保存订阅。先用真实 Mihomo 检查，再切换配置；检查失败保持原配置，启动失败恢复原配置。
- **代理出口**：客户端通过 VLESS WebSocket 接入；出口由导入配置中的规则和 Zashboard 选择的策略组决定。
- **独立凭据**：管理密码、混合代理认证、控制器令牌、VLESS UUID 分开生成。链接、客户端 YAML 和 API 令牌都只在登录后提供。
- **受管理的监听**：导入配置不能取消认证或替换管理端口。内核控制器只监听 `127.0.0.1:9090`，WebSocket 入站只监听回环地址；混合 HTTP/SOCKS5 端口为 `7890`，始终要求认证。TUN 与额外入站被关闭。
- **持久化**：`state.json` 保存凭据，`user.yaml` 保存原始导入配置，`runtime.yaml` 为受管理的运行配置。策略选择由 Mihomo 缓存保存。面板对模式、日志等即时设置的调整需写入导入配置后才能跨重启保留。
- **更新**：修改 `versions.json` 中的版本、资源地址和 SHA256 后重新构建。面板中的内核升级和任意路径重载被禁止，避免下载错误架构或绕过监听设置。

## 验证

GitHub Actions 同时验证原生 ARM64 与 x86 共享构建器模拟场景，确认生成的镜像仍为 `linux/arm64`。容器测试覆盖空数据目录启动、登录、真实 VLESS/WebSocket 流量、HTTP 代理认证、控制器 WebSocket、配置检查失败保护和重新建容器后的数据保留。浏览器测试覆盖登录、Zashboard 自动连接和网页配置导入。

本地逻辑检查：

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

## 上游与许可

- [MetaCubeX/mihomo](https://github.com/MetaCubeX/mihomo/tree/v1.19.32)：GPL-3.0，未修改的上游二进制，对应源码见版本标签。
- [Zephyruso/zashboard](https://github.com/Zephyruso/zashboard/tree/v3.29.1)：MIT，未修改的发行版静态资源，许可保存在 `licenses/Zashboard-MIT.txt`。
- 本仓库整合程序采用 GPL-3.0，见 `LICENSE`。

部署参考：[Zeabur Dockerfile](https://zeabur.com/docs/en-US/deploy/methods/dockerfile)、[公网入口](https://zeabur.com/docs/en-US/deploy/networking/public-networking)、[模板格式](https://zeabur.com/docs/en-US/template/template-format)。
