# 沒有 Mac 也能做出 Mac App：用 GitHub 的雲端 Mac 打包

流程：把這個資料夾上傳到 GitHub → 按一個按鈕 → 下載做好的 .app（壓縮檔）。免買 Mac。

## 步驟
1. 註冊 GitHub 帳號（免費）：github.com
2. 右上角「+」→ New repository，取名（例如 trpg-translator），按 Create repository。
3. 在新儲存庫頁面按「uploading an existing file」，把本資料夾裡**所有內容**拖進去（包含 .github 資料夾），按 Commit changes。
   - 若 .github 資料夾沒有被上傳：Add file → Create new file，檔名欄輸入 `.github/workflows/build-mac.yml`，把該檔內容貼進去再 Commit。
4. 上方進入 Actions 分頁 → 左側選「Build macOS app」→ Run workflow → Run workflow。
5. 等約 5 到 15 分鐘，綠色勾勾表示成功（紅色叉叉 = 失敗，點進去看記錄）。
6. 點進那次執行，往下捲到 Artifacts，下載：
   - TRPG_Translator_mac_arm64：給 Apple Silicon Mac（近幾年的 Mac 幾乎都是）
   - TRPG_Translator_mac_x86_64：給 Intel Mac
7. 解壓縮得到 TRPG Translator.app，拖進「應用程式」資料夾即可使用。

## Mac 使用者第一次開啟
- 這個 App 沒有經過 Apple 公證，雙擊可能被擋。請按右鍵 → 打開 → 再按「打開」。
- 仍被擋：系統設定 → 隱私權與安全性 → 找到被擋的 App → 仍要打開。
- 或在終端機執行：xattr -dr com.apple.quarantine "/Applications/TRPG Translator.app"
- API Key 與設定會存在 ~/Library/Application Support/TRPG_Translator/，不會包在 App 裡。

## 注意
- 系統提示詞存放在獨立的 `system_prompt.txt`，可在程式內按「檢視 / 編輯系統提示詞…」修改；打包成 App 後，這個檔案會建立在 `~/Library/Application Support/TRPG_Translator/`。
- 本版本新增的功能（排程、強制停止、深色模式、自動切換模型、各模型分開計時等）在 Mac 上都可使用；只有「縮到系統匣」不支援（Mac 請直接用 Dock 縮小視窗），該選項在 Mac 版會自動隱藏。
- 不要把 .env（內有 API Key）上傳到 GitHub；已附的 .gitignore 會擋住，但用網頁拖曳上傳時它不會生效，請自行確認沒有放進去。
- 更新程式：到儲存庫直接覆蓋 .py 檔，再重新執行一次 Run workflow。
- 若 Intel 版（macos-15-intel）因 GitHub 調整 runner 而失敗，可只保留 Apple Silicon 那份。
- 公開儲存庫使用標準 runner 通常免費；私人儲存庫有每月免費額度，macOS 的計時倍率較高，詳情請看 GitHub 計費說明。

## 版本
- 目前版本 2.7.0，視窗標題會顯示「TRPG_Translator [版號]」。要發佈新版時，可在 GitHub 建立標籤（例如 v2.6.0）觸發自動打包。
