# PulseBrief

**PulseBrief 每日 AI 新闻助手**是一个面向个人订阅的新闻整理项目。用户设置关注主题和关键词，服务端根据授权来源检索候选内容，由 Agent 按证据决定读取哪些文章、调用哪些工具，并生成带来源链接的中文简报。Android App 用于订阅、阅读和查看历史；Vue 网页作为辅助管理界面。

当前阶段是可运行的面试作品和受控试用项目。它尚未完成中国综合新闻覆盖、规模化运行及公众上线所需的全部验收，因此不要将其描述为已全面商业化的新闻服务。

## 功能

- 邮箱验证码注册与登录、个人订阅偏好、简报历史和阅读状态。
- Agent 使用模型 Function Calling 自主选择工具、决定调用次数，并判断何时结束。
- 五个项目工具：`list_dir`、`read_file`、`search_content`、`write_file`、`bash`。工具运行在服务端隔离工作区，并限制文件访问范围及命令执行能力。
- 摘要保留重要事件、关键事实和出处链接；数字声明由来源证据校验。证据不足时不发布不确定摘要。
- Android App 可检查新简报并显示系统通知；邮件只用于账户验证码。
- Vue H5 提供辅助访问界面。

## 技术栈

| 部分 | 技术 |
|---|---|
| Android App | Flutter、Dart |
| 后端 API 与 Agent | Python 3.12 或 3.13、FastAPI、Pydantic、SQLAlchemy |
| 后台任务 | Celery、Redis |
| 数据库与迁移 | PostgreSQL 16、Alembic |
| 模型 | OpenAI 兼容接口；当前可配置通义千问 |
| 辅助网页 | Vue 3、Vite、JavaScript |
| 本机运行与公网连接 | Docker Compose、Cloudflare Tunnel（可选） |

仓库名、Docker Compose 项目名和应用镜像前缀为 `pulsebrief`。数据保存在独立 Docker volumes 中；日常重启容器不会清除账户、订阅和简报数据。

## 快速启动

### 本机服务

1. 安装并启动 Docker Desktop。
2. 复制 `.env.example` 为 `.env`，填写模型服务、邮箱验证码和数据库所需的本机配置。密钥只放在 `.env`，不要提交到 Git。
3. 双击项目目录中的 `启动项目.bat`。它会启动 API、Agent 后台任务、数据库、网页和必要的调度服务；首次启动需要构建镜像。
4. 本机网页地址为 `http://127.0.0.1:8080`，API 文档为 `http://127.0.0.1:8000/docs`。关闭项目可双击 `停止项目.bat`；数据库数据卷会保留。

若需从 Android 真机访问本机服务，请保持电脑、Docker 和网络连接运行。外网访问可使用 Cloudflare Tunnel；配置好隧道令牌后，启动器会自动启动连接器。公开访问需要公网 HTTPS、域名路由和相应服务合规条件。

### 运行方式和依赖关系

```text
Android App / 浏览器
        │ HTTPS（手机不必和电脑处于同一局域网）
        ▼
Cloudflare Tunnel（可选；电脑和隧道在线时才可公网访问）
        │
        ├── Vue 网页
        └── FastAPI ── Celery worker/beat
                         │
                    PostgreSQL + Redis
```

当前采用“本机电脑提供服务”的方式，不是托管云服务器：电脑关机、Docker 停止或网络中断时，App 无法连接；隧道只负责安全转发请求，不会替电脑托管后端。手机在同一 Wi-Fi 时可用局域网地址进行本地联调；日常外网访问使用配置好的 HTTPS API 域名和隧道。域名可以先不配置，本机网页与 API 仍可在电脑上使用。

PostgreSQL、Redis 和任务数据存放在 Docker volumes。普通停止或重启会保留这些卷；这不等同于异地备份。项目提供手动数据库备份和恢复步骤，不会在电脑上定时弹窗或自动启动备份任务。重要数据应定期手动备份到另一块磁盘或可信的私有存储，并实际演练恢复。

### Android App

Release 默认访问 `https://api.pulsebrief.cloud`。用户可从 GitHub Releases 下载签名后的 APK（发布后补充链接）；开发者也可在 `mobile_app/` 使用 Flutter 构建。

```powershell
$env:PUB_CACHE = 'F:\Resources\pub-cache'
$env:GRADLE_USER_HOME = 'F:\Resources\gradle-cache'
$env:ANDROID_USER_HOME = 'F:\Resources\codex-tools\pulsebrief\.android-user-home'
Set-Location .\mobile_app
F:\Resources\codex-tools\pulsebrief\flutter\bin\flutter.bat pub get
F:\Resources\codex-tools\pulsebrief\flutter\bin\flutter.bat test
F:\Resources\codex-tools\pulsebrief\flutter\bin\flutter.bat build apk --release
```

## 项目结构

```text
backend/       FastAPI、Agent、Celery 任务、数据库迁移
mobile_app/    Flutter Android App
web/           Vue 3 辅助网页
data/          来源配置、许可与质量门禁数据
scripts/       启动、手动备份和质量评估脚本
infra/         Docker 与部署配置
docs/          开发说明
artifacts/     本地评测和运行记录（不作为公开资料发布）
```

## 质量与安全设计

- 密钥通过环境变量注入；本地 `.env` 被 Git 忽略。
- 数据库变更使用 Alembic migration；Celery 将耗时生成工作移出 API 请求进程。
- Agent 的工具输入、访问路径、输出结构和来源证据均有约束；不将工具 shell 暴露为任意宿主机命令入口。
- 账户数据按用户隔离；系统错误与模型调用阶段会留下可审计的运行记录。
- 运行部署采取只读容器文件系统、最小权限和健康检查等措施。

## 当前进度

- 后端 Docker 回归与静态检查通过；Flutter 分析通过，App 的 18 项测试通过。
- Android Release APK 已在 OPPO 真机安装并启动到登录页面。完整的邮箱登录、历史读取和系统通知真机流程仍待验收。
- Qwen 已完成隔离 Agent 摘要样本，验证了模型自主选择检索/读取工具及来源证据检查。单篇结果不能代表整体准确率或规模化成本。
- 本机 Docker 服务健康检查通过；公网 API 连通性需从目标网络复测。当前自托管方式依赖本机电脑、Docker 和 Cloudflare Tunnel 持续运行。
- 本机 Git 仓库保留完整开发历史。公开 GitHub 仓库已创建，隐私审查后的源码快照已准备，当前等待 GitHub 身份验证后推送。

## 尚待补足

- 扩大合法可用新闻来源和类别覆盖，建立持续的人工作事实性、相关性评估；目前来源范围不足以代表中国每日综合新闻。
- 完成真机登录与通知验收，以及不同 Android 厂商的后台运行检查。
- 完成真实模型并发、配额、时延和成本评估；设计目标不等于容量承诺。
- 完成独立安全评审、异地备份与整套恢复演练。
- 检查新闻内容使用授权、个人信息保护和公众新闻服务合规要求后，再决定是否公开运营。
- 根域网页入口仍需完成 HTTPS 路由配置；API 子域公网连通性需要复测。

## 隐私与内容说明

公开仓库不包含 `.env`、本机数据库、用户数据、邮箱地址、运行日志或内部安全评估明细。公开演示应使用虚构或获准样例。新闻摘要需要展示原文出处与适用的署名信息，RSS 可访问本身不等于获得转载、保存或 AI 总结的授权。

## 开发检查

```powershell
docker compose run --rm test
```

Flutter 检查见 Android App 一节。数据库 schema 变更须新增 Alembic migration；不要把生产数据或模型密钥写入测试快照。

