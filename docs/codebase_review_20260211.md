# プラグイン現状レポート: 公図XML整合ツール (Kozu XML Integrator)

レビュー日: 2026-02-11

---

## 全体構成

```
kozu_xml_integrator/
├── __init__.py              # エントリポイント (classFactory)
├── kozu_xml_integrator.py   # プラグインメインクラス
├── kozu_main_window.py      # スタンドアロンウィンドウ (主要UI)
├── kozu_main_window.ui      # Qt Designer UIファイル
├── metadata.txt             # プラグインメタデータ (v0.1, experimental)
├── core/                    # コアモジュール群
│   ├── xml_parser.py        # lxml.iterparseによるストリーミングXML解析
│   ├── geometry_builder.py  # QgsGeometry構築 (X/Yスワップ対応)
│   ├── database_manager.py  # SpatiaLite管理 (スキーマv2.0.0)
│   ├── spatial_join.py      # 行政界との空間結合
│   ├── search_index.py      # 大字→小字→地番の階層的検索インデックス
│   ├── importer.py          # インポートオーケストレーション
│   └── integration_engine.py # 統合ワークフロー (v2)
├── transform/               # 座標変換モジュール群
│   ├── helmert_transform.py # ヘルマート変換 (4パラメータ)
│   ├── tps_transform.py     # Thin Plate Spline変換
│   ├── matching_algorithm.py # 地番/形状マッチング
│   ├── spatial_fitting.py   # チェーンポジショニング (PRIMARY)
│   ├── xml_joiner.py        # XML結合 (同一大字内)
│   └── leveling.py          # オーバーラップ解消
├── ui/                      # UIコントローラ群
│   ├── import_panel.py      # インポートタブ
│   ├── integration_panel.py # 統合タブ
│   ├── browse_panel.py      # 検索タブ
│   ├── export_panel.py      # エクスポートタブ
│   └── transform_panel.py   # 変換タブ (最大1550行)
└── docs/
    └── integration_algorithm_v2.md # 統合アルゴリズム設計書
```

---

## 主要技術要素

- **QGIS Plugin Architecture**: Plugin Builder生成構造、`classFactory` / `initGui` / `unload` ライフサイクル
- **SpatiaLite Database**: スキーマv2.0.0、テーブル: `t_xml_meta`, `t_fude_poly`, `t_fude_edge`, `t_transform_log`, `t_schema_info`
- **法務局地図XML (公図)**: GM_Point, GM_Curve, GM_Surface の空間属性と 筆界点/筆界線/筆 のテーマ属性
- **座標系**: 公共座標系 (EPSG:6676 JGD2011 CS VIII) と任意座標系のデュアルハンドリング
- **X/Y座標スワップ**: 日本の慣習 (X=北, Y=東) vs QGIS標準 (X=東, Y=北)
- **ヘルマート変換**: 4パラメータ2D類似変換 (平行移動、回転、均一スケール)、最小二乗法
- **Thin Plate Spline (TPS)**: 制御点による非線形スムーズワーピング
- **チェーンポジショニング**: 公共座標アンカーから地番マッチング経由で任意座標マップを配置
- **信頼性追跡**: HIGH/MEDIUM/LOW の3段階
- **レベリング**: 反復縮小法によるオーバーラップ解消（公共座標データは不調整）
- **lxml.iterparse**: メモリ効率的なストリーミングXML解析
- **QThread Workers**: Import, Transform, Integration, Export の非同期バックグラウンド処理

---

## 実装状況

| モジュール | 状態 | 備考 |
|-----------|------|------|
| XMLパーサ | **完成** | 3パス解析、メモリ効率的 |
| ジオメトリビルダ | **完成** | X/Yスワップ、曲線方向対応 |
| データベース管理 | **完成** | スキーマv2.0.0、マイグレーション対応 |
| 空間結合 | **完成** | QgsSpatialIndex利用 |
| 検索インデックス | **完成** | 全角/半角正規化、自然順ソート |
| インポート | **完成** | QThread非同期処理 |
| ヘルマート変換 | **完成** | numpy最小二乗法 |
| TPS変換 | **完成** | 正則化サポート |
| マッチングアルゴリズム | **完成** | 地番/形状/境界の3戦略 |
| チェーンポジショニング | **完成** | 波状伝播アルゴリズム |
| XML結合 | **完成** | BFS連結成分、ヘルマート連鎖 |
| レベリング | **完成** | 反復縮小法 |
| 統合エンジン | **一部未完** | 簡略3ステップは動作、完全5ステップはスタブ |
| メインウィンドウ統合タブ | **スタブ** | `TODO: Implement integration using IntegrationEngine` |
| エクスポート機能 | **スタブ** | `TODO: Implement export` |

---

## コード中の構造的注釈・設計原則

### 1. 設計思想 — spatial_fitting.py (最重要)

```
Design Philosophy:
  - Municipality boundary = ultimate container (最終的な包含境界)
  - Oaza shapes = positioning GUIDES (NOT exact targets - they have gaps)
  - Public coordinate maps = ANCHORS (DO NOT transform)
  - Chain positioning: public coords → adjacent arbitrary → next adjacent...
```

**公共座標データは絶対に変換しない** という不変条件がコード全体の根幹。

### 2. 統合アルゴリズムv2 — docs/integration_algorithm_v2.md

- 優先順位: `公共座標 > 市区町村界 > XMLトポロジ > 地番マッチ > 大字界`
- 精度: 公共座標=★★★、市区町村界=★★★、大字界=★☆☆
- 設計方針: **「自動処理は8割の精度を目指す。残り2割は人間がQGISで調整」**

### 3. X/Y座標スワップ — geometry_builder.py

日本の座標系慣習 (X=北, Y=東) と QGIS標準 (X=東, Y=北) の変換を明示的に処理。

### 4. SRIDに関する設計判断 — database_manager.py (543-564行)

```
SRID update removed due to SpatiaLite geometry column constraint
→ geometry created with SRID=0, actual CRS tracked in text field
```

SpatiaLiteのジオメトリカラム制約により、SRID=0で統一し、CRS情報はテキストフィールドで管理。

### 5. レベリングの制約 — leveling.py

```
Key constraint: "Public coordinate parcels are NEVER adjusted"
```

重複解消時も公共座標データは不可侵。

### 6. 後方互換性エイリアス — transform/__init__.py

リファクタリング時のクラス名変更に対応:
- `ContainerLoader` → `PositioningGuideLoader`
- `RubberSheetFitter` → `ChainPositioner`

### 7. TODOコメント

- kozu_main_window.py:68: `TODO: We are going to let the user set this up in a future iteration` (ツールバー設定)
- kozu_main_window.py:483: `TODO: Implement integration using IntegrationEngine`
- kozu_main_window.py:657: `TODO: Implement export`

---

## 発見された潜在的な問題

### 1. SQLインジェクションリスク — kozu_main_window.py:509

`_load_preview_data` メソッドで `oaza_name` がf-stringでSQL文に直接埋め込まれている。
パラメータバインディング (`?`) に変更すべき。

### 2. SRID不整合 — integration_engine.py

スキーマではSRID=0で管理しているのに、`_move_to_oaza_center()` と `_translate_geometries_fallback()` で `GeomFromText(?, 6676)` を使用。

### 3. 存在しない属性参照 — integration_engine.py (756行付近)

`self.oaza_boundary_path` を参照しているが、`__init__` で定義されていない。
`self.config.oaza_boundary_layer` を使うべきと思われる。

### 4. 変数スコープのバグ — leveling.py:246

```python
if 'iteration' in dir()  # 間違い
if 'iteration' in locals()  # 正しい
```

`dir()` はモジュールレベルの名前を返すため、ローカル変数の存在確認には `locals()` を使うべき。

---

## UIモード

UIは **KozuMainWindow** (スタンドアロンウィンドウ)。左パネルにタブ、右パネルにプレビューマップキャンバス。

メインウィンドウはGSIタイルオーバーレイ対応 (標準地図, 写真, 淡色地図, OpenStreetMap)。
デフォルトCRS: EPSG:6676 (JGD2011 / Japan Plane Rectangular CS VIII)。

---

## 各ファイルの行数 (参考)

| ファイル | 行数 |
|---------|------|
| transform_panel.py | 1550 |
| database_manager.py | 901 |
| import_panel.py | 888 |
| integration_engine.py | 815 |
| spatial_fitting.py | 815 |
| integration_panel.py | 723 |
| kozu_main_window.py | 669 |
| xml_joiner.py | 544 |
| matching_algorithm.py | 512 |
| xml_parser.py | 489 |
| leveling.py | 444 |
| tps_transform.py | 441 |
| importer.py | 426 |
| helmert_transform.py | 410 |
| browse_panel.py | 382 |
| spatial_join.py | 378 |
| search_index.py | 335 |
| geometry_builder.py | 338 |
| export_panel.py | 281 |
| kozu_xml_integrator.py | 238 |

---

## まとめ

コアアルゴリズム（XML解析、座標変換、チェーンポジショニング）は高い完成度で実装されている。
UIも主要なタブ（インポート、変換、検索、エクスポート）が動作可能な状態。
残タスクは主にメインウィンドウからの統合エンジン呼び出しとエクスポート機能の接続。
設計文書と構造注釈が豊富で、特に「公共座標不可侵」「大字界はガイドであり厳密な目標ではない」
「8割自動・2割手動調整」という設計思想が一貫している。
