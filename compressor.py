#!/usr/bin/env python3
"""
Compactador de Video
Duplo clique para abrir. Requer FFmpeg instalado no sistema.
"""

# ══════════════════════════════════════════════════════════════════════════════
# BOOTSTRAP — instala customtkinter automaticamente na primeira execução
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

    if importlib.util.find_spec("customtkinter") is None:
        try:
            import tkinter as _tk
            _root = _tk.Tk()
            _root.title("Compactador de Video")
            _root.geometry("360x80")
            _root.resizable(False, False)
            _tk.Label(_root, text="Instalando dependencias (primeira execucao)...", pady=20).pack()
            _root.update()
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "customtkinter", "--quiet"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _root.destroy()
        except subprocess.CalledProcessError:
            try:
                import tkinter.messagebox as _mb
                _mb.showerror(
                    "Erro de instalacao",
                    "Nao foi possivel instalar customtkinter.\n"
                    "Execute no terminal:\n  pip install customtkinter",
                )
            except Exception:
                print("Erro: execute 'pip install customtkinter'")
            sys.exit(1)
        except ImportError:
            print("tkinter nao encontrado. Instale Python com suporte a tkinter.")
            sys.exit(1)


_bootstrap()

# ══════════════════════════════════════════════════════════════════════════════
# IMPORTS
# ══════════════════════════════════════════════════════════════════════════════
import json
import os
import platform
import shutil
import threading
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from tkinter import filedialog, messagebox

# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTES
# ══════════════════════════════════════════════════════════════════════════════
SAFETY_MARGIN = 0.95           # 5% de margem para nao ultrapassar o limite
AUDIO_RESERVE_KBPS = 128       # reserva de bitrate para audio
MIN_VIDEO_KBPS = 50            # bitrate minimo de video
MIN_DURATION_S = 0.5           # guarda contra divisao por zero
TEMP_SUFFIX = "_vcomp_tmp"
SUPPORTED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}

# Tamanhos em MB por plataforma
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

APP_W, APP_H = 500, 460

# Fontes são criadas dentro de _build_ui(), depois que o Tk está ativo.
# CTkFont é subclasse de tkinter.font.Font e exige uma janela inicializada.


# ══════════════════════════════════════════════════════════════════════════════
# EXCECOES
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
    """Toda a logica de FFmpeg — sem codigo de UI."""

    @staticmethod
    def find_ffmpeg() -> tuple[str, str]:
        """Retorna (ffmpeg, ffprobe) ou levanta FFmpegNotFoundError."""
        script_dir = Path(sys.argv[0]).resolve().parent

        pairs = [("ffmpeg", "ffprobe")]
        if platform.system() == "Windows":
            pairs += [
                (r"C:\ffmpeg\bin\ffmpeg.exe",   r"C:\ffmpeg\bin\ffprobe.exe"),
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

        raise FFmpegNotFoundError("FFmpeg nao encontrado no sistema.")

    @staticmethod
    def probe_video(ffprobe_path: str, input_path: str) -> dict:
        """Retorna metadados do video via ffprobe JSON."""
        cmd = [
            ffprobe_path, "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-show_format",
            str(input_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            raise ProbeError(f"Falha ao analisar video: {exc}")

        fmt = data.get("format", {})
        streams = data.get("streams", [])

        # Duracao: preferir format.duration (mais confiavel)
        duration_s = float(fmt.get("duration") or 0)
        if duration_s <= 0:
            for s in streams:
                d = float(s.get("duration") or 0)
                if d > 0:
                    duration_s = d
                    break

        if duration_s <= 0:
            raise ProbeError("Nao foi possivel determinar a duracao do video.")

        vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if vstream is None:
            raise ProbeError("Nenhuma faixa de video encontrada no arquivo.")

        return {
            "duration_s": duration_s,
            "width": int(vstream.get("width") or 0),
            "height": int(vstream.get("height") or 0),
            "codec": vstream.get("codec_name", "?"),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "size_bytes": int(fmt.get("size") or Path(input_path).stat().st_size),
        }

    @staticmethod
    def _detect_accel(ffmpeg_path: str) -> str:
        """Detecta melhor acelerador de hardware disponivel."""
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
                    return "nvenc"

            if "h264_vaapi" in encoders and Path("/dev/dri/renderD128").exists():
                return "vaapi"

            if "h264_videotoolbox" in encoders:
                return "videotoolbox"

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        return "cpu"

    def build_command(
        self,
        ffmpeg_path: str,
        input_path: str,
        output_path: str,
        target_bytes: int,
        probe: dict,
    ) -> list[str]:
        """Monta o comando FFmpeg com aceleracao automatica."""
        duration_s = max(probe["duration_s"], MIN_DURATION_S)
        has_audio = probe["has_audio"]

        audio_bits = AUDIO_RESERVE_KBPS * 1000 * duration_s if has_audio else 0
        video_kbps = max(
            int(((target_bytes * 8 * SAFETY_MARGIN) - audio_bits) / duration_s / 1000),
            MIN_VIDEO_KBPS,
        )
        maxrate = int(video_kbps * 1.5)
        bufsize = int(video_kbps * 3)

        accel = self._detect_accel(ffmpeg_path)

        base = [ffmpeg_path, "-y"]
        audio = ["-c:a", "aac", "-b:a", f"{AUDIO_RESERVE_KBPS}k"] if has_audio else ["-an"]
        tail = audio + ["-movflags", "+faststart", "-progress", "pipe:2", str(output_path)]

        if accel == "nvenc":
            cmd = base + [
                "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                "-i", str(input_path),
                "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail
        elif accel == "vaapi":
            cmd = base + [
                "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
                "-hwaccel_device", "/dev/dri/renderD128",
                "-i", str(input_path),
                "-c:v", "h264_vaapi",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
                "-vf", "format=nv12,hwupload",
            ] + tail
        elif accel == "videotoolbox":
            cmd = base + [
                "-i", str(input_path),
                "-c:v", "h264_videotoolbox",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail
        else:  # cpu — libx264 ultrafast para maxima velocidade
            cmd = base + [
                "-i", str(input_path),
                "-c:v", "libx264", "-preset", "ultrafast",
                "-b:v", f"{video_kbps}k", "-maxrate", f"{maxrate}k", "-bufsize", f"{bufsize}k",
            ] + tail

        return cmd

    def compress(
        self,
        ffmpeg_path: str,
        input_path: str,
        output_path: str,
        target_bytes: int,
        probe: dict,
        on_progress: Callable[[float, str], None],
        cancel: threading.Event,
    ) -> None:
        """Executa a compressao. Levanta CompressionError se falhar."""
        cmd = self.build_command(ffmpeg_path, input_path, output_path, target_bytes, probe)
        proc = subprocess.Popen(
            cmd, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )

        recent_lines: list[str] = []
        block: list[str] = []

        try:
            for raw_line in proc.stderr:
                if cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    return

                line = raw_line.strip()
                recent_lines.append(line)
                if len(recent_lines) > 40:
                    recent_lines.pop(0)

                block.append(line)
                if line.startswith("progress="):
                    parsed = _parse_progress_block(block, probe["duration_s"])
                    if parsed:
                        on_progress(*parsed)
                    block.clear()
        finally:
            # Garante que o processo foi finalizado em qualquer caminho de saida
            if proc.returncode is None:
                proc.wait()

        if proc.returncode != 0 and not cancel.is_set():
            raise CompressionError("\n".join(recent_lines[-15:]))


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

    elapsed = max(0, int(out_us)) / 1_000_000
    pct = min(100.0, elapsed / max(duration_s, 0.001) * 100)

    fps = data.get("fps", "?")
    speed = data.get("speed", "?")
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


# ══════════════════════════════════════════════════════════════════════════════
# UI — MAQUINA DE ESTADOS
# ══════════════════════════════════════════════════════════════════════════════
_DROP     = "drop"
_CONFIG   = "config"
_PROGRESS = "progress"
_DONE     = "done"


class VideoCompressorApp(ctk.CTk):

    def __init__(self, handler: FFmpegHandler, ffmpeg: str, ffprobe: str):
        super().__init__()
        self.handler = handler
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe

        self.state = _DROP
        self.input_path: Optional[Path] = None
        self.probe: Optional[dict] = None
        self.target_bytes: Optional[int] = None
        self.cancel_evt = threading.Event()
        self.comp_thread: Optional[threading.Thread] = None
        self.tmp_path: Optional[Path] = None

        self.title("Compactador de Video")
        self.geometry(f"{APP_W}x{APP_H}")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._go(_DROP)

    # ── Construcao da UI ──────────────────────────────────────────────────────

    def _build_ui(self):
        # Fontes instanciadas aqui — Tk ja esta ativo via super().__init__()
        self._f_bold   = ctk.CTkFont(weight="bold")
        self._f_bold_l = ctk.CTkFont(size=15, weight="bold")
        self._f_bold_xl = ctk.CTkFont(size=26, weight="bold")
        self._f_small  = ctk.CTkFont(size=11)
        self._f_icon   = ctk.CTkFont(size=52)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._frames = {
            _DROP:     self._make_drop_frame(),
            _CONFIG:   self._make_config_frame(),
            _PROGRESS: self._make_progress_frame(),
            _DONE:     self._make_done_frame(),
        }
        for f in self._frames.values():
            f.grid(row=0, column=0, sticky="nsew", padx=24, pady=24)

    def _make_drop_frame(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self)
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.grid(row=0, column=0)

        ctk.CTkLabel(inner, text="▶", font=self._f_icon).pack(pady=(0, 14))
        ctk.CTkLabel(inner, text="Clique para abrir um video", font=self._f_bold_l).pack()
        ctk.CTkLabel(
            inner,
            text="MP4 · MOV · MKV · AVI · WEBM · FLV · WMV",
            font=self._f_small, text_color="gray",
        ).pack(pady=(6, 24))
        ctk.CTkButton(
            inner, text="Abrir Video", width=200, height=44, font=self._f_bold_l,
            command=self._browse,
        ).pack()

        # Drag-and-drop — silenciosamente opcional (requer tkinterdnd2)
        try:
            from tkinterdnd2 import DND_FILES  # type: ignore
            f.drop_target_register(DND_FILES)
            f.dnd_bind("<<Drop>>", lambda e: self._load(e.data.strip().strip("{}")))
        except Exception:
            pass

        return f

    def _make_config_frame(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self)
        f.grid_columnconfigure(0, weight=1)

        self._lbl_name = ctk.CTkLabel(f, text="", font=self._f_bold, wraplength=440)
        self._lbl_name.grid(row=0, column=0, pady=(16, 2), padx=20)

        self._lbl_info = ctk.CTkLabel(f, text="", font=self._f_small, text_color="gray")
        self._lbl_info.grid(row=1, column=0, pady=(0, 2), padx=20)

        self._lbl_warn = ctk.CTkLabel(f, text="", font=self._f_small, text_color="#f0a500")
        self._lbl_warn.grid(row=2, column=0, pady=(0, 8), padx=20)

        ctk.CTkLabel(f, text="Tamanho maximo:", font=self._f_bold).grid(
            row=3, column=0, sticky="w", padx=24, pady=(0, 6)
        )

        grid = ctk.CTkFrame(f, fg_color="transparent")
        grid.grid(row=4, column=0, padx=20, sticky="ew")
        grid.grid_columnconfigure((0, 1), weight=1)

        self._preset_btns: list[ctk.CTkButton] = []
        for i, (label, mb) in enumerate(PRESETS):
            row, col = divmod(i, 2)
            btn = ctk.CTkButton(
                grid, text=label, height=36,
                fg_color="transparent", border_width=1,
                font=self._f_small,
                command=lambda lbl=label, size=mb: self._pick_preset(lbl, size),
            )
            btn.grid(row=row, column=col, padx=3, pady=2, sticky="ew")
            self._preset_btns.append(btn)

        custom_row = ctk.CTkFrame(f, fg_color="transparent")
        custom_row.grid(row=5, column=0, padx=20, pady=(6, 0), sticky="w")
        self._entry_custom = ctk.CTkEntry(
            custom_row, placeholder_text="Tamanho em MB (ex: 8.5)", width=220, state="disabled",
        )
        self._entry_custom.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(custom_row, text="MB").pack(side="left")

        actions = ctk.CTkFrame(f, fg_color="transparent")
        actions.grid(row=6, column=0, padx=20, pady=(12, 16), sticky="ew")
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

        return f

    def _make_progress_frame(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self)
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.grid(row=0, column=0, padx=20)

        ctk.CTkLabel(inner, text="Comprimindo...", font=self._f_bold_l).pack(pady=(0, 20))

        self._pbar = ctk.CTkProgressBar(inner, width=400, height=18)
        self._pbar.set(0)
        self._pbar.pack()

        self._lbl_pct = ctk.CTkLabel(inner, text="0%", font=self._f_bold_xl)
        self._lbl_pct.pack(pady=(10, 4))

        self._lbl_stats = ctk.CTkLabel(inner, text="", font=self._f_small, text_color="gray")
        self._lbl_stats.pack()

        self._btn_cancel = ctk.CTkButton(
            inner, text="Cancelar", width=130, height=36,
            fg_color="transparent", border_width=1,
            command=self._cancel,
        )
        self._btn_cancel.pack(pady=(22, 0))

        return f

    def _make_done_frame(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self)
        f.grid_rowconfigure(0, weight=1)
        f.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.grid(row=0, column=0)

        ctk.CTkLabel(inner, text="✓", font=ctk.CTkFont(size=56, weight="bold"), text_color="#4caf50").pack(pady=(0, 10))
        ctk.CTkLabel(inner, text="Concluido!", font=self._f_bold_l).pack()

        self._lbl_sizes = ctk.CTkLabel(inner, text="", font=self._f_small, text_color="gray")
        self._lbl_sizes.pack(pady=(6, 20))

        ctk.CTkButton(
            inner, text="Salvar Como...", width=220, height=44, font=self._f_bold_l,
            command=self._save,
        ).pack(pady=(0, 10))
        ctk.CTkButton(
            inner, text="Comprimir outro video", width=220, height=36,
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
        path = filedialog.askopenfilename(
            title="Selecionar video",
            filetypes=[("Videos", exts), ("Todos os arquivos", "*.*")],
        )
        if path:
            self._load(path)

    def _load(self, path: str):
        p = Path(path).resolve()
        if p.suffix.lower() not in SUPPORTED_EXT:
            messagebox.showerror(
                "Formato invalido",
                f"Arquivo '{p.name}' nao e um video suportado.\n\nFormatos: {', '.join(sorted(SUPPORTED_EXT))}",
            )
            return
        try:
            probe = self.handler.probe_video(self.ffprobe, str(p))
        except ProbeError as exc:
            messagebox.showerror("Erro ao analisar video", str(exc))
            return

        self.input_path = p
        self.probe = probe
        self.target_bytes = None
        self._fill_config()
        self._go(_CONFIG)

    # ── Handlers — CONFIG ─────────────────────────────────────────────────────

    def _fill_config(self):
        p = self.probe
        self._lbl_name.configure(text=self.input_path.name)
        dur = int(p["duration_s"])
        self._lbl_info.configure(
            text=f"{p['width']}x{p['height']}  •  {dur // 60}:{dur % 60:02d}  •  {p['size_bytes'] / 1048576:.1f} MB  •  {p['codec']}"
        )
        self._lbl_warn.configure(text="")
        self._entry_custom.configure(state="disabled")
        self._entry_custom.delete(0, "end")
        for btn in self._preset_btns:
            btn.configure(fg_color="transparent")

    def _pick_preset(self, label: str, mb: Optional[int]):
        for btn in self._preset_btns:
            selected = btn.cget("text") == label
            btn.configure(fg_color=("gray70", "gray30") if selected else "transparent")

        if mb is None:
            self._entry_custom.configure(state="normal")
            self.target_bytes = None
        else:
            self._entry_custom.configure(state="disabled")
            self.target_bytes = mb * 1024 * 1024
            # Avisa se o arquivo ja esta abaixo do alvo
            if self.probe["size_bytes"] <= self.target_bytes:
                self._lbl_warn.configure(
                    text=f"⚠  O video ja esta abaixo de {mb} MB. Compressao ira reduzir a qualidade."
                )
            else:
                self._lbl_warn.configure(text="")

    def _back(self):
        self.input_path = None
        self.probe = None
        self._go(_DROP)

    def _start_compress(self):
        # Resolve tamanho alvo
        if self.target_bytes is None:
            # Verifica se "Personalizado" esta selecionado (entry habilitada) ou nada foi escolhido
            if self._entry_custom.cget("state") == "disabled":
                messagebox.showwarning("Selecione um tamanho", "Escolha o tamanho maximo desejado.")
                return
            raw = self._entry_custom.get().replace(",", ".").strip()
            try:
                mb = float(raw)
                if mb <= 0:
                    raise ValueError()
                self.target_bytes = int(mb * 1024 * 1024)
            except ValueError:
                messagebox.showerror("Valor invalido", "Informe um tamanho valido em MB (ex: 8.5).")
                return

        # Arquivo temporario de saida (mesmo diretorio do input)
        stem = self.input_path.stem
        hex4 = os.urandom(4).hex()
        self.tmp_path = self.input_path.parent / f"{stem}{TEMP_SUFFIX}_{hex4}.mp4"

        self.cancel_evt.clear()
        self._pbar.set(0)
        self._lbl_pct.configure(text="0%")
        self._lbl_stats.configure(text="")
        self._btn_cancel.configure(text="Cancelar", state="normal")
        self._go(_PROGRESS)

        self.comp_thread = threading.Thread(target=self._worker, daemon=True)
        self.comp_thread.start()

    # ── Worker thread ─────────────────────────────────────────────────────────

    def _worker(self):
        try:
            self.handler.compress(
                ffmpeg_path=self.ffmpeg,
                input_path=str(self.input_path),
                output_path=str(self.tmp_path),
                target_bytes=self.target_bytes,
                probe=self.probe,
                on_progress=lambda pct, s: self.after(0, self._on_progress, pct, s),
                cancel=self.cancel_evt,
            )
            if not self.cancel_evt.is_set():
                self.after(0, self._on_done)
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

    def _on_done(self):
        out_mb = self.tmp_path.stat().st_size / 1048576
        in_mb = self.probe["size_bytes"] / 1048576
        reduction = int((1 - out_mb / in_mb) * 100)
        self._lbl_sizes.configure(
            text=f"{in_mb:.1f} MB  →  {out_mb:.1f} MB   ({reduction}% menor)"
        )
        self._go(_DONE)

    def _on_error(self, msg: str):
        messagebox.showerror("Erro na compressao", f"FFmpeg reportou:\n\n{msg[:600]}")
        self._go(_CONFIG)

    def _cancel(self):
        self.cancel_evt.set()
        self._btn_cancel.configure(text="Cancelando...", state="disabled")

    # ── Handlers — DONE ───────────────────────────────────────────────────────

    def _save(self):
        if not self.tmp_path or not self.tmp_path.exists():
            messagebox.showerror("Arquivo nao encontrado", "O arquivo comprimido nao esta mais disponivel.")
            return

        default = f"{self.input_path.stem}_comprimido.mp4"
        dest = filedialog.asksaveasfilename(
            title="Salvar video comprimido",
            defaultextension=".mp4",
            initialfile=default,
            filetypes=[("Video MP4", "*.mp4"), ("Todos os arquivos", "*.*")],
        )
        if not dest:
            return

        try:
            shutil.move(str(self.tmp_path), dest)
            self.tmp_path = None
            messagebox.showinfo("Salvo!", f"Video salvo em:\n{dest}")
        except OSError as exc:
            messagebox.showerror("Erro ao salvar", f"Nao foi possivel salvar:\n{exc}")

    def _reset(self):
        self._del_tmp()
        self.input_path = None
        self.probe = None
        self.target_bytes = None
        self._go(_DROP)

    # ── Limpeza ───────────────────────────────────────────────────────────────

    def _del_tmp(self):
        if self.tmp_path and self.tmp_path.exists():
            try:
                self.tmp_path.unlink()
            except OSError:
                pass
        self.tmp_path = None

    def _on_close(self):
        if self.state == _PROGRESS:
            if not messagebox.askyesno("Cancelar compressao?",
                                       "Uma compressao esta em andamento.\nDeseja cancelar e sair?"):
                return
            self.cancel_evt.set()
            if self.comp_thread:
                self.comp_thread.join(timeout=3)
        self._del_tmp()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
# DIALOG: FFMPEG AUSENTE
# ══════════════════════════════════════════════════════════════════════════════
def _show_no_ffmpeg():
    win = ctk.CTk()
    win.title("FFmpeg nao encontrado")
    win.geometry("440x260")
    win.resizable(False, False)

    # Fontes criadas depois do CTk() para que Tk esteja ativo
    f_bold_l = ctk.CTkFont(size=15, weight="bold")
    ctk.CTkLabel(win, text="FFmpeg nao encontrado", font=f_bold_l).pack(pady=(28, 10))

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
    app.mainloop()


if __name__ == "__main__":
    main()
