<div align="center">

<img src="./resources/img/logo.ico" width="320" alt="EuoraCraft Launcher Logo" />

[简体中文](./README.md) · [繁體中文](./README.zh-TW.md) · [English](./README.en-US.md) · [日本語](./README.ja-JP.md) · [Русский](./README.ru-RU.md) · **Deutsch**

**Ein moderner Minecraft-Drittanbieter-Launcher in Python, mit Plugins erweiterbar – bau dir deinen eigenen Launcher**

*Modern Minecraft third-party launcher written in Python, with plugin extensibility, build your own launcher.*

[![Release](https://img.shields.io/github/v/release/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Release&color=40c9c0)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![Stars](https://img.shields.io/github/stars/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Stars&color=f0b429)](https://github.com/ECLTeam/EuoraCraft-Launcher/stargazers)
[![License](https://img.shields.io/badge/License-GPL--3.0-3fb950?style=flat-square)](./LICENSE)
[![Platform](https://img.shields.io/badge/Plattformen-Windows%20%7C%20Linux%20%7C%20macOS-64748b?style=flat-square)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)

<br>

[![Website](https://img.shields.io/badge/Website-euora.cn-f0b429?style=for-the-badge)](https://www.euora.cn)
[![Dokumentation](https://img.shields.io/badge/Dokumentation-docs.euora.cn-40c9c0?style=for-the-badge)](https://docs.euora.cn)
[![Download](https://img.shields.io/badge/Download-GitHub%20Releases-3b82c4?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![Feedback](https://img.shields.io/badge/Feedback-GitHub%20Issues-8b949e?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/issues)

</div>

> **Hinweis**: Dieser Launcher ist ein unabhängiges Community-Werkzeug. Er steht in keiner Verbindung zu Mojang Studios, Microsoft oder verbundenen Unternehmen und ist von diesen weder autorisiert noch unterstützt. Minecraft ist eine Marke von Mojang Synergies AB.
>
> Mit dem Herunterladen, Installieren oder Verwenden dieser Software bestätigst du, die [Nutzungsbedingungen](https://docs.euora.cn/de/guide/user-agreement/) gelesen zu haben und ihnen zuzustimmen.

---

## Überblick

**EuoraCraft Launcher** ist ein Minecraft-Drittanbieter-Launcher, mit dem du das Spiel installieren und starten sowie verschiedene Versionen und Modpacks verwalten kannst. Neu bei Minecraft? Dann beginne mit dem Abschnitt „Schnellstart“ weiter unten.

---

## Funktionen

- **Spielverwaltung**: verschiedene Spielversionen und Modpacks zentral ansehen und starten.
- **Konten hinzufügen**: Microsoft-Konten, Offline-Konten und Drittanbieter-Anmeldedienste wie LittleSkin.
- **Modpacks und Mods installieren**: Modpacks online suchen und installieren, Mods und weitere Ressourcen verwalten.
- **Individuelles Aussehen**: mehrere integrierte Designs (Classic, Folia, Absolutely, Catppuccin), heller und dunkler Modus sowie eigene Hintergründe.
- **Plugins**: bei Bedarf Plugins ergänzen und den Funktionsumfang erweitern.
- **Absturzanalyse**: wenn das Spiel ausfällt, hilft der Launcher bei der Suche nach der wahrscheinlichen Ursache.

---

## Schnellstart

1. Öffne die [Download-Seite](https://github.com/ECLTeam/EuoraCraft-Launcher/releases) und wähle die passende Variante für dein System (**Windows / Linux / macOS**).
2. Installiere und starte EuoraCraft Launcher und füge anschließend wie angezeigt ein Konto hinzu.
3. Installiere die gewünschte Spielversion oder das Modpack und starte danach das Spiel.

> Erste Schritte, häufige Fragen und ausführliche Beschreibungen der einzelnen Funktionen findest du auf der [Dokumentationsseite](https://docs.euora.cn).

## Probleme oder Vorschläge?

Schau zuerst in die [Bedienungsdokumentation und die FAQ](https://docs.euora.cn). Du kannst auch [ein Problem melden oder eine Idee vorschlagen](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new/choose). Beschreibe, was passiert ist und was du zuvor getan hast, und füge bei Bedarf Screenshots bei; entferne vor dem Absenden Passwörter und andere persönliche Daten.

---

## Verwandte Projekte

- [Qomicex.Tauri (QML)](https://github.com/Qomicex-Public/Qomicex.Tauri/) — ein Minecraft-Drittanbieter-Launcher auf Tauri-Basis, betreut von der QML-Community.

---

## Mitwirken

Hilfe bei Übersetzungen, Dokumentation oder Code ist willkommen; Details stehen im [Leitfaden für Mitwirkende](CONTRIBUTING.md) (auf Chinesisch). Entwicklerdokumentation findest du in der [Projektdokumentation](docs/README.md).

### Aus dem Quellcode starten

Benötigt Python 3.11+, Node.js 22 und pnpm 10:

```bash
git clone --recurse-submodules https://github.com/ECLTeam/EuoraCraft-Launcher.git
cd EuoraCraft-Launcher
pip install -e ".[dev]"
cd frontend && pnpm install && pnpm build && cd ..
python main.py
```

`frontend` und `ECL/minecraft/game` sind Submodule: Änderungen darin werden in den jeweiligen Repositories committet, hier wird nur der Submodul-Zeiger aktualisiert. Anforderungen an die Umgebung, Ablauf zum Einreichen und die nötigen Prüfungen stehen im [Leitfaden für Mitwirkende](CONTRIBUTING.md).

## Mitwirkende

Danke an alle Entwicklerinnen, Entwickler und Unterstützer aus der Community, die zu EuoraCraft Launcher beigetragen haben!

<a href="https://github.com/ECLTeam/EuoraCraft-Launcher/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ECLTeam/EuoraCraft-Launcher" />
</a>

---

## Danksagung

- [PyTaruiX](https://github.com/ECLTeam/PyTaruiX), [Naive UI](https://github.com/tusen-ai/naive-ui) — Grundlage des Launchers
- Alle Mitwirkenden und Unterstützer aus der Community

---

## Lizenz

Dieses Projekt ist unter der [GNU General Public License v3.0](./LICENSE) veröffentlicht.

<div align="center">

**EuoraCraft Launcher** · ECLTeam

Copyright © 2026 ECLTeam

</div>
