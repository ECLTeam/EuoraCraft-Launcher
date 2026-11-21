<div align="center">

<img src="./resources/img/logo.ico" width="320" alt="EuoraCraft Launcher Logo" />

[简体中文](./README.md) · [繁體中文](./README.zh-TW.md) · **English** · [日本語](./README.ja-JP.md) · [Русский](./README.ru-RU.md) · [Deutsch](./README.de-DE.md)

**A modern Minecraft third-party launcher written in Python, with plugin extensibility — build your own launcher**

*Modern Minecraft third-party launcher written in Python, with plugin extensibility, build your own launcher.*

[![Release](https://img.shields.io/github/v/release/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Release&color=40c9c0)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![Stars](https://img.shields.io/github/stars/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Stars&color=f0b429)](https://github.com/ECLTeam/EuoraCraft-Launcher/stargazers)
[![License](https://img.shields.io/badge/License-GPL--3.0-3fb950?style=flat-square)](./LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-64748b?style=flat-square)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)

<br>

[![Website](https://img.shields.io/badge/Website-euora.cn-f0b429?style=for-the-badge)](https://www.euora.cn)
[![Docs](https://img.shields.io/badge/Docs-docs.euora.cn-40c9c0?style=for-the-badge)](https://docs.euora.cn)
[![Download](https://img.shields.io/badge/Download-GitHub%20Releases-3b82c4?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![Issues](https://img.shields.io/badge/Issues-GitHub%20Issues-8b949e?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/issues)

</div>

> **Disclaimer**: This launcher is an independent community tool. It is not affiliated with, authorized by, or endorsed by Mojang Studios, Microsoft, or their affiliates. Minecraft is a trademark of Mojang Synergies AB.
>
> By downloading, installing, or using this software, you confirm that you have read and agree to the [User Agreement](https://docs.euora.cn/en/guide/user-agreement/).

---

## Introduction

**EuoraCraft Launcher** is a third-party Minecraft launcher that helps you install and launch the game, and manage different versions and modpacks. New to Minecraft? Start from "Quick start" below.

---

## Features

- **Manage your game**: browse and launch different game versions and modpacks from one place.
- **Add accounts**: Microsoft accounts, offline accounts and third-party login services such as LittleSkin.
- **Install modpacks and mods**: search and install modpacks online, and manage game mods and other resources.
- **Make it yours**: several built-in themes (Classic, Folia, Absolutely, Catppuccin), light/dark mode and custom backgrounds.
- **Plugins**: add plugins as needed to extend the launcher.
- **Crash analysis**: when the game fails, helps you find the likely cause.

---

## Quick start

1. Open the [download page](https://github.com/ECLTeam/EuoraCraft-Launcher/releases) and pick the build for your system (**Windows / Linux / macOS**).
2. Install and open EuoraCraft Launcher, then add an account as prompted.
3. Install the game version or modpack you want to play, then launch the game.

> For first-time setup, common questions and detailed feature docs, see the [documentation site](https://docs.euora.cn).

## Problems or suggestions?

Check the [user documentation and FAQ](https://docs.euora.cn) first, or [report a problem / suggest an idea](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new/choose). Describe what happened and what you did beforehand, and attach screenshots when useful; remember to censor passwords and other personal information before submitting.

---

## Related projects

- [Qomicex.Tauri (QML)](https://github.com/Qomicex-Public/Qomicex.Tauri/) — a Tauri-based third-party Minecraft launcher maintained by the QML community.

---

## Contributing

Help with translations, documentation or code is welcome; see the [contributing guide](CONTRIBUTING.md) (in Chinese) for details. Developer documentation lives in the [project docs](docs/README.md).

### Run from source

Requires Python 3.11+, Node.js 22 and pnpm 10:

```bash
git clone --recurse-submodules https://github.com/ECLTeam/EuoraCraft-Launcher.git
cd EuoraCraft-Launcher
pip install -e ".[dev]"
cd frontend && pnpm install && pnpm build && cd ..
python main.py
```

`frontend` and `ECL/minecraft/game` are submodules: change them in their own repositories and update the submodule pointer here. See the [contributing guide](CONTRIBUTING.md) for environment requirements, the submission flow and the checks to run.

## Contributors

Thanks to every developer and community supporter who contributed to EuoraCraft Launcher!

<a href="https://github.com/ECLTeam/EuoraCraft-Launcher/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ECLTeam/EuoraCraft-Launcher" />
</a>

---

## Acknowledgements

- [PyTaruiX](https://github.com/ECLTeam/PyTaruiX), [Naive UI](https://github.com/tusen-ai/naive-ui) — powering the launcher
- All contributors and community supporters

---

## License

Released under the [GNU General Public License v3.0](./LICENSE).

<div align="center">

**EuoraCraft Launcher** · ECLTeam

Copyright © 2026 ECLTeam

</div>
