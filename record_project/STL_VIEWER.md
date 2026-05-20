# STL ビューア機能

既存の `record_gui.html` （レコード生成）に加えて、新しい **STL ビューア** 機能が追加されました。

## アクセス方法（更新）

STL ビューア機能は `record_gui.html` に統合されました。

- **レコード生成 + STL 検証/再生**: http://127.0.0.1:8765/

## STL ビューアの機能

### 📁 STL アップロード
- ファイルをドラッグ&ドロップ、または選択ボタンからアップロード
- 生成済みの STL ファイルに対応
  - 7/10/12 inch
  - 33/45/78 RPM
  - 片面/両面
  - Stereo/Mono

### 📊 レコード情報表示
自動的に検出される項目：
- **Size**: レコードサイズ（インチ）
- **RPM**: 回転数
- **Quality**: 溝品質 (preview/draft/high/full/max)
- **Groove**: ステレオ / モノラル
- **Spacing**: 溝間隔係数
- **Duration**: 音声時間

### 🎵 オーディオ再生
- UploadしたSTL から自動抽出した WAV を再生
- 標準的な HTML audio player で制御
- ブラウザのデフォルト再生機能を利用

### 💾 ダウンロード
- 復元された WAV ファイルをダウンロード
- 複数面の場合は `_sideA`, `_sideB` で自動区別
- 「全部ダウンロード」ボタンで一括取得

### 📈 波形表示
- 復元された音声の波形プレビュー表示
- リアルタイム描画

### ⚙️ オプション
- **Apply inverse RIAA**: RIAA 再生イコライゼーションを適用
  - レコード製造時の RIAA pre-emphasis を逆適用
  - 元の音声に近い周波数特性で復元

## 動作フロー

1. STL ファイルをアップロード
2. バックエンド (`gui_server.py` の `/api/reverse_record` エンドポイント) が処理：
   - STL ファイルをパース
   - レコード形状から音声を復元（`reverse_record/stl_to_audio.py`）
   - WAV ファイルを `/outputs/` フォルダに保存
3. レコード情報、再生可能な WAV URL、ダウンロードリストを返す
4. ブラウザで表示・再生

## 技術詳細

### フロントエンド（更新）
- `record_gui.html` に STL アップロード・再生・波形表示の UI が統合されています。
- Canvas を使った波形描画と再生コントロールは `record_gui.html` 内で動作します。

### バックエンド
- `gui_server.py`: `/api/reverse_record` エンドポイント追加
- `reverse_record/stl_to_audio.py`: STL → 音声変換ロジック
  - GroovePoint 復元（座標逆算）
  - IEC 45/45 デコード（Lateral/Vertical → Left/Right）
  - RIAA 逆フィルター（オプション）

## 使用例

### 基本的な使用
```
1. サーバーを起動
2. http://127.0.0.1:8765/viewer にアクセス
3. 生成済み STL ファイルを選択/ドラッグ
4. 自動的に WAV に変換・再生
5. 必要に応じてダウンロード
```

### RIAA イコライゼーション付き
```
1. "Apply inverse RIAA" チェックボックスを有効化
2. STL ファイルを選択
3. RIAA 逆フィルターを通した WAV が生成される
```

## 対応フォーマット

- **入力**: Binary/ASCII STL（既存プロジェクトが生成したもの）
- **出力**: PCM 16bit Stereo/Mono, 44.1 kHz WAV

## トラブルシューティング

### "Unable to extract groove geometry from STL"
- STL ファイルが破損しているか、このプロジェクトで生成されたものではない可能性があります

### 再生が出来ない
- ブラウザの audio player が対応していない可能性
- WAV ファイルが正常に生成されているか確認（ダウンロードして確認）

### 波形が表示されない
- JavaScript の Canvas サポートが必要です

