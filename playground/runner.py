"""
Playground Runner:
Starts either the Sample API (port 8001) or Sample Web App (port 8002) or both.
"""

import sys
import uvicorn
import multiprocessing

def run_api(port: int = 8001):
    print(f"🚀 Starting Sample API target on http://127.0.0.1:{port}")
    uvicorn.run("playground.sample_api:app", host="127.0.0.1", port=port, log_level="warning")

def run_web(port: int = 8002):
    print(f"🌐 Starting Sample Web App target on http://127.0.0.1:{port}")
    uvicorn.run("playground.sample_web:app", host="127.0.0.1", port=port, log_level="warning")

def run_all():
    p1 = multiprocessing.Process(target=run_api, args=(8001,))
    p2 = multiprocessing.Process(target=run_web, args=(8002,))
    p1.start()
    p2.start()
    print("✨ Both playground targets are running.")
    print(" - Sample API: http://127.0.0.1:8001 (Docs: http://127.0.0.1:8001/docs)")
    print(" - Sample Web: http://127.0.0.1:8002")
    try:
        p1.join()
        p2.join()
    except KeyboardInterrupt:
        p1.terminate()
        p2.terminate()
        print("\n🛑 Stopped playground targets.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "api":
        run_api()
    elif len(sys.argv) > 1 and sys.argv[1] == "web":
        run_web()
    else:
        run_all()
