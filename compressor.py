#!/usr/bin/env python3
"""
Compactador de Vídeo
Duplo clique para abrir. Requer FFmpeg instalado no sistema.
"""

# ══════════════════════════════════════════════════════════════════════════════
# BOOTSTRAP — instala dependências automaticamente na primeira execução
# ══════════════════════════════════════════════════════════════════════════════
import sys
import importlib.util
import subprocess

def _bootstrap():
    if sys.version_info < (3, 8):
        try:
            import tkinter.messagebox as _mb
            _mb.showerror("Python desatualizado", "Python 3.8 ou superior é necessário.")
        except Exception:
            print("Erro: Python 3.8+ necessário.")
        sys.exit(1)

    missing = [
        pkg for pkg in ("customtkinter", "PIL", "cv2")
        if importlib.util.find_spec(pkg) is None
    ]
    # nomes de módulo → pacote pip
    pip_names = {"PIL": "pillow", "cv2": "opencv-python-headless"}

    if missing:
        try:
            import tkinter as _tk
            _root = _tk.Tk()
            _root.title("Compactador de Vídeo")
            _root.geometry("360x80")
            _root.resizable(False, False)
            _tk.Label(_root, text="Instalando dependências (primeira execução)...", pady=20).pack()
            _root.update()
            packages = [pip_names.get(m, m) for m in missing]
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install"] + packages + ["--quiet"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _root.destroy()
        except subprocess.CalledProcessError:
            try:
                import tkinter.messagebox as _mb
                _mb.showerror(
                    "Erro de instalação",
                    "Não foi possível instalar as dependências.\n"
                    "Execute no terminal:\n  pip install customtkinter pillow",
                )
            except Exception:
                print("Erro: execute 'pip install customtkinter pillow'")
            sys.exit(1)
        except ImportError:
            print("tkinter não encontrado. Instale Python com suporte a tkinter.")
            sys.exit(1)


_bootstrap()

# ══════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ══════════════════════════════════════════════════════════════════════════════
import json
import os
import platform
import re
import shutil
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import tkinter as tk
import customtkinter as ctk
from tkinter import filedialog, messagebox
try:
    from PIL import Image, ImageTk  # type: ignore
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ══════════════════════════════════════════════════════════════════════════════
SAFETY_MARGIN      = 0.95   # 5% de margem para não ultrapassar o limite
AUDIO_RESERVE_KBPS = 128    # reserva de bitrate para áudio
MIN_VIDEO_KBPS     = 50     # bitrate mínimo de vídeo
MIN_DURATION_S     = 0.5    # guarda contra divisão por zero
H265_KBPS_THRESHOLD = 400   # abaixo disto H.265 entrega qualidade superior
TEMP_SUFFIX        = "_vcomp_tmp"
THUMB_W, THUMB_H   = 200, 112  # thumbnail 16:9 na tela CONFIG
SUPPORTED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}

# Velocidade esperada de compressão por acelerador (vezes tempo real)
_SPEED_FACTOR = {"nvenc": 30, "vaapi": 15, "videotoolbox": 20, "cpu": 4}

PRESETS: list[tuple[str, Optional[int]]] = [
    ("WhatsApp  16 MB",       16),
    ("Discord  10 MB",        10),
    ("Discord Nitro  25 MB",  25),
    ("Discord Boost  100 MB", 100),
    ("Telegram  50 MB",       50),
    ("Email  25 MB",          25),
    ("Personalizado...",      None),
]

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

APP_W, APP_H = 560, 600

# Fontes são criadas dentro de _build_ui(), depois que o Tk está ativo.
# CTkFont é subclasse de tkinter.font.Font e exige uma janela inicializada.


# ══════════════════════════════════════════════════════════════════════════════
# EXCEÇÕES
# ══════════════════════════════════════════════════════════════════════════════
class FFmpegNotFoundError(Exception):
    pass

class ProbeError(Exception):
    pass

class CompressionError(Exception):
    pass


# ══════════════════════════════════════════════════════════════════════════════
# FFMPEG HANDLER
# ══════════════════════════════════════════════════════════════════════════════
class FFmpegHandler:
    """Toda a lógica de FFmpeg — sem código de UI."""

    # Resultado da detecção de hardware fica em cache para a sessão (#11)
    _accel_cache: Optional[str] = None

    @staticmethod
    def find_ffmpeg() -> tuple[str, str]:
        """Retorna (ffmpeg, ffprobe) ou levanta FFmpegNotFoundError."""
        script_dir = Path(sys.argv[0]).resolve().parent

        pairs = [("ffmpeg", "ffprobe")]
        if platform.system() == "Windows":
            pairs += [
                (r"C:\ffmpeg\bin\ffmpeg.exe",    r"C:\ffmpeg\bin\ffprobe.exe"),
                (str(script_dir / "ffmpeg.exe"), str(script_dir / "ffprobe.exe")),
            ]
        else:
            pairs += [
                ("/usr/local/bin/ffmpeg",       "/usr/local/bin/ffprobe"),
                ("/opt/homebrew/bin/ffmpeg",    "/opt/homebrew/bin/ffprobe"),
                (str(script_dir / "ffmpeg"),    str(script_dir / "ffprobe")),
            ]

        for ff, fp in pairs:
            ff_path = shutil.which(ff) or (ff if Path(ff).is_file() else None)
            fp_path = shutil.which(fp) or (fp if Path(fp).is_file() else None)
            if ff_path and fp_path:
                return ff_path, fp_path

        raise FFmpegNotFoundError("FFmpeg não encontrado no sistema.")

    @staticmethod
    def probe_video(ffprobe_path: str, input_path: str) -> dict:
        """Retorna metadados do vídeo via ffprobe JSON. (#12: busca só os campos necessários)"""
        cmd = [
            ffprobe_path, "-v", "quiet",
            "-print_format", "json",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,duration"
            ":format=duration,size",
            str(input_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)  # #17
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"Falha ao analisar vídeo: {exc}")
        except subprocess.TimeoutExpired:
            raise ProbeError("Análise demorou mais de 10s — arquivo pode estar em rede lenta.")

        fmt     = data.get("format", {})
        streams = data.get("streams", [])

        # Duração: preferir format.duration (mais confiável)
        duration_s = float(fmt.get("duration") or 0)
        if duration_s <= 0:
            for s in streams:
                d = float(s.get("duration") or 0)
                if d > 0:
                    duration_s = d
                    break

        if duration_s <= 0:
            raise ProbeError("Não foi possível determinar a duração do vídeo.")

        vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if vstream is None:
            raise ProbeError("Nenhuma faixa de vídeo encontrada no arquivo.")

        return {
            "duration_s":  duration_s,
            "width":       int(vstream.get("width")  or 0),
            "height":      int(vstream.get("height") or 0),
            "codec":       vstream.get("codec_name", "?"),
            "has_audio":   any(s.get("codec_type") == "audio" for s in streams),
            "size_bytes":  int(fmt.get("size") or Path(input_path).stat().st_size),
        }

    @staticmethod
    def get_thumbnail(ffmpeg_path: str, input_path: str, out_path: str) -> bool:
        """Extrai um frame em ~1s como PNG. Retorna True se bem-sucedido. (#1)"""
        try:
            r = subprocess.run(
                [ffmpeg_path, "-y", "-loglevel", "warning",
                 "-ss", "1", "-i", str(input_path),
                 "-frames:v", "1", "-q:v", "3", str(out_path)],
                capture_output=True, timeout=10,
            )
            return r.returncode == 0 and Path(out_path).exists()
        except Exception:
            return False

    @classmethod
    def _detect_accel(cls, ffmpeg_path: str) -> str:
        """Detecta melhor acelerador de hardware disponível. Resultado é cacheado. (#11)"""
        if cls._accel_cache is not None:
            return cls._accel_cache

        result = "cpu"
        try:
            res = subprocess.run(
                [ffmpeg_path, "-encoders", "-v", "quiet"],
                capture_output=True, text=True, timeout=10,
            )
            encoders = res.stdout + res.stderr

            if "h264_nvenc" in encoders:
                test = subprocess.run(
                    [ffmpeg_path, "-f", "lavfi", "-i", "nullsrc", "-t", "0.1",
                     "-c:v", "h264_nvenc", "-f", "null", "-"],
                    capture_output=True, timeout=10,
                )
                if test.returncode == 0:
                    result = "nvenc"
            elif "h264_vaapi" in encoders and Path("/dev/dri/renderD128").exists():
                result = "vaapi"
            elif "h264_videotoolbox" in encoders:
                result = "videotoolbox"

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        cls._accel_cache = result
        return result

    def build_command(
        self,
        ffmpeg_path: str,
        input_path: str,
        output_path: str,
        target_bytes: int,
        probe: dict,
        use_h265: bool = False,
        trim_start: Optional[float] = None,
        trim_end: Optional[float] = None,
        max_height: Optional[int] = None,
        quality: str = "fast",
    ) -> tuple[list[str], str]:
        """Monta o comando FFmpeg com aceleração automática. Retorna (cmd, accel_name)."""
        duration_s = probe["duration_s"]
        effective_s = max(
            (trim_end or duration_s) - (trim_start or 0),
            MIN_DURATION_S,
        )
        has_audio = probe["has_audio"]

        audio_bits = AUDIO_RESERVE_KBPS * 1000 * effective_s if has_audio else 0
        video_kbps = max(
            int(((target_bytes * 8 * SAFETY_MARGIN) - audio_bits) / effective_s / 1000),
            MIN_VIDEO_KBPS,
        )
        maxrate = int(video_kbps * 1.5)
        bufsize = int(video_kbps * 3)

        force_h265 = use_h265 or (video_kbps < H265_KBPS_THRESHOLD)
        accel = self._detect_accel(ffmpeg_path)

        base = [ffmpeg_path, "-y", "-loglevel", "warning"]

        seek_args: list[str] = []
        if trim_start and trim_start > 0:
            seek_args += ["-ss", f"{trim_start:.3f}"]

        end_args: list[str] = []
        if trim_end and trim_end < duration_s:
            end_args += ["-to", f"{trim_end:.3f}"]

        # Filtro de resolução — só reduz, nunca amplia
        scale_vf = (f"scale=-2:min(ih\\,{max_height})" if max_height else "")

        audio = ["-c:a", "aac", "-b:a", f"{AUDIO_RESERVE_KBPS}k"] if has_audio else ["-an"]
        tail  = end_args + audio + ["-movflags", "+faststart",
                                    "-progress", "pipe:2", str(output_path)]

        cpu_preset   = "medium" if quality == "good" else "ultrafast"
        nvenc_preset = "p4"     if quality == "good" else "p1"

        if force_h265:
            accel_label = "H.265 CPU"
            vf = (["-vf", scale_vf] if scale_vf else [])
            cmd = base + seek_args + ["-i", str(input_path)] + vf + [
                "-c:v", "libx265", "-preset", cpu_preset, "-tag:v", "hvc1",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail

        elif accel == "nvenc":
            accel_label = "GPU NVENC"
            vf = (["-vf", scale_vf] if scale_vf else [])
            cmd = base + seek_args + ["-i", str(input_path)] + vf + [
                "-c:v", "h264_nvenc", "-preset", nvenc_preset,
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail

        elif accel == "vaapi":
            accel_label = "GPU VAAPI"
            vaapi_vf = (f"{scale_vf}," if scale_vf else "") + "format=nv12,hwupload"
            cmd = base + seek_args + [
                "-hwaccel", "vaapi",
                "-hwaccel_device", "/dev/dri/renderD128",
                "-i", str(input_path),
                "-c:v", "h264_vaapi",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
                "-vf", vaapi_vf,
            ] + tail

        elif accel == "videotoolbox":
            accel_label = "GPU VideoToolbox"
            vf = (["-vf", scale_vf] if scale_vf else [])
            cmd = base + seek_args + ["-i", str(input_path)] + vf + [
                "-c:v", "h264_videotoolbox",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail

        else:
            accel_label = "CPU libx264"
            vf = (["-vf", scale_vf] if scale_vf else [])
            cmd = base + seek_args + ["-i", str(input_path)] + vf + [
                "-c:v", "libx264", "-preset", cpu_preset,
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail

        return cmd, accel_label

    def compress(
        self,
        ffmpeg_path: str,
        input_path: str,
        output_path: str,
        target_bytes: int,
        probe: dict,
        on_progress: Callable[[float, str], None],
        cancel: threading.Event,
        use_h265: bool = False,
        trim_start: Optional[float] = None,
        trim_end: Optional[float] = None,
        max_height: Optional[int] = None,
        quality: str = "fast",
    ) -> str:
        """
        Executa a compressão. Se o output ultrapassar target_bytes,
        refaz automaticamente com bitrate mais conservador (segunda passagem).
        Retorna accel_label. Levanta CompressionError se falhar.
        """
        eff_duration = max(
            (trim_end or probe["duration_s"]) - (trim_start or 0),
            MIN_DURATION_S,
        )

        def _run_encode(tb: int, phase: str) -> tuple[str, list[str]]:
            """Codifica para target_bytes=tb. Retorna (accel_label, recent_lines)."""
            cmd, label = self.build_command(
                ffmpeg_path, input_path, output_path, tb, probe,
                use_h265=use_h265, trim_start=trim_start, trim_end=trim_end,
                max_height=max_height, quality=quality,
            )
            proc = subprocess.Popen(
                cmd, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
            )
            recent: list[str] = []
            block:  list[str] = []
            try:
                for raw in proc.stderr:
                    if cancel.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        return label, recent
                    line = raw.strip()
                    recent.append(line)
                    if len(recent) > 40:
                        recent.pop(0)
                    block.append(line)
                    if line.startswith("progress="):
                        parsed = _parse_progress_block(block, eff_duration)
                        if parsed:
                            pct, stats = parsed
                            on_progress(pct, f"{phase}{stats}" if phase else stats)
                        block.clear()
            finally:
                if proc.returncode is None:
                    proc.wait()
            return label, recent, proc  # type: ignore[return-value]

        # ── Primeira passagem (95 % safety margin) ────────────────────────────
        accel_label, lines, proc = _run_encode(target_bytes, "")
        if cancel.is_set():
            return accel_label
        if proc.returncode != 0:
            raise CompressionError("\n".join(lines[-15:]))

        # ── Verificação de tamanho ────────────────────────────────────────────
        actual = Path(output_path).stat().st_size
        if actual > target_bytes:
            # Segunda passagem: mira em 88% do limite garantido
            # Usa relação actual/target para compensar a imprecisão do encoder
            second_target = int(min(
                target_bytes * 0.88,
                target_bytes * (target_bytes / actual) * 0.90,
            ))
            second_target = max(second_target, MIN_VIDEO_KBPS * 1000)

            on_progress(0.0, "Ajustando (2ª passagem)...")
            accel_label, lines2, proc2 = _run_encode(second_target, "")
            if not cancel.is_set() and proc2.returncode != 0:
                raise CompressionError("\n".join(lines2[-15:]))

        return accel_label


def _parse_progress_block(lines: list[str], duration_s: float) -> Optional[tuple[float, str]]:
    """Converte um bloco key=value do -progress pipe:2 em (percent, stats_str)."""
    data: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip()

    out_us = data.get("out_time_us", "")
    if not out_us or not out_us.lstrip("-").isdigit():
        return None

    out_us_int = int(out_us)
    if out_us_int < 0:  # FFmpeg emite -1 antes do primeiro frame estar pronto
        return None

    elapsed = out_us_int / 1_000_000
    pct     = min(100.0, elapsed / max(duration_s, 0.001) * 100)

    fps     = data.get("fps",     "?")
    speed   = data.get("speed",   "?")
    bitrate = data.get("bitrate", "?")

    eta = ""
    try:
        spd = float(speed.rstrip("x"))
        if spd > 0:
            remaining = int((duration_s - elapsed) / spd)
            if remaining > 0:
                eta = f"  ETA {remaining}s"
    except (ValueError, AttributeError):
        pass

    return pct, f"{fps} fps  •  {bitrate}  •  {speed}{eta}"


def _friendly_probe_error(raw: str) -> str:
    """Traduz erros crus do ffprobe em mensagens amigáveis. (#10)"""
    low = raw.lower()
    if "moov atom not found" in low:
        return "O arquivo MP4 está incompleto (transferência interrompida?)."
    if "invalid data found" in low or "invalid argument" in low:
        return "O arquivo parece corrompido ou incompleto."
    if "no such file" in low:
        return "Arquivo não encontrado."
    if "permission denied" in low:
        return "Sem permissão para ler o arquivo."
    if "decoder" in low and ("not found" in low or "unknown" in low):
        return "Codec não reconhecido pelo FFmpeg instalado."
    if "10s" in low or "timeout" in low:
        return "Análise expirou — o arquivo pode estar em rede lenta ou ser muito grande."
    return raw


def _open_folder(path: Path) -> None:
    """Abre a pasta que contém o arquivo no explorador do SO. (#5)"""
    folder = str(path.parent)
    try:
        if platform.system() == "Windows":
            os.startfile(folder)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
    except Exception:
        pass


# ── Diálogos de arquivo nativos (Linux: zenity → kdialog → tkinter fallback) ──

def _ask_open_files() -> list[str]:
    """Seletor de arquivos — tenta dialogo nativo no Linux, fallback tkinter."""
    if platform.system() == "Linux":
        result = _linux_ask_open()
        if result is not None:
            return result
    exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXT))
    return list(filedialog.askopenfilenames(
        title="Selecionar vídeo(s)",
        filetypes=[("Vídeos", exts), ("Todos os arquivos", "*.*")],
    ))


def _linux_ask_open() -> Optional[list[str]]:
    """Usa zenity ou kdialog. Retorna None se nenhum estiver disponível."""
    exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXT))
    # zenity (GNOME / GTK)
    try:
        r = subprocess.run(
            ["zenity", "--file-selection", "--multiple",
             f"--file-filter=Vídeos | {exts}",
             "--separator=\n", "--title=Selecionar vídeo(s)"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return [p for p in r.stdout.strip().split("\n") if p]
        return []   # usuário cancelou
    except FileNotFoundError:
        pass
    # kdialog (KDE)
    try:
        r = subprocess.run(
            ["kdialog", "--getopenfilename", str(Path.home()), exts,
             "--title", "Selecionar vídeo(s)"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return [p for p in r.stdout.strip().split("\n") if p]
        return []
    except FileNotFoundError:
        pass
    return None  # nenhum nativo disponível — tkinter assume


def _ask_save_file(initial: str) -> Optional[str]:
    """Seletor de destino — tenta dialogo nativo no Linux, fallback tkinter."""
    if platform.system() == "Linux":
        result = _linux_ask_save(initial)
        if result is not None:
            return result or None
    p = Path(initial)
    dest = filedialog.asksaveasfilename(
        title="Salvar vídeo comprimido",
        defaultextension=".mp4",
        initialfile=p.name,
        initialdir=str(p.parent),
        filetypes=[("Vídeo MP4", "*.mp4"), ("Todos os arquivos", "*.*")],
    )
    return dest or None


def _linux_ask_save(initial: str) -> Optional[str]:
    """Usa zenity ou kdialog para salvar. Retorna None se nenhum disponível."""
    # zenity
    try:
        r = subprocess.run(
            ["zenity", "--file-selection", "--save", "--confirm-overwrite",
             f"--filename={initial}", "--title=Salvar vídeo comprimido"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return r.stdout.strip() or ""
        return ""
    except FileNotFoundError:
        pass
    # kdialog
    try:
        r = subprocess.run(
            ["kdialog", "--getsavefilename", initial, "*.mp4",
             "--title", "Salvar vídeo comprimido"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return r.stdout.strip() or ""
        return ""
    except FileNotFoundError:
        pass
    return None


def _parse_time(s: str) -> Optional[float]:
    """Converte mm:ss ou hh:mm:ss em segundos. Retorna None se inválido. (#16)"""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if not m:
        return None
    parts = [int(x) for x in m.groups(default="0")]
    h, mi, sec = (parts[0], parts[1], parts[2]) if m.group(3) else (0, parts[0], parts[1])
    return h * 3600 + mi * 60 + sec


def _fmt_time(t: float) -> str:
    """Float de segundos → 'm:ss' ou 'h:mm:ss'."""
    t = int(max(0.0, t))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ══════════════════════════════════════════════════════════════════════════════
# PLAYER INTERATIVO DE RECORTE
# ══════════════════════════════════════════════════════════════════════════════
try:
    import cv2 as _cv2       # type: ignore
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

_PDISP_W  = 640   # largura do preview de vídeo
_PDISP_H  = 360   # altura  do preview de vídeo
_TL_H     = 72    # altura do canvas de timeline
_TL_PAD   = 20    # margem horizontal da barra


class VideoTrimDialog:
    """
    Dialog modal com player interativo para escolha de ponto de recorte.
    Requer opencv-python-headless (instalado pelo bootstrap).
    .result → (start_s, end_s) ou None se cancelado.
    """

    def __init__(
        self,
        parent: ctk.CTk,
        ffmpeg_path: str,
        input_path: str,
        duration_s: float,
        initial_start: float = 0.0,
        initial_end: Optional[float] = None,
    ):
        self.result: Optional[tuple[float, float]] = None

        if not _CV2_OK:
            messagebox.showerror(
                "Player não disponível",
                "opencv-python-headless não está instalado.\n"
                "Execute:  pip install opencv-python-headless",
            )
            return

        self.duration_s    = max(duration_s, 0.001)
        self.trim_start    = float(initial_start)
        self.trim_end      = float(initial_end if initial_end is not None else duration_s)
        self._current_time = 0.0
        self._playing      = False
        self._drag_what: Optional[str] = None
        self._after_id     = None

        self.cap = _cv2.VideoCapture(str(input_path))
        if not self.cap.isOpened():
            messagebox.showerror("Erro", f"Não foi possível abrir:\n{input_path}")
            return

        raw_fps    = self.cap.get(_cv2.CAP_PROP_FPS)
        self.fps   = raw_fps if raw_fps and raw_fps > 0 else 25.0

        # ── Janela ────────────────────────────────────────────────────────────
        self._win = ctk.CTkToplevel(parent)
        self._win.title(f"Recortar — {Path(input_path).name}")
        self._win.resizable(False, False)
        self._win.transient(parent)
        self._win.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._build_ui()

        # Centraliza sobre o pai
        self._win.update_idletasks()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        px, py = parent.winfo_x(), parent.winfo_y()
        dw, dh = 680, 634
        self._win.geometry(f"{dw}x{dh}+{px + (pw - dw)//2}+{py + (ph - dh)//2}")

        self._seek(self.trim_start)

        self._win.focus_force()
        self._win.bind("<space>",        lambda e: self._toggle_play())
        self._win.bind("<Left>",         lambda e: self._seek(self._current_time - 2))
        self._win.bind("<Right>",        lambda e: self._seek(self._current_time + 2))
        self._win.bind("<Shift-Left>",   lambda e: self._seek(self._current_time - 10))
        self._win.bind("<Shift-Right>",  lambda e: self._seek(self._current_time + 10))

        self._win.grab_set()
        self._win.wait_window()

    # ── Construção ────────────────────────────────────────────────────────────

    def _build_ui(self):
        win = self._win
        win.grid_columnconfigure(0, weight=1)

        # Preview de vídeo
        self._video_label = ctk.CTkLabel(
            win, text="Carregando...",
            width=_PDISP_W, height=_PDISP_H,
            fg_color="black", corner_radius=0,
        )
        self._video_label.grid(row=0, column=0, padx=20, pady=(14, 0))

        # Timeline canvas (tk nativo — sem CTkCanvas)
        self._canvas = tk.Canvas(
            win, width=_PDISP_W, height=_TL_H,
            bg="#1a1a1a", highlightthickness=0,
        )
        self._canvas.grid(row=1, column=0, padx=20, pady=(8, 0))
        self._canvas.bind("<Button-1>",        self._on_press)
        self._canvas.bind("<B1-Motion>",       self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)

        # Controles de reprodução
        ctrl = ctk.CTkFrame(win, fg_color="transparent")
        ctrl.grid(row=2, column=0, padx=20, pady=(6, 0), sticky="ew")
        ctrl.grid_columnconfigure(1, weight=1)

        btns = ctk.CTkFrame(ctrl, fg_color="transparent")
        btns.grid(row=0, column=0)
        for label, delta in [("-10s", -10), ("-2s", -2)]:
            ctk.CTkButton(
                btns, text=label, width=52, height=32,
                fg_color="transparent", border_width=1,
                command=lambda d=delta: self._seek(self._current_time + d),
            ).pack(side="left", padx=2)
        self._btn_play = ctk.CTkButton(
            btns, text="▶  Play", width=100, height=32,
            command=self._toggle_play,
        )
        self._btn_play.pack(side="left", padx=2)
        for label, delta in [("+2s", 2), ("+10s", 10)]:
            ctk.CTkButton(
                btns, text=label, width=52, height=32,
                fg_color="transparent", border_width=1,
                command=lambda d=delta: self._seek(self._current_time + d),
            ).pack(side="left", padx=2)

        self._lbl_time = ctk.CTkLabel(
            ctrl, text="0:00 / 0:00",
            font=ctk.CTkFont(family="Courier", size=12),
        )
        self._lbl_time.grid(row=0, column=1, sticky="e")

        # Botões de snap (posição atual → marcador)
        snap = ctk.CTkFrame(win, fg_color="transparent")
        snap.grid(row=3, column=0, padx=20, pady=(6, 0), sticky="ew")
        snap.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkButton(
            snap, text="⬤  Marcar início aqui", height=34,
            fg_color="transparent", border_width=1, text_color="#4fc3f7",
            command=self._snap_start,
        ).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        ctk.CTkButton(
            snap, text="Marcar fim aqui  ⬤", height=34,
            fg_color="transparent", border_width=1, text_color="#f07b3f",
            command=self._snap_end,
        ).grid(row=0, column=1, padx=(4, 0), sticky="ew")

        # Ações
        actions = ctk.CTkFrame(win, fg_color="transparent")
        actions.grid(row=4, column=0, padx=20, pady=(8, 16), sticky="ew")
        actions.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(
            actions, text="✗  Cancelar", height=42,
            fg_color="transparent", border_width=1,
            command=self._on_cancel,
        ).grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(
            actions, text="✓  Confirmar recorte", height=42,
            command=self._on_confirm,
        ).grid(row=0, column=1, padx=(6, 0), sticky="ew")

    # ── Reprodução ────────────────────────────────────────────────────────────

    def _toggle_play(self):
        if self._playing:
            self._playing = False
            self._btn_play.configure(text="▶  Play")
        else:
            if self._current_time >= self.trim_end - 0.1:
                self._seek(self.trim_start)
            self._playing = True
            self._btn_play.configure(text="⏸  Pausar")
            self._play_step()

    def _play_step(self):
        if not self._playing:
            return
        ret, frame = self.cap.read()
        if not ret:
            self._playing = False
            self._btn_play.configure(text="▶  Play")
            return

        pos = self.cap.get(_cv2.CAP_PROP_POS_MSEC) / 1000.0
        self._current_time = pos

        if pos >= self.trim_end:
            self._playing = False
            self._btn_play.configure(text="▶  Play")
            self._seek(self.trim_end)
            return

        self._show_frame(frame)
        self._draw_timeline()
        self._update_time_label()
        self._after_id = self._win.after(max(1, int(1000 / self.fps)), self._play_step)

    def _seek(self, time_s: float):
        was_playing = self._playing
        if self._playing:
            self._playing = False
            self._btn_play.configure(text="▶  Play")
        if self._after_id:
            self._win.after_cancel(self._after_id)
            self._after_id = None

        self._current_time = max(0.0, min(self.duration_s, time_s))
        self.cap.set(_cv2.CAP_PROP_POS_MSEC, self._current_time * 1000)
        ret, frame = self.cap.read()
        if ret:
            self._show_frame(frame)
        self._draw_timeline()
        self._update_time_label()

    def _show_frame(self, frame_bgr):
        try:
            h, w  = frame_bgr.shape[:2]
            scale = min(_PDISP_W / w, _PDISP_H / h)
            nw = max(1, int(w * scale))
            nh = max(1, int(h * scale))

            frame_rgb = _cv2.cvtColor(frame_bgr, _cv2.COLOR_BGR2RGB)
            img = Image.fromarray(frame_rgb).resize((nw, nh), Image.LANCZOS)

            canvas_img = Image.new("RGB", (_PDISP_W, _PDISP_H), (0, 0, 0))
            canvas_img.paste(img, ((_PDISP_W - nw) // 2, (_PDISP_H - nh) // 2))

            ctk_img = ctk.CTkImage(canvas_img, size=(_PDISP_W, _PDISP_H))
            self._video_label.configure(image=ctk_img, text="")
            self._video_label._ctk_img = ctk_img   # evita GC
        except Exception:
            pass

    # ── Timeline ──────────────────────────────────────────────────────────────

    def _time_to_x(self, t: float) -> int:
        ratio = max(0.0, min(1.0, t / self.duration_s))
        return int(_TL_PAD + ratio * (_PDISP_W - 2 * _TL_PAD))

    def _x_to_time(self, x: int) -> float:
        ratio = (x - _TL_PAD) / (_PDISP_W - 2 * _TL_PAD)
        return max(0.0, min(self.duration_s, ratio * self.duration_s))

    def _draw_timeline(self):
        c = self._canvas
        c.delete("all")

        x0, x1       = _TL_PAD, _PDISP_W - _TL_PAD
        bar_top       = 26
        bar_bot       = 44
        handle_top    = bar_top - 22
        handle_bot    = bar_top - 10

        xs = self._time_to_x(self.trim_start)
        xe = self._time_to_x(self.trim_end)
        xn = self._time_to_x(self._current_time)

        # Trilha de fundo
        c.create_rectangle(x0, bar_top, x1, bar_bot, fill="#333333", outline="")
        # Região selecionada
        c.create_rectangle(xs, bar_top, xe, bar_bot, fill="#2a7ac0", outline="")

        # Handle de início (azul)
        c.create_rectangle(xs - 3, bar_top - 12, xs + 3, bar_bot + 6,
                            fill="#4fc3f7", outline="")
        c.create_rectangle(xs - 10, handle_top, xs + 10, handle_bot,
                            fill="#4fc3f7", outline="#1a1a1a")
        c.create_text(max(xs, x0 + 24), bar_bot + 14,
                      text=_fmt_time(self.trim_start),
                      fill="#4fc3f7", font=("Courier", 8), anchor="n")

        # Handle de fim (laranja)
        c.create_rectangle(xe - 3, bar_top - 12, xe + 3, bar_bot + 6,
                            fill="#f07b3f", outline="")
        c.create_rectangle(xe - 10, handle_top, xe + 10, handle_bot,
                            fill="#f07b3f", outline="#1a1a1a")
        c.create_text(min(xe, x1 - 24), bar_bot + 14,
                      text=_fmt_time(self.trim_end),
                      fill="#f07b3f", font=("Courier", 8), anchor="n")

        # Agulha (branca) — posição atual
        c.create_line(xn, 0, xn, bar_bot + 6, fill="#ffffff", width=2)
        c.create_oval(xn - 5, 1, xn + 5, 11, fill="#ffffff", outline="")

    def _update_time_label(self):
        self._lbl_time.configure(
            text=f"{_fmt_time(self._current_time)} / {_fmt_time(self.duration_s)}"
        )

    # ── Eventos de mouse na timeline ──────────────────────────────────────────

    def _on_press(self, event):
        xs = self._time_to_x(self.trim_start)
        xe = self._time_to_x(self.trim_end)
        x  = event.x

        if abs(x - xs) <= 12:
            self._drag_what = "start"
            if self._playing:
                self._toggle_play()
        elif abs(x - xe) <= 12:
            self._drag_what = "end"
            if self._playing:
                self._toggle_play()
        else:
            self._drag_what = "seek"
            self._seek(self._x_to_time(x))

    def _on_drag(self, event):
        x = max(_TL_PAD, min(_PDISP_W - _TL_PAD, event.x))
        t = self._x_to_time(x)
        if self._drag_what == "start":
            self.trim_start = max(0.0, min(t, self.trim_end - 0.1))
            self._seek(self.trim_start)
        elif self._drag_what == "end":
            self.trim_end = min(self.duration_s, max(t, self.trim_start + 0.1))
            self._seek(self.trim_end)
        elif self._drag_what == "seek":
            self._seek(t)

    def _on_release(self, _event):
        self._drag_what = None

    # ── Snap: ajusta marcador à posição atual ─────────────────────────────────

    def _snap_start(self):
        self.trim_start = max(0.0, min(self._current_time, self.trim_end - 0.1))
        self._draw_timeline()

    def _snap_end(self):
        self.trim_end = min(self.duration_s, max(self._current_time, self.trim_start + 0.1))
        self._draw_timeline()

    # ── Fechar ────────────────────────────────────────────────────────────────

    def _on_confirm(self):
        self.result = (self.trim_start, self.trim_end)
        self._cleanup()

    def _on_cancel(self):
        self._cleanup()

    def _cleanup(self):
        self._playing = False
        if self._after_id:
            try:
                self._win.after_cancel(self._after_id)
            except Exception:
                pass
        if hasattr(self, "cap") and self.cap:
            self.cap.release()
        try:
            self._win.destroy()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# UI — MÁQUINA DE ESTADOS
# ══════════════════════════════════════════════════════════════════════════════
_DROP     = "drop"
_CONFIG   = "config"
_PROGRESS = "progress"
_DONE     = "done"


class VideoCompressorApp(ctk.CTk):

    def __init__(self, handler: FFmpegHandler, ffmpeg: str, ffprobe: str):
        super().__init__()
        self.handler  = handler
        self.ffmpeg   = ffmpeg
        self.ffprobe  = ffprobe

        self.state = _DROP

        self.input_path:   Optional[Path] = None
        self.probe:        Optional[dict] = None
        self.target_bytes: Optional[int]  = None

        self._trim_start:  Optional[float] = None
        self._trim_end:    Optional[float] = None
        self._max_height:  Optional[int]   = None   # None = original
        self._quality:     str             = "fast" # "fast" | "good"

        self.cancel_evt  = threading.Event()
        self.comp_thread: Optional[threading.Thread] = None
        self.tmp_path:    Optional[Path] = None
        self._saved_path: Optional[Path] = None  # para "Abrir pasta" (#5)
        self._thumb_path: Optional[Path] = None  # arquivo PNG temporário (#1)

        self.title("Compactador de Vídeo")
        self.geometry(f"{APP_W}x{APP_H}")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._go(_DROP)

    # ── Construção da UI ─────────────────────────────────────────────────────

    def _build_ui(self):
        # Fontes instanciadas aqui — Tk já está ativo via super().__init__()
        self._f_bold    = ctk.CTkFont(weight="bold")
        self._f_bold_l  = ctk.CTkFont(size=15, weight="bold")
        self._f_bold_xl = ctk.CTkFont(size=26, weight="bold")
        self._f_small   = ctk.CTkFont(size=11)
        self._f_icon    = ctk.CTkFont(size=52)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._frames = {
            _DROP:     self._make_drop_frame(),
            _CONFIG:   self._make_config_frame(),
            _PROGRESS: self._make_progress_frame(),
            _DONE:     self._make_done_frame(),
        }
        for f in self._frames.values():
            f.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)

    # ── Frame DROP ────────────────────────────────────────────────────────────

    def _make_drop_frame(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self)
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.grid(row=0, column=0)

        ctk.CTkLabel(inner, text="▶", font=self._f_icon).pack(pady=(0, 10))
        ctk.CTkLabel(inner, text="Arraste um vídeo ou clique para abrir",
                     font=self._f_bold_l).pack()
        ctk.CTkLabel(
            inner, text="MP4 · MOV · MKV · AVI · WEBM · FLV · WMV",
            font=self._f_small, text_color="gray",
        ).pack(pady=(4, 16))
        ctk.CTkButton(
            inner, text="Abrir Vídeo", width=200, height=44, font=self._f_bold_l,
            command=self._browse,
        ).pack()

        # Drag-and-drop — silenciosamente opcional (requer tkinterdnd2)
        try:
            from tkinterdnd2 import DND_FILES  # type: ignore
            f.drop_target_register(DND_FILES)
            f.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

        return f

    # ── Frame CONFIG ──────────────────────────────────────────────────────────

    def _make_config_frame(self) -> ctk.CTkFrame:
        outer = ctk.CTkFrame(self)
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(outer)
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        f = scroll

        # ── Thumbnail + info ─────────────────────────────────────────────────
        head = ctk.CTkFrame(f, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        head.grid_columnconfigure(1, weight=1)

        self._thumb_label = ctk.CTkLabel(
            head, text="", width=THUMB_W, height=THUMB_H,
            fg_color=("gray80", "gray20"), corner_radius=6,
        )
        self._thumb_label.grid(row=0, column=0, rowspan=3, padx=(0, 12))

        self._lbl_name = ctk.CTkLabel(
            head, text="", font=self._f_bold, wraplength=300, anchor="w", justify="left",
        )
        self._lbl_name.grid(row=0, column=1, sticky="ew")

        self._lbl_info = ctk.CTkLabel(
            head, text="", font=self._f_small, text_color="gray",
            anchor="w", justify="left",
        )
        self._lbl_info.grid(row=1, column=1, sticky="ew")

        self._lbl_estimate = ctk.CTkLabel(
            head, text="", font=self._f_small, text_color="gray",
            anchor="w", justify="left",
        )
        self._lbl_estimate.grid(row=2, column=1, sticky="ew")

        self._lbl_warn = ctk.CTkLabel(f, text="", font=self._f_small, text_color="#f0a500")
        self._lbl_warn.grid(row=1, column=0, pady=(0, 4), padx=8)

        # ── Bloco 1: Tamanho máximo ───────────────────────────────────────────
        ctk.CTkLabel(f, text="Tamanho máximo:", font=self._f_bold).grid(
            row=2, column=0, sticky="w", padx=12, pady=(4, 4),
        )

        pgrid = ctk.CTkFrame(f, fg_color="transparent")
        pgrid.grid(row=3, column=0, padx=8, sticky="ew")
        pgrid.grid_columnconfigure((0, 1), weight=1)

        self._preset_btns: list[ctk.CTkButton] = []
        for i, (label, mb) in enumerate(PRESETS):
            row_i, col = divmod(i, 2)
            btn = ctk.CTkButton(
                pgrid, text=label, height=34,
                fg_color="transparent", border_width=1, font=self._f_small,
                command=lambda lbl=label, size=mb: self._pick_preset(lbl, size),
            )
            btn.grid(row=row_i, column=col, padx=3, pady=2, sticky="ew")
            self._preset_btns.append(btn)

        custom_row = ctk.CTkFrame(f, fg_color="transparent")
        custom_row.grid(row=4, column=0, padx=8, pady=(4, 0), sticky="w")
        self._entry_custom = ctk.CTkEntry(
            custom_row, placeholder_text="Tamanho em MB (ex: 8.5)",
            width=200, state="disabled",
        )
        self._entry_custom.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(custom_row, text="MB").pack(side="left")

        # ── Bloco 2: Resolução + Qualidade ────────────────────────────────────
        opts_card = ctk.CTkFrame(f, fg_color=("gray88", "gray18"), corner_radius=8)
        opts_card.grid(row=5, column=0, sticky="ew", padx=8, pady=(10, 0))
        opts_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(opts_card, text="Resolução:", font=self._f_small, anchor="w").grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 4),
        )
        self._seg_res = ctk.CTkSegmentedButton(
            opts_card,
            values=["Original", "720p", "480p", "360p"],
            command=self._on_res_change,
            font=self._f_small,
        )
        self._seg_res.set("Original")
        self._seg_res.grid(row=0, column=1, sticky="w", padx=(0, 12), pady=(10, 4))

        ctk.CTkLabel(opts_card, text="Qualidade:", font=self._f_small, anchor="w").grid(
            row=1, column=0, sticky="w", padx=12, pady=(0, 10),
        )
        self._seg_quality = ctk.CTkSegmentedButton(
            opts_card,
            values=["⚡ Rápido", "★ Melhor qualidade"],
            command=self._on_quality_change,
            font=self._f_small,
        )
        self._seg_quality.set("⚡ Rápido")
        self._seg_quality.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=(0, 10))

        # ── Bloco 3: Recorte ──────────────────────────────────────────────────
        trim_row = ctk.CTkFrame(f, fg_color="transparent")
        trim_row.grid(row=6, column=0, sticky="ew", padx=8, pady=(8, 0))
        trim_row.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            trim_row, text="✂  Recortar vídeo...", height=34, width=160,
            fg_color="transparent", border_width=1, font=self._f_small,
            command=self._open_trim_dialog,
        ).grid(row=0, column=0, padx=(0, 10))

        self._lbl_trim = ctk.CTkLabel(
            trim_row, text="Sem recorte",
            font=self._f_small, text_color="gray", anchor="w",
        )
        self._lbl_trim.grid(row=0, column=1, sticky="w")

        ctk.CTkButton(
            trim_row, text="✕", width=28, height=28,
            fg_color="transparent", font=self._f_small,
            command=self._clear_trim,
        ).grid(row=0, column=2)

        # ── Ações ─────────────────────────────────────────────────────────────
        actions = ctk.CTkFrame(f, fg_color="transparent")
        actions.grid(row=7, column=0, padx=8, pady=(10, 8), sticky="ew")
        ctk.CTkButton(
            actions, text="← Voltar", width=100, height=40,
            fg_color="transparent", border_width=1,
            command=self._back,
        ).pack(side="left")
        self._btn_compress = ctk.CTkButton(
            actions, text="Comprimir", width=180, height=40, font=self._f_bold_l,
            command=self._start_compress,
        )
        self._btn_compress.pack(side="right")

        return outer

    # ── Frame PROGRESS ────────────────────────────────────────────────────────

    def _make_progress_frame(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self)
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.grid(row=0, column=0, padx=20)

        ctk.CTkLabel(inner, text="Comprimindo...", font=self._f_bold_l).pack(pady=(0, 6))

        # Nome do arquivo atual e posição na fila (#8)
        self._lbl_progress_file = ctk.CTkLabel(
            inner, text="", font=self._f_small, text_color="gray", wraplength=460,
        )
        self._lbl_progress_file.pack(pady=(0, 14))

        self._pbar = ctk.CTkProgressBar(inner, width=420, height=18)
        self._pbar.set(0)
        self._pbar.pack()

        self._lbl_pct = ctk.CTkLabel(inner, text="0%", font=self._f_bold_xl)
        self._lbl_pct.pack(pady=(10, 4))

        self._lbl_stats = ctk.CTkLabel(inner, text="", font=self._f_small, text_color="gray")
        self._lbl_stats.pack()

        # Acelerador em uso (#4)
        self._lbl_accel = ctk.CTkLabel(inner, text="", font=self._f_small, text_color="gray")
        self._lbl_accel.pack(pady=(2, 0))

        self._btn_cancel = ctk.CTkButton(
            inner, text="Cancelar", width=130, height=36,
            fg_color="transparent", border_width=1,
            command=self._cancel,
        )
        self._btn_cancel.pack(pady=(18, 0))

        return f

    # ── Frame DONE ────────────────────────────────────────────────────────────

    def _make_done_frame(self) -> ctk.CTkFrame:
        outer = ctk.CTkFrame(self)
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        # Conteúdo rolável — o cartão de salvar ocupa espaço extra
        scroll = ctk.CTkScrollableFrame(outer)
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        f = scroll

        # ── Header ───────────────────────────────────────────────────────────
        ctk.CTkLabel(
            f, text="✓", font=ctk.CTkFont(size=56, weight="bold"), text_color="#4caf50",
        ).grid(row=0, column=0, pady=(12, 0))
        ctk.CTkLabel(f, text="Concluído!", font=self._f_bold_l).grid(row=1, column=0)

        # Resumo de tamanhos (#8)
        self._lbl_sizes = ctk.CTkLabel(f, text="", font=self._f_small, text_color="gray")
        self._lbl_sizes.grid(row=2, column=0, pady=(4, 0), padx=12)

        self._results_box = ctk.CTkScrollableFrame(f, width=460, height=64)
        self._results_box.grid(row=3, column=0, pady=(4, 8), padx=12)
        self._results_box.grid_columnconfigure(0, weight=1)

        # ── Cartão de salvar (tudo dentro da janela) ─────────────────────────
        card = ctk.CTkFrame(f, fg_color=("gray88", "gray18"), corner_radius=8)
        card.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 10))
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(card, text="Salvar como:", font=self._f_bold,
                     anchor="w").grid(row=0, column=0, sticky="w", padx=14, pady=(12, 4))

        path_row = ctk.CTkFrame(card, fg_color="transparent")
        path_row.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 6))
        path_row.grid_columnconfigure(0, weight=1)

        self._entry_save_path = ctk.CTkEntry(
            path_row, placeholder_text="Caminho de destino...",
        )
        self._entry_save_path.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            path_row, text="Procurar", width=84, height=30,
            fg_color="transparent", border_width=1, font=self._f_small,
            command=self._browse_save_path,
        ).grid(row=0, column=1)

        self._btn_save = ctk.CTkButton(
            card, text="Salvar", width=220, height=42, font=self._f_bold_l,
            command=self._save,
        )
        self._btn_save.grid(row=2, column=0, padx=14, pady=(0, 6))

        self._lbl_save_status = ctk.CTkLabel(
            card, text="", font=self._f_small, wraplength=440,
        )
        self._lbl_save_status.grid(row=3, column=0, padx=14, pady=(0, 12))

        # ── Ações secundárias ─────────────────────────────────────────────────
        self._btn_open_folder = ctk.CTkButton(
            f, text="Abrir pasta", width=220, height=34,
            fg_color="transparent", border_width=1,
            command=self._open_saved_folder,
        )
        self._btn_open_folder.grid(row=5, column=0, pady=(0, 6))

        ctk.CTkButton(
            f, text="Comprimir outro vídeo", width=220, height=34,
            fg_color="transparent", border_width=1,
            command=self._reset,
        ).grid(row=6, column=0, pady=(0, 16))

        return outer

    # ── State Machine ─────────────────────────────────────────────────────────

    def _go(self, new_state: str):
        self.state = new_state
        for key, frame in self._frames.items():
            if key == new_state:
                frame.grid()
            else:
                frame.grid_remove()

    # ── Handlers — DROP ───────────────────────────────────────────────────────

    def _browse(self):
        paths = _ask_open_files()
        if paths:
            self._load(paths[0])

    def _on_drop(self, event):
        raw = event.data.strip()
        paths = re.findall(r"\{([^}]+)\}|(\S+)", raw)
        for match in paths:
            p = match[0] or match[1]
            if p:
                self._load(p)
                break  # um arquivo por vez

    def _load(self, path: str):
        """Analisa o arquivo, extrai thumbnail e vai para CONFIG. (#10, #17)"""
        p = Path(path).resolve()
        if p.suffix.lower() not in SUPPORTED_EXT:
            messagebox.showerror(
                "Formato inválido",
                f"'{p.name}' não é um vídeo suportado.\n\nFormatos: {', '.join(sorted(SUPPORTED_EXT))}",
            )
            return

        # Feedback visual durante o probe (#17)
        self.configure(cursor="watch")
        self.update()

        try:
            probe = self.handler.probe_video(self.ffprobe, str(p))
        except ProbeError as exc:
            self.configure(cursor="")
            messagebox.showerror("Erro ao analisar vídeo", _friendly_probe_error(str(exc)))
            return
        finally:
            self.configure(cursor="")

        self.input_path  = p
        self.probe       = probe
        self.target_bytes = None

        # Thumbnail em thread separada para não travar a UI (#1)
        self._del_thumb()
        threading.Thread(target=self._fetch_thumbnail, daemon=True).start()

        self._fill_config()
        self._go(_CONFIG)

    def _fetch_thumbnail(self):
        """Extrai thumbnail em background e atualiza a UI quando pronto. (#1)"""
        if not self.input_path:
            return
        fd, tmp_str = tempfile.mkstemp(suffix=".png", prefix="vcomp_thumb_")
        os.close(fd)
        tmp = Path(tmp_str)
        ok = self.handler.get_thumbnail(self.ffmpeg, str(self.input_path), str(tmp))
        if ok:
            self._thumb_path = tmp
            self.after(0, self._show_thumbnail)

    def _show_thumbnail(self):
        """Carrega o PNG extraído e exibe no label. (#1)"""
        if not self._thumb_path or not self._thumb_path.exists():
            return
        try:
            if _PIL_AVAILABLE:
                img = Image.open(str(self._thumb_path))
                img = img.resize((THUMB_W, THUMB_H), Image.LANCZOS)
                ctk_img = ctk.CTkImage(img, size=(THUMB_W, THUMB_H))
                self._thumb_label.configure(image=ctk_img, text="")
                self._thumb_label._ctk_image = ctk_img  # evita garbage collection
            else:
                from tkinter import PhotoImage
                tk_img = PhotoImage(file=str(self._thumb_path))
                # Redimensiona por subsample aproximado
                sw = max(1, tk_img.width()  // THUMB_W)
                sh = max(1, tk_img.height() // THUMB_H)
                s  = max(sw, sh, 1)
                tk_img = tk_img.subsample(s, s)
                self._thumb_label.configure(image=tk_img, text="")
                self._thumb_label._tk_image = tk_img
        except Exception:
            pass

    # ── Handlers — CONFIG ─────────────────────────────────────────────────────

    def _fill_config(self):
        p = self.probe
        self._lbl_name.configure(text=self.input_path.name)
        dur = int(p["duration_s"])
        self._lbl_info.configure(
            text=f"{p['width']}×{p['height']}  •  {dur // 60}:{dur % 60:02d}"
                 f"  •  {p['size_bytes'] / 1048576:.1f} MB  •  {p['codec']}"
        )

        # Estimativa de tempo
        accel = FFmpegHandler._accel_cache or "cpu"
        speed_factor = _SPEED_FACTOR.get(accel, 4)
        est_s = max(1, int(p["duration_s"] / speed_factor))
        unit  = "s" if est_s < 60 else "min"
        est_v = est_s if est_s < 60 else round(est_s / 60, 1)
        self._lbl_estimate.configure(text=f"Tempo estimado: ~{est_v} {unit}  ({accel.upper()})")

        self._lbl_warn.configure(text="")
        self._thumb_label.configure(image=None, text="")

        # Reseta presets e campos
        self._entry_custom.configure(state="disabled")
        self._entry_custom.delete(0, "end")
        self._trim_start = None
        self._trim_end   = None
        self._lbl_trim.configure(text="Sem recorte")
        self._seg_res.set("Original")
        self._seg_quality.set("⚡ Rápido")
        self._max_height = None
        self._quality    = "fast"
        for btn in self._preset_btns:
            btn.configure(fg_color="transparent")

    def _pick_preset(self, label: str, mb: Optional[int]):
        for btn in self._preset_btns:
            btn.configure(fg_color=("gray70", "gray30") if btn.cget("text") == label else "transparent")

        if mb is None:
            self._entry_custom.configure(state="normal")
            self.target_bytes = None
            self._lbl_warn.configure(text="")
        else:
            self._entry_custom.configure(state="disabled")
            self.target_bytes = mb * 1024 * 1024

            # Tamanho estimado do output (#7)
            size_bytes = self.probe["size_bytes"] if self.probe else 0
            target     = self.target_bytes
            est_out_mb = mb * SAFETY_MARGIN
            warn_parts: list[str] = []

            if self.probe and size_bytes <= target:
                warn_parts.append(f"⚠  O vídeo já está abaixo de {mb} MB — qualidade será reduzida.")
            else:
                warn_parts.append(f"Resultado estimado: ≈{est_out_mb:.1f} MB")

            # Sugere H.265 se a compressão for muito agressiva (#13)
            if self.probe:
                duration_s   = max(self.probe["duration_s"], MIN_DURATION_S)
                has_audio    = self.probe["has_audio"]
                audio_bits   = AUDIO_RESERVE_KBPS * 1000 * duration_s if has_audio else 0
                video_kbps   = max(
                    int(((target * 8 * SAFETY_MARGIN) - audio_bits) / duration_s / 1000),
                    MIN_VIDEO_KBPS,
                )
                if video_kbps < H265_KBPS_THRESHOLD:
                    warn_parts.append("  💡 H.265 será usado automaticamente para essa taxa de compressão.")

            self._lbl_warn.configure(text="  ".join(warn_parts))

    def _back(self):
        self._del_thumb()
        self.input_path = None
        self.probe = None
        self._go(_DROP)

    def _open_trim_dialog(self):
        if not self.input_path or not self.probe:
            return
        dialog = VideoTrimDialog(
            parent        = self,
            ffmpeg_path   = self.ffmpeg,
            input_path    = str(self.input_path),
            duration_s    = self.probe["duration_s"],
            initial_start = self._trim_start or 0.0,
            initial_end   = self._trim_end   or self.probe["duration_s"],
        )
        if dialog.result is not None:
            start, end = dialog.result
            # Descarta marcadores redundantes (início ≈ 0 ou fim ≈ duração total)
            self._trim_start = start if start > 0.5 else None
            self._trim_end   = end   if end < self.probe["duration_s"] - 0.5 else None
            if self._trim_start is None and self._trim_end is None:
                self._lbl_trim.configure(text="Sem recorte", text_color="gray")
            else:
                s = _fmt_time(self._trim_start or 0)
                e = _fmt_time(self._trim_end   or self.probe["duration_s"])
                self._lbl_trim.configure(
                    text=f"De {s} até {e}", text_color=("gray10", "gray90"),
                )

    def _clear_trim(self):
        self._trim_start = None
        self._trim_end   = None
        self._lbl_trim.configure(text="Sem recorte", text_color="gray")

    def _on_res_change(self, val: str):
        self._max_height = {"720p": 720, "480p": 480, "360p": 360}.get(val)

    def _on_quality_change(self, val: str):
        self._quality = "good" if "Melhor" in val else "fast"

    def _start_compress(self):
        # Resolve tamanho alvo
        if self.target_bytes is None:
            if self._entry_custom.cget("state") == "disabled":
                messagebox.showwarning("Selecione um tamanho", "Escolha o tamanho máximo desejado.")
                return
            raw = self._entry_custom.get().replace(",", ".").strip()
            try:
                mb = float(raw)
                if mb <= 0:
                    raise ValueError()
                self.target_bytes = int(mb * 1024 * 1024)
            except ValueError:
                messagebox.showerror("Valor inválido", "Informe um tamanho válido em MB (ex: 8.5).")
                return

        # Recorte definido pelo VideoTrimDialog (já validado)
        trim_start = self._trim_start
        trim_end   = self._trim_end

        # Valida espaço em disco (#15)
        tmp_dir   = Path(tempfile.gettempdir())
        free_bytes = shutil.disk_usage(tmp_dir).free
        needed    = self.probe["size_bytes"] if self.probe else 0
        if free_bytes < needed:
            free_mb   = free_bytes / 1048576
            needed_mb = needed    / 1048576
            messagebox.showerror(
                "Espaço insuficiente",
                f"Espaço livre em disco: {free_mb:.0f} MB\n"
                f"Necessário (estimativa): {needed_mb:.0f} MB\n\n"
                "Libere espaço e tente novamente.",
            )
            return

        # Arquivo temporário no diretório do sistema (#14)
        stem = self.input_path.stem
        fd, tmp_str = tempfile.mkstemp(
            suffix=".mp4",
            prefix=f"{stem[:40]}{TEMP_SUFFIX}_",
            dir=tmp_dir,
        )
        os.close(fd)
        self.tmp_path = Path(tmp_str)

        self._trim_start = trim_start
        self._trim_end   = trim_end
        self._use_h265   = False  # ativado automaticamente em build_command via H265_KBPS_THRESHOLD

        self.cancel_evt.clear()
        self._pbar.set(0)
        self._lbl_pct.configure(text="0%")
        self._lbl_stats.configure(text="")
        self._lbl_accel.configure(text="")
        self._btn_cancel.configure(text="Cancelar", state="normal")

        self._lbl_progress_file.configure(text=self.input_path.name)

        self._go(_PROGRESS)

        self.comp_thread = threading.Thread(target=self._worker, daemon=True)
        self.comp_thread.start()

    # ── Worker thread ─────────────────────────────────────────────────────────

    def _worker(self):
        try:
            accel = self.handler.compress(
                ffmpeg_path  = self.ffmpeg,
                input_path   = str(self.input_path),
                output_path  = str(self.tmp_path),
                target_bytes = self.target_bytes,
                probe        = self.probe,
                on_progress  = lambda pct, s: self.after(0, self._on_progress, pct, s),
                cancel       = self.cancel_evt,
                use_h265     = self._use_h265,
                trim_start   = self._trim_start,
                trim_end     = self._trim_end,
                max_height   = self._max_height,
                quality      = self._quality,
            )
            if not self.cancel_evt.is_set():
                self.after(0, self._on_done, accel)
            else:
                self._del_tmp()
                self.after(0, self._go, _CONFIG)
        except CompressionError as exc:
            self._del_tmp()
            self.after(0, self._on_error, str(exc))
        except Exception as exc:
            self._del_tmp()
            self.after(0, self._on_error, f"Erro inesperado:\n{exc}")

    def _on_progress(self, pct: float, stats: str):
        self._pbar.set(pct / 100)
        self._lbl_pct.configure(text=f"{pct:.0f}%")
        self._lbl_stats.configure(text=stats)

    def _on_done(self, accel_label: str = ""):
        self._lbl_accel.configure(text=f"Acelerador: {accel_label}" if accel_label else "")

        out_mb = self.tmp_path.stat().st_size / 1048576
        in_mb  = self.probe["size_bytes"] / 1048576
        reduction = int((1 - out_mb / in_mb) * 100)

        self._lbl_sizes.configure(
            text=f"{in_mb:.1f} MB  →  {out_mb:.1f} MB   ({reduction}% menor)"
        )
        for w in self._results_box.winfo_children():
            w.destroy()

        default_save = self.input_path.parent / f"{self.input_path.stem}_comprimido.mp4"
        self._entry_save_path.delete(0, "end")
        self._entry_save_path.insert(0, str(default_save))
        self._lbl_save_status.configure(text="")
        self._btn_save.configure(text="Salvar", state="normal")

        self._go(_DONE)

    def _on_error(self, msg: str):
        messagebox.showerror("Erro na compressão", f"FFmpeg reportou:\n\n{msg[:600]}")
        self._go(_CONFIG)

    def _cancel(self):
        self.cancel_evt.set()
        self._btn_cancel.configure(text="Cancelando...", state="disabled")

    # ── Handlers — DONE ───────────────────────────────────────────────────────

    def _browse_save_path(self):
        current = self._entry_save_path.get().strip()
        if not current and self.input_path:
            current = str(self.input_path.parent / f"{self.input_path.stem}_comprimido.mp4")
        result = _ask_save_file(current or str(Path.home() / "video_comprimido.mp4"))
        if result:
            self._entry_save_path.delete(0, "end")
            self._entry_save_path.insert(0, result)

    def _save(self):
        if not self.tmp_path or not self.tmp_path.exists():
            self._lbl_save_status.configure(
                text="⚠ Arquivo temporário não encontrado.", text_color="#e57373")
            return

        dest_str = self._entry_save_path.get().strip()
        if not dest_str:
            self._lbl_save_status.configure(
                text="⚠ Informe o caminho de destino.", text_color="#e57373")
            return

        dest = Path(dest_str)
        if not dest.suffix:
            dest = dest.with_suffix(".mp4")

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.tmp_path), str(dest))
            self.tmp_path = None
            self._saved_path = dest
            self._btn_save.configure(text="✓ Salvo!", state="disabled")
            self._lbl_save_status.configure(
                text=f"Salvo em: {dest}", text_color="#4caf50")
        except OSError as exc:
            self._lbl_save_status.configure(
                text=f"⚠ Erro ao salvar: {exc}", text_color="#e57373")

    def _open_saved_folder(self):
        """Abre a pasta do arquivo salvo no explorador. (#5)"""
        if self._saved_path and self._saved_path.exists():
            _open_folder(self._saved_path)
        elif self.input_path:
            _open_folder(self.input_path)

    def _reset(self):
        self._del_tmp()
        self._del_thumb()
        self.input_path   = None
        self.probe        = None
        self.target_bytes = None
        self._saved_path  = None
        self._trim_start  = None
        self._trim_end    = None
        self._max_height  = None
        self._quality     = "fast"
        self._entry_save_path.delete(0, "end")
        self._lbl_save_status.configure(text="")
        self._btn_save.configure(text="Salvar", state="normal")
        self._go(_DROP)

    # ── Limpeza ───────────────────────────────────────────────────────────────

    def _del_tmp(self):
        if self.tmp_path and self.tmp_path.exists():
            try:
                self.tmp_path.unlink()
            except OSError:
                pass
        self.tmp_path = None

    def _del_thumb(self):
        if self._thumb_path and self._thumb_path.exists():
            try:
                self._thumb_path.unlink()
            except OSError:
                pass
        self._thumb_path = None

    def _on_close(self):
        if self.state == _PROGRESS:
            if not messagebox.askyesno(
                "Cancelar compressão?",
                "Uma compressão está em andamento.\nDeseja cancelar e sair?",
            ):
                return
            self.cancel_evt.set()
            if self.comp_thread:
                self.comp_thread.join(timeout=3)
        self._del_tmp()
        self._del_thumb()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# DIALOG: FFMPEG AUSENTE
# ══════════════════════════════════════════════════════════════════════════════
def _show_no_ffmpeg():
    win = ctk.CTk()
    win.title("FFmpeg não encontrado")
    win.geometry("440x260")
    win.resizable(False, False)

    f_bold_l = ctk.CTkFont(size=15, weight="bold")
    ctk.CTkLabel(win, text="FFmpeg não encontrado", font=f_bold_l).pack(pady=(28, 10))

    os_name = platform.system()
    if os_name == "Darwin":
        instructions = "macOS:\n  brew install ffmpeg\n\nou baixe em ffmpeg.org/download"
    elif os_name == "Windows":
        instructions = "Windows:\n  1. Baixe em ffmpeg.org/download\n  2. Extraia e adicione ao PATH"
    else:
        instructions = "Linux:\n  sudo apt install ffmpeg\n  sudo dnf install ffmpeg\n  sudo pacman -S ffmpeg"

    ctk.CTkLabel(
        win,
        text=f"Este app precisa do FFmpeg para funcionar.\n\n{instructions}",
        justify="left", wraplength=380,
    ).pack(padx=30)

    btns = ctk.CTkFrame(win, fg_color="transparent")
    btns.pack(pady=20)
    ctk.CTkButton(
        btns, text="Abrir ffmpeg.org",
        command=lambda: webbrowser.open("https://ffmpeg.org/download.html"),
    ).pack(side="left", padx=6)
    ctk.CTkButton(
        btns, text="Fechar", fg_color="transparent", border_width=1,
        command=win.destroy,
    ).pack(side="left", padx=6)

    win.mainloop()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
def main():
    handler = FFmpegHandler()
    try:
        ffmpeg, ffprobe = handler.find_ffmpeg()
    except FFmpegNotFoundError:
        _show_no_ffmpeg()
        return

    app = VideoCompressorApp(handler, ffmpeg, ffprobe)

    # Abre arquivo passado por argumento ou arrastado pro ícone (#3)
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.exists() and p.suffix.lower() in SUPPORTED_EXT:
            app.after(200, lambda: app._load(str(p)))

    app.mainloop()


if __name__ == "__main__":
    main()
