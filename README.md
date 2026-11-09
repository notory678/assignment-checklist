# 과제 제출 누락 방지 도우미

한국어 과제 공지에서 제출 조건을 찾아 체크리스트로 만드는 Cloud Programming 개인 프로젝트입니다. 현재 버전은 공지 제목과 본문을 PostgreSQL에 저장하고, 목록과 상세 내용을 조회할 수 있습니다.

## 로컬 실행

Docker Desktop을 열고 프로젝트 폴더의 터미널에서 실행합니다.

```bash
docker compose up --build -d --wait
```

맥에서 `docker` 명령을 찾을 수 없다면 터미널에서 `export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"`를 실행합니다. 8000번 포트를 사용 중이라면 `APP_PORT=8001 docker compose up --build -d --wait`로 실행하고 주소의 포트도 `8001`로 바꿉니다.

## 공지 등록과 조회

브라우저에서 [API 문서](http://127.0.0.1:8000/docs)를 열고 `POST /assignments`의 **Try it out**을 누릅니다. 아래 예시를 입력하고 **Execute**를 누르면 `201` 응답에 저장된 공지의 `id`가 표시됩니다.

```json
{
  "title": "클라우드 프로그래밍 프로젝트",
  "body": "배포 주소를 포함하세요.\n발표 자료를 준비하세요."
}
```

- `GET /assignments`는 최근 공지부터 제목과 ID, 등록 시각을 보여줍니다.
- `GET /assignments/{assignment_id}`에 저장된 ID를 넣으면 본문까지 조회합니다. 없는 ID는 `404`를 반환합니다.
- 공백뿐인 제목이나 본문은 `422`로 거절합니다. 정상 본문의 줄바꿈은 그대로 저장합니다.

`docker compose down`으로 내렸다가 `docker compose up -d --wait`로 다시 올린 뒤 같은 ID를 조회할 수 있습니다. 공지는 PostgreSQL 볼륨에 보관됩니다.

## 구현 순서

1. 수동 체크리스트 항목과 완료 상태, 주소, 메모를 저장합니다.
2. `pypdf`로 텍스트 PDF를 페이지별로 읽습니다. 글자를 추출할 수 없는 PDF에는 텍스트 붙여넣기를 안내합니다.
3. [LangExtract](https://github.com/google/langextract)로 후보와 근거를 찾고, 원문과 일치하는 구절을 화면에서 강조합니다. 학생이 확인한 항목을 저장합니다.
