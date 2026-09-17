from fastapi import FastAPI

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "Auction backend is live!"}