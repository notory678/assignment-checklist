"""Manage this project's local-only Ollama server on macOS (Python 3.13+)."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import tarfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / '.local-ai'
BINARY = STATE / 'runtime' / 'ollama'
PID_FILE = STATE / 'server.pid'
BASE_URL = 'http://127.0.0.1:11434'
MODEL = 'qwen3:4b'
VERSION = 'v0.40.16'
ARCHIVE_URL = f'https://github.com/ollama/ollama/releases/download/{VERSION}/ollama-darwin.tgz'
ARCHIVE_SHA256 = 'a743717494315da7cd352982b805f6b992d4b4452ca117fd5fbf86c4ee46fca0'


def request(route, payload=None, timeout=3):
    data = None if payload is None else json.dumps(payload).encode()
    return urlopen(Request(BASE_URL + route, data=data, headers={'Content-Type': 'application/json'}), timeout=timeout)


def ready():
    try:
        with request('/api/version') as response:
            return response.status == 200
    except OSError:
        return False


def own_pid():
    try:
        pid = int(PID_FILE.read_text())
        command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'command='], text=True).strip()
        return pid if str(BINARY) + ' serve' in command else None
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


def install():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('이 설치 도우미는 Apple Silicon 맥용입니다. 다른 환경은 README를 확인해 주세요.')
    if BINARY.exists():
        return
    STATE.mkdir(exist_ok=True)
    archive = STATE / 'ollama-darwin.tgz'
    partial = STATE / 'ollama-darwin.tgz.part'
    print('공식 Ollama 실행 파일 다운로드 중…', flush=True)
    digest = hashlib.sha256()
    with urlopen(ARCHIVE_URL, timeout=30) as source, partial.open('wb') as target:
        while block := source.read(1024 * 1024):
            target.write(block)
            digest.update(block)
    if digest.hexdigest() != ARCHIVE_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError('설치 파일의 확인값이 일치하지 않습니다. 설치를 중단했습니다.')
    partial.replace(archive)
    BINARY.parent.mkdir(exist_ok=True)
    with tarfile.open(archive) as package:
        package.extractall(BINARY.parent, filter='data')
    archive.unlink()
    if not BINARY.exists():
        raise RuntimeError('다운로드한 설치 파일에서 Ollama를 찾지 못했습니다.')
    BINARY.chmod(0o755)
    print('Ollama 설치 완료. 실행 파일은 프로젝트의 .local-ai 폴더에 보관합니다.', flush=True)


def start():
    if ready():
        if not own_pid():
            raise RuntimeError('11434번에서 다른 Ollama가 실행 중입니다. 기존 서버를 종료한 뒤 다시 실행해 주세요.')
        print('프로젝트의 로컬 AI 서버가 이미 실행 중입니다.', flush=True)
        return
    if not BINARY.exists():
        raise RuntimeError('먼저 python3.13 scripts/local_ai.py setup을 실행해 주세요.')
    STATE.mkdir(exist_ok=True)
    environment = dict(os.environ, OLLAMA_HOST='127.0.0.1:11434',
                       OLLAMA_MODELS=str(STATE / 'models'), OLLAMA_NO_CLOUD='1',
                       OLLAMA_MAX_LOADED_MODELS='1', OLLAMA_NUM_PARALLEL='1')
    with (STATE / 'server.log').open('ab') as output:
        process = subprocess.Popen([str(BINARY), 'serve'], cwd=BINARY.parent, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    PID_FILE.write_text(str(process.pid))
    for _ in range(60):
        if ready():
            print('로컬 AI 서버 시작 완료. 외부 AI 서비스 호출은 비활성화되어 있습니다.', flush=True)
            return
        if process.poll() is not None:
            break
        time.sleep(.5)
    raise RuntimeError('서버가 시작되지 않았습니다. .local-ai/server.log를 확인해 주세요.')


def pull():
    with request('/api/tags') as response:
        installed = json.load(response).get('models', [])
    if any(item.get('name') == MODEL for item in installed):
        print(f'{MODEL} 모델이 이미 설치되어 있습니다.', flush=True)
        return
    print(f'{MODEL} 모델 다운로드 중 (약 2.5GB)…', flush=True)
    previous = None
    with request('/api/pull', {'model': MODEL, 'stream': True}, timeout=60) as response:
        for line in response:
            progress = json.loads(line)
            if progress.get('error'):
                raise RuntimeError(progress['error'])
            total = progress.get('total', 0)
            percent = int(progress.get('completed', 0) * 100 / total) // 10 * 10 if total else None
            current = (progress.get('status'), percent)
            if current != previous:
                print(current[0], '' if percent is None else f'{percent}%', flush=True)
                previous = current
    print('모델 준비 완료.', flush=True)


def stop():
    pid = own_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        print('프로젝트의 로컬 AI 서버를 종료했습니다.')
    else:
        print('프로젝트의 로컬 AI 서버가 실행 중이 아닙니다.')
    PID_FILE.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'start', 'stop', 'status'])
    action = parser.parse_args().action
    if action == 'setup':
        install()
        start()
        pull()
    elif action == 'start':
        start()
    elif action == 'stop':
        stop()
    else:
        print('로컬 AI 서버:', '실행 중' if ready() else '중지됨')
        if ready():
            with request('/api/tags') as response:
                print('설치된 모델:', ', '.join(item['name'] for item in json.load(response).get('models', [])))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, URLError) as error:
        raise SystemExit(str(error))
