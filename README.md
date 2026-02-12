# Kozu XML Integrator / 公図XML整合ツール

法務局地図XML（公図）を森林計画図の地番情報と照合し、位置調整を行った上でGISレイヤとして出力するQGISプラグインです。

森林計画図上での地番と照合、位置関係を判断することを目的とします。

## 主な機能

- 法務局地図XMLファイルの読み込み・パース
- 森林計画図との地番照合
- 座標変換・位置調整
- オーバーレイタイル表示（QGIS登録済みXYZ接続・プロジェクトレイヤー対応）
- GISレイヤとしての出力

## 動作要件

- QGIS 3.0 以上

## インストール

1. このリポジトリをクローンまたはダウンロード
2. `kozu_xml_integrator` フォルダを QGIS のプラグインディレクトリにコピー
   - Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`
   - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins\`
   - macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/`
3. QGISを起動し、プラグインマネージャから有効化

## ライセンス

GPL-2.0-or-later
