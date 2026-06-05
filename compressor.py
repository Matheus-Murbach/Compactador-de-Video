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
        pkg for pkg in ("customtkinter", "PIL")
        if importlib.util.find_spec(pkg) is None
    ]
    # PIL é o nome do módulo; o pacote pip é pillow
    pip_names = {"PIL": "pillow"}

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
    ) -> tuple[list[str], str]:
        """Monta o comando FFmpeg com aceleração automática. Retorna (cmd, accel_name). (#13, #16)"""
        duration_s = probe["duration_s"]
        # Duração efetiva considera o corte para calcular bitrate (#16)
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

        # Usa H.265 se explicitamente pedido ou quando o bitrate é muito baixo (#13)
        force_h265 = use_h265 or (video_kbps < H265_KBPS_THRESHOLD)
        accel = self._detect_accel(ffmpeg_path)

        base = [ffmpeg_path, "-y", "-loglevel", "warning"]

        # Parâmetros de recorte antes do input para seek rápido (#16)
        seek_args: list[str] = []
        if trim_start and trim_start > 0:
            seek_args += ["-ss", f"{trim_start:.3f}"]

        end_args: list[str] = []
        if trim_end and trim_end < duration_s:
            end_args += ["-to", f"{trim_end:.3f}"]

        audio = ["-c:a", "aac", "-b:a", f"{AUDIO_RESERVE_KBPS}k"] if has_audio else ["-an"]
        tail  = end_args + audio + ["-movflags", "+faststart",
                                    "-progress", "pipe:2", str(output_path)]

        if force_h265:
            # H.265 sempre via CPU (libx265) — GPU H.265 fica como melhoria futura
            accel_label = "H.265 CPU"
            cmd = base + seek_args + [
                "-i", str(input_path),
                "-c:v", "libx265", "-preset", "ultrafast", "-tag:v", "hvc1",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail

        elif accel == "nvenc":
            accel_label = "GPU NVENC"
            cmd = base + seek_args + [
                "-i", str(input_path),
                "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail

        elif accel == "vaapi":
            accel_label = "GPU VAAPI"
            cmd = base + seek_args + [
                "-hwaccel", "vaapi",
                "-hwaccel_device", "/dev/dri/renderD128",
                "-i", str(input_path),
                "-c:v", "h264_vaapi",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
                "-vf", "format=nv12,hwupload",
            ] + tail

        elif accel == "videotoolbox":
            accel_label = "GPU VideoToolbox"
            cmd = base + seek_args + [
                "-i", str(input_path),
                "-c:v", "h264_videotoolbox",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail

        else:  # cpu — libx264 ultrafast para máxima velocidade
            accel_label = "CPU libx264"
            cmd = base + seek_args + [
                "-i", str(input_path),
                "-c:v", "libx264", "-preset", "ultrafast",
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
    ) -> str:
        """Executa a compressão. Retorna accel_label. Levanta CompressionError se falhar."""
        cmd, accel_label = self.build_command(
            ffmpeg_path, input_path, output_path, target_bytes, probe,
            use_h265=use_h265, trim_start=trim_start, trim_end=trim_end,
        )
        proc = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )

        recent_lines: list[str] = []
        block: list[str] = []

        # Duração efetiva para calcular % considerando o corte (#16)
        eff_duration = max(
            (trim_end or probe["duration_s"]) - (trim_start or 0),
            MIN_DURATION_S,
        )

        try:
            for raw_line in proc.stderr:
                if cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    return accel_label

                line = raw_line.strip()
                recent_lines.append(line)
                if len(recent_lines) > 40:
                    recent_lines.pop(0)

                block.append(line)
                if line.startswith("progress="):
                    parsed = _parse_progress_block(block, eff_duration)
                    if parsed:
                        on_progress(*parsed)
                    block.clear()
        finally:
            if proc.returncode is None:
                proc.wait()

        if proc.returncode != 0 and not cancel.is_set():
            raise CompressionError("\n".join(recent_lines[-15:]))

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


def _parse_time(s: str) -> Optional[float]:
    """Converte mm:ss ou hh:mm:ss em segundos. Retorna None se inválido. (#16)"""
    s = s.strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if not m:
        return None
    parts = [int(x) for x in m.groups(default="0")]
    h, mi, sec = (parts[0], parts[1], parts[2]) if m.group(3) else (0, parts[0], parts[1])
    return h * 3600 + mi * 60 + sec


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

        # Fila de arquivos (#8) — substitui self.input_path único
        self._queue: list[Path]      = []
        self._queue_idx: int         = 0
        self._queue_results: list[str] = []

        # Arquivo atual sendo processado
        self.input_path: Optional[Path] = None
        self.probe:      Optional[dict] = None
        self.target_bytes: Optional[int] = None
        self.accel_label: str = ""

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
        ctk.CTkLabel(inner, text="Clique para abrir um vídeo", font=self._f_bold_l).pack()
        ctk.CTkLabel(
            inner, text="MP4 · MOV · MKV · AVI · WEBM · FLV · WMV",
            font=self._f_small, text_color="gray",
        ).pack(pady=(4, 16))

        ctk.CTkButton(
            inner, text="Abrir Vídeo(s)", width=200, height=44, font=self._f_bold_l,
            command=self._browse,
        ).pack()

        # Lista compacta de arquivos na fila (#8)
        self._lbl_queue_count = ctk.CTkLabel(
            inner, text="", font=self._f_small, text_color="gray",
        )
        self._lbl_queue_count.pack(pady=(12, 0))

        self._queue_listbox = ctk.CTkScrollableFrame(inner, width=420, height=80)
        # não chama .pack() aqui — _update_queue_display controla a visibilidade
        self._queue_listbox.grid_columnconfigure(0, weight=1)
        self._queue_listbox_labels: list[ctk.CTkLabel] = []

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(pady=(6, 0))
        ctk.CTkButton(
            btn_row, text="Limpar fila", width=120, height=30,
            fg_color="transparent", border_width=1, font=self._f_small,
            command=self._clear_queue,
        ).pack(side="left", padx=4)
        self._btn_go_config = ctk.CTkButton(
            btn_row, text="Continuar →", width=140, height=30,
            font=self._f_small,
            command=self._queue_to_config,
        )
        self._btn_go_config.pack(side="left", padx=4)

        self._update_queue_display()

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

        # Conteúdo rolável para acomodar todos os campos (#1, #13, #16)
        scroll = ctk.CTkScrollableFrame(outer)
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        f = scroll  # alias conveniente

        # ── Cabeçalho: thumbnail + info ──────────────────────────────────────
        head = ctk.CTkFrame(f, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        head.grid_columnconfigure(1, weight=1)

        # Thumbnail (#1)
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

        # Estimativa de tempo (#2)
        self._lbl_estimate = ctk.CTkLabel(
            head, text="", font=self._f_small, text_color="gray",
            anchor="w", justify="left",
        )
        self._lbl_estimate.grid(row=2, column=1, sticky="ew")

        # Aviso / tamanho estimado (#7)
        self._lbl_warn = ctk.CTkLabel(
            f, text="", font=self._f_small, text_color="#f0a500",
        )
        self._lbl_warn.grid(row=1, column=0, pady=(0, 4), padx=8)

        # ── Presets ───────────────────────────────────────────────────────────
        ctk.CTkLabel(f, text="Tamanho máximo:", font=self._f_bold).grid(
            row=2, column=0, sticky="w", padx=12, pady=(4, 4),
        )

        grid = ctk.CTkFrame(f, fg_color="transparent")
        grid.grid(row=3, column=0, padx=8, sticky="ew")
        grid.grid_columnconfigure((0, 1), weight=1)

        self._preset_btns: list[ctk.CTkButton] = []
        for i, (label, mb) in enumerate(PRESETS):
            row, col = divmod(i, 2)
            btn = ctk.CTkButton(
                grid, text=label, height=34,
                fg_color="transparent", border_width=1,
                font=self._f_small,
                command=lambda lbl=label, size=mb: self._pick_preset(lbl, size),
            )
            btn.grid(row=row, column=col, padx=3, pady=2, sticky="ew")
            self._preset_btns.append(btn)

        custom_row = ctk.CTkFrame(f, fg_color="transparent")
        custom_row.grid(row=4, column=0, padx=8, pady=(4, 0), sticky="w")
        self._entry_custom = ctk.CTkEntry(
            custom_row, placeholder_text="Tamanho em MB (ex: 8.5)",
            width=200, state="disabled",
        )
        self._entry_custom.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(custom_row, text="MB").pack(side="left")

        # ── H.265 (#13) ───────────────────────────────────────────────────────
        self._chk_h265_var = ctk.BooleanVar(value=False)
        self._chk_h265 = ctk.CTkCheckBox(
            f,
            text="Usar H.265 (melhor qualidade, mais lento — recomendado para compressões >90%)",
            variable=self._chk_h265_var,
            font=self._f_small,
        )
        self._chk_h265.grid(row=5, column=0, sticky="w", padx=12, pady=(8, 0))

        # ── Recorte (#16) ─────────────────────────────────────────────────────
        trim_frame = ctk.CTkFrame(f, fg_color="transparent")
        trim_frame.grid(row=6, column=0, sticky="w", padx=8, pady=(6, 0))

        ctk.CTkLabel(trim_frame, text="Recortar (opcional):", font=self._f_small).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(trim_frame, text="De").pack(side="left", padx=(0, 4))
        self._entry_trim_start = ctk.CTkEntry(trim_frame, placeholder_text="0:00", width=70)
        self._entry_trim_start.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(trim_frame, text="Até").pack(side="left", padx=(0, 4))
        self._entry_trim_end = ctk.CTkEntry(trim_frame, placeholder_text="mm:ss", width=70)
        self._entry_trim_end.pack(side="left")

        # ── Botões de ação ────────────────────────────────────────────────────
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
        f = ctk.CTkFrame(self)
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.grid(row=0, column=0)

        ctk.CTkLabel(
            inner, text="✓", font=ctk.CTkFont(size=56, weight="bold"), text_color="#4caf50",
        ).pack(pady=(0, 8))
        ctk.CTkLabel(inner, text="Concluído!", font=self._f_bold_l).pack()

        # Resumo de resultados (fila: lista; único: linha simples) (#8)
        self._lbl_sizes = ctk.CTkLabel(inner, text="", font=self._f_small, text_color="gray")
        self._lbl_sizes.pack(pady=(4, 4))

        self._results_box = ctk.CTkScrollableFrame(inner, width=420, height=70)
        self._results_box.pack(pady=(0, 16))
        self._results_box.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            inner, text="Salvar Como...", width=220, height=44, font=self._f_bold_l,
            command=self._save,
        ).pack(pady=(0, 6))

        # Abrir pasta (#5)
        self._btn_open_folder = ctk.CTkButton(
            inner, text="Abrir pasta", width=220, height=34,
            fg_color="transparent", border_width=1,
            command=self._open_saved_folder,
        )
        self._btn_open_folder.pack(pady=(0, 6))

        ctk.CTkButton(
            inner, text="Comprimir outro vídeo", width=220, height=34,
            fg_color="transparent", border_width=1,
            command=self._reset,
        ).pack()

        return f

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
        exts = " ".join(f"*{e}" for e in sorted(SUPPORTED_EXT))
        paths = filedialog.askopenfilenames(
            title="Selecionar vídeo(s)",
            filetypes=[("Vídeos", exts), ("Todos os arquivos", "*.*")],
        )
        for p in paths:
            self._enqueue(p)

    def _on_drop(self, event):
        # tkinterdnd2 entrega múltiplos arquivos separados por espaço ou em {}
        raw = event.data.strip()
        paths = re.findall(r"\{([^}]+)\}|(\S+)", raw)
        for match in paths:
            p = match[0] or match[1]
            if p:
                self._enqueue(p)

    def _enqueue(self, path: str):
        p = Path(path).resolve()
        if p.suffix.lower() not in SUPPORTED_EXT:
            messagebox.showerror(
                "Formato inválido",
                f"'{p.name}' não é um vídeo suportado.\n\nFormatos: {', '.join(sorted(SUPPORTED_EXT))}",
            )
            return
        if p not in self._queue:
            self._queue.append(p)
        self._update_queue_display()

    def _clear_queue(self):
        self._queue.clear()
        self._update_queue_display()

    def _update_queue_display(self):
        n = len(self._queue)
        # Mostra contagem e lista
        if n == 0:
            self._lbl_queue_count.configure(text="")
            self._queue_listbox.pack_forget()
            for lbl in self._queue_listbox_labels:
                lbl.destroy()
            self._queue_listbox_labels.clear()
            self._btn_go_config.configure(state="disabled")
        else:
            self._lbl_queue_count.configure(text=f"{n} arquivo(s) na fila")
            self._queue_listbox.pack(pady=(4, 0))
            # Rebuild lista
            for lbl in self._queue_listbox_labels:
                lbl.destroy()
            self._queue_listbox_labels.clear()
            for i, p in enumerate(self._queue):
                lbl = ctk.CTkLabel(
                    self._queue_listbox, text=f"{i+1}. {p.name}",
                    font=self._f_small, anchor="w",
                )
                lbl.grid(row=i, column=0, sticky="ew", padx=6, pady=1)
                self._queue_listbox_labels.append(lbl)
            self._btn_go_config.configure(state="normal")

    def _queue_to_config(self):
        """Carrega o primeiro arquivo da fila e vai para CONFIG."""
        if not self._queue:
            return
        self._queue_idx = 0
        self._load_from_queue()

    def _load_from_queue(self):
        """Carrega self._queue[self._queue_idx] com probe + thumbnail."""
        if self._queue_idx >= len(self._queue):
            return
        p = self._queue[self._queue_idx]
        self._load(str(p))

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
        # Informação principal
        queue_info = (
            f"  [{self._queue_idx + 1}/{len(self._queue)}]"
            if len(self._queue) > 1 else ""
        )
        self._lbl_name.configure(text=self.input_path.name + queue_info)
        dur = int(p["duration_s"])
        self._lbl_info.configure(
            text=f"{p['width']}×{p['height']}  •  {dur // 60}:{dur % 60:02d}"
                 f"  •  {p['size_bytes'] / 1048576:.1f} MB  •  {p['codec']}"
        )

        # Estimativa de tempo (#2)
        accel = FFmpegHandler._accel_cache or "cpu"
        speed_factor = _SPEED_FACTOR.get(accel, 4)
        est_s = max(1, int(p["duration_s"] / speed_factor))
        unit  = "s" if est_s < 60 else "min"
        est_v = est_s if est_s < 60 else round(est_s / 60, 1)
        self._lbl_estimate.configure(text=f"Tempo estimado: ~{est_v} {unit}  ({accel.upper()})")

        self._lbl_warn.configure(text="")
        # Limpa thumbnail placeholder
        self._thumb_label.configure(image=None, text="")

        # Reseta presets e campos
        self._entry_custom.configure(state="disabled")
        self._entry_custom.delete(0, "end")
        self._entry_trim_start.delete(0, "end")
        self._entry_trim_end.delete(0, "end")
        self._chk_h265_var.set(False)
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
                    warn_parts.append("  💡 H.265 recomendado para essa taxa de compressão.")
                    self._chk_h265_var.set(True)

            self._lbl_warn.configure(text="  ".join(warn_parts))

    def _back(self):
        self._del_thumb()
        if len(self._queue) > 1:
            # Volta para a fila no DROP
            self.input_path = None
            self.probe = None
            self._go(_DROP)
        else:
            self._queue.clear()
            self._update_queue_display()
            self.input_path = None
            self.probe = None
            self._go(_DROP)

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

        # Valida recorte (#16)
        trim_start: Optional[float] = None
        trim_end:   Optional[float] = None
        ts_raw = self._entry_trim_start.get().strip()
        te_raw = self._entry_trim_end.get().strip()
        if ts_raw:
            trim_start = _parse_time(ts_raw)
            if trim_start is None:
                messagebox.showerror("Recorte inválido", "Início inválido. Use o formato mm:ss (ex: 0:30).")
                return
        if te_raw:
            trim_end = _parse_time(te_raw)
            if trim_end is None:
                messagebox.showerror("Recorte inválido", "Fim inválido. Use o formato mm:ss (ex: 1:20).")
                return
        if trim_start is not None and trim_end is not None:
            if trim_end <= trim_start:
                messagebox.showerror("Recorte inválido", "'Até' deve ser maior que 'De'.")
                return
            if self.probe and trim_end > self.probe["duration_s"]:
                messagebox.showerror("Recorte inválido",
                    f"'Até' excede a duração do vídeo ({int(self.probe['duration_s'])}s).")
                return

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
        self._use_h265   = self._chk_h265_var.get()

        self.cancel_evt.clear()
        self._pbar.set(0)
        self._lbl_pct.configure(text="0%")
        self._lbl_stats.configure(text="")
        self._lbl_accel.configure(text="")
        self._btn_cancel.configure(text="Cancelar", state="normal")

        # Informação de arquivo atual na fila (#8)
        q_info = (f"Arquivo {self._queue_idx + 1}/{len(self._queue)}: "
                  if len(self._queue) > 1 else "")
        self._lbl_progress_file.configure(text=f"{q_info}{self.input_path.name}")

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
        # Atualiza o label do acelerador (#4)
        self._lbl_accel.configure(text=f"Acelerador: {accel_label}" if accel_label else "")

        out_mb = self.tmp_path.stat().st_size / 1048576
        in_mb  = self.probe["size_bytes"] / 1048576
        reduction = int((1 - out_mb / in_mb) * 100)
        result_line = f"{self.input_path.name}: {in_mb:.1f} MB → {out_mb:.1f} MB ({reduction}% menor)"
        self._queue_results.append(result_line)

        # Se há mais arquivos na fila, processa o próximo (#8)
        self._queue_idx += 1
        if self._queue_idx < len(self._queue):
            # Salva o arquivo atual automaticamente no mesmo diretório do input
            auto_dest = self.input_path.parent / f"{self.input_path.stem}_comprimido.mp4"
            try:
                shutil.move(str(self.tmp_path), str(auto_dest))
                self.tmp_path = None
            except OSError:
                pass
            # Carrega próximo arquivo
            self._load_from_queue()
            if self.state == _CONFIG:
                self._start_compress()
            return

        # Último (ou único) arquivo — vai para DONE
        self._lbl_sizes.configure(
            text="" if len(self._queue_results) > 1
            else f"{in_mb:.1f} MB  →  {out_mb:.1f} MB   ({reduction}% menor)"
        )
        # Preenche lista de resultados para fila (#8)
        for w in self._results_box.winfo_children():
            w.destroy()
        if len(self._queue_results) > 1:
            for i, r in enumerate(self._queue_results):
                ctk.CTkLabel(
                    self._results_box, text=r, font=self._f_small, anchor="w",
                ).grid(row=i, column=0, sticky="ew", padx=6, pady=1)

        self._go(_DONE)

    def _on_error(self, msg: str):
        messagebox.showerror("Erro na compressão", f"FFmpeg reportou:\n\n{msg[:600]}")
        self._go(_CONFIG)

    def _cancel(self):
        self.cancel_evt.set()
        self._btn_cancel.configure(text="Cancelando...", state="disabled")

    # ── Handlers — DONE ───────────────────────────────────────────────────────

    def _save(self):
        if not self.tmp_path or not self.tmp_path.exists():
            messagebox.showerror("Arquivo não encontrado", "O arquivo comprimido não está mais disponível.")
            return

        default = f"{self.input_path.stem}_comprimido.mp4"
        dest = filedialog.asksaveasfilename(
            title="Salvar vídeo comprimido",
            defaultextension=".mp4",
            initialfile=default,
            filetypes=[("Vídeo MP4", "*.mp4"), ("Todos os arquivos", "*.*")],
        )
        if not dest:
            return

        try:
            shutil.move(str(self.tmp_path), dest)
            self.tmp_path = None
            self._saved_path = Path(dest)
            messagebox.showinfo("Salvo!", f"Vídeo salvo em:\n{dest}")
        except OSError as exc:
            messagebox.showerror("Erro ao salvar", f"Não foi possível salvar:\n{exc}")

    def _open_saved_folder(self):
        """Abre a pasta do arquivo salvo no explorador. (#5)"""
        if self._saved_path and self._saved_path.exists():
            _open_folder(self._saved_path)
        elif self.input_path:
            _open_folder(self.input_path)

    def _reset(self):
        self._del_tmp()
        self._del_thumb()
        self._queue.clear()
        self._queue_idx = 0
        self._queue_results.clear()
        self._update_queue_display()
        self.input_path  = None
        self.probe       = None
        self.target_bytes = None
        self._saved_path = None
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
            app.after(150, lambda: app._enqueue(str(p)))
            app.after(300, app._queue_to_config)

    app.mainloop()


if __name__ == "__main__":
    main()
