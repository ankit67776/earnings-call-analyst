from fastapi import FastAPI

app = FastAPI(title="Earnings Call Analyst")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
