# 트러블슈팅 기록

로컬 LLM을 컨테이너에서 돌리면서 겪은 문제와 해결 과정을 남긴다.
같은 증상을 만났을 때 참고할 수 있도록 **오진했던 경로까지 함께** 적었다.

테스트 환경: Windows 11 / RTX 2070 (8GB VRAM) / Docker Desktop / `qwen2.5:3b`

---

## 1. GPU를 쓰는데도 응답 생성이 90초 걸린 문제

### 증상

일정 등록은 정상인데 `reply` 단계만 극단적으로 느렸다.

```json
{ "stage": "tool",  "status": "ok",   "duration_ms": 266.6 }
{ "stage": "reply", "status": "warn", "duration_ms": 90021.3,
  "detail": "LLM 미응답 · 템플릿 사용" }
```

### 첫 번째 단서 — 이건 "느린 것"이 아니다

`90021.3ms`는 설정한 `LLM_TIMEOUT=90`에 정확히 붙는 값이다.
그 전에는 `45017.4ms`였고, 당시 타임아웃은 45초였다.

**타임아웃 값에 딱 맞는 숫자가 나오면 느린 게 아니라 잘린 것이다.**
성능 문제와 실패 문제는 접근이 다르므로 여기서 방향을 잡았다.

### 오진 1 — GPU를 못 잡은 줄 알았다

CPU로 폴백됐다고 의심했지만 아니었다.

```
$ docker compose exec ollama ollama ps
NAME          SIZE      PROCESSOR    CONTEXT    UNTIL
qwen2.5:3b    2.2 GB    100% GPU     4096       4 minutes from now
```

`100% GPU`로 멀쩡히 표시됐다. **이 표시를 믿은 것이 원인 파악을 늦췄다.**

### 오진 2 — 컨테이너 네트워크를 의심했다

호스트에서 Ollama를 직접 호출해 봤는데 42ms가 나왔다.
"네트워크 문제구나" 싶었지만, 이것도 잘못된 판단이었다.

**50토큰 생성이 42ms에 끝날 리가 없다.** 요청이 실패해 즉시 에러가 돌아온 것이었다.
원인은 PowerShell의 `Set-Content -Encoding utf8`이 BOM을 붙여 JSON 파싱이 깨진 것.
`Measure-Command`가 출력을 삼켜서 에러가 보이지 않았다.

> 측정할 때는 시간과 함께 **응답 본문과 상태 코드를 반드시 같이 확인**해야 한다.

### 계측 — 로드 시간과 생성 시간을 분리

컨테이너 안에서 같은 요청을 연속 두 번 보냈다.

```bash
docker compose exec mcp-client python -c "
import time,httpx
t=time.time()
r=httpx.post('http://ollama:11434/api/chat', json={
    'model':'qwen2.5:3b',
    'messages':[{'role':'user','content':'안녕하세요'}],
    'stream':False,'keep_alive':'30m','options':{'num_predict':50}
}, timeout=120)
print(r.status_code, round(time.time()-t,1),'초')"
```

| 회차 | 결과 |
|---|---|
| 1회차 | `200` **10.0초** |
| 2회차 | `200` **0.3초** |

10초는 모델 로드, 0.3초가 실제 생성 속도였다.
50토큰에 0.3초면 응답 상한인 220토큰도 2초 안쪽이어야 한다.
**즉 90초가 나올 구조가 아니었다.**

### 원인

`OLLAMA_NUM_PARALLEL`의 기본값이 병렬 처리 슬롯을 여러 개 할당한다.
슬롯마다 컨텍스트 4096짜리 KV 캐시가 따로 붙기 때문에 8GB VRAM을 초과하고,
초과분이 CPU로 밀리면서 생성 속도가 무너진다.

문제는 **이 상태에서도 `ollama ps`는 `100% GPU`로 표시된다**는 점이다.
모델 가중치가 GPU에 올라간 것과 실행 전체가 GPU에서 도는 것은 다르다.

### 해결

`docker-compose.yml`의 `ollama` 서비스에 추가했다.

```yaml
    gpus: all
    environment:
      OLLAMA_KEEP_ALIVE: "${LLM_KEEP_ALIVE:-30m}"
      OLLAMA_NUM_PARALLEL: "1"
      OLLAMA_MAX_LOADED_MODELS: "1"
```

여기에 콜드 로드 10초를 없애기 위한 조치를 더했다.

- 요청마다 `keep_alive`를 실어 모델이 메모리에 머물게 함
- 기동 시 `warmup()`으로 모델을 미리 올림

### 결과

```json
{ "stage": "reply", "status": "ok", "duration_ms": 739.6, "detail": "LLM 생성" }
```

**90,021ms → 739ms.**

### 남은 한계

여러 설정을 한 번에 바꾼 뒤 측정했기 때문에,
`OLLAMA_NUM_PARALLEL=1`이 단독 원인이라는 것을 **단일 변수 실험으로 검증하지는 못했다.**
정황(VRAM 용량, 슬롯당 KV 캐시 크기, 계측된 생성 속도)상 가장 유력한 설명이지만,
엄밀하게는 추정이 섞여 있다.

---

## 2. nginx가 죽은 IP를 계속 붙들고 있던 문제

### 증상

`docker compose up -d` 직후 대시보드의 health가 계속 에러였다.
`down` 후 `up`을 하면 정상으로 돌아왔다.

```
schedule-dashboard    Up 12 minutes (unhealthy)
schedule-mcp-client   Up 4 minutes (healthy)     ← 재생성 시각이 다르다
```

### 원인

nginx는 `proxy_pass`에 호스트명을 직접 쓰면 **설정 로드 시점에 IP를 해석해 영구 캐싱**한다.
`docker compose up -d`로 `mcp-client`만 재생성되면 컨테이너 IP가 바뀌는데,
nginx는 계속 옛 주소로 요청을 보낸다.

`down` 후 `up`하면 nginx도 새로 뜨기 때문에 "재시작하면 되는 문제"로 보였고,
그래서 원인 파악이 늦어졌다.

### 해결

변수를 쓰면 nginx가 요청 시점마다 DNS를 다시 본다.

```nginx
    location /api/ {
        resolver 127.0.0.11 valid=10s ipv6=off;   # Docker 내장 DNS
        set $agent "mcp-client:8000";
        rewrite ^/api/(.*)$ /$1 break;
        proxy_pass http://$agent;
        ...
    }
```

헬스체크도 함께 고쳤다. nginx는 `listen 80`으로 IPv4만 수신하는데
busybox `wget`이 `localhost`를 `::1`로 먼저 해석하면 연결이 거부된다.

```dockerfile
CMD wget -qO- http://127.0.0.1/ > /dev/null || exit 1
```

`mcp-server`에도 헬스체크를 붙이고 `mcp-client`가 `service_healthy`를 기다리게 했다.
컨테이너가 뜬 것과 서비스가 요청을 받을 준비가 된 것은 다르다.

---

## 3. keep_alive 타입 오류 — 장애가 조용히 감춰진 사례

### 증상

Ollama 로그에 400이 찍히는데 대시보드는 멀쩡히 동작했다.

```
schedule-ollama | [GIN] | 400 | 320.326µs | POST "/api/chat"
```

trace에는 `"LLM 미응답 · 템플릿 사용"`으로만 표시됐다.

### 원인

Ollama의 `keep_alive`는 `"30m"` 같은 duration이면 문자열,
무기한을 뜻하는 `-1`이면 **숫자**여야 한다.
환경변수를 그대로 넘기면서 문자열 `"-1"`이 전달돼 파싱에 실패했다.

```python
os.getenv("LLM_KEEP_ALIVE", "30m")   # → '-1' (str)  ✗
```

### 해결

```python
def keep_alive_value() -> Union[str, int]:
    raw = os.getenv("LLM_KEEP_ALIVE", "30m").strip()
    try:
        return int(raw)
    except ValueError:
        return raw
```

### 설계상의 교훈

이 프로젝트는 "LLM이 죽어도 템플릿으로 계속 동작한다"를 의도적으로 설계했다.
가용성 측면에서는 정확히 의도대로 동작했지만, **디버깅 관점에서는 장애를 감췄다.**

400이 계속 나는 상황에서도 시스템은 멀쩡해 보였고,
화면에는 `warn` 하나만 떴다. 그래서 원인을 찾는 데 오래 걸렸다.

폴백은 남기되 실패 사유를 기록하도록 고쳤다.

```python
        if res.status_code != 200:
            detail = res.text[:200].replace("\n", " ")
            self.last_error = f"HTTP {res.status_code}: {detail}"
            logger.warning("LLM 오류 응답 %s: %s", res.status_code, detail)
            return None
```

> **조용한 폴백은 가용성을 지키지만 관측 가능성을 해친다.**
> 폴백하더라도 왜 폴백했는지는 남겨야 한다.

---

## 4. 코드를 고쳤는데 반영되지 않는 문제

### 증상

`llm.py`를 수정하고 `docker compose up -d`를 했는데 동작이 그대로였다.
`.env`의 `LLM_TIMEOUT`은 45→90으로 반영됐는데 코드 변경만 빠진,
어중간한 상태가 만들어져 혼란스러웠다.

### 원인

Dockerfile이 `COPY app ./app`로 코드를 이미지에 굽는다.
환경변수는 컨테이너 실행 시 주입되므로 재빌드 없이 반영되지만,
**코드는 이미지를 다시 만들어야 한다.**

### 해결

```bash
docker compose up -d --build
```

반영 여부는 컨테이너 안에서 직접 확인하는 것이 확실하다.

```bash
docker compose exec mcp-client grep -c "keep_alive_value" app/llm.py
```

---

## 5. 오해하기 쉬운 정상 동작 두 가지

### `schedule-ollama-init`이 Exited(0)이다

정상이다. 모델만 내려받고 끝나는 일회성 컨테이너이며 `restart: "no"`로 지정돼 있다.
`success` / `모델 준비 완료` 로그 뒤에 종료되는 것이 설계대로의 동작이다.

확인해야 할 것은 `schedule-ollama` 쪽이 `Up (healthy)`인지다.

### `ollama ps` 결과가 비어 있다

에러가 아니라 **모델이 아직 메모리에 올라가지 않았다**는 뜻이다.
채팅을 한 번 보내면 로드되면서 목록에 나타난다.

기동 직후 비어 있다면 warmup이 아직 성공하지 못한 것이다.
`mcp-client`와 `ollama`가 동시에 재생성될 때 warmup이 너무 이른 시점에
요청을 보내면서 어긋날 수 있다.

```bash
docker compose logs mcp-client | grep -i 예열
```

warmup은 첫 요청의 지연을 없애는 **편의 기능이지 정상 동작의 조건이 아니다.**
실패해도 첫 메시지가 10초 걸릴 뿐 이후 요청은 정상이다.

---

## 빠른 점검 순서

LLM 응답이 느릴 때 이 순서로 좁히면 된다.

```bash
# 1. 컨테이너 상태 — 전부 healthy인가
docker compose ps -a

# 2. 모델이 올라와 있는가, PROCESSOR가 GPU인가
docker compose exec ollama ollama ps

# 3. 요청이 실제로 도착하는가, 상태 코드는 무엇인가
docker compose logs ollama --tail 20

# 4. 로드 시간과 생성 시간 분리 (연속 2회 실행)
docker compose exec mcp-client python -c "
import time,httpx
t=time.time()
r=httpx.post('http://ollama:11434/api/chat', json={
    'model':'qwen2.5:3b','messages':[{'role':'user','content':'안녕하세요'}],
    'stream':False,'keep_alive':'30m','options':{'num_predict':50}}, timeout=120)
print(r.status_code, round(time.time()-t,1),'초')"

# 5. LLM의 최근 실패 사유
curl http://localhost:8003/health
```

---

## 정리하며

이번 디버깅에서 얻은 것을 세 줄로 요약하면 이렇다.

**타임아웃 값에 정확히 붙는 숫자는 성능 지표가 아니라 실패 신호다.**
`45017ms`, `90021ms` 같은 값을 보면 "느리다"가 아니라 "잘렸다"로 읽어야 한다.

**도구의 요약 지표를 그대로 믿으면 안 된다.**
`ollama ps`의 `100% GPU`는 모델 가중치의 위치를 말할 뿐,
실행 전체가 GPU에서 돈다는 보장이 아니다.

**폴백은 관측 가능성을 해칠 수 있다.**
장애를 흡수하는 설계일수록 실패 사유를 명시적으로 남겨야 한다.