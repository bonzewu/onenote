# onenote

把 Microsoft OneNote 桌面版（Mac／Windows）的 `.one` 節檔案匯出成 **Markdown** 或 **JSON**。

- 不需要安裝或開啟 OneNote，也不需要登入 Microsoft 帳號
- 零執行期相依套件，純 Python 標準函式庫，依 [MS-ONESTORE] 與 [MS-ONE] 規格重新實作解析器
- 預設會自動找到 Mac 版 OneNote 的備份資料夾，一行指令就能把所有筆記本匯出

## 功能

| 項目 | 支援情形 |
| --- | --- |
| 頁面標題、建立／修改時間、作者 | ✅ 寫入 YAML front matter |
| 段落、粗體、斜體、底線、刪除線、上下標 | ✅ |
| 超連結（含 OneNote 的 `HYPERLINK` 欄位碼） | ✅ |
| 標題樣式（標題 1–6）、引言、程式碼、引用 | ✅ |
| 項目符號清單、編號清單、多層縮排 | ✅ |
| 待辦事項核取方塊 | ✅ 轉為 `- [ ]`／`- [x]` |
| 表格（含表格中的清單與圖片） | ✅ 轉為 GFM 表格 |
| 內嵌圖片、附件檔 | ✅ 檔案中有資料時輸出到 `assets/` |
| 子頁面層級 | ✅ 反映在索引頁的縮排與 front matter 的 `level` |
| 多個備份版本、回收筒 | ✅ 預設取最新版本、略過回收筒，可用參數改變 |
| 手寫筆跡（Ink） | ⚠️ 以文字提示標示位置 |
| 密碼保護（加密）的節 | ❌ 回報錯誤並略過 |
| OneDrive／Office 365 下載的 FSSHTTPB 格式 `.one` | ❌ 回報「不支援」並略過 |

### 關於 Mac 版備份中的圖片

Mac 版 OneNote 的自動備份**只保留圖片的參照，不包含圖片本體**：
檔案中的參照是 `<invfdo>`，代表無效的檔案資料物件，真正的圖片存放在 OneDrive 雲端。
遇到這種情況，匯出的 Markdown 會在原位置留下 `*［圖片未包含在備份檔中：檔名］*` 提示，
匯出結束時的摘要也會統計數量。如果需要完整的圖片，請從 OneNote 或 OneDrive 網頁版另外匯出。

## 安裝

需要 Python 3.12 以上，建議用 [uv](https://docs.astral.sh/uv/)。

```bash
# 不安裝，直接執行（推薦）
uvx --from git+https://github.com/bonzewu/onenote onenote-export --help

# 或安裝成全域指令
uv tool install git+https://github.com/bonzewu/onenote
onenote-export --help
```

## 使用方式

```bash
# 自動找到 Mac 版 OneNote 的備份資料夾，匯出到 ./onenote-export
onenote-export

# 先看看有哪些節與頁面，不寫入任何檔案
onenote-export --list

# 指定來源與輸出資料夾
onenote-export ~/Desktop/我的筆記本 -o ~/Documents/OneNote匯出

# 只匯出單一節
onenote-export "備忘.one (2026-9-27).one" -o 備忘

# 匯出每一個備份版本，連回收筒一起匯出
onenote-export --all-versions --include-recycle-bin -o 完整備份

# 輸出 JSON（保留完整結構，適合再用程式處理）
onenote-export --format json -o json匯出
```

Mac 版的備份資料夾位置如下（資料夾名稱會隨介面語言而不同，例如「備份」或「Backup」，程式會自動偵測）：

```
~/Library/Containers/com.microsoft.onenote.mac/Data/Library/Application Support/Microsoft User Data/OneNote/15.0/備份/
```

### 參數

| 參數 | 說明 |
| --- | --- |
| `inputs` | 一或多個 `.one` 檔案或資料夾（會遞迴尋找）；省略時自動尋找 Mac 版備份資料夾 |
| `-o, --output` | 輸出資料夾，預設 `./onenote-export` |
| `-f, --format` | `markdown`（預設）或 `json` |
| `--all-versions` | 匯出同一節的所有備份版本，預設只匯出日期最新的一份 |
| `--include-recycle-bin` | 一併匯出 `OneNote_RecycleBin` |
| `--list` | 只列出節與頁面標題 |
| `--force` | 輸出資料夾已有內容時仍然寫入（不會刪除既有檔案，同名檔案會被覆寫） |
| `-v` / `-q` | 顯示除錯訊息／只顯示錯誤 |

結束代碼：`0` 全部成功、`1` 部分節匯出失敗、`2` 參數錯誤或找不到輸入。

### 輸出結構

```
onenote-export/
├── index.md                     # 所有筆記本與節的總索引
└── 我的筆記本/
    └── 備忘/
        ├── index.md             # 這個節的頁面清單（子頁面會縮排）
        ├── 001 第一頁.md
        ├── 002 第二頁.md
        └── assets/
            └── 001 第一頁/
                └── 截圖.png
```

頁面檔名前的編號就是 OneNote 中的頁面順序。每個頁面的開頭是 YAML front matter：

```markdown
---
title: "第一頁"
notebook: "我的筆記本"
section: "備忘"
created: "2026-09-27T10:15:00+08:00"
modified: "2026-09-27T11:02:13+08:00"
author: "bonzewu"
---

# 第一頁

內容……
```

匯出結果可以直接放進 Obsidian、VS Code、Typora 或任何支援 GitHub Flavored Markdown 的工具。

## 運作原理

```
.one 檔案
  │  onestore.py   MS-ONESTORE 儲存層：檔頭 → 交易紀錄 → 檔案節點清單 → 物件空間 → 修訂版
  ▼
物件圖（ExtendedGUID → 物件宣告）
  │  properties.py 解碼每個物件的屬性集（含物件參照串流）
  │  document.py   MS-ONE 語意層：節 → 頁面 → 大綱 → 段落／表格／圖片
  ▼
model.py（與格式無關的純資料）
  │  markdown.py   轉成 GFM；exporter.py 寫檔、建立索引
  ▼
Markdown／JSON
```

實作時特別處理的細節：

- **交易紀錄**：檔案節點清單中可能殘留未提交的節點，必須以交易紀錄中的數量為準。
  一筆交易可能跨越兩個片段，片段結尾也可能有填充位元組。
- **修訂版**：每個物件空間有多個修訂版，要依角色（role）找到目前生效的版本，
  再沿著 `ridDependent` 鏈合併相依修訂版中的物件。
- **全域 ID 表**：物件參照以 CompactID 壓縮儲存，必須用宣告當下生效的 ID 表還原。
- **超連結**：OneNote 把連結存成 `﷟HYPERLINK "網址"` 欄位碼加上顯示文字，
  欄位碼可能被切成多個文字段。

## 開發

```bash
git clone https://github.com/bonzewu/onenote.git
cd onenote
uv sync                      # 建立 .venv 並安裝開發工具

uv run pytest                # 執行全部測試
uv run pytest --cov          # 含覆蓋率報告
uv run ruff check src tests  # 靜態檢查
uv run ruff format src tests # 自動格式化
```

### 測試

| 類型 | 位置 | 內容 |
| --- | --- | --- |
| 單元測試 | `tests/unit/` | 二進位讀取、交易紀錄、檔案節點清單、屬性集、文字段與超連結、Markdown 轉換、檔名處理 |
| 功能測試 | `tests/functional/` | 解析 Apache Tika 的真實 `.one` 檔，比對 Tika 測試中的預期文字、圖片、附件與表格 |
| 端對端測試 | `tests/e2e/` | 以子行程執行 CLI，再用 markdown-it 把輸出解析成 HTML，檢查標題、表格、連結，並確認每個圖片與索引連結都指向實際存在的檔案 |
| 本機測試 | `tests/local/` | 匯出本機所有 OneNote 備份並確認沒有失敗；沒有備份資料夾時自動略過，也不會輸出任何筆記內容 |

測試用的 `.one` 檔案來自 Apache Tika（Apache License 2.0），詳見 [`tests/fixtures/README.md`](tests/fixtures/README.md)。
**請勿把個人筆記放進 repo**：`.gitignore` 已排除匯出資料夾與 `tests/fixtures/` 以外的 `.one` 檔。

## 參考資料

- [MS-ONESTORE]：OneNote Revision Store File Format
- [MS-ONE]：OneNote File Format
- [Apache Tika](https://github.com/apache/tika) 的 OneNote 解析器與測試資料
- [pyOneNote](https://github.com/DissectMalware/pyOneNote)：早期的 Python 解析器，本專案參考了它的屬性 ID 對照表

[MS-ONESTORE]: https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-onestore/
[MS-ONE]: https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-one/

## 授權

[MIT](LICENSE)
