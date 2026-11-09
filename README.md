# 과제 제출 누락 방지 도우미

한국어 과제 공지에서 제출 조건을 찾아 체크리스트로 만들고, 조건마다 근거 문구와 페이지를 확인할 수 있는 Cloud Programming 개인 프로젝트입니다. 초기 버전에는 FastAPI 실행과 상태 확인 경로가 있습니다.

## 로컬 실행

Python 3.13이 설치된 환경에서 이 저장소 폴더를 연 뒤 실행합니다.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn main:app --reload
```

브라우저에서 [서버 상태](http://127.0.0.1:8000/health)를 열어 `{"status":"ok"}` 응답을 확인합니다. [API 문서](http://127.0.0.1:8000/docs)에서 요청을 실행할 수 있습니다.

## 구현 순서

1. 과제명과 공지 텍스트를 PostgreSQL에 저장하고 상세 화면에서 다시 조회합니다. 공백 입력은 거절하고, 서버 재실행 후 기록 보존을 확인합니다.
2. 수동 체크리스트 항목과 완료 상태, 주소, 메모를 저장합니다.
3. `pypdf`로 텍스트 PDF를 페이지별로 읽습니다. 글자를 추출할 수 없는 PDF에는 텍스트 붙여넣기를 안내합니다.
4. [LangExtract](https://github.com/google/langextract)로 후보와 근거를 찾고, 원문과 일치하는 구절을 화면에서 강조합니다. 학생이 확인한 항목을 저장합니다.
