# 낙화놀이 예매 감시

네이버 지도 [백제문화단지 예매 목록](https://map.naver.com/p/entry/place/12019039?placePath=%2Fticket)을 2분마다 확인합니다.
이름에 "낙화놀이"가 들어간 상품이 생기면 상품에 들어가 2026-11-07을 고르고, 수량 1매를 선택한 뒤 "다음"까지 누릅니다.
11/7을 고를 수 있으면 그 순간 11/7이 미리 선택된 예매 링크를 ntfy로 폰에 먼저 보냅니다.

- 실행: GitHub Actions(`.github/workflows/watch.yml`)에서 실행 하나가 약 5시간 40분 동안 2분마다 검사하고, 시작할 때 다음 실행을 예약해 둡니다.
  GitHub 예약 실행(cron)은 몇 시간씩 밀려서 매시 예비용으로만 씁니다. 컴퓨터가 꺼져 있어도 동작합니다.
- 알림: 상품이 처음 보일 때, 목록에서 사라졌다가 다시 보일 때, 11/7이 매진·판매 전에서 예매 가능으로 바뀔 때 보냅니다.
  목록을 3번 연속 못 읽으면 오류 알림도 보냅니다.
- 결제: 네이버페이 비밀번호가 필요하므로 알림을 눌러 폰에서 직접 마무리합니다.
- 11/7이 지나면 워크플로가 스스로 꺼집니다. 그 전에 멈추려면 GitHub 저장소의 Actions 탭에서 워크플로를 끄면 됩니다.

## 수동 테스트

GitHub Actions 탭 → `watch` → Run workflow에서 키워드와 날짜를 넣으면 다른 상품으로 시험할 수 있습니다(예: `주중 입장권`, `2026-11-06`).

로컬에서 로그인한 상태로 최종 버튼까지 시험하려면 다음을 실행합니다.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python watcher.py login        # 열린 크롬 창에서 네이버 로그인 → .naver_auth.json 저장
NTFY_TOPIC=<주제> .venv/bin/python watcher.py check --keyword "주중 입장권" --date 2026-11-06 \
  --auth .naver_auth.json --submit --headed --no-state
```

`--submit`은 주문서의 "동의하고 결제하기"까지 누르고, 네이버페이 결제창이 뜨면 거기서 멈춥니다.
