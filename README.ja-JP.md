<div align="center">

<img src="./resources/img/logo.ico" width="320" alt="EuoraCraft Launcher Logo" />

[简体中文](./README.md) · [繁體中文](./README.zh-TW.md) · [English](./README.en-US.md) · **日本語** · [Русский](./README.ru-RU.md) · [Deutsch](./README.de-DE.md)

**Python で書かれたモダンな Minecraft サードパーティランチャー。プラグインで拡張して、自分だけのランチャーを作ろう**

*Modern Minecraft third-party launcher written in Python, with plugin extensibility, build your own launcher.*

[![Release](https://img.shields.io/github/v/release/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Release&color=40c9c0)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![Stars](https://img.shields.io/github/stars/ECLTeam/EuoraCraft-Launcher?style=flat-square&logo=github&label=Stars&color=f0b429)](https://github.com/ECLTeam/EuoraCraft-Launcher/stargazers)
[![License](https://img.shields.io/badge/License-GPL--3.0-3fb950?style=flat-square)](./LICENSE)
[![Platform](https://img.shields.io/badge/%E3%83%97%E3%83%A9%E3%83%83%E3%83%88%E3%83%95%E3%82%A9%E3%83%BC%E3%83%A0-Windows%20%7C%20Linux%20%7C%20macOS-64748b?style=flat-square)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)

<br>

[![公式サイト](https://img.shields.io/badge/%E5%85%AC%E5%BC%8F%E3%82%B5%E3%82%A4%E3%83%88-euora.cn-f0b429?style=for-the-badge)](https://www.euora.cn)
[![ドキュメント](https://img.shields.io/badge/%E3%83%89%E3%82%AD%E3%83%A5%E3%83%A1%E3%83%B3%E3%83%88-docs.euora.cn-40c9c0?style=for-the-badge)](https://docs.euora.cn)
[![ダウンロード](https://img.shields.io/badge/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89-GitHub%20Releases-3b82c4?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)
[![フィードバック](https://img.shields.io/badge/%E3%83%95%E3%82%A3%E3%83%BC%E3%83%89%E3%83%90%E3%83%83%E3%82%AF-GitHub%20Issues-8b949e?style=for-the-badge)](https://github.com/ECLTeam/EuoraCraft-Launcher/issues)

</div>

> **免責事項**：本ランチャーは独立したコミュニティ製ツールです。Mojang Studios、Microsoft およびその関連会社とは一切関係がなく、許諾や承認も受けていません。Minecraft は Mojang Synergies AB の商標です。
>
> 本ソフトウェアをダウンロード、インストール、または使用した時点で、[利用規約](https://docs.euora.cn/ja/guide/user-agreement/)を読み、同意したものとみなされます。

---

## 概要

**EuoraCraft Launcher** は Minecraft のサードパーティランチャーです。ゲームのインストールと起動、さまざまなバージョンや MOD パックの管理をまとめて行えます。Minecraft が初めての方は、下の「クイックスタート」からどうぞ。

---

## 機能

- **ゲーム管理**：バージョンや MOD パックを一覧から確認して起動できます。
- **アカウント追加**：Microsoft アカウント、オフラインアカウント、サードパーティのログインサービス（LittleSkin など）に対応。
- **MOD パックと MOD の導入**：MOD パックをオンラインで検索・インストールし、MOD などのリソースを管理できます。
- **見た目のカスタマイズ**：複数の内蔵テーマ（Classic、Folia、Absolutely、Catppuccin）、ライト／ダークモード、背景画像の変更。
- **プラグイン拡張**：必要なプラグインを追加してランチャーの機能を拡張できます。
- **クラッシュ解析**：ゲームが落ちたときに、原因の手がかりを探します。

---

## クイックスタート

1. [ダウンロードページ](https://github.com/ECLTeam/EuoraCraft-Launcher/releases)を開き、お使いの環境に合ったビルド（**Windows / Linux / macOS**）を選びます。
2. EuoraCraft Launcher をインストールして起動し、画面の案内に従ってアカウントを追加します。
3. 遊びたいバージョンまたは MOD パックをインストールし、完了後にゲームを起動します。

> 初回の使い方、よくある質問、各機能の詳しい説明は[ドキュメントサイト](https://docs.euora.cn)をご覧ください。

## 問題や提案がある場合

まず[利用ガイドとよくある質問](https://docs.euora.cn)を確認してください。[不具合の報告や提案](https://github.com/ECLTeam/EuoraCraft-Launcher/issues/new/choose)も受け付けています。何が起きたか、その前に何をしたかを書き、必要に応じてスクリーンショットを添付してください。投稿前にパスワードなどの個人情報を伏せてください。

---

## 関連プロジェクト

- [Qomicex.Tauri（QML）](https://github.com/Qomicex-Public/Qomicex.Tauri/) — QML コミュニティがメンテナンスする Tauri 製の Minecraft サードパーティランチャー。

---

## コントリビュート

翻訳、ドキュメント、コードの改善を歓迎します。詳しくは[コントリビュートガイド](CONTRIBUTING.md)（中国語）をご覧ください。開発向けの資料は[プロジェクトドキュメント](docs/README.md)にあります。

### ソースから実行する

Python 3.11 以上、Node.js 22、pnpm 10 が必要です。

```bash
git clone --recurse-submodules https://github.com/ECLTeam/EuoraCraft-Launcher.git
cd EuoraCraft-Launcher
pip install -e ".[dev]"
cd frontend && pnpm install && pnpm build && cd ..
python main.py
```

`frontend` と `ECL/minecraft/game` はサブモジュールです。内部の変更はそれぞれのリポジトリにコミットし、このリポジトリではサブモジュールのポインタだけを更新します。環境要件、提出の流れ、実行するチェックについては[コントリビュートガイド](CONTRIBUTING.md)を参照してください。

## コントリビューター

EuoraCraft Launcher に貢献してくださったすべての開発者とコミュニティの皆さんに感謝します。

<a href="https://github.com/ECLTeam/EuoraCraft-Launcher/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=ECLTeam/EuoraCraft-Launcher" />
</a>

---

## 謝辞

- [PyTaruiX](https://github.com/ECLTeam/PyTaruiX)、[Naive UI](https://github.com/tusen-ai/naive-ui) — ランチャーを支える技術
- すべてのコントリビューターとコミュニティの皆さん

---

## ライセンス

本プロジェクトは [GNU General Public License v3.0](./LICENSE) の下で公開されています。

<div align="center">

**EuoraCraft Launcher** · ECLTeam

Copyright © 2026 ECLTeam

</div>
