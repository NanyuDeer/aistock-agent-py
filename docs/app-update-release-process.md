# App 新版本发布流程（应用内自动更新）

> 目标：发一个新版本时，让**已安装的用户打开 App 后自动弹出更新提示**，点击「立即更新」即可下载安装新 APK。本文档是完整操作手册，按步骤执行即可。

---

## 1. 更新机制原理（先看懂，避免踩坑）

App（仅 **Android** 端）每次启动时，由 [useAppUpdate.ts](file:///d:/aistock/aistock-app-frontend/src/shared/utils/useAppUpdate.ts) 触发检查：

```
启动 → 拉取 https://gupiao.yaozhineng.com/download/version.json
     → 读出 versionCode（线上）
     → 与本机已安装 versionCode 对比（plus.android 读取）
     → 线上 > 本机  → 弹更新框（可「立即更新」/「永久关闭」/叉掉）
     → 否则         → 不提示
```

关键字段（来自 [version.json](file:///d:/aistock/aistock-frontend/public/download/version.json)，Web 前端仓库托管）：

| 字段 | 作用 |
|------|------|
| `versionName` | 展示版本号（如 `0.1.1`），仅展示用 |
| `versionCode` | **核心**，更新对比用的是它，**必须严格递增** |
| `downloadUrl` | 相对 `…/download/` 的 APK 文件名 |
| `description` / `features` | 弹窗里显示的更新文案 |

### ⚠️ 最重要的一条铁律：`versionCode` 必须严格递增

更新判断只看 `versionCode`：
- 线上 `versionCode` **大于** 本机已装的 `versionCode` → 才弹更新。
- 若是 **等于或小于** 已装版本 → **不会弹更新**（App 认为已是最新）。
- `versionName`（如 0.1.1）与 `versionCode` 相互独立，不改 `versionCode` 只改 `versionName` 无法触发更新。

> 本次示例：修复「0.1.1 打不开」的正式版仍叫 `0.1.1`、`versionCode` 仍为 `101`。
> **注意**：若存量用户已装上故障的 `versionCode=101`，线上也是 `101`，则这些用户**不会收到 App 内更新提示**，需直接重新安装 APK。要让这类用户也能自动更新，应把本次 `versionCode` 升到 `102` 及以上。

---

## 0. 前置条件与权限（打包前先确认，缺一项都会卡住）

发布需要以下环境 / 权限，**由项目负责人准备并提供给执行人**，缺一不可：

- **HBuilderX（App 开发版）**：云打包必需；用普通版无法 `发行→云打包`。
- **DCloud 账号已登录**，且对该 App（appid `__UNI__73E911F`）有**云打包权限**。
- **正式签名证书**：`.keystore` 密钥库文件 + 别名 + 密码，由负责人保管。**必须是历史发版用的同一份正式证书**（见第 3 节，决定能否覆盖升级）。
- **服务器 SSH 权限**：本机 `~/.ssh` 的公钥已被授权到 `aistock@121.37.46.229`，能免密执行 `ssh` / `scp`。
- **代码访问权限**：能拉取/推送两个 GitHub 仓库（`aistock-app-frontend` 分支 `master`、`aistock-frontend` 分支 `main`）。

> 若执行人发现自己缺某项权限，先找负责人补齐，不要用错误替代（如调试证书、普通版 HBuilder）勉强进行。

---

## 2. 发布前准备（改版本号 + 更新文案）

### 2.0 打包前：先拉到最新代码（避免漏掉已合入的修复）

改版本号前，务必先确保本地基于**最新主干**，否则会把其他成员已提交的修复漏掉：

```bash
# aistock-app-frontend 仓库（分支 master）
cd d:/aistock/aistock-app-frontend
git checkout master && git pull origin master
git log --oneline -3   # 确认已包含你要打包的那版提交
```

### 2.1 修改 `src/manifest.json`（App 端打包依据）

文件：[src/manifest.json](file:///d:/aistock/aistock-app-frontend/src/manifest.json)

```jsonc
{ 
  "name": "洞见",
  "versionName": "0.1.1",   // 新版本号，展示用
  "versionCode": 101        // 必须大于已发布版本（升级的关键）
}
```

### 2.2 更新文案写法（重点：只写「本次更新了什么」，不写 App 介绍）

文案文件：[aistock-frontend/public/download/version.json](file:///d:/aistock/aistock-frontend/public/download/version.json)

**写法规范**：
- `description`：一句话讲清本次改了什么 / 修了什么（不要写「这是一款 AI 股票 App」这类介绍）。
- `features`：逐条列出本次具体改动，用户看得懂、能感知的功能点即可。

示例：

```json
{
  "appName": "洞见",
  "versionName": "0.1.1",
  "versionCode": 101,
  "downloadUrl": "aistock-0.1.1.apk",
  "fileSize": "22.9 MB",
  "releaseDate": "2026-08-24",
  "minSystem": "Android 5.0 及以上",
  "description": "本版修复 APP 启动白屏、无法进入应用的问题。",
  "features": [
    "修复部分设备 APP 启动白屏/闪退（兼容老旧 Android WebView）",
    "修复 K 线图加载异常导致的页面空白",
    "优化 APP 首屏启动稳定性"
  ]
}
```

> 本次 `versionCode`、`downloadUrl`、`description`、`features` 均需随版本更新保持一致。
> **`fileSize` 也要同步**：每次打包 APK 大小会变，按打包产物实际大小更新（保留 1 位小数）；`releaseDate`、`minSystem` 视情况同步更新，避免弹窗/下载页显示不准。

---

## 3. 打包 APK（HBuilderX）

1. 打开本项目 `aistock-app-frontend`（确保 HBuilderX 是 **App 开发版**、DCloud 账号已登录，见第 0 节）。
2. 顶部菜单：**发行 → 原生 App-云打包 → Android**。
3. **选择证书**：必须选择**与历史发版完全相同的正式签名证书**（密钥库 `.keystore` + 别名 + 密码，由负责人提供）。
   - ⚠️ **严禁用 HBuilder 调试证书**——签名与正式包不一致，用户装旧版时会提示"签名冲突、无法覆盖安装"，升级直接失败。
4. 填写证书信息（别名/密码务必与负责人提供的一致），确认后进行云打包。
5. 打包完成产物位于：`dist/release/apk/__UNI__73E911F__*.apk`。
6. **重命名**为 `aistock-<versionName>.apk`（如 `aistock-0.1.1.apk`），与 `version.json` 中 `downloadUrl` 一致。

---

## 4. 将安装包放到服务器

安装包最终由 Web 前端静态目录下发，下载根地址为：

```
https://gupiao.yaozhineng.com/download/
```

即服务器 `/var/www/aistock/download/` 目录。

**放置**（把本地 `aistock-0.1.1.apk` 上传到该目录）：

```bash
scp dist/release/apk/aistock-0.1.1.apk aistock@121.37.46.229:/var/www/aistock/download/
```

> 上线后 App 更新的下载地址即：`https://gupiao.yaozhineng.com/download/aistock-0.1.1.apk`。

⚠️ **注意**：服务器 Web 重新部署时若执行 `rm -rf /var/www/aistock/*`，会**连带删除 APK**。若重新构建前端，**必须在上传产物后重新 scp APK**，或在部署命令中保留 `download` 目录。

---

## 5. 部署 Web 前端（让 version.json 对新版本生效）

更新文案及下载信息都在 Web 前端静态资源里，需要构建部署一次（服务器 `/home/aistock/aistock-frontend`，分支 `main`）：

```bash
ssh aistock@121.37.46.229

# 1) 关键前置：非交互 SSH 下 node/npm 不在 PATH，必须先用绝对路径导入（否则报 npm: command not found / spawn sh ENOENT）
export PATH=/home/aistock/.nvm/versions/node/v20.20.2/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# 2) 拉最新代码（web 仓库分支 main）
cd /home/aistock/aistock-frontend
git pull origin main

# 3) 构建并部署到 Web 根目录
npm run build && rm -rf /var/www/aistock/* && cp -r dist/* /var/www/aistock/

# 4) 重要：部署完重新上传 APK（见第 4 节，否则被 rm -rf 清掉）
scp dist/release/apk/aistock-0.1.1.apk aistock@121.37.46.229:/var/www/aistock/download/
```

> 后端（aistock-app-api）仅在本次有后端代码改动时才需要同步部署；**只发 App 新版本、纯文案/APK 变更时无需动后端**。

---

## 6. 发版前自检清单

- [ ] 代码已基于**最新主干**（app `master`、web `main`，已 git pull）
- [ ] `manifest.json` 的 `versionCode` **大于**线上已发布版本
- [ ] 打包使用**正式签名证书**（非 HBuilder 调试证书）
- [ ] `manifest.json` 与 `version.json` 的 `versionName` / `versionCode` **一致**
- [ ] `version.json` 的 `downloadUrl` / `fileSize` 与服务器上 APK **一致**
- [ ] APK 已上传到 `/var/www/aistock/download/` 且大小非 0
- [ ] 文案 `description`/`features` 写的是「本次更新内容」，非 App 介绍
- [ ] Web 前端已重新构建部署，`version.json` 已更新到线上
- [ ] 真机（Android）装旧版后打开，能正常弹更新、能下载成功

---

## 7. 完整发布流程速览（10 步）

```
⓪ 确认前置条件与权限    → 见第 0 节（证书/DCloud/SSH/仓库访问）
① git pull 到最新主干    → app(master)、web(main)
② 改 src/manifest.json    → versionName / versionCode
③ 改 public/download/version.json → 文案 + versionCode + downloadUrl + fileSize
④ HBuilderX 云打包(正式证书)→ 产出 dist/release/apk/*.apk
⑤ 重命名 APK              → aistock-<版本号>.apk
⑥ 提交并推送代码          → app: master、web: main
⑦ 上传 APK 到服务器        → /var/www/aistock/download/
⑧ 部署 Web 前端            → 见第 5 节（先 export PATH，部署后重新上传 APK）
⑨ （可选）部署后端          → 仅当后端有改动
⑩ 自检 + 真机验证          → 见第 6 节
```

---

## 8. 常见问题

| 现象 | 原因 / 处理 |
|------|-------------|
| 用户打开不弹更新 | 线上 `versionCode ≤` 已装版本 → 必须把 `versionCode` 加 1 再发 |
| 在线 `0.1.1` 用户收不到本次 `0.1.1` | 同 `versionCode` 不触发；需把 `versionCode` 升到 `102+` 才能让他们自动更新 |
| 已装到最新版仍反复弹更新 | 本机 `versionCode` 读取失败返回 `0` 导致误判为比线上旧。读取须走 `plus.android.invoke(pkgInfo,'get','versionCode')`，不可直接 `pkgInfo.versionCode` 属性访问（见 `src/shared/utils/useAppUpdate.ts`）；读取失败时应保守不弹 |
| 手机不提示下载 | Android 需允许安装未知应用（弹窗内已给引导） |
| iOS / H5 / 小程序 | 该机制仅 Android App 端生效，其他端需另走应用商店更新 |
| 网页 `/download` 打不开 | 服务器 Caddy 需保证 `try_files {path} /index.html`（含 `download` 目录下的 APK 静态资源） |