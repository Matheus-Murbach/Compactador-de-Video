# Compactador de Vídeo — CLAUDE.md

## O que é

App desktop de arquivo único (`compressor.py`) para comprimir vídeos com 2 cliques.
O usuário dá duplo clique no arquivo, a janela abre, arrasta/seleciona um vídeo, escolhe o preset e salva.

## Como rodar

```bash
python3 compressor.py
# ou com arquivo direto:
python3 compressor.py /caminho/video.mp4
```

Na primeira execução o bootstrap instala `customtkinter` e `pillow` via pip automaticamente.
FFmpeg deve estar instalado no sistema — o app exibe instruções se não encontrar.

## Dependências

| Dependência | Fonte | Observação |
|---|---|---|
| Python 3.8+ | Sistema | Obrigatório |
| FFmpeg + ffprobe | Sistema | `apt install ffmpeg` / brew / choco |
| customtkinter | pip (auto) | Instalado pelo bootstrap |
| pillow | pip (auto) | Instalado pelo bootstrap; thumbnail usa PIL |
| tkinterdnd2 | pip (manual) | Opcional — drag-and-drop de arquivo na janela |

## Arquivo único: estrutura de `compressor.py`

```
_bootstrap()                  ← instala customtkinter + pillow se ausentes

Constantes                    ← SAFETY_MARGIN, PRESETS, SUPPORTED_EXT, etc.

class FFmpegHandler            ← toda lógica FFmpeg, sem UI
  find_ffmpeg()               ← busca no PATH + caminhos comuns
  probe_video()               ← ffprobe JSON, timeout=10s
  get_thumbnail()             ← extrai frame em ~1s como PNG
  _detect_accel()             ← NVENC → VAAPI → VideoToolbox → CPU (cacheado)
  build_command()             ← retorna (cmd: list[str], accel_label: str)
  compress()                  ← executa, parseia -progress pipe:2, retorna accel_label

_parse_progress_block()       ← converte bloco key=value em (percent, stats_str)
_friendly_probe_error()       ← traduz erros do ffprobe para PT-BR
_open_folder()                ← abre pasta no explorador (startfile/open/xdg-open)
_ask_open_files()             ← seletor de arquivos: zenity → kdialog → tkinter
_ask_save_file()              ← seletor de destino: zenity → kdialog → tkinter
_parse_time()                 ← "mm:ss" ou "hh:mm:ss" → float segundos

class VideoCompressorApp       ← toda a UI, máquina de estados
  Estados: DROP → CONFIG → PROGRESS → DONE
  Fila: _queue: list[Path], processamento sequencial com mesmo preset

_show_no_ffmpeg()             ← dialog com instruções por SO + link ffmpeg.org
main()                        ← entry point
```

## Máquina de estados

```
DROP → CONFIG → PROGRESS → DONE
         ↑_______↓ (cancelar/erro volta para CONFIG)
         ↑___________________↓ ("Comprimir outro vídeo" reseta tudo)
```

| Estado | Frame | O que mostra |
|---|---|---|
| `_DROP` | `_make_drop_frame` | Botão abrir + listbox da fila |
| `_CONFIG` | `_make_config_frame` | Thumbnail + info + presets + H.265 + trim |
| `_PROGRESS` | `_make_progress_frame` | Barra + % + fps + acelerador + cancelar |
| `_DONE` | `_make_done_frame` | Resultados + cartão salvar inline |

## Constantes importantes

```python
SAFETY_MARGIN      = 0.95   # garante 5% abaixo do limite da plataforma
AUDIO_RESERVE_KBPS = 128    # reservado para faixa de áudio
MIN_VIDEO_KBPS     = 50     # floor de bitrate de vídeo
H265_KBPS_THRESHOLD = 400   # ativa H.265 automaticamente abaixo deste valor
TEMP_SUFFIX        = "_vcomp_tmp"
SUPPORTED_EXT      = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".m4v"}
APP_W, APP_H       = 560, 600
```

## Cálculo de bitrate

```python
effective_s  = (trim_end or duration_s) - (trim_start or 0)
audio_bits   = 128_000 * effective_s   # se tem áudio
video_kbps   = ((target_bytes * 8 * 0.95) - audio_bits) / effective_s / 1000
video_kbps   = max(video_kbps, 50)    # floor
```

## Aceleração de hardware

Ordem de tentativa: `h264_nvenc` (NVIDIA) → `h264_vaapi` (Intel/AMD Linux) → `h264_videotoolbox` (macOS) → `libx264 -preset ultrafast` (CPU).

Resultado fica em `FFmpegHandler._accel_cache` (class variable) — detecção acontece só uma vez por sessão.

H.265 sempre via `libx265` (CPU) com `-tag:v hvc1` para compatibilidade Apple. Ativado automaticamente se `video_kbps < 400` ou se o usuário marcar o checkbox.

## Threading

- Compressão roda em thread daemon (`_worker`)
- Todo acesso à UI do worker usa `self.after(0, callback)` — nunca direto
- Cancelamento via `threading.Event` → `proc.terminate()` → `proc.kill()` (timeout 5s)
- Thumbnail extraída em thread separada para não travar a UI

## Arquivos temporários

- Criados com `tempfile.mkstemp(suffix=".mp4", prefix=f"{stem}{TEMP_SUFFIX}_")` no diretório do sistema (`tempfile.gettempdir()`)
- Thumbnail: `tempfile.mkstemp(suffix=".png", prefix="vcomp_thumb_")`
- Cleanup em todos os caminhos: cancelamento, erro, fechar janela, reset

## Dialogs de arquivo no Linux

`filedialog` do tkinter é instável em muitas distros. A app tenta na ordem:
1. `zenity` (GNOME/GTK) — `--file-selection --multiple` / `--save`
2. `kdialog` (KDE) — `--getopenfilename` / `--getsavefilename`
3. Fallback: `filedialog` do tkinter

No Windows/macOS usa tkinter diretamente (funciona bem).

## Tela DONE — salvar dentro da janela

Não usa `filedialog.asksaveasfilename` nem `messagebox`. Todo o fluxo de salvar está no cartão embutido na tela DONE:
- Campo pré-preenchido com `{stem}_comprimido.mp4` na pasta do arquivo original
- Botão "Procurar" → `_ask_save_file()` (dialog nativo)
- Botão "Salvar" → move temp para destino, mostra status inline em verde/vermelho

## Bugs conhecidos / pendentes

- Duplo clique para abrir `.py` no Linux depende da configuração do ambiente (associação de arquivo com Python). No Windows funciona nativamente.
- Drag-and-drop nativo requer `tkinterdnd2` instalado manualmente (`pip install tkinterdnd2`).
- VAAPI testado apenas com `/dev/dri/renderD128` — máquinas com GPU em path diferente caem no fallback CPU.
- Compressão em fila: arquivos intermediários são salvos automaticamente em `{stem}_comprimido.mp4` na pasta do original, sem confirmação do usuário.

## Commits relevantes

| Hash | O que fez |
|---|---|
| `bad9651` | Versão inicial (4 estados, presets, progresso, cancelamento) |
| `45de66d` | 4 correções da revisão crítica (CTkFont, proc.wait, elif morto, out_time_us=-1) |
| `884d464` | 16 melhorias: thumbnail, fila, H.265, trim, estimativas, cache HW, etc. |
| `54e474d` | 3 bugs: _parse_time hh:mm:ss, pack/grid mismatch, mktemp deprecated |
| `fc2187a` | Dialogs nativos no Linux (zenity/kdialog) + save inline na tela DONE |
