.PHONY: help up down logs restart test smoke clean ps pull
.DEFAULT_GOAL := help

help:          ## 사용 가능한 명령 보기
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up:            ## 전체 스택 기동 (첫 실행은 모델 다운로드로 시간이 걸린다)
	docker compose up -d --build
	@echo "대시보드: http://localhost:$${DASHBOARD_PORT:-3000}"

down:          ## 전체 종료
	docker compose down

restart:
	docker compose restart mcp-client mcp-server api-server

logs:
	docker compose logs -f mcp-client mcp-server api-server

ps:
	docker compose ps

pull:          ## LLM 모델만 다시 받기
	docker compose run --rm ollama-init

test:          ## 단위 테스트 (도커 없이 로컬 파이썬으로 실행)
	@bash scripts/test.sh

smoke:         ## 기동된 스택에 실제 대화를 넣어 보는 통합 확인
	@bash scripts/smoke.sh

clean:         ## 컨테이너와 모델 볼륨까지 삭제
	docker compose down -v
