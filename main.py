"""
BugHunter AI — Master Launcher.
Unified entrypoint for running the Web Dashboard, Playground Targets, or CLI.

Usage:
    python main.py web          # Starts BugHunter Web Dashboard (http://127.0.0.1:8000)
    python main.py playground   # Starts Sample API (8001) and Web App (8002)
    python main.py demo         # Starts both Playground and Web Dashboard concurrently
    python main.py cli          # Starts Interactive Rich Terminal CLI
"""

import sys
import subprocess
import uvicorn
import multiprocessing


def start_playground():
    from playground.runner import run_all
    run_all()


def start_web(port: int = 8000):
    print(f"\n🚀 BugHunter AI Web Kontrol Paneli başlatılıyor...")
    print(f"👉 Tarayıcınızda açın: http://127.0.0.1:{port}\n")
    uvicorn.run("web.app:app", host="127.0.0.1", port=port, log_level="info")


def start_demo():
    print("=" * 70)
    print("🎯 BugHunter AI — Uçtan Uca Canlı Demo Modu Başlatılıyor")
    print("=" * 70)
    print("1. Örnek Hedef API:      http://127.0.0.1:8001/openapi.json")
    print("2. Örnek Web Portalı:   http://127.0.0.1:8002")
    print("3. BugHunter Dashboard: http://127.0.0.1:8000")
    print("=" * 70)

    p_targets = multiprocessing.Process(target=start_playground)
    p_targets.start()

    try:
        start_web(8000)
    except KeyboardInterrupt:
        p_targets.terminate()
        print("\n🛑 Demo durduruldu.")


def start_cli():
    from cli import run_cli
    run_cli()


def show_help():
    print("""
🎯 BugHunter AI — İnsan Onaylı Otonom Test ve Hata Analiz Ajanı

Kullanım:
    python main.py web          # İnteraktif Web Dashboard'u başlatır (Port: 8000)
    python main.py playground   # Test hedeflerini başlatır (API: 8001, Web: 8002)
    python main.py demo         # Tüm sistemi (Dashboard + Hedefler) tek tıkla başlatır
    python main.py cli          # Terminal üzerinden interaktif CLI başlatır
    python -m pytest tests/     # Tüm birim ve entegrasyon testlerini çalıştırır
""")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_help()
    else:
        cmd = sys.argv[1].lower()
        if cmd == "web":
            start_web()
        elif cmd in ["playground", "targets"]:
            start_playground()
        elif cmd == "demo":
            start_demo()
        elif cmd == "cli":
            start_cli()
        else:
            show_help()
