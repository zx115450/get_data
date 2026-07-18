"""支持 `python -m server` 直接启动后端。"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("server.app:app", host="127.0.0.1", port=8000, log_level="warning", access_log=False)
