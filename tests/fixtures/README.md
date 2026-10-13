# 測試用 OneNote 檔案

這些 `.one` 檔案取自 [Apache Tika](https://github.com/apache/tika) 專案的測試資料
（`tika-parsers/.../tika-parser-microsoft-module/src/test/resources/test-documents/`），
依 [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0) 授權再散布，未經修改。

| 檔案 | 用途 |
| --- | --- |
| `testOneNote.one` | 一般頁面與內嵌圖片 |
| `testOneNote1.one` | 兩頁、表格、圖片、編號清單、超連結 |
| `testOneNote3.one` | 粗體文字、多個大綱 |
| `testOneNote2016.one` | OneNote 2016 建立的最小節 |
| `testOneNoteEmbeddedWordDoc.one` | 內嵌 Word 附件 |
| `test-tika-4303-Chinese-notes.one` | 中文內容與項目符號清單 |
| `testOneNoteFromOffice365.one` | FSSHTTPB 封裝格式（應回報不支援） |
| `testOneNote-fuzz1.one` | 損毀的 `.onetoc2`（應安全失敗） |

**請勿**把個人的 OneNote 備份放進這個資料夾或 repo；
針對本機備份的測試位於 `tests/local/`，會直接讀取 Mac 上的備份資料夾。
