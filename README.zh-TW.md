<div align="center">

<img src="./resources/img/logo.ico" width="320" alt="EuoraCraft Launcher Logo" />

[简体中文](./README.md) · **繁體中文** · [English](./README.en-US.md) · [日本語](./README.ja-JP.md) · [Русский](./README.ru-RU.md) · [Deutsch](./README.de-DE.md)

**一個使用 Python 編寫的現代化 Minecraft 第三方啟動器，支援外掛擴充功能，打造屬於自己的啟動器吧**

*Modern Minecraft third-party launcher written in Python, with plugin extensibility, build your own launcher.*

[![Release](https://img.shields.io/github/v/release/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Release&color=40c9c0)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![Stars](https://img.shields.io/github/stars/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Stars&color=f0b429)](https://github.com/ECLTeam/EuoraCraft-Launcher/stargazers)
[![License](https://img.shields.io/badge/License-GPL--3.0-3fb950?style=flat-square)](./LICENSE)
[![Platform](https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-Windows%20%7C%20Linux%20%7C%20macOS-64748b?style=flat-square)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)

<br>

[![官網](https://img.shields.io/badge/%E5%AE%98%E7%B6%B2-euora.cn-f0b429?style=for-the-badge)](https://www.euora.cn)
[![文件](https://img.shields.io/badge/%E6%96%87%E4%BB%B6-docs.euora.cn-40c9c0?style=for-the-badge)](https://docs.euora.cn)
[![下載](https://img.shields.io/badge/%E4%B8%8B%E8%BC%89-GitHub%20Releases-3b82c4?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![回饋](https://img.shields.io/badge/%E5%9B%9E%E9%A5%8B-GitHub%20Issues-8b949e?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/issues)

</div>

> **免責聲明**：本啟動器是獨立的第三方社群工具，與 Mojang Studios、Microsoft 及其關係企業沒有任何關聯，也未取得其授權或認可。Minecraft 是 Mojang Synergies AB 的註冊商標。
>
> 下載、安裝或使用本軟體，即表示您已閱讀並同意[使用者條款](https://docs.euora.cn/zh-tw/guide/user-agreement/)。

---

## 介紹

**EuoraCraft Launcher** 是一款 Minecraft 第三方啟動器，協助你安裝並啟動遊戲、管理不同版本與整合包。初次接觸 Minecraft，也可以從下方的「快速開始」著手。

---

## 功能特色

- **管理遊戲**：集中檢視與啟動不同版本的遊戲與整合包。
- **新增帳號**：支援 Microsoft 帳號、離線帳號與第三方登入服務（例如 LittleSkin）。
- **安裝整合包與模組**：線上搜尋並安裝整合包，管理遊戲模組（Mod）等資源。
- **個性外觀**：多套內建主題（經典、Folia、Absolutely、Catppuccin）、明暗模式與自訂背景。
- **外掛擴充**：依需求加入外掛，豐富啟動器功能。
- **崩潰分析**：遊戲出錯時，協助找出可能原因。

---

## 快速開始

1. 開啟[下載頁面](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)，選擇適合自己電腦系統的版本（**Windows / Linux / macOS**）。
2. 安裝並開啟 EuoraCraft Launcher，依介面提示新增帳號。
3. 安裝想玩的遊戲版本或整合包，完成後啟動遊戲。

> 首次使用、常見問題與各功能的詳細說明，請參閱[文件站](https://docs.euora.cn)。

## 遇到問題或有建議？

可以先查看[使用文件與常見問題](https://docs.euora.cn)，也可以[回報問題或提出建議](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new/choose)。回報時請說明遇到什麼狀況、先前做了哪些操作，並視需要附上截圖；送出前記得遮住密碼等個人資訊。

---

## 合作專案

- [Qomicex.Tauri（QML）](https://github.com/Qomicex-Public/Qomicex.Tauri/) — 由 QML 社群維護的 Tauri 第三方 Minecraft 啟動器專案。

---

## 參與貢獻

歡迎協助完善翻譯、文件或程式碼，具體方式見[貢獻指南](CONTRIBUTING.md)。開發相關說明見[專案文件](docs/README.md)。

### 從原始碼執行

需要 Python 3.11+、Node.js 22 與 pnpm 10：

```bash
git clone --recurse-submodules https://github.com/ECLTeam/EuoraCraft-Launcher.git
cd EuoraCraft-Launcher
pip install -e ".[dev]"
cd frontend && pnpm install && pnpm build && cd ..
python main.py
```

`frontend` 與 `ECL/minecraft/game` 是子模組，內部變更提交到各自的倉庫，主倉庫只更新子模組指標。環境需求、提交流程與檢查範圍見[貢獻指南](CONTRIBUTING.md)。

## 貢獻者

感謝所有為 EuoraCraft Launcher 做出貢獻的開發者與社群支持者！

<a href="https://github.com/ECLTeam/EuoraCraft-Launcher/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ECLTeam/EuoraCraft-Launcher" />
</a>

---

## 致謝

- [PyTaruiX](https://github.com/ECLTeam/PyTaruiX)、[Naive UI](https://github.com/tusen-ai/naive-ui) — 為啟動器提供支援
- 所有貢獻者與社群支持者

---

## 授權條款

本專案基於 [GNU General Public License v3.0](./LICENSE) 開源。

<div align="center">

**EuoraCraft Launcher** · ECLTeam

Copyright © 2026 ECLTeam

</div>
