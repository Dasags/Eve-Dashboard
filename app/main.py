from fastapi import FastAPI

app = FastAPI(title="EVE-World")


@app.get("/health")
def health():
    return {"status": "ok"}
