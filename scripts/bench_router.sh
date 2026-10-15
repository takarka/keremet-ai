#!/bin/bash
# Сравнение моделей на одной задаче: реальный расход берём из /api/v1/key (usage до/после).
cd /c/work/projects/keremet-ai
export PATH="$PATH:$(cygpath "$LOCALAPPDATA")/Microsoft/WinGet/Packages/Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe/platform-tools"
TASK='Открой 2ГИС. Найди «Starbucks» и верни название и рейтинг первого заведения в выдаче. Ничего не бронируй и не звони.'
spent() { .venv/Scripts/python.exe -c "
import httpx,os
from dotenv import load_dotenv; load_dotenv('.env')
print(httpx.get('https://openrouter.ai/api/v1/key',headers={'Authorization':'Bearer '+os.environ['OPENROUTER_API_KEY']}).json()['data']['usage'])"; }
for M in anthropic/claude-haiku-5.5 typesafe/jev-router; do
  adb shell input keyevent KEYCODE_HOME; sleep 2
  B=$(spent)
  MAX_SECONDS=300 timeout 330 .venv/Scripts/python.exe -u -m agent run "$TASK" --model $M --steps 25 --max-cost 0.25 > /tmp/bench_$(basename $M).log 2>&1
  A=$(spent)
  echo "$M real_cost_usd=$(.venv/Scripts/python.exe -c "print(round($A-$B,5))")"
  tail -8 /tmp/bench_$(basename $M).log | grep -E "Статус|Итог|Шагов|Токены"
done
