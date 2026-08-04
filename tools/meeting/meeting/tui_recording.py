"""録音プロセスの起動・停止（TUI から切り出した非 UI ロジック）。

Textual に依存しないため、Pilot を起動せずに単体テストできる。UI への出力は `emit` コールバックへ
渡し、状態更新の通知は `on_started` で返す（どちらもワーカースレッドから呼ばれる前提で、
メインスレッドへのマーシャリングは呼び出し側＝TUI の責務）。

録音は `subprocess.Popen` で非ブロッキング起動し、標準出力をパイプで受けて 1 行ずつ emit へ流す
（`subprocess.run` で標準出力を継承すると Textual が専有する画面と衝突して表示が壊れるため、
必ずパイプで受ける）。停止は Ctrl+C ではなく既存の stop-file ポーリング機構（recorder 側で実装済み）
を使う。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

from meeting import runner
from meeting.config import MeetingConfig
from meeting.runner import Runner as SubprocessRunner

# subprocess.Popen 互換の seam（テストで差し替え。既定は実 subprocess.Popen）。
PopenFactory = Callable[..., "subprocess.Popen[str]"]
# ユーザー向け出力の seam（TUI はログウィジェットへの書込に差し替える）。
Emit = Callable[[str], None]

# recorder が起動直後に出す機械可読行（`sessionId=yyyyMMdd-HHmmss`）。オーケストレータが
# セッションを把握するための Unit A 側の契約（03-unit-a-録音.md / Q6=A・BR-IO-01）。
# 書式まで照合するのは、ID をそのまま一覧の行・選択対象として扱うため（他の行を巻き込まない）。
_SESSION_ID_LINE = re.compile(r"^sessionId=(\d{8}-\d{6})$")

# 録音の graceful 停止（stop-file 検知→WAV 確定）を待つ上限秒。
# recorder は stop-file を 250ms 間隔でポーリングし、ローカル完結のため確定は速い。
STOP_TIMEOUT_SEC = 15.0
# 無応答で kill したあとプロセスを回収するまでの上限秒。
_KILL_WAIT_SEC = 5.0


def parse_session_id(line: str) -> str | None:
    """recorder の stdout 1 行からセッション ID を取り出す（純粋。該当しなければ None）。"""
    match = _SESSION_ID_LINE.match(line.strip())
    return match.group(1) if match else None


class RecordingController:
    """録音プロセス（同時に 1 つ）の生存と停止を持つ。"""

    def __init__(self, cfg: MeetingConfig, *, run: SubprocessRunner, popen: PopenFactory) -> None:
        self._cfg = cfg
        self._run = run
        self._popen = popen
        self._process: "subprocess.Popen[str] | None" = None
        self._session_id: str | None = None

    def is_recording(self) -> bool:
        """録音プロセスが生きているか。"""
        return self._process is not None and self._process.poll() is None

    @property
    def session_id(self) -> str | None:
        """録音中セッションの ID（stdout の `sessionId=` 由来。録音していなければ None）。

        プロセスの終了とともに捨てる。残すと、録音が終わった後も一覧が「録音中」を出し続ける。
        """
        return self._session_id if self.is_recording() else None

    def request_stop(self) -> Path:
        """停止シグナル（stop-file）を作り、そのパスを返す（recorder がポーリングして止まる）。"""
        self._cfg.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self._cfg.stop_file.touch()
        return self._cfg.stop_file

    def run_until_exit(
        self,
        minutes: int,
        *,
        emit: Emit,
        on_started: Callable[[], None],
        on_session_id: Callable[[str], None] = lambda _session_id: None,
    ) -> int | None:
        """（ワーカースレッドで実行）必要ならビルド → Popen → 標準出力を 1 行ずつ emit へ流す。

        プロセス終了までブロックするが、ワーカースレッド内なので UI はフリーズしない。
        戻り値は recorder の終了コード（0=正常, 1=録音中の致命的異常/部分保存, 2=起動時異常）。
        起動できなかった場合（ビルド失敗・exe 未解決。理由は emit 済み）は None を返す。

        `on_session_id` は `sessionId=` 行を読んだ時点で 1 回だけ呼ぶ。Popen 直後（`on_started`）
        では ID がまだ決まっていないため、一覧の更新と選択の移動はこちらを起点にする。
        emit と同じくワーカースレッドから呼ばれる（メインスレッドへの受け渡しは呼び出し側の責務）。
        """
        if runner.needs_recorder_build(self._cfg):
            emit("録音 exe が未ビルド/古いためビルドします（dotnet build）...")
            build = self._run(["dotnet", "build", "src/recorder", "-c", "Release"], cwd=str(self._cfg.repo_root))
            if build.returncode != 0:
                emit("ビルドに失敗しました。")
                return None

        exe = runner.resolve_recorder_exe(self._cfg)
        if exe is None:
            emit("録音 exe を解決できませんでした（dist/recorder も src/recorder/bin も見つかりません）。")
            return None

        cmd = runner.build_recorder_command(self._cfg, exe, minutes)
        emit(f"録音開始（最大 {minutes} 分 / 「停止」ボタンで停止）...")
        process = self._popen(
            cmd,
            cwd=str(self._cfg.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        # 順序が重要: 先に前回の ID を捨てる。逆順だと「新しいプロセスは生きている・ID は前回のもの」
        # という一瞬が生まれ、`session_id` が別セッションを録音中として返す。
        self._session_id = None
        self._process = process
        on_started()
        assert process.stdout is not None
        for line in process.stdout:
            text = line.rstrip("\n")
            emit(text)
            session_id = parse_session_id(text)
            if session_id is not None and self._session_id is None:
                self._session_id = session_id
                on_session_id(session_id)
        rc = process.wait()
        self._process = None
        self._session_id = None
        return rc

    def stop_and_wait(self, timeout_sec: float = STOP_TIMEOUT_SEC) -> None:
        """（ブロッキング）停止を要求し graceful 停止を待つ。無応答なら最終手段として kill する。

        TUI 終了時に呼ぶ（知らずに録音が継続するのを防ぐ）。kill 経路では manifest が不完全に
        なり得るが、無応答のまま録音が残り続けるよりは安全側に倒す。
        """
        process = self._process
        if process is None or process.poll() is not None:
            return
        self.request_stop()
        try:
            process.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_KILL_WAIT_SEC)
