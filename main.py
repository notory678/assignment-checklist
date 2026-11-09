from fastapi import FastAPI

app = FastAPI(title="과제 제출 누락 방지 도우미")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
