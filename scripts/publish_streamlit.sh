#!/usr/bin/env bash
# 將目前的程式碼 + 「加密後」的資料庫發布到 streamlit 分支，供 Streamlit Community Cloud 部署。
# repo 為公開，明文資料庫絕不進入 git；以單一 commit 強制覆寫，加密檔也不會在歷史中累積。
set -euo pipefail

DB=data/usequity.db
ENC=data/usequity.db.enc

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
# orphan 分支沿用目前 HEAD 的檔案樹（.gitignore 已排除 *.db，明文不會被加入）
git checkout -q --orphan "streamlit-publish-$$"

if [ ! -f "$DB" ]; then
  echo "::warning::找不到 $DB，只發布程式碼（先執行一次 FMP 選股回測 workflow）"
elif [ -z "${DB_KEY:-}" ]; then
  echo "::warning::未設定 DB_KEY secret，只發布程式碼、不含資料。請到 Settings → Secrets → Actions 新增 DB_KEY"
else
  python -m usequity.crypto encrypt "$DB" "$ENC"
  git add -f "$ENC"
fi

# 保險：任何明文 .db 都不得進入 commit
if git diff --cached --name-only | grep -E '\.db$'; then
  echo "::error::偵測到明文資料庫被加入 commit，中止發布"
  exit 1
fi

git commit -q -m "streamlit snapshot $(TZ=Asia/Taipei date '+%F %H:%M') (source ${GITHUB_SHA:-local})"
git push -q -f origin HEAD:streamlit
echo "已發布到 streamlit 分支$( [ -f "$ENC" ] && echo "（加密資料庫 $(du -h "$ENC" | cut -f1)）")"
