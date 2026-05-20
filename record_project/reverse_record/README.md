# reverse_record

このフォルダには、3Dレコード形状から音声ファイルを再構成するツールが入っています。

## 使い方

```
python reverse_record/stl_to_audio.py path/to/record.stl --output-dir recovered_audio
```

## 特長

- 本プロジェクトが生成するレコード STL に対応
- 7", 10", 12" のサイズをサポート
- 33, 45, 78 RPM に対応
- Mono / Stereo の groove に対応
- Side A / Side B の 1 面ファイル、両面 STL に対応
- 生成された WAV はデフォルトで 44.1 kHz 16-bit 形式

## オプション

- `--output-dir`: 出力ファイルを保存するディレクトリ
- `--side A|B|auto`: 単一ファイル入力時に A 面 / B 面を指定
- `--inverse-riaa`: RIAA 再生イコライゼーションを適用して元の再生音に近づける

## 例

```
python reverse_record/stl_to_audio.py my_record.stl --output-dir recovered_audio
```

```
python reverse_record/stl_to_audio.py output_sideA.stl --output-dir recovered_audio --side A
```
