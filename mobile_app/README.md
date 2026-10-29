# PulseBrief Android App

Flutter Android 客户端，连接项目根目录中的 FastAPI 服务。

## 当前功能

- 邮箱验证码登录/注册（服务端 SMTP 配置完成后可用）
- 仅限开发环境的本机体验登录及安全存储访问令牌
- 今日简报、历史列表与详情
- 话题、关键词、排除词、摘要长度和每日启用状态设置
- 手动生成预览简报并轮询服务端任务状态
- 简报收藏和已读状态
- 来源原文跳转和 Agent 工具调用记录
- 在「我的」页导出个人数据 JSON，并复制到系统剪贴板
- App 重启后恢复查询未完成的生成任务
- 可修改后端地址，适配 Android 模拟器与真机局域网

验证码首次成功后自动创建账户；邮箱只用于身份验证，不用于简报提醒。阿里云 SMTP 已通过真实验证码投递与登录验收。发布的简报会进入 App 内通知中心，未读数量与服务端已读状态同步。演示登录只允许开发环境，不能公开部署。Android 系统推送和 iOS 版本尚未实现。

## 构建

项目与工具缓存统一放在 D/F 盘：

```powershell
$env:PUB_CACHE = 'F:\Resources\flutter-pub-cache'
$env:GRADLE_USER_HOME = 'F:\Resources\gradle-cache'
$env:ANDROID_USER_HOME = 'F:\Resources\codex-tools\pulsebrief\.android-user-home'
$env:ANDROID_HOME = 'F:\Resources\codex-tools\pulsebrief\android-sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:JAVA_HOME = 'C:\Program Files\Java\jdk-17'
$flutter = 'F:\Resources\codex-tools\pulsebrief\flutter\bin\flutter.bat'
& $flutter pub get
& $flutter analyze
& $flutter test
& $flutter build apk --debug
```

`ANDROID_USER_HOME` 指向的目录保存了与当前侧载版相同的本机调试签名；重新构建 Release 时应保留该设置，否则 Android 会把新包视为不同签名，无法覆盖安装。该调试签名仅供本机侧载验收，不能用于公开商店发布。

Debug APK 输出到 `build/app/outputs/flutter-apk/app-debug.apk`；可安装包副本位于 `../artifacts/PulseBrief-Android-debug.apk`。Android 模拟器默认连接 `http://10.0.2.2:8000`，Release 默认连接 `https://api.pulsebrief.cloud`。





## 当前 Release APK

当前可侧载版本为 `1.0.0+24`，APK 位于 `../artifacts/PulseBrief-Android-release.apk`。本版在任务无匹配但证据校验失败时，改为明确提示候选内容未能完成核验且未发布。Flutter analyze 通过，18 项 Widget 测试通过。Release 包大小 53,066,521 字节，SHA-256 `BEC008897114A8315CF253E5A1A473EDFD717BFB8A09415F5DF4C6C1F2E3DF50`；签名与 +22 一致，可覆盖本机侧载版。实体手机当前未出现在 ADB 列表，因此尚未真机安装验收。该包使用本机 Android debug 签名，只适合侧载。


