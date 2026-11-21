<div align="center">

<img src="./resources/img/logo.ico" width="320" alt="EuoraCraft Launcher Logo" />

[简体中文](./README.md) · [繁體中文](./README.zh-TW.md) · [English](./README.en-US.md) · [日本語](./README.ja-JP.md) · **Русский** · [Deutsch](./README.de-DE.md)

**Современный сторонний лаунчер Minecraft на Python с поддержкой плагинов — собери свой лаунчер**

*Modern Minecraft third-party launcher written in Python, with plugin extensibility, build your own launcher.*

[![Release](https://img.shields.io/github/v/release/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Release&color=40c9c0)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![Stars](https://img.shields.io/github/stars/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Stars&color=f0b429)](https://github.com/ECLTeam/EuoraCraft-Launcher/stargazers)
[![License](https://img.shields.io/badge/License-GPL--3.0-3fb950?style=flat-square)](./LICENSE)
[![Platform](https://img.shields.io/badge/%D0%9F%D0%BB%D0%B0%D1%82%D1%84%D0%BE%D1%80%D0%BC%D1%8B-Windows%20%7C%20Linux%20%7C%20macOS-64748b?style=flat-square)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)

<br>

[![Сайт](https://img.shields.io/badge/%D0%A1%D0%B0%D0%B9%D1%82-euora.cn-f0b429?style=for-the-badge)](https://www.euora.cn)
[![Документация](https://img.shields.io/badge/%D0%94%D0%BE%D0%BA%D1%83%D0%BC%D0%B5%D0%BD%D1%82%D0%B0%D1%86%D0%B8%D1%8F-docs.euora.cn-40c9c0?style=for-the-badge)](https://docs.euora.cn)
[![Загрузка](https://img.shields.io/badge/%D0%97%D0%B0%D0%B3%D1%80%D1%83%D0%B7%D0%BA%D0%B0-GitHub%20Releases-3b82c4?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![Обратная связь](https://img.shields.io/badge/%D0%9E%D0%B1%D1%80%D0%B0%D1%82%D0%BD%D0%B0%D1%8F%20%D1%81%D0%B2%D1%8F%D0%B7%D1%8C-GitHub%20Issues-8b949e?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/issues)

</div>

> **Отказ от ответственности**: этот лаунчер — независимый инструмент сообщества. Он не связан с Mojang Studios, Microsoft и их аффилированными компаниями, не авторизован и не одобрен ими. Minecraft — товарный знак Mojang Synergies AB.
>
> Загружая, устанавливая или используя это программное обеспечение, вы подтверждаете, что прочитали и принимаете [Пользовательское соглашение](https://docs.euora.cn/ru/guide/user-agreement/).

---

## О лаунчере

**EuoraCraft Launcher** — сторонний лаунчер Minecraft, который помогает устанавливать и запускать игру, управлять разными версиями и сборками. Если вы только знакомитесь с Minecraft, начните с раздела «Быстрый старт» ниже.

---

## Возможности

- **Управление игрой**: просмотр и запуск разных версий игры и сборок в одном месте.
- **Аккаунты**: аккаунты Microsoft, офлайн-аккаунты и сторонние сервисы входа (например, LittleSkin).
- **Сборки и моды**: поиск и установка сборок онлайн, управление модами и другими ресурсами.
- **Внешний вид**: несколько встроенных тем (Classic, Folia, Absolutely, Catppuccin), светлая и тёмная темы, свои фоны.
- **Плагины**: подключайте плагины по необходимости и расширяйте возможности лаунчера.
- **Анализ крашей**: при сбое игры помогает найти вероятную причину.

---

## Быстрый старт

1. Откройте [страницу загрузки](https://github.com/ECLTeam/EuoraCraft-Launcher/releases) и выберите сборку для своей системы (**Windows / Linux / macOS**).
2. Установите и запустите EuoraCraft Launcher, затем добавьте аккаунт по подсказкам в интерфейсе.
3. Установите нужную версию игры или сборку и запустите игру.

> Первый запуск, частые вопросы и подробное описание функций — на [сайте документации](https://docs.euora.cn).

## Проблемы и предложения

Сначала загляните в [руководство и раздел частых вопросов](https://docs.euora.cn), а затем можете [сообщить о проблеме или предложить идею](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new/choose). Опишите, что произошло и что вы делали до этого, при необходимости приложите скриншоты; перед отправкой скройте пароли и другие личные данные.

---

## Связанные проекты

- [Qomicex.Tauri (QML)](https://github.com/Qomicex-Public/Qomicex.Tauri/) — сторонний лаунчер Minecraft на Tauri, который поддерживает сообщество QML.

---

## Участие в разработке

Мы рады помощи с переводами, документацией и кодом; подробности — в [руководстве для контрибьюторов](CONTRIBUTING.md) (на китайском). Документация для разработчиков — в [документации проекта](docs/README.md).

### Запуск из исходников

Требуются Python 3.11+, Node.js 22 и pnpm 10:

```bash
git clone --recurse-submodules https://github.com/ECLTeam/EuoraCraft-Launcher.git
cd EuoraCraft-Launcher
pip install -e ".[dev]"
cd frontend && pnpm install && pnpm build && cd ..
python main.py
```

`frontend` и `ECL/minecraft/game` — подмодули: изменения в них коммитятся в свои репозитории, а здесь обновляется только указатель подмодуля. Требования к окружению, порядок отправки изменений и список проверок — в [руководстве для контрибьюторов](CONTRIBUTING.md).

## Участники

Спасибо всем разработчикам и участникам сообщества, которые помогали EuoraCraft Launcher!

<a href="https://github.com/ECLTeam/EuoraCraft-Launcher/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ECLTeam/EuoraCraft-Launcher" />
</a>

---

## Благодарности

- [PyTaruiX](https://github.com/ECLTeam/PyTaruiX), [Naive UI](https://github.com/tusen-ai/naive-ui) — технологии, на которых работает лаунчер
- Все контрибьюторы и участники сообщества

---

## Лицензия

Проект распространяется под [GNU General Public License v3.0](./LICENSE).

<div align="center">

**EuoraCraft Launcher** · ECLTeam

Copyright © 2026 ECLTeam

</div>
